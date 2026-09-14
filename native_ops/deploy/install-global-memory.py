"""Local owner-authorized MCP history update, with preserved runtime rollback."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import time

ROOT = Path('/home/ops-user/ops-control-plane')
RELEASE = 'ops-native-2026.09.12.1'


def main():
    assert os.geteuid() == 0
    os.umask(0o022)
    manifest = runpy.run_path(str(ROOT/'native_ops/release.py'))['verify'](ROOT)
    assert manifest['release_id'] == RELEASE
    wheel = ROOT/'artifacts/global-memory-2026-09-12/wheels/ops_broker-0.1.3-py3-none-any.whl'
    # Validate wheel source files against the reviewed manifest before root install.
    import zipfile
    with zipfile.ZipFile(wheel) as z:
        for source in (ROOT/'broker/src/ops_broker').glob('*.py'):
            assert z.read('ops_broker/'+source.name) == source.read_bytes()
    archive = Path('/var/lib/ops-native-archives')/('global-memory-'+str(time.time_ns()))
    archive.mkdir(mode=0o700)
    venv = Path('/opt/ops-broker/venv')
    shutil.copytree(venv, archive/'broker-venv', symlinks=True)
    published = Path('/usr/local/share/ops-native/releases')/(RELEASE+'.json')
    assert not published.exists()
    def run(*args):
        return subprocess.run(args, check=True, capture_output=True, timeout=180)
    try:
        run(str(venv/'bin/python'), '-m', 'pip', 'install', '--no-index', '--no-deps', '--force-reinstall', str(wheel))
        run(str(venv/'bin/python'), '-m', 'pip', 'check')
        run('/usr/sbin/runuser', '-u', 'opsbroker', '--', str(venv/'bin/python'), '-I', '-c',
            'from ops_broker.service import BrokerService; assert callable(BrokerService.list_memory_history)')
        run('/usr/sbin/runuser', '-u', 'ops-user', '--', '/usr/bin/env',
            'XDG_RUNTIME_DIR=/run/user/1000', 'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',
            '/usr/bin/systemctl', '--user', 'start', 'ops-memory-sync.service')
        status = json.loads(Path('/home/ops-user/.local/state/ops-memory-sync/latest.json').read_text())
        assert status['checked_at'] >= time.time()-180 and status['failed'] == 0
        published.write_bytes((ROOT/'native_ops/releases/current.json').read_bytes())
        published.chmod(0o444)
        receipt = {'release': RELEASE, 'broker_package': '0.1.3', 'archive': str(archive),
                   'manifest_sha256': hashlib.sha256(published.read_bytes()).hexdigest(),
                   'history_capture': status, 'api_acceptance': 'pending_provider_credentials',
                   'whole_project_complete': False, 'installed_at': int(time.time())}
        proof = published.with_name(RELEASE+'-install.json')
        proof.write_text(json.dumps(receipt, indent=2)+'\n')
        proof.chmod(0o444)
        print(json.dumps(receipt))
    except BaseException:
        venv.rename(archive/'failed-broker-venv')
        (archive/'broker-venv').rename(venv)
        raise


if __name__ == '__main__':
    main()
