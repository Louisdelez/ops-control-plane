"""Repair only public code/metadata modes of the two newly installed wheels."""
import json,os,subprocess,time
from pathlib import Path
assert os.geteuid()==0
changed=[]
for base,name,version in [('/opt/ops-native/venv','ops_native','0.1.11'),('/opt/ops-broker/venv','ops_broker','0.1.1')]:
    site=Path(base)/'lib/python3.14/site-packages'
    for root in [site/name,site/(name+'-'+version+'.dist-info')]:
        assert root.is_dir() and not root.is_symlink()
        for path in [root,*root.rglob('*')]:
            assert not path.is_symlink() and path.stat().st_uid==0
            mode=0o755 if path.is_dir() else 0o644
            if path.stat().st_mode&0o777!=mode:
                changed.append(str(path));path.chmod(mode)
for identity,python,module in [
    ('ops-user','/opt/ops-native/venv/bin/python','ops_native.workflows'),
    ('hermesd','/opt/ops-native/venv/bin/python','ops_native.cli'),
    ('opsbroker','/opt/ops-broker/venv/bin/python','ops_broker.service')]:
    subprocess.run(['/usr/sbin/runuser','-u',identity,'--',python,'-I','-c','import '+module],check=True)
subprocess.run(['systemctl','reset-failed','ops-broker.service','ops-native.service'],check=True)
subprocess.run(['systemctl','restart','ops-broker.service','ops-native.service','ops-native-capabilities.timer'],check=True)
subprocess.run(['/usr/sbin/runuser','-u','ops-user','--','env','XDG_RUNTIME_DIR=/run/user/1000',
                'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',
                'systemctl','--user','restart','ops-cli-pilot.service'],check=True)
started=time.time()
for _ in range(20):
    status=json.loads(Path('/var/lib/hermes/native-ops/connector/status.json').read_text())
    if status.get('status')=='running' and status.get('updated_at',0)>started:break
    time.sleep(1)
else:raise RuntimeError('Fresh connector heartbeat not observed')
for unit in ['ops-broker.service','ops-native.service']:
    subprocess.run(['systemctl','is-active','--quiet',unit],check=True)
report={'status':'repaired','files_corrected':len(changed),'service_identity_imports':True,
        'fresh_heartbeat':True,'checked_at':int(time.time()),'release':'ops-native-2026.09.11.9'}
target=Path('/usr/local/share/ops-native/releases/ops-native-2026.09.11.9-permissions-repair.json')
with target.open('x') as out:json.dump(report,out);out.write('\n')
target.chmod(0o444);print(json.dumps(report))
