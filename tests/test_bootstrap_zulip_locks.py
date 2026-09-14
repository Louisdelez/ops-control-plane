"""Exercise bootstrap calls with real pipes, child processes and lifecycle locks."""
import fcntl
import os
from pathlib import Path
import runpy
from types import SimpleNamespace
import subprocess
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('realm', [False, True])
def test_bootstrap_keeps_global_guard_and_acquires_separate_zulip_lock(monkeypatch, tmp_path, realm):
    ns = runpy.run_path(str(ROOT / 'deploy/control-plane/bin/control-plane-bootstrap'))
    fn = ns['_provision_realm' if realm else '_install_and_activate']
    g = fn.__globals__
    global_path = tmp_path / 'global.lock'
    global_fd = os.open(global_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(global_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    local_path = tmp_path / 'zulip' / 'operation.lock'
    probe = tmp_path / 'probe.py'
    probe.write_text('''import os, sys, fcntl, runpy
from pathlib import Path
assert '--deployment-lock-fd' not in sys.argv
library = runpy.run_path(sys.argv[1])
local = Path(sys.argv[2]); global_path = Path(sys.argv[3]); global_fd = int(sys.argv[4])
assert os.fstat(global_fd).st_ino == global_path.stat().st_ino
other = os.open(global_path, os.O_RDWR)
try:
    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError: pass
else: raise AssertionError('Global crash-recovery guard lost')
fd = library['acquire_deployment_lock'](local)
try: library['acquire_deployment_lock'](local)
except library['ZulipLocalError']: pass
else: raise AssertionError('Concurrent Zulip operation admitted')
flag = '--admin-password-fd' if '--admin-password-fd' in sys.argv else '--password-fd'
assert os.read(int(sys.argv[sys.argv.index(flag)+1]), 1024) == b'synthetic-only'
if '--approver-id-fd' in sys.argv:
    os.write(int(sys.argv[sys.argv.index('--approver-id-fd')+1]), b'42')
os.close(fd)
''')
    popen = subprocess.Popen
    invoked = []
    def launch(argv, **kwargs):
        assert argv[0].endswith('provision-zulip.py' if realm else 'provision-openbao.py')
        assert global_fd in kwargs['pass_fds']
        invoked.append(argv)
        process = popen([sys.executable, str(probe), str(ROOT / 'deploy/zulip-local/lib/zulip_local.py'), str(local_path), str(global_path), str(global_fd), *argv[1:]], **kwargs)
        # Parent death must not release the guard while the child still owns it.
        os.close(global_fd)
        return process
    monkeypatch.setitem(g, 'subprocess', SimpleNamespace(Popen=launch, DEVNULL=subprocess.DEVNULL, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setitem(g, 'append_audit', lambda *a, **k: None)
    monkeypatch.setitem(g, '_read_json', lambda *a: {'release_id': 'synthetic'})
    for name in ('_set_transaction', '_write_approver_state'):
        monkeypatch.setitem(g, name, lambda *a, **k: None)
    if not realm:
        monkeypatch.setitem(g, '_provision_realm', lambda *a: 42)
    worker = {name: lambda *a, **k: None for name in ('preserve_legacy_hermes_source', 'disable_local_inference', 'ensure_docker', 'run_command', 'atomic_json', 'require_docker_networks_absent', 'validate_docker_route_baseline')}
    try:
        if realm:
            result = fn(worker, bytearray(b'synthetic-only'), global_fd)
        else:
            result = fn(worker, tmp_path, {'artifacts': {'atlas_rpm': {'source_path': 'x.rpm'}, 'hermes_source_archive': {'source_path': 'x.tar'}}}, {'backup_path': str(tmp_path)}, bytearray(b'synthetic-only'), bytearray(b'synthetic-only'), {'zulip_local': False, 'orchestrator': True, 'hermes': True, 'zulip_bridge': True}, {}, global_fd)
        assert result == 42 and len(invoked) == 1
        # Both locks are released after the provisioner exits.
        for path in (global_path, local_path):
            with path.open('rb') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        try: os.close(global_fd)
        except OSError: pass
