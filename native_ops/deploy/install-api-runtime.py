"""Install the prepared native API adapter, inert until owner activation. No provider keys requested."""
from pathlib import Path
import os,subprocess,runpy,json,secrets,time,shutil,pwd,urllib.error
assert os.geteuid()==0
ROOT=Path('/home/ops-user/ops-control-plane');source=ROOT/'native_ops/deploy'
archive=Path('/var/lib/ops-native-archives')/('api-runtime-'+str(time.time_ns()));archive.mkdir(mode=0o700,parents=True)
def run(argv,**kw):return subprocess.run(argv,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,**kw)
def write(path,data,mode=0o644):
 path=Path(path)
 if path.is_symlink():raise RuntimeError('unexpected symlink')
 if path.exists():shutil.copy2(path,archive/str(path).strip('/').replace('/','_'))
 temp=path.with_name(path.name+'.api-next');fd=os.open(temp,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,mode)
 with os.fdopen(fd,'wb') as f:f.write(data.encode() if isinstance(data,str) else data);f.flush();os.fsync(f.fileno())
 os.chown(temp,0,0);os.chmod(temp,mode);os.replace(temp,path)
try:pwd.getpwnam('opsnativeai')
except KeyError:run(['/usr/sbin/useradd','--system','--home-dir','/var/lib/ops-native-model','--shell','/usr/sbin/nologin','opsnativeai'])
folder=Path('/etc/ops-native-model');folder.mkdir(mode=0o755,exist_ok=True)
for sourcefile,target in [(source/'model-config.json',folder/'config.json'),(source/'agent.hcl',folder/'agent.hcl'),(ROOT/'catalog/model-catalog.v2.json',folder/'model-catalog.json'),(ROOT/'catalog/provider-integrations.v1.json',folder/'provider-integrations.json')]:write(target,sourcefile.read_bytes())
# These are local machine credentials consumed by systemd, never assistant/account tokens.
ns=runpy.run_path(str(source/'provider-secrets.py'));vault=ns['Vault']()
try:
 values=json.loads(Path('/var/lib/atlas-local-repair/credentials.json').read_bytes());password=values['openbao_louis'];values.clear()
 login=vault.call('POST','auth/userpass/login/ops-user',{'password':password});password='';vault.token=login['auth']['client_token'];login.clear()
 policy='path "kv-infra-shared/data/llm/qwen" { capabilities = ["read"] }\npath "kv-infra-shared/data/llm/deepseek" { capabilities = ["read"] }\n'
 vault.call('PUT','sys/policies/acl/ops-native-api',{'policy':policy})
 vault.call('POST','auth/approle/role/ops-native-api',{'token_policies':['ops-native-api'],'token_period':'15m','secret_id_ttl':0,'secret_id_num_uses':0})
 encrypted=Path('/etc/credstore.encrypted');encrypted.mkdir(mode=0o700,exist_ok=True)
 if not (encrypted/'ops-native-api-role-id').exists():ns['seal']('ops-native-api-role-id',vault.call('GET','auth/approle/role/ops-native-api/role-id')['data']['role_id'])
 if not (encrypted/'ops-native-api-secret-id').exists():ns['seal']('ops-native-api-secret-id',vault.call('POST','auth/approle/role/ops-native-api/secret-id',{})['data']['secret_id'])
 if not (encrypted/'ops-native-facade-token').exists():ns['seal']('ops-native-facade-token',secrets.token_urlsafe(48))
finally:
 if vault.token:
  try:vault.call('POST','auth/token/revoke-self',{})
  finally:vault.token=''
for name in ['ops-native-model.service','ops-native-secrets.service']:write('/etc/systemd/system/'+name,(source/name).read_bytes())
drop=Path('/etc/systemd/system/ops-native.service.d');drop.mkdir(exist_ok=True)
write(drop/'api-credential.conf','[Service]\nLoadCredentialEncrypted=ops-native-facade-token:/etc/credstore.encrypted/ops-native-facade-token\n')
run(['/usr/bin/systemctl','daemon-reload'])
# No secrets agent or model is started: provider records are intentionally absent.
run(['/usr/bin/systemctl','restart','ops-native.service'])
print('NATIVE_API_RUNTIME_INSTALLED_INACTIVE',str(archive))
