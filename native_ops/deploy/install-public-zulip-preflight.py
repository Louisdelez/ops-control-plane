"""Install a fixed read-only capability locally, preserving prior policy."""
from pathlib import Path
import os,json,shutil,subprocess,time,yaml
from ops_broker.policy import RBACPolicy
from ops_broker.runbooks import ExecutablePolicy,RunbookRegistry
assert os.geteuid()==0
root=Path('/home/ops-user/ops-control-plane')
a=Path('/var/lib/ops-native-archives')/('public-zulip-preflight-'+str(time.time_ns()));a.mkdir(mode=0o700)
files={
'/usr/local/libexec/ops-runbooks/public-zulip-preflight':('broker/deploy/helpers/public-zulip-preflight',0o755),
'/opt/ops-broker/runbooks/local/public-zulip-preflight.yaml':('runbooks/local/public-zulip-preflight.yaml',0o644),
'/etc/sudoers.d/ops-broker-public-zulip-preflight':('broker/deploy/sudoers/ops-broker-public-zulip-preflight',0o440)}
subprocess.run(['/usr/sbin/visudo','-cf',str(root/files['/etc/sudoers.d/ops-broker-public-zulip-preflight'][0])],check=True,capture_output=True)
prior={}
for i,name in enumerate([*files,'/etc/ops-broker/rbac.yaml','/etc/ops-broker/executables.yaml']):
 p=Path(name);assert not p.is_symlink()
 if p.exists():
  backup=a/str(i);shutil.copy2(p,backup);prior[name]=str(backup)
 else:prior[name]=None
try:
 for name,(source,mode) in files.items():
  p=Path(name);shutil.copyfile(root/source,p);p.chmod(mode)
 p=Path('/etc/ops-broker/rbac.yaml');v=yaml.safe_load(p.read_text());books=v['roles']['supervised-operator']['runbooks']
 if 'inventory.public-zulip-preflight.v1' not in books:books.append('inventory.public-zulip-preflight.v1')
 RBACPolicy.from_data(v);p.write_text(yaml.safe_dump(v,sort_keys=False))
 p=Path('/etc/ops-broker/executables.yaml');v=yaml.safe_load(p.read_text());helpers=v['sudo']['allowed_helpers']
 if '/usr/local/libexec/ops-runbooks/public-zulip-preflight' not in helpers:helpers.append('/usr/local/libexec/ops-runbooks/public-zulip-preflight')
 p.write_text(yaml.safe_dump(v,sort_keys=False))
 r=RunbookRegistry.load(Path('/opt/ops-broker/runbooks'),ExecutablePolicy.load(p));assert r.get('inventory.public-zulip-preflight.v1').action_class=='A'
 subprocess.run(['/usr/sbin/visudo','-c'],check=True,capture_output=True)
except BaseException:
 for name,backup in prior.items():
  if backup:shutil.copy2(backup,name)
  else:Path(name).unlink(missing_ok=True)
 raise
result={'archive':str(a),'capability':'inventory.public-zulip-preflight.v1','remote_changes':False}
(a/'result.json').write_text(json.dumps(result));print(json.dumps(result))
