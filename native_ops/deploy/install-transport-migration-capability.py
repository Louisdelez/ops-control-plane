"""Install local maintenance under direct owner construction authorization of 14 September.

No remote command is executed by this installer. Previous local policy and files
are archived and restored on failure. No credentials are read or printed.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

ROOT=Path('/home/ops-user/ops-control-plane')
MANIFEST=ROOT/'artifacts/completion-final-2026-09-14/transport-migration-capability.json'
MISSION='ce011514-448a-4194-a989-86292c11660d'
FILES={
 'broker/deploy/helpers/migrate-observer-transports':('/usr/local/libexec/ops-runbooks/migrate-observer-transports',0o755),
 'broker/deploy/sudoers/ops-broker-migrate-observer-transports':('/etc/sudoers.d/ops-broker-migrate-observer-transports',0o440),
 'runbooks/local/migrate-observer-transports.yaml':('/opt/ops-broker/runbooks/local/migrate-observer-transports.yaml',0o644),
}

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def validate_release(manifest_path=MANIFEST,root=ROOT):
    if manifest_path.is_symlink():raise ValueError('Unsafe release manifest')
    value=json.loads(manifest_path.read_text())
    if value.get('schema_version')!=1 or set(value['files'])!=set(FILES):
        raise ValueError('Unexpected release scope')
    for relative,expected in value['files'].items():
        path=root/relative
        if path.is_symlink() or path.resolve()!=path.absolute() or digest(path)!=expected:
            raise ValueError('Release digest mismatch')
    installer=root/'native_ops/deploy/install-transport-migration-capability.py'
    if installer.is_symlink() or digest(installer)!=value['installer_sha256']:
        raise ValueError('Installer digest mismatch')
    return digest(manifest_path)


def run(*args):
    return subprocess.run(args,stdin=subprocess.DEVNULL,capture_output=True,check=True,timeout=90)

def user(*args):
    return run('/usr/sbin/runuser','-u','ops-user','--','/usr/bin/env',
        'XDG_RUNTIME_DIR=/run/user/1000','DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',*args)

def replace(destination,source,uid,gid,mode):
    if destination.is_symlink():raise ValueError('Unsafe destination')
    temporary=destination.with_name(destination.name+'.catalog-next')
    with temporary.open('xb') as stream:
        stream.write(source.read_bytes());stream.flush();os.fsync(stream.fileno())
    os.chown(temporary,uid,gid);os.chmod(temporary,mode);os.replace(temporary,destination)

def install():
    if os.geteuid()!=0:raise ValueError('Local administrator required')
    os.umask(0o077)
    release_hash=validate_release()
    action_id='direct-owner-construction-2026-09-14'
    pilot=Path('/home/ops-user/.local/state/ops-cli-pilot/attempts.sqlite3')
    with sqlite3.connect('file:'+str(pilot)+'?mode=ro',uri=True) as db:
        if db.execute("SELECT 1 FROM attempts WHERE status='running'").fetchone():raise ValueError('Pilot busy')
        if db.execute("SELECT 1 FROM approvals WHERE status='executing'").fetchone():raise ValueError('Action executing')
    archive=Path('/var/lib/ops-native-archives')/('migrate-observer-transports-'+str(time.time_ns()))
    archive.mkdir(mode=0o700,parents=True)
    # Seal source bytes into a private root-owned directory before touching live files.
    manifest=json.loads(MANIFEST.read_text())
    candidates={}
    for index,(relative,(destination,mode)) in enumerate(FILES.items()):
        candidate=archive/('candidate-'+str(index));candidate.write_bytes((ROOT/relative).read_bytes())
        if digest(candidate)!=manifest['files'][relative]:raise ValueError('Source changed during staging')
        candidates[Path(destination)]=(candidate,mode)
    run('/usr/sbin/visudo','-cf',str(candidates[Path('/etc/sudoers.d/ops-broker-migrate-observer-transports')][0]))
    code='''import json,yaml
from pathlib import Path
from ops_broker.policy import RBACPolicy
p=yaml.safe_load(Path('/etc/ops-broker/rbac.yaml').read_text())
b=p['roles']['supervised-operator']['runbooks']
if 'security.migrate-observer-transports.v1' not in b:b.append('security.migrate-observer-transports.v1')
RBACPolicy.from_data(p)
e=yaml.safe_load(Path('/etc/ops-broker/executables.yaml').read_text())
h=e['sudo']['allowed_helpers']
if '/usr/local/libexec/ops-runbooks/migrate-observer-transports' not in h:h.append('/usr/local/libexec/ops-runbooks/migrate-observer-transports')
print(json.dumps({'rbac':yaml.safe_dump(p,sort_keys=False),'executables':yaml.safe_dump(e,sort_keys=False)}))
'''
    rendered=json.loads(run('/opt/ops-broker/venv/bin/python','-I','-c',code).stdout)
    for name,content in rendered.items():
        candidate=archive/(name+'.yaml');candidate.write_text(content)
        candidates[Path('/etc/ops-broker')/(name+'.yaml')]=(candidate,0o640)
    prior={}
    for index,destination in enumerate(candidates):
        if destination.is_symlink():raise ValueError('Unsafe installed file')
        if destination.exists():
            saved=archive/('before-'+str(index));shutil.copy2(destination,saved)
            st=destination.stat();prior[str(destination)]=[str(saved),st.st_uid,st.st_gid,st.st_mode&0o777]
        else:prior[str(destination)]=None
    (archive/'prior.json').write_text(json.dumps(prior))
    (archive/'authorization.json').write_text(json.dumps({'action_id':action_id,'manifest_sha256':release_hash}))
    try:
        was_active=user('/usr/bin/systemctl','--user','is-active','ops-cli-pilot.service').stdout.strip()==b'active'
    except subprocess.CalledProcessError as error:
        if error.returncode!=3:raise
        was_active=False
    try:
        if was_active:user('/usr/bin/systemctl','--user','stop','ops-cli-pilot.service')
        for destination,(source,mode) in candidates.items():
            gid=destination.stat().st_gid if destination.exists() else 0
            replace(destination,source,0,gid,mode)
        run('/usr/sbin/visudo','-c')
        check="from pathlib import Path;from ops_broker.runbooks import ExecutablePolicy,RunbookRegistry;r=RunbookRegistry.load(Path('/opt/ops-broker/runbooks'),ExecutablePolicy.load(Path('/etc/ops-broker/executables.yaml')));assert r.get('security.migrate-observer-transports.v1').action_class=='C'"
        run('/opt/ops-broker/venv/bin/python','-I','-c',check)
        run('/usr/bin/systemctl','daemon-reload')
        run('/usr/bin/systemctl','restart','ops-broker.service')
        for unit in ['ops-broker.service','ops-native.service']:run('/usr/bin/systemctl','is-active','--quiet',unit)
        if was_active:user('/usr/bin/systemctl','--user','start','ops-cli-pilot.service')
        result={'status':'installed','manifest_sha256':release_hash,'action_id':action_id,
                'remote_changes':False,'remote_checks_executed':False,'archive':str(archive)}
        (archive/'result.json').write_text(json.dumps(result));return result
    except BaseException:
        for name,info in prior.items():
            destination=Path(name)
            if info is None:destination.unlink(missing_ok=True)
            else:replace(destination,Path(info[0]),*info[1:])
        run('/usr/bin/systemctl','daemon-reload')
        run('/usr/bin/systemctl','restart','ops-broker.service')
        if was_active:user('/usr/bin/systemctl','--user','start','ops-cli-pilot.service')
        (archive/'result.json').write_text(json.dumps({'status':'rolled_back','action_id':action_id}))
        raise

if __name__=='__main__':
    try:print(json.dumps(install()))
    except Exception as error:
        print(json.dumps({'status':'unavailable','error_type':type(error).__name__}),file=sys.stderr)
        raise SystemExit(1)
