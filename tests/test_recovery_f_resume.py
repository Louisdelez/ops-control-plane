import ast
from pathlib import Path
import runpy
import subprocess
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'artifacts/recovery-patch/resume-recovery-f'
UNIT = 'ops-control-plane-bootstrap-network-baseline.service'
COMMAND = ['/usr/bin/systemctl', 'reset-failed', UNIT]
MISSING = f'Failed to reset failed state of unit {UNIT}: Unit {UNIT} not loaded.\n'.encode()


def wrapper(stderr=MISSING, state='inactive', valid=True):
    source = runpy.run_path(str(SOURCE))['PROGRAM']
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'observed_run')
    checks = []
    def validate(**kwargs):
        checks.append(kwargs)
        if not valid:
            raise ValueError('unit differs')
    env = {
        'subprocess': subprocess, 'json': __import__('json'),
        'original_run': lambda *a, **k: SimpleNamespace(returncode=1, stderr=stderr, stdout=None),
        'namespace': {
            '_validate_network_baseline_unit': validate,
            '_strict_systemctl_properties': lambda _: {'ActiveState': state},
        },
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), env)
    return env['observed_run'], checks


def test_unloaded_reset_reconciles_only_after_identity_and_state_checks():
    run, checks = wrapper()
    assert run(COMMAND).returncode == 0
    assert checks == [{'require_active': False}]


@pytest.mark.parametrize('stderr,state', [(b'Access denied\n', 'inactive'), (MISSING, 'active'), (MISSING, 'failed')])
def test_other_failures_are_not_suppressed(stderr, state):
    run, _ = wrapper(stderr, state)
    assert run(COMMAND).returncode == 1


def test_changed_unit_is_rejected():
    run, _ = wrapper(valid=False)
    with pytest.raises(ValueError):
        run(COMMAND)


def test_start_failure_is_never_suppressed():
    run, checks = wrapper()
    assert run(['/usr/bin/systemctl', 'start', UNIT]).returncode == 1
    assert checks == []
