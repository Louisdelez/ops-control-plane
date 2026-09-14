"""Install the reviewed approval/workflow repair, retaining packages and helpers."""
from pathlib import Path
import hashlib
import json
import os
import runpy
import shutil
import sqlite3
import subprocess
import time

ROOT=Path('/home/ops-user/ops-control-plane')
OUT=ROOT/'artifacts/continuation-2026-09-11'
RELEASE='ops-native-2026.09.11.10'

def run(*args):
    return subprocess.run(args,check=True,capture_output=True,timeout=120)

def user(*args):
    return run('/usr/sbin/runuser','-u','ops-user','--','/usr/bin/env',
        'XDG_RUNTIME_DIR=/run/user/1000','DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',*args)

def atomic(path, source, mode):
    temporary=path.with_name(path.name+'.continuation-next')
    with temporary.open('xb') as output:
        output.write(source.read_bytes());output.flush();os.fsync(output.fileno())
    temporary.chmod(mode);os.replace(temporary,path)

def main():
    assert os.geteuid()==0
    os.umask(0o077)
    assert runpy.run_path(str(ROOT/'native_ops/release.py'))['verify'](ROOT)['release_id']==RELEASE
    for relative,digest in json.loads((OUT/'payload.json').read_text()).items():
        path=ROOT/relative
        assert not path.is_symlink() and hashlib.sha256(path.read_bytes()).hexdigest()==digest
    published=Path('/usr/local/share/ops-native/releases')/(RELEASE+'.json')
    assert not published.exists(),'Release already installed'
    with sqlite3.connect('file:/home/ops-user/.local/state/ops-cli-pilot/attempts.sqlite3?mode=ro',uri=True) as db:
        assert not db.execute("SELECT 1 FROM attempts WHERE status='running'").fetchone(),'Pilot busy'
        assert not db.execute("SELECT 1 FROM approvals WHERE status='executing'").fetchone(),'Approval execution in progress'
    with sqlite3.connect('file:/var/lib/ops-broker/state.db?mode=ro',uri=True) as db:
        assert not db.execute("SELECT 1 FROM actions WHERE status='running'").fetchone(),'Broker action running'
    archive=Path('/var/lib/ops-native-archives')/('continuation-'+str(time.time_ns()))
    archive.mkdir(mode=0o700,parents=True)
    packages={Path('/opt/ops-native/venv'):['ops_native-0.1.11-py3-none-any.whl','ops_memory-0.1.2-py3-none-any.whl'],
              Path('/opt/ops-broker/venv'):['ops_broker-0.1.2-py3-none-any.whl']}
    for index,venv in enumerate(packages):shutil.copytree(venv,archive/('venv-'+str(index)),symlinks=True)
    memory=Path('/opt/ops-memory/src/ops_memory')
    shutil.copytree(memory,archive/'memory-source',symlinks=True)
    helpers={Path('/usr/local/libexec/ops-runbooks/remote-preflight'):ROOT/'broker/deploy/helpers/remote-preflight',
             Path('/usr/local/libexec/ops-runbooks/remote-preflight-worker'):ROOT/'broker/deploy/helpers/remote-preflight-worker'}
    for target in helpers:
        assert target.is_file() and not target.is_symlink(),str(target)
        shutil.copy2(target,archive/target.name)
    user('/usr/bin/systemctl','--user','stop','ops-cli-pilot.service')
    run('/usr/bin/systemctl','stop','ops-native-capabilities.timer','ops-native-capabilities.service','ops-native.service','ops-broker.service')
    try:
        for venv,wheels in packages.items():
            previous_umask=os.umask(0o022)
            try:run(str(venv/'bin/python'),'-m','pip','install','--no-index','--no-deps','--force-reinstall',*[str(OUT/'wheels'/wheel) for wheel in wheels])
            finally:os.umask(previous_umask)
            run(str(venv/'bin/python'),'-m','pip','check')
        for target,source in helpers.items():atomic(target,source,0o755)
        for source in (ROOT/'memory/src/ops_memory').glob('*.py'):atomic(memory/source.name,source,0o644)
        for venv,source,module in [(Path('/opt/ops-native/venv'),'native_ops/ops_native','ops_native'),
                                    (Path('/opt/ops-broker/venv'),'broker/src/ops_broker','ops_broker')]:
            code="from pathlib import Path;import "+module+";target=Path("+module+".__file__).parent;source=Path("+repr(str(ROOT/source))+");assert all((target/f.name).read_bytes()==f.read_bytes() for f in source.glob('*.py'))"
            run(str(venv/'bin/python'),'-I','-c',code)
        for identity,python,module in [('ops-user','/opt/ops-native/venv/bin/python','ops_native.workflows'),
                ('hermesd','/opt/ops-native/venv/bin/python','ops_native.cli'),
                ('opsbroker','/opt/ops-broker/venv/bin/python','ops_broker.service')]:
            run('/usr/sbin/runuser','-u',identity,'--',python,'-I','-c','import '+module)
        assert all((memory/f.name).read_bytes()==f.read_bytes() for f in (ROOT/'memory/src/ops_memory').glob('*.py'))
        run('/usr/sbin/runuser','-u','hermesd','--','/usr/bin/env','PYTHONPATH=/opt/ops-memory/src','/opt/ops-memory/mcp-venv/bin/python','-c','import ops_memory.mcp_server')
        started=time.time()
        run('/usr/bin/systemctl','start','ops-broker.service','ops-native.service','ops-native-capabilities.timer')
        user('/usr/bin/systemctl','--user','start','ops-cli-pilot.service')
        for _ in range(15):
            state=json.loads(Path('/var/lib/hermes/native-ops/connector/status.json').read_text())
            if state.get('status')=='running' and state['updated_at']>started:break
            time.sleep(1)
        else:raise RuntimeError('Connector did not recover')
        for unit in ['ops-broker.service','ops-native.service','ops-native-approval.service','ops-memory.service','openbao.service']:
            run('/usr/bin/systemctl','is-active','--quiet',unit)
        user('/usr/bin/systemctl','--user','is-active','--quiet','ops-cli-pilot.service')
        published.write_bytes((ROOT/'native_ops/releases/current.json').read_bytes());published.chmod(0o444)
        result={'release':RELEASE,'status':'installed','broker_version':'0.1.2','native_version':'0.1.11','memory_version':'0.1.2',
            'archive':str(archive),'verified_at':int(time.time()),'manifest_sha256':hashlib.sha256(published.read_bytes()).hexdigest(),
            'changes':['approval summary requester and observer-safe followup','durable observation workflows','documented prod VPN fallback','read-only MCP annotations and native Hermes profiles'],
            'remote_mutations':False}
        proof=published.with_name(RELEASE+'-install.json');proof.write_text(json.dumps(result,indent=2)+'\n');proof.chmod(0o444)
        (archive/'result.json').write_text(json.dumps(result)+'\n')
        print(json.dumps(result))
    except BaseException:
        run('/usr/bin/systemctl','stop','ops-native.service','ops-broker.service')
        user('/usr/bin/systemctl','--user','stop','ops-cli-pilot.service')
        for index,venv in enumerate(packages):
            venv.rename(archive/('failed-venv-'+str(index)))
            (archive/('venv-'+str(index))).rename(venv)
        for target in helpers:atomic(target,archive/target.name,0o755)
        memory.rename(archive/'failed-memory-source');(archive/'memory-source').rename(memory)
        run('/usr/bin/systemctl','start','ops-broker.service','ops-native.service','ops-native-capabilities.timer')
        user('/usr/bin/systemctl','--user','start','ops-cli-pilot.service')
        raise

if __name__=='__main__':main()
