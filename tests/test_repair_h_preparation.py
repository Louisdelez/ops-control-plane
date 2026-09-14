import base64
import hashlib
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / 'artifacts/repair-h/prepare-release-h.py'


@pytest.mark.parametrize('script_name', ['repair-h/prepare-release-h.py', 'repair-i/prepare-release-i.py', 'repair-j/prepare-release-j.py', 'repair-k/prepare-release-k.py', 'audit-l/prepare-release-l.py', 'repair-m/prepare-release-m.py', 'repair-n/prepare-release-n.py'])
@pytest.mark.parametrize('interruption', [None, 1, 2])
def test_archival_resumes_without_losing_terminal_evidence(tmp_path, monkeypatch, interruption, script_name):
    ns = runpy.run_path(str(SCRIPT.parents[1] / script_name))
    run = ns['reconcile']
    env = run.__globals__
    anchor = tmp_path / 'anchor'
    anchor.mkdir()
    (anchor / 'old-helper').write_bytes(b'old release')
    ledger = tmp_path / 'old-ledger'
    ledger.mkdir()
    (ledger / 'audit').write_bytes(b'original audit')
    state = tmp_path / 'repair'
    state.mkdir()
    plan = state / 'plan.json'
    payload = b'new reviewed helper'
    plan.write_text(json.dumps({'phase': 'prepared', 'release': ns['RELEASE'], 'archive_paths': [str(anchor), str(ledger)], 'files': {'control-plane-bootstrap': {'raw': base64.b64encode(payload).decode(), 'sha256': hashlib.sha256(payload).hexdigest(), 'mode': 0o555}}}))
    for key, value in {'ANCHOR': anchor, 'STATE': state, 'PLAN': plan, 'LOCK': tmp_path / 'lock', 'FORWARDING_CONF': tmp_path / 'sysctl.conf', 'OLD_UNITS': ()}.items():
        monkeypatch.setitem(env, key, value)
    monkeypatch.setitem(env, 'command', lambda *a: None)
    real_os = env['os']
    facade = SimpleNamespace(**vars(real_os))
    calls = 0
    def rename(a, b):
        nonlocal calls
        real_os.rename(a, b)
        calls += 1
        if calls == interruption:
            raise OSError('simulated interruption after rename')
    facade.rename = rename
    monkeypatch.setitem(env, 'os', facade)
    if interruption:
        with pytest.raises(OSError):
            run()
    facade.rename = real_os.rename
    run()
    run()
    assert json.loads(plan.read_text())['phase'] == 'complete'
    assert (state / 'archive' / anchor.relative_to('/') / 'old-helper').read_bytes() == b'old release'
    assert (state / 'archive' / ledger.relative_to('/') / 'audit').read_bytes() == b'original audit'
    assert (anchor / 'control-plane-bootstrap').read_bytes() == payload
    assert not ledger.exists()
