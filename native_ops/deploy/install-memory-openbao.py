"""Install optional memory credential bridge without requesting provider keys."""
from pathlib import Path
import hashlib,json,os,runpy,shutil,sqlite3,subprocess,time,urllib.error
ROOT=Path('/home/ops-user/ops-control-plane');OUT=ROOT/'artifacts/memory-openbao-2026-09-11';RELEASE='ops-native-2026.09.11.8'
assert os.geteuid()==0
assert runpy.run_path(str(ROOT/'native_ops/release.py'))['verify'](ROOT)['release_id']==RELEASE
payload=json.loads((OUT/'payload.json').read_text())
for name,digest in payload.items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
marker=Path('/etc/ops-memory/api-openbao-managed');assert not marker.exists(),'Managed bridge already present'
store=Path('/etc/credstore.encrypted');role_name='ops-memory-api'
for name in ['embedding-api-token','reranker-api-token',role_name+'-role-id',role_name+'-secret-id']:assert not (store/name).exists(),'Existing credential requires review'
pilot_db=Path('/home/ops-user/.local/state/ops-cli-pilot/attempts.sqlite3')
if pilot_db.exists():
 with sqlite3.connect('file:'+str(pilot_db)+'?mode=ro',uri=True) as db:
  assert not db.execute("SELECT 1 FROM attempts WHERE status='running'").fetchone(),'Pilot busy'
  assert not db.execute("SELECT 1 FROM approvals WHERE status='executing'").fetchone(),'Action running'
archive=Path('/var/lib/ops-native-archives')/('memory-openbao-'+str(time.time_ns()));archive.mkdir(parents=True,mode=0o700)
venv=Path('/opt/ops-native/venv');shutil.copytree(venv,archive/'venv',symlinks=True)
config=Path('/etc/ops-memory/memory.toml');shutil.copy2(config,archive/'memory.toml')
lib=runpy.run_path(str(ROOT/'native_ops/deploy/provider-secrets.py'));vault=lib['Vault']()
p=Path('/var/lib/atlas-local-repair/credentials.json');st=p.lstat();assert not p.is_symlink() and st.st_uid==0 and not st.st_mode&0o077
prepared=json.loads(p.read_bytes());password=prepared['openbao_louis'];prepared.clear()
login=vault.call('POST','auth/userpass/login/ops-user',{'password':password});password='';vault.token=login['auth']['client_token'];login.clear()
created_role=False;created_policy=False;files={};installed=False
units=['ops-memory-api-secrets.service','ops-memory-api-secrets.timer']
def run(*args):return subprocess.run(args,check=True,capture_output=True,timeout=120)
def user(*args):return run('/usr/sbin/runuser','-u','ops-user','--','/usr/bin/env','XDG_RUNTIME_DIR=/run/user/1000','DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',*args)
def install(source,target,mode=0o644):
 p=Path(target);assert not p.is_symlink()
 files[str(p)]=p.read_bytes() if p.exists() else None
 tmp=p.with_name(p.name+'.memory-next')
 with tmp.open('xb') as f:f.write(Path(source).read_bytes());f.flush();os.fsync(f.fileno())
 os.chown(tmp,0,0);os.chmod(tmp,mode);os.replace(tmp,p)
try:
 for path in ['auth/approle/role/'+role_name,'sys/policies/acl/'+role_name]:
  try:vault.call('GET',path)
  except urllib.error.HTTPError as e:
   if e.code!=404:raise
  else:raise RuntimeError('Existing OpenBao service identity requires review')
 vault.call('PUT','sys/policies/acl/'+role_name,{'policy':'path "kv-infra-shared/data/llm/siliconflow" { capabilities = ["read"] }'});created_policy=True
 vault.call('POST','auth/approle/role/'+role_name,{'token_policies':[role_name],'token_ttl':'60s','token_max_ttl':'60s','token_num_uses':2,'secret_id_ttl':0,'secret_id_num_uses':0,'token_bound_cidrs':['127.0.0.1/32'],'secret_id_bound_cidrs':['127.0.0.1/32']});created_role=True
 rid=vault.call('GET','auth/approle/role/'+role_name+'/role-id')['data']['role_id']
 sid=vault.call('POST','auth/approle/role/'+role_name+'/secret-id',{})['data']['secret_id']
 lib['seal'](role_name+'-role-id',rid);lib['seal'](role_name+'-secret-id',sid);rid=sid=''
 run('/usr/bin/systemctl','stop','ops-native-capabilities.timer','ops-native-capabilities.service')
 user('/usr/bin/systemctl','--user','stop','ops-cli-pilot.service')
 run('/usr/bin/systemctl','stop','ops-native.service')
 run(str(venv/'bin/python'),'-m','pip','install','--no-index','--no-deps','--force-reinstall',str(OUT/'wheels/ops_native-0.1.10-py3-none-any.whl'))
 run(str(venv/'bin/python'),'-m','pip','check')
 code="from importlib.metadata import distribution;from pathlib import Path;p=distribution('ops-native');assert p.version=='0.1.10';assert all(p.locate_file('ops_native/'+f.name).read_bytes()==f.read_bytes() for f in Path('/home/ops-user/ops-control-plane/native_ops/ops_native').glob('*.py'))"
 run(str(venv/'bin/python'),'-I','-c',code)
 for unit in units:install(ROOT/'native_ops/deploy'/unit,'/etc/systemd/system/'+unit)
 managed=archive/'managed.json';managed.write_text(json.dumps({'reference':'kv-infra-shared/llm/siliconflow','field':'api_key','owner':'ops-memory-api'}))
 install(managed,marker)
 run('/usr/bin/systemctl','daemon-reload')
 run('/usr/bin/systemctl','enable','--now','ops-memory-api-secrets.timer')
 run('/usr/bin/systemctl','start','ops-memory-api-secrets.service')
 run('/usr/bin/systemctl','start','ops-native.service','ops-native-capabilities.timer')
 user('/usr/bin/systemctl','--user','start','ops-cli-pilot.service')
 for unit in ['ops-memory.service','ops-memory-api-secrets.timer','ops-native.service','ops-broker.socket']:run('/usr/bin/systemctl','is-active','--quiet',unit)
 status=json.loads(Path('/run/ops-memory-api/status.json').read_text());assert status['reason'] in {'not_configured','configured'}
 published=Path('/usr/local/share/ops-native/releases')/(RELEASE+'.json');assert not published.exists()
 published.write_bytes((ROOT/'native_ops/releases/current.json').read_bytes());published.chmod(0o444)
 result={'release':RELEASE,'status':'installed','native_version':'0.1.10','memory_api':status['reason'],'reference':'kv-infra-shared/llm/siliconflow','field':'api_key','archive':str(archive),'manifest_sha256':hashlib.sha256(published.read_bytes()).hexdigest(),'provider_requests':False,'verified_at':int(time.time())}
 proof=published.with_name(RELEASE+'-install.json');proof.write_text(json.dumps(result,indent=2)+'\n');proof.chmod(0o444)
 (archive/'result.json').write_text(json.dumps(result));installed=True;print(json.dumps(result))
finally:
 if not installed:
  subprocess.run(['/usr/bin/systemctl','disable','--now','ops-memory-api-secrets.timer'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  subprocess.run(['/usr/bin/systemctl','stop','ops-memory-api-secrets.service','ops-native-capabilities.timer','ops-native-capabilities.service','ops-native.service'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  try:user('/usr/bin/systemctl','--user','stop','ops-cli-pilot.service')
  except subprocess.CalledProcessError:pass
  if venv.exists():venv.rename(archive/'failed-venv')
  (archive/'venv').rename(venv)
  shutil.copy2(archive/'memory.toml',config)
  for name,raw in files.items():
   p=Path(name)
   if raw is None:p.unlink(missing_ok=True)
   else:p.write_bytes(raw)
  for name in ['embedding-api-token','reranker-api-token',role_name+'-role-id',role_name+'-secret-id']:
   p=store/name
   if p.exists():p.rename(archive/name)
  if created_role:vault.call('DELETE','auth/approle/role/'+role_name)
  if created_policy:vault.call('DELETE','sys/policies/acl/'+role_name)
  run('/usr/bin/systemctl','daemon-reload')
  run('/usr/bin/systemctl','start','ops-native.service','ops-native-capabilities.timer','ops-memory.service')
  user('/usr/bin/systemctl','--user','start','ops-cli-pilot.service')
 try:vault.call('POST','auth/token/revoke-self',{})
 finally:vault.token=''
