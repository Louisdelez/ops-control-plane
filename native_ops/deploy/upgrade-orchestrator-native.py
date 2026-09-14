"""Upgrade the installed daemon with an offline wheel and rollback its state on failure."""
from pathlib import Path
import os,json,subprocess,shutil,time,hashlib
assert os.geteuid()==0
root=Path('/home/ops-user/ops-control-plane')
wheel=root/'artifacts/completion-2026-09-10/wheels/ops_orchestrator-0.2.0-py3-none-any.whl'
assert wheel.is_file()
archive=Path('/var/lib/ops-native-archives')/('orchestrator-'+str(time.time_ns()));archive.mkdir(mode=0o700)
def run(*args):return subprocess.run(args,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
identity=archive/'finance.sysusers'
identity.write_text('g opsfinance -\nu opsfinance - "Ops provider finance worker" /nonexistent /usr/sbin/nologin\nm opsorchestrator opsfinance\n')
run('systemd-sysusers',str(identity))
venv=Path('/opt/ops-orchestrator/venv'); candidate=venv.with_name('venv.next')
if candidate.exists():raise RuntimeError('Candidate already exists')
shutil.copytree(venv,candidate,symlinks=True)
try:
 run(str(candidate/'bin/python'),'-m','pip','install','--no-index','--no-deps',str(wheel))
 run(str(candidate/'bin/python'),'-m','pip','check')
 for entry in (candidate/'bin').iterdir():
  if entry.is_file() and not entry.is_symlink() and entry.read_bytes().startswith(b'#!'):
   entry.write_bytes(entry.read_bytes().replace(str(candidate).encode(),str(venv).encode()))
 config=json.loads((root/'orchestrator/config/orchestrator.json').read_text())
 # Generation remains in the separately gated native API facade. MCP discovery
 # and cost previews are available without granting this daemon paid routing.
 for grant in config['access']['route_grants']:grant['remote_allowed']=False
 public=Path('/usr/local/share/ops-native/orchestrator-0.2.0');public.mkdir(parents=True,exist_ok=True)
 for name in ['model-catalog.v2.json','provider-integrations.v1.json']:
  shutil.copyfile(root/'catalog'/name,public/name);(public/name).chmod(0o644)
 config['paths']['model_catalogue']=str(public/'model-catalog.v2.json')
 config['paths']['provider_integrations']=str(public/'provider-integrations.v1.json')
 staged=archive/'config.next.json';staged.write_text(json.dumps(config,indent=2)+'\n');staged.chmod(0o600)
 run(str(candidate/'bin/python'),'-m','ops_orchestrator.cli','check-config','--config',str(staged))
 import pwd,grp
 pwd.getpwnam(config['paths']['finance_socket_user']);grp.getgrnam(config['paths']['finance_socket_group'])
except BaseException:
 shutil.rmtree(candidate);raise
config_path=Path('/etc/ops-orchestrator/config.json')
shutil.copy2(config_path,archive/'config.json')
was_active=run('systemctl','is-active','ops-orchestrator.service').stdout.strip()==b'active'
run('systemctl','stop','ops-orchestrator.service')
state=Path('/var/lib/ops-orchestrator');shutil.copytree(state,archive/'state',symlinks=True)
for original in [state,*state.rglob('*')]:
 m=original.stat();os.chown(archive/'state'/original.relative_to(state),m.st_uid,m.st_gid)
venv.rename(archive/'venv');candidate.rename(venv)
try:
 meta=config_path.stat();shutil.copyfile(staged,config_path);os.chown(config_path,meta.st_uid,meta.st_gid);os.chmod(config_path,meta.st_mode&0o777)
 run('systemctl','start','ops-orchestrator.service')
 results={}
 for attempt in range(30):
  try:
   for endpoint in ['health','budgets','model-performance','provider-integrations','catalogue']:
    r=run('curl','--silent','--show-error','--fail','--unix-socket','/run/ops-orchestrator/api.sock','http://localhost/v1/'+endpoint)
    data=json.loads(r.stdout);results[endpoint]={'ok':True,'fields':sorted(data)}
   break
  except (subprocess.CalledProcessError,ValueError):time.sleep(1)
 else:raise RuntimeError('Endpoint verification failed')
 result={'status':'installed','package':'ops-orchestrator','version':'0.2.0','wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'archive':str(archive),'endpoints':results,'remote_routing':False}
 (archive/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
except BaseException:
 run('systemctl','stop','ops-orchestrator.service')
 venv.rename(archive/'failed-venv');(archive/'venv').rename(venv)
 shutil.copy2(archive/'config.json',config_path)
 state.rename(archive/'failed-state');shutil.copytree(archive/'state',state,symlinks=True)
 # copytree preserves modes, but ownership needs explicit restoration.
 for p in [state,*state.rglob('*')]:
  original=archive/'state'/p.relative_to(state);m=original.stat();os.chown(p,m.st_uid,m.st_gid)
 if was_active:
  run('systemctl','reset-failed','ops-orchestrator.service')
  run('systemctl','start','ops-orchestrator.service')
 raise
