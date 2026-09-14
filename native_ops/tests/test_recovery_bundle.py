import importlib.util
from pathlib import Path
import subprocess
import tarfile
import io
import os

import pytest

spec = importlib.util.spec_from_file_location('recovery_bundle', Path(__file__).parents[1]/'deploy/backup-recovery-bundle.py')
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


def test_real_age_roundtrip_excludes_private_identity(tmp_path):
    identity = tmp_path/'identity'
    subprocess.run(['/usr/bin/age-keygen','-o',str(identity)], check=True, capture_output=True)
    recipient = tmp_path/'recipient'
    result = subprocess.run(['/usr/bin/age-keygen','-y',str(identity)], check=True, capture_output=True)
    recipient.write_bytes(result.stdout)
    source = tmp_path/'component'
    source.write_bytes(b'synthetic component recovery state')
    source.chmod(0o600)
    work = tmp_path/'work';work.mkdir()
    output = tmp_path/'bundle.age'
    report = bundle.package({'memory/state.txt':source},work,output,recipient,identity)
    assert report['decryption_verified'] and not report['full_stack_restore_verified']
    assert report['private_identity_included'] is False
    plaintext = subprocess.run(['/usr/bin/age','-d','-i',str(identity),str(output)], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(plaintext)) as archive:
        assert archive.getnames() == ['memory/state.txt']
        assert archive.extractfile('memory/state.txt').read() == source.read_bytes()
    with pytest.raises(ValueError,match='identity'):
        bundle.package({'identity':identity},work,tmp_path/'forbidden.age',recipient,identity)


def test_backup_source_rejects_symlink_ancestors_and_stale_data(tmp_path):
    actual=tmp_path/'actual';actual.mkdir()
    source=actual/'backup.db';source.write_bytes(b'fixture');source.chmod(0o600)
    link=tmp_path/'link';link.symlink_to(actual,target_is_directory=True)
    with pytest.raises(ValueError):bundle.regular(link/'backup.db')
    os.utime(source,(1,1))
    with pytest.raises(ValueError,match='Stale'):bundle.latest(actual,'*.db')


def test_corrupt_sqlite_is_rejected(tmp_path):
    path=tmp_path/'bad.db';path.write_bytes(b'not a database')
    with pytest.raises(Exception):bundle.sqlite_check(path)
