"""Install the built native wheel, retaining a rollback copy and package metadata."""
from pathlib import Path
import os,subprocess,shutil,time,json,hashlib
assert os.geteuid()==0
root=Path('/home/ops-user/ops-control-plane');wheel=root/'artifacts/completion-2026-09-10/wheels/ops_native-0.1.7-py3-none-any.whl'
archive=Path('/var/lib/ops-native-archives')/('native-wheel-'+str(time.time_ns()));archive.mkdir(mode=0o700)
venv=Path('/opt/ops-native/venv');shutil.copytree(venv,archive/'venv',symlinks=True)
def run(*args):return subprocess.run(args,check=True,capture_output=True)
run('systemctl','stop','ops-native.service')
try:
 run(str(venv/'bin/python'),'-m','pip','install','--no-index','--no-deps','--force-reinstall',str(wheel))
 run(str(venv/'bin/python'),'-m','pip','check')
 run(str(venv/'bin/python'),'-c','from ops_native.cli_pilot import command; from ops_native.execution import execution_mode; assert command("codex"); assert execution_mode() in {"cli","api","hybrid"}')
 check='''from importlib.metadata import distribution
import hashlib,base64
p=distribution('ops-native')
assert p.version=='0.1.7'
for f in p.files:
 if f.hash:
  assert base64.urlsafe_b64encode(hashlib.sha256(p.locate_file(f).read_bytes()).digest()).decode().rstrip('=')==f.hash.value,str(f)
'''
 run(str(venv/'bin/python'),'-c',check)
 run('systemctl','reset-failed','ops-native.service');run('systemctl','start','ops-native.service')
 time.sleep(7);run('systemctl','is-active','ops-native.service')
 status=json.loads(Path('/var/lib/hermes/native-ops/connector/status.json').read_text())
 assert status['status']=='running' and time.time()-status['updated_at']<15
 result={'version':'0.1.7','record_integrity':True,'connector_status':status['status'],'archive':str(archive),'wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest()}
 (archive/'result.json').write_text(json.dumps(result));print(json.dumps(result))
except BaseException:
 run('systemctl','stop','ops-native.service');venv.rename(archive/'failed-venv');(archive/'venv').rename(venv)
 run('systemctl','reset-failed','ops-native.service');run('systemctl','start','ops-native.service');raise
