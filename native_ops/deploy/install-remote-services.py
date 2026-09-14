"""Install only the reviewed read-only remote observation extension locally."""
from pathlib import Path
import hashlib,json,os,runpy,shutil,sqlite3,subprocess,time
ROOT=Path('/home/ops-user/ops-control-plane');RELEASE='ops-native-2026.09.11.15'
assert os.geteuid()==0
assert runpy.run_path(str(ROOT/'native_ops/release.py'))['verify'](ROOT)['release_id']==RELEASE
archive=Path('/var/lib/ops-native-archives')/('remote-services-'+str(time.time_ns()));archive.mkdir(parents=True,mode=0o700)
def run(*args):return subprocess.run(args,check=True,capture_output=True,timeout=60)
def user(*args):return run('/usr/sbin/runuser','-u','ops-user','--','/usr/bin/env','XDG_RUNTIME_DIR=/run/user/1000','DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',*args)
# Do not interrupt a native CLI request for a registry refresh.
pilot_db=Path('/home/ops-user/.local/state/ops-cli-pilot/attempts.sqlite3')
if pilot_db.exists():
 with sqlite3.connect('file:'+str(pilot_db)+'?mode=ro',uri=True) as db:
  assert not db.execute("SELECT 1 FROM attempts WHERE status='running'").fetchone(),'Pilot busy'
  assert not db.execute("SELECT 1 FROM approvals WHERE status='executing'").fetchone(),'Action running'
source=ROOT/'broker/deploy'
files={Path('/usr/local/libexec/ops-runbooks')/name:(source/'helpers'/name,0o755) for name in ['remote-services','remote-services-worker']}
files[Path('/etc/systemd/system/ops-remote-services@.service')]=(source/'systemd/ops-remote-services@.service',0o644)
files[Path('/etc/sudoers.d/ops-broker-remote-services')]=(source/'sudoers/ops-broker-remote-services',0o440)
files[Path('/opt/ops-broker/runbooks/local/remote-services.yaml')]=(ROOT/'runbooks/local/remote-services.yaml',0o644)
run('/usr/sbin/visudo','-cf',str(source/'sudoers/ops-broker-remote-services'))
# Merge this one capability into the installed policy, preserving native identities.
code='''import yaml,json
from pathlib import Path
from ops_broker.policy import RBACPolicy
rbac=yaml.safe_load(Path('/etc/ops-broker/rbac.yaml').read_text())
books=rbac['roles']['supervised-operator']['runbooks']
if 'inventory.remote-services.v1' not in books:books.append('inventory.remote-services.v1')
RBACPolicy.from_data(rbac)
ex=yaml.safe_load(Path('/etc/ops-broker/executables.yaml').read_text())
helpers=ex['sudo']['allowed_helpers']
if '/usr/local/libexec/ops-runbooks/remote-services' not in helpers:helpers.append('/usr/local/libexec/ops-runbooks/remote-services')
print(json.dumps({'rbac':yaml.safe_dump(rbac,sort_keys=False),'executables':yaml.safe_dump(ex,sort_keys=False)}))
'''
rendered=json.loads(run('/opt/ops-broker/venv/bin/python','-I','-c',code).stdout)
for name,content in rendered.items():
 candidate=archive/(name+'.yaml');candidate.write_text(content)
 files[Path('/etc/ops-broker')/(name+'.yaml')]=(candidate,0o640)
prior={}
for index,p in enumerate(files):
 assert not p.is_symlink()
 if p.exists():
  saved=archive/('before-'+str(index));shutil.copy2(p,saved);st=p.stat();prior[str(p)]=(str(saved),st.st_uid,st.st_gid,st.st_mode&0o777)
 else:prior[str(p)]=None
(archive/'prior.json').write_text(json.dumps(prior))
def replace(p,source,uid,gid,mode):
 next_path=p.with_name(p.name+'.preflight-next')
 with next_path.open('xb') as f:f.write(Path(source).read_bytes());f.flush();os.fsync(f.fileno())
 os.chown(next_path,uid,gid);os.chmod(next_path,mode);os.replace(next_path,p)
try:
 user('/usr/bin/systemctl','--user','stop','ops-cli-pilot.service')
 for p,(source,mode) in files.items():
  gid=p.stat().st_gid if p.exists() else 0
  replace(p,source,0,gid,mode)
 run('/usr/sbin/visudo','-c')
 check="from pathlib import Path;from ops_broker.runbooks import ExecutablePolicy,RunbookRegistry;r=RunbookRegistry.load(Path('/opt/ops-broker/runbooks'),ExecutablePolicy.load(Path('/etc/ops-broker/executables.yaml')));assert r.get('inventory.remote-services.v1').action_class=='A'"
 run('/opt/ops-broker/venv/bin/python','-I','-c',check)
 run('/usr/bin/systemctl','daemon-reload')
 # Refresh each persistent MCP client without changing any remote machine.
 run('/usr/bin/systemctl','restart','ops-broker.service','ops-native.service')
 user('/usr/bin/systemctl','--user','start','ops-cli-pilot.service')
 for unit in ['ops-broker.service','ops-native.service','ops-native-approval.service']:run('/usr/bin/systemctl','is-active','--quiet',unit)
 published=Path('/usr/local/share/ops-native/releases')/(RELEASE+'.json');assert not published.exists()
 published.write_bytes((ROOT/'native_ops/releases/current.json').read_bytes());published.chmod(0o444)
 result={'release':RELEASE,'status':'installed','capability':'inventory.remote-services.v1','class':'A','remote_changes':False,'remote_checks_executed':False,'archive':str(archive),'manifest_sha256':hashlib.sha256(published.read_bytes()).hexdigest(),'verified_at':int(time.time())}
 proof=published.with_name(RELEASE+'-install.json');proof.write_text(json.dumps(result,indent=2)+'\n');proof.chmod(0o444)
 (archive/'result.json').write_text(json.dumps(result));print(json.dumps(result))
except BaseException:
 for name,info in prior.items():
  p=Path(name)
  if info is None:p.unlink(missing_ok=True)
  else:replace(p,*info)
 run('/usr/bin/systemctl','daemon-reload')
 run('/usr/bin/systemctl','restart','ops-broker.service','ops-native.service')
 user('/usr/bin/systemctl','--user','start','ops-cli-pilot.service')
 raise
