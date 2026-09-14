from pathlib import Path
import subprocess,json,hashlib,os,time
assert os.geteuid()==0
root=Path('/home/ops-user/ops-control-plane')
subprocess.run(['/usr/bin/python3',str(root/'native_ops/release.py')],check=True,stdout=subprocess.DEVNULL)
code='''from importlib.metadata import distribution
import hashlib,base64,json
from pathlib import Path
p=distribution('ops-native');assert p.version=='0.1.8'
for f in p.files:
 if f.hash:assert base64.urlsafe_b64encode(hashlib.sha256(p.locate_file(f).read_bytes()).digest()).decode().rstrip('=')==f.hash.value
for f in Path('/home/ops-user/ops-control-plane/native_ops/ops_native').glob('*.py'):
 assert p.locate_file('ops_native/'+f.name).read_bytes()==f.read_bytes(),f.name
print(json.dumps({'version':p.version,'record_matches':True,'source_matches':True}))
'''
r=subprocess.run(['/opt/ops-native/venv/bin/python','-c',code],check=True,capture_output=True,text=True)
units=['ops-native.service','ops-native-approval.service','ops-broker.socket','ops-memory.service','openbao.service','ops-orchestrator.service','ops-zulip-backup.timer']
for unit in units:subprocess.run(['systemctl','is-active','--quiet',unit],check=True)
status=json.loads(Path('/var/lib/hermes/native-ops/connector/status.json').read_text());assert status['status']=='running' and time.time()-status['updated_at']<20
backup=json.loads(Path('/var/lib/ops-zulip-backups/latest.json').read_text());assert backup['database_restore_verified']
manifest=root/'native_ops/releases/current.json';dest=Path('/usr/local/share/ops-native/releases');dest.mkdir(parents=True,exist_ok=True)
p=dest/'ops-native-2026.09.11.1.json'
if p.exists() and p.read_bytes()!=manifest.read_bytes():raise RuntimeError('Refusing to rewrite a published release')
p.write_bytes(manifest.read_bytes());p.chmod(0o444)
result={'release':'ops-native-2026.09.11.1','manifest_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'native_package':json.loads(r.stdout),'active_units':units,'connector':'running','backup_restore_verified':True,'desktop_sha256':hashlib.sha256(Path('/home/ops-user/.local/lib/ops-desktop/ops-desktop').read_bytes()).hexdigest(),'verified_at':int(time.time())}
proof=dest/'ops-native-2026.09.11.1-install.json';proof.write_text(json.dumps(result,indent=2)+'\n');proof.chmod(0o444);print(json.dumps(result))
