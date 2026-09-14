"""Install the reviewed publication bundle inert; activation requires Class C."""
from pathlib import Path
import hashlib,json,os,shutil,subprocess,time,yaml
from ops_broker.policy import RBACPolicy
from ops_broker.runbooks import ExecutablePolicy,RunbookRegistry
assert os.geteuid()==0
root=Path('/home/ops-user/ops-control-plane');source=root/'native_ops/deploy/public-zulip-v3'
archive=Path('/var/lib/ops-native-archives')/('public-zulip-v3-'+str(time.time_ns()));archive.mkdir(mode=0o700)
dest=Path('/usr/local/libexec/ops-zulip-public-20260912-3');assert not dest.exists()
assets=json.loads((source/'assets.json').read_text())
for name,digest in assets.items():assert hashlib.sha256((source/name).read_bytes()).hexdigest()==digest
for name in ['remote.py','publish.py']:compile((source/name).read_text(),name,'exec')
files={
Path('/usr/local/libexec/ops-runbooks/publish-zulip-v3'):(root/'broker/deploy/helpers/publish-zulip-v3',0o755),
Path('/etc/sudoers.d/ops-broker-publish-zulip-v3'):(root/'broker/deploy/sudoers/ops-broker-publish-zulip-v3',0o440),
Path('/opt/ops-broker/runbooks/local/publish-zulip-v3.yaml'):(root/'runbooks/local/publish-zulip-v3.yaml',0o644)}
for name in ['ops-zulip-publication-v3.service','ops-zulip-publication-v3-recover.service']:files[Path('/etc/systemd/system')/name]=(source/name,0o644)
for p in files:assert not p.exists()
subprocess.run(['/usr/sbin/visudo','-cf',str(root/'broker/deploy/sudoers/ops-broker-publish-zulip-v3')],check=True,capture_output=True)
for name in ['rbac','executables']:shutil.copy2('/etc/ops-broker/'+name+'.yaml',archive/(name+'.yaml'))
try:
 dest.mkdir(mode=0o755)
 for name in [*assets,'assets.json']:
  shutil.copyfile(source/name,dest/name);(dest/name).chmod(0o444)
 dest.chmod(0o555)
 for p,(src,mode) in files.items():shutil.copyfile(src,p);p.chmod(mode)
 p=Path('/etc/ops-broker/rbac.yaml');v=yaml.safe_load(p.read_text());books=v['roles']['supervised-operator']['runbooks']
 if 'network.publish-zulip.v3' not in books:books.append('network.publish-zulip.v3')
 RBACPolicy.from_data(v);p.write_text(yaml.safe_dump(v,sort_keys=False))
 p=Path('/etc/ops-broker/executables.yaml');v=yaml.safe_load(p.read_text());helpers=v['sudo']['allowed_helpers']
 if '/usr/local/libexec/ops-runbooks/publish-zulip-v3' not in helpers:helpers.append('/usr/local/libexec/ops-runbooks/publish-zulip-v3')
 p.write_text(yaml.safe_dump(v,sort_keys=False))
 r=RunbookRegistry.load(Path('/opt/ops-broker/runbooks'),ExecutablePolicy.load(p));assert r.get('network.publish-zulip.v3').action_class=='C'
 subprocess.run(['systemctl','daemon-reload'],check=True)
 subprocess.run(['systemd-analyze','verify','/etc/systemd/system/ops-zulip-publication-v3.service','/etc/systemd/system/ops-zulip-publication-v3-recover.service'],check=True,capture_output=True)
 subprocess.run(['/usr/sbin/visudo','-c'],check=True,capture_output=True)
except BaseException:
 for name in ['rbac','executables']:shutil.copy2(archive/(name+'.yaml'),'/etc/ops-broker/'+name+'.yaml')
 for p in files:p.unlink(missing_ok=True)
 if dest.exists():dest.chmod(0o755);shutil.rmtree(dest)
 subprocess.run(['systemctl','daemon-reload'],check=True)
 raise
result={'archive':str(archive),'bundle_sha256':hashlib.sha256((dest/'assets.json').read_bytes()).hexdigest(),'activation':'inert_waits_for_class_C','capability':'network.publish-zulip.v3'}
(archive/'result.json').write_text(json.dumps(result));print(json.dumps(result))
