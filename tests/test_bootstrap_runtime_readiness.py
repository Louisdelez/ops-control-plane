from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_unsealed_standby_is_not_ready_for_root_recovery(monkeypatch):
    ns = runpy.run_path(str(ROOT / 'deploy/control-plane/bin/control-plane-bootstrap'))
    wait = ns['_wait_openbao']
    env = wait.__globals__
    states = iter([(429, {'initialized': True, 'sealed': False, 'standby': True}), (200, {'initialized': True, 'sealed': False, 'standby': False})])
    calls = []
    def request(*a, **k):
        calls.append(a)
        return next(states)
    monkeypatch.setitem(env, 'openbao_request', request)
    monkeypatch.setitem(env, 'time', SimpleNamespace(monotonic=lambda: 0, sleep=lambda _: None))
    assert wait()['standby'] is False
    assert len(calls) == 2


@pytest.mark.parametrize('present', [False, True])
def test_missing_docker_is_absent_but_broken_executable_is_an_error(monkeypatch, present):
    ns = runpy.run_path(str(ROOT / 'deploy/control-plane/bin/control-plane-deployment-worker'))
    check = ns['docker_is_rootful']
    env = check.__globals__
    original = env['subprocess']
    def unavailable(*a, **k):
        raise FileNotFoundError('/usr/bin/docker')
    monkeypatch.setitem(env, 'subprocess', SimpleNamespace(run=unavailable, PIPE=original.PIPE, DEVNULL=original.DEVNULL))
    monkeypatch.setitem(env, 'os', SimpleNamespace(path=SimpleNamespace(lexists=lambda p: present)))
    if present:
        with pytest.raises(FileNotFoundError):
            check()
    else:
        assert check() is False


def test_http_failure_audit_context_does_not_include_response_or_arbitrary_paths():
    ns = runpy.run_path(str(ROOT / 'deploy/control-plane/bin/control-plane-bootstrap'))
    error = ns['BootstrapError']('openbao_rejected', 'private response')
    error.http_status = 503
    error.request_method = 'GET'
    error.request_path = '/v1/sys/generate-root/attempt'
    context = ns['_openbao_failure_context']
    assert context(error) == {'http_status': 503, 'request_method': 'GET', 'request_path': '/v1/sys/generate-root/attempt'}
    error.http_status = True
    error.request_method = 'private'
    error.request_path = '/v1/secret/private'
    assert context(error) == {}
