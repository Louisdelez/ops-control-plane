"""Regression for a sealed release being rejected by its recovery supervisor."""
import hashlib
import os
from pathlib import Path
import runpy
import stat
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('fault', [None, 'writable-helper', 'writable-unit', 'different-helper'])
def test_recovery_validates_sealed_release_modes(tmp_path, monkeypatch, fault):
    ns = runpy.run_path(str(ROOT / 'deploy/control-plane/bin/control-plane-bootstrap'))
    validate = ns['_validate_standard_recovery_unit_bytes']
    env = validate.__globals__
    release = tmp_path / 'releases' / 'test-release'
    tree = release / 'tree'
    unit = ns['RECOVERY_UNITS'][1]
    installed = tmp_path / 'installed'
    helper = installed / 'helper'
    unit_path = installed / unit
    staged_helper = tree / 'deploy/control-plane/bin/control-plane-bootstrap'
    staged_unit = tree / 'deploy/control-plane/systemd' / unit
    for path, payload, mode in (
        (helper, b'reviewed helper', 0o700),
        (unit_path, b'reviewed unit', 0o644),
        (staged_helper, b'reviewed helper', 0o555),
        (staged_unit, b'reviewed unit', 0o444),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(mode)
    release.chmod(0o500)
    if fault == 'writable-helper':
        staged_helper.chmod(0o755)
    elif fault == 'writable-unit':
        staged_unit.chmod(0o644)
    elif fault == 'different-helper':
        helper.write_bytes(b'other helper')

    # The fixture is owned by the unprivileged test runner. Only ownership is
    # adapted; real inode types, modes, link counts, contents and races remain
    # checked by the production reader.
    def ownership(info):
        fields = {k: getattr(info, k) for k in dir(info) if k.startswith('st_')}
        return SimpleNamespace(**dict(fields, st_uid=0, st_gid=0))
    facade = SimpleNamespace(**vars(os))
    facade.lstat = lambda p: ownership(os.lstat(p))
    facade.fstat = lambda fd: ownership(os.fstat(fd))
    monkeypatch.setitem(env, 'os', facade)
    monkeypatch.setitem(env, 'DEPLOYMENT_RELEASES_ROOT', release.parent)
    monkeypatch.setitem(env, 'INSTALLED_HELPER', helper)
    monkeypatch.setitem(env, '_independent_source_digest', lambda *a, **k: ('a' * 64, b'worker'))
    monkeypatch.setitem(env, '_standard_recovery_enablement', lambda u: (installed / 'link', unit_path))
    transaction = {'staged_release': str(release), 'release_id': release.name, 'source_tree_sha256': 'a' * 64}
    marker = {'bootstrap_helper_sha256': hashlib.sha256(b'reviewed helper').hexdigest()}
    if fault:
        with pytest.raises(ns['BootstrapError']):
            validate(marker, transaction, unit)
    else:
        assert validate(marker, transaction, unit) == unit_path
