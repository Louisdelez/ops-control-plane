"""Exercise execve of source entrypoints, not an interpreter substituted by tests."""
from pathlib import Path
import runpy
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINTS = ('scripts/install-broker.sh', 'scripts/install-orchestrator.sh',
               'scripts/install-hermes.sh', 'scripts/install-zulip-bridge.sh',
               'deploy/zulip-local/bin/install.sh')

@pytest.mark.parametrize('relative', ENTRYPOINTS)
def test_reviewed_installer_can_be_executed_directly(relative):
    result = subprocess.run([str(ROOT / relative), '--help'], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    assert result.returncode == 0
    assert b'Usage:' in result.stdout

@pytest.mark.parametrize('kind,expected_errno', [('missing', 2), ('not_executable', 13)])
def test_launch_failure_is_attributed_without_logging_arguments(tmp_path, monkeypatch, kind, expected_errno):
    w = runpy.run_path(str(ROOT / 'deploy/control-plane/bin/control-plane-deployment-worker'))
    command = tmp_path / 'entrypoint'
    if kind == 'not_executable':
        command.write_text('#!/bin/sh\nexit 0\n'); command.chmod(0o600)
    events = []
    monkeypatch.setitem(w['run_command'].__globals__, 'append_audit', events.append)
    with pytest.raises(w['DeploymentError']) as failure:
        w['run_command']('install-zulip', [str(command), 'private-argument-must-not-be-recorded'], timeout=5)
    assert failure.value.code == 'step_failed'
    assert events == [{'event':'step_failed','step':'install-zulip','reason':'launch_failed','errno':expected_errno}]

@pytest.mark.parametrize('program', ['control-plane-bootstrap','control-plane-deployment-worker'])
def test_source_verification_rejects_unexecutable_entrypoint(tmp_path, monkeypatch, program):
    w=runpy.run_path(str(ROOT/'deploy/control-plane/bin'/program))
    relative='deploy/zulip-local/bin/install.sh'
    script=tmp_path/relative;script.parent.mkdir(parents=True);script.write_text('#!/bin/bash\nexit 0\n');script.chmod(0o644)
    if program.endswith('worker'):
        with pytest.raises(w['DeploymentError'],match='not executable'):
            w['source_tree_digest'](tmp_path,[relative])
    else:
        f=w['_independent_source_digest']
        monkeypatch.setitem(f.__globals__,'SOURCE_ROOTS',(relative,))
        with pytest.raises(w['BootstrapError'],match='not executable'):f(tmp_path)
