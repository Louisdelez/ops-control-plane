import hashlib
import json
import os
from pathlib import Path
import runpy
from types import SimpleNamespace
import pytest

@pytest.fixture
def migration(tmp_path, monkeypatch):
    ns = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'deploy/control-plane/bin/control-plane-deployment-worker'))
    g = ns['preserve_legacy_hermes_source'].__globals__
    source = tmp_path / 'hermes-agent'; source.mkdir(mode=0o750)
    (source / 'historical-file').write_bytes(b'preserve original content')
    backup = tmp_path / 'backup'; backup.mkdir(mode=0o700)
    archive = backup / 'managed-files.tar'; archive.write_bytes(b'authenticated backup fixture')
    metadata = {'phase': 'installing', 'backup_path': str(backup), 'transaction_id': 'audit-test',
                'managed_paths_existing': [str(source)], 'managed_archive': str(archive),
                'managed_archive_sha256': hashlib.sha256(archive.read_bytes()).hexdigest()}
    monkeypatch.setitem(g, 'HERMES_SOURCE_ROOT', source)
    monkeypatch.setitem(g, 'BACKUP_ROOT', backup)
    monkeypatch.setitem(g, 'validate_private_file', lambda p: None)
    monkeypatch.setitem(g, 'systemctl_state', lambda *a: False)
    monkeypatch.setitem(g, 'append_audit', lambda row: None)
    return ns, g, source, backup, metadata

@pytest.mark.parametrize('interrupt', [False, True])
def test_preserves_original_and_resumes_after_rename(migration, monkeypatch, interrupt):
    ns, g, source, backup, metadata = migration
    original_inode = source.stat().st_ino
    real_os = g['os']; facade = SimpleNamespace(**vars(real_os))
    def interrupted_rename(a, b):
        real_os.rename(a, b)
        raise OSError('simulated interruption after atomic rename')
    if interrupt:
        facade.rename = interrupted_rename; monkeypatch.setitem(g, 'os', facade)
        with pytest.raises(OSError):ns['preserve_legacy_hermes_source'](backup, metadata)
        assert json.loads((backup / 'transaction.json').read_text())['hermes_source_migration']['phase'] == 'planned'
        facade.rename = real_os.rename
    ns['preserve_legacy_hermes_source'](backup, metadata)
    assert not source.exists()
    preserved = backup / 'hermes-legacy-source'
    assert preserved.stat().st_ino == original_inode
    assert (preserved / 'historical-file').read_bytes() == b'preserve original content'
    source.mkdir(); (source / '.atlas-source.json').write_text('{}')
    ns['preserve_legacy_hermes_source'](backup, metadata)
    assert (source / '.atlas-source.json').exists()
    assert metadata['hermes_source_migration']['phase'] == 'preserved'

@pytest.mark.parametrize('invalid', ['backup', 'unrecorded_destination', 'active', 'phase'])
def test_refuses_unproved_or_live_migration(migration, monkeypatch, invalid):
    ns, g, source, backup, metadata = migration
    if invalid == 'backup':metadata['managed_archive_sha256'] = '0' * 64
    elif invalid == 'unrecorded_destination':(backup / 'hermes-legacy-source').mkdir()
    elif invalid == 'active':monkeypatch.setitem(g, 'systemctl_state', lambda *a: True)
    else:metadata['phase'] = 'snapshotting'
    with pytest.raises(ns['DeploymentError']):ns['preserve_legacy_hermes_source'](backup, metadata)
    assert (source / 'historical-file').read_bytes() == b'preserve original content'
    assert 'hermes_source_migration' not in metadata


def test_corrupt_rollback_archive_never_removes_installed_tree(migration, monkeypatch):
    ns, g, source, backup, metadata = migration
    metadata.update(hermes_source_archive_sha256=ns['HERMES_SOURCE_ARCHIVE_SHA256'],
                    hermes_transaction_temp_paths=[str(p) for p in ns['HERMES_TRANSACTION_TEMP_PATHS']],
                    managed_archive_sha256='0' * 64)
    removed = []
    monkeypatch.setitem(g, 'MANAGED_BACKUP_PATHS', (source,))
    monkeypatch.setitem(g, 'stop_dependents', lambda: None)
    monkeypatch.setitem(g, 'stop_docker_runtime', lambda: None)
    monkeypatch.setitem(g, '_unit_is_loaded', lambda unit: False)
    monkeypatch.setitem(g, 'run_command', lambda *a, **kw: None)
    monkeypatch.setitem(g, '_safe_remove_managed', lambda path: removed.append(path))
    with pytest.raises(ns['DeploymentError'], match='checksum differs'):
        ns['rollback_transaction'](backup, metadata)
    assert removed == []
    assert (source / 'historical-file').read_bytes() == b'preserve original content'


@pytest.mark.parametrize('filename', ['control-plane-bootstrap', 'control-plane-deployment-worker'])
def test_preflight_classifies_legacy_and_rejects_unsafe_root(tmp_path, monkeypatch, filename):
    ns = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'deploy/control-plane/bin' / filename))
    f = ns['hermes_source_disposition']; source = tmp_path / 'hermes-agent'
    monkeypatch.setitem(f.__globals__, 'HERMES_SOURCE_ROOT', source)
    monkeypatch.setitem(f.__globals__, 'BACKUP_ROOT' if filename.endswith('worker') else 'DEPLOYMENT_BACKUP_ROOT', tmp_path / 'backup')
    assert f() == 'absent'
    source.mkdir(mode=0o750)
    assert f() == 'legacy-preserve-and-replace'
    source.chmod(0o777)
    with pytest.raises(Exception, match='unsafe'): f()
    source.chmod(0o750)
    (source / '.atlas-source.json').symlink_to(tmp_path / 'absent-receipt')
    with pytest.raises(Exception, match='unsafe'): f()


@pytest.mark.parametrize('name', ['broker', 'orchestrator', 'hermes', 'zulip-bridge'])
def test_explicit_installer_exit_logs_status_without_arguments(tmp_path, name):
    import subprocess
    logger = tmp_path / 'logger'
    logger.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ATLAS_TEST_LOG"\n')
    logger.chmod(0o700)
    log = tmp_path / 'exit.log'
    env = dict(os.environ, PATH=str(tmp_path) + ':' + os.environ['PATH'], ATLAS_TEST_LOG=str(log))
    script = Path(__file__).resolve().parents[1] / f'scripts/install-{name}.sh'
    result = subprocess.run(['/usr/bin/bash', str(script), '--audit-invalid-argument'],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert result.returncode != 0
    assert f'script=install-{name} line=' in log.read_text()
    assert f'status={result.returncode}' in log.read_text()
    assert '--audit-invalid-argument' not in log.read_text()
