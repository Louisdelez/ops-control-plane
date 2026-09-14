"""Bridge one OpenBao reference into named, encrypted systemd credentials.

The worker receives only its dedicated AppRole, never the owner's login. Provider
values stay in process memory and systemd-creds stdin; logs contain status only.
"""
import fcntl,hashlib,json,os,ssl,stat,subprocess,tempfile,time,urllib.error,urllib.request
from pathlib import Path
from . import memory_capabilities
ORIGIN='https://127.0.0.1:8200/v1/'
REFERENCE='kv-infra-shared/data/llm/siliconflow'
STORE=Path('/etc/credstore.encrypted')
RUNTIME=Path('/run/ops-memory-api')
MARKER=Path('/etc/ops-memory/api-openbao-managed')
NAMES=('embedding-api-token','reranker-api-token')
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):raise RuntimeError('Redirect refused')
class Vault:
 def __init__(self):
  self.opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect(),urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile='/etc/pki/ca-trust/source/anchors/openbao-local.crt')))
 def call(self,method,path,data=None,token=''):
  req=urllib.request.Request(ORIGIN+path,method=method,headers={'Content-Type':'application/json','X-Vault-Token':token},data=None if data is None else json.dumps(data).encode())
  with self.opener.open(req,timeout=5) as r:raw=r.read(65537)
  if len(raw)>65536:raise ValueError('Oversized response')
  return json.loads(raw) if raw else {}
def protected(path,maximum=65536):
 st=path.lstat()
 if not stat.S_ISREG(st.st_mode) or st.st_uid!=0 or st.st_mode&0o077 or st.st_size>maximum:raise ValueError('Unsafe protected file')
 fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
 try:
  current=os.fstat(fd)
  if (current.st_dev,current.st_ino)!=(st.st_dev,st.st_ino):raise ValueError('Protected file changed')
  raw=os.read(fd,maximum+1)
 finally:os.close(fd)
 if len(raw)>maximum:raise ValueError('Oversized protected file')
 return raw
def resolve(vault,credentials,reference=REFERENCE):
 role=protected(credentials/'ops-memory-api-role-id',4096).decode().strip()
 secret=protected(credentials/'ops-memory-api-secret-id',4096).decode().strip()
 login=vault.call('POST','auth/approle/login',{'role_id':role,'secret_id':secret})
 role=secret='';token=login['auth']['client_token'];login.clear()
 try:
  try:data=vault.call('GET',reference,token=token)
  except urllib.error.HTTPError as e:
   if e.code==404:return None
   raise
  value=data['data']['data'].get('api_key');data.clear()
  if not isinstance(value,str) or not 8<=len(value)<=4096 or not value.isascii() or any(c.isspace() or ord(c)<33 or ord(c)>126 for c in value):raise ValueError('Invalid provider credential')
  return value
 finally:
  vault.call('POST','auth/token/revoke-self',{},token=token);token=''
def atomic(path,raw,mode=0o600):
 fd,name=tempfile.mkstemp(prefix='.'+path.name+'-',dir=path.parent)
 try:
  os.fchmod(fd,mode)
  with os.fdopen(fd,'wb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
  os.replace(name,path)
 finally:Path(name).unlink(missing_ok=True)
def install(key,store=STORE,runtime=RUNTIME,names=NAMES):
 digest=hashlib.sha256(key.encode()).hexdigest()
 previous_hash=runtime/('credential-digest' if names==NAMES else names[0]+'-digest')
 if previous_hash.exists() and protected(previous_hash,128).decode()==digest and all((store/n).is_file() for n in names):return False
 before={n:protected(store/n) if (store/n).exists() else None for n in names}
 staged={}
 try:
  for name in names:
   fd,raw=tempfile.mkstemp(prefix='.memory-api-',dir=store);os.close(fd);path=Path(raw);path.unlink();staged[name]=path
   subprocess.run(['/usr/bin/systemd-creds','encrypt','--name='+name,'-',str(path)],input=key.encode(),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=15)
   path.chmod(0o600)
  for name,path in staged.items():os.replace(path,store/name)
  atomic(previous_hash,digest.encode())
  return True
 except BaseException:
  for name,raw in before.items():
   if raw is None:(store/name).unlink(missing_ok=True)
   else:atomic(store/name,raw)
  raise
 finally:
  for path in staged.values():path.unlink(missing_ok=True)
def run():
 if os.geteuid()!=0 or not MARKER.is_file() or MARKER.is_symlink():raise PermissionError('Managed administrator context required')
 directory=Path(os.environ['CREDENTIALS_DIRECTORY'])
 if not directory.is_absolute():raise ValueError('Credentials directory required')
 import tomllib
 config=tomllib.loads(memory_capabilities.CONFIG.read_text())
 references={
  'https://api.jina.ai/v1/embeddings':'kv-infra-shared/data/llm/providers/jina',
  'https://api.jina.ai/v1/rerank':'kv-infra-shared/data/llm/providers/jina',
  'https://api.voyageai.com/v1/embeddings':'kv-infra-shared/data/llm/providers/voyage',
  'https://api.cohere.com/v2/rerank':'kv-infra-shared/data/llm/providers/cohere',
  'https://api.siliconflow.com/v1/embeddings':REFERENCE,
  'https://api.siliconflow.com/v1/rerank':REFERENCE,
 }
 status={'ready':False,'checked_at':time.time(),'reason':'not_configured','providers':{}};rotated=False
 for section in ['embedding','reranker']:
  ready=False
  try:
   reference=references[config[section]['api']['url']]
   key=resolve(Vault(),directory,reference)
   if key is not None:
    rotated=install(key,names=(section+'-api-token',)) or rotated
    key='';ready=True
  except Exception as error:
   print(json.dumps({"provider":section,"failure_type":type(error).__name__,"status":getattr(error,"code",None),"errno":getattr(error,"errno",None)}))
  status['providers'][section]=ready
 status['ready']=all(status['providers'].values())
 status['reason']='configured' if status['ready'] else ('partially_configured' if any(status['providers'].values()) else 'not_configured')
 atomic(RUNTIME/'status.json',(json.dumps(status)+'\n').encode(),0o644)
 changed=memory_capabilities.reconcile()
 if rotated and not changed:subprocess.run(['/usr/bin/systemctl','restart','ops-memory.service'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=45)
 print(json.dumps({'memory_api':status['reason'],'ready':status['ready']}))
if __name__=='__main__':
 try:
  fd=os.open('/run/lock/ops-execution-mode.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
  with os.fdopen(fd,'w') as lock:
   fcntl.flock(lock,fcntl.LOCK_EX);run()
 except Exception:print('{"memory_api":"reconciliation_failed","ready":false}');raise SystemExit(1)
