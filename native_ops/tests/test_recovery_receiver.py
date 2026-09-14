import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile

import pytest

spec=importlib.util.spec_from_file_location('recovery_receiver',Path(__file__).parents[2]/'broker/deploy/recovery/receive-recovery-bundle.py')
receiver=importlib.util.module_from_spec(spec);spec.loader.exec_module(receiver)


def stream(extra=None, mismatch=False):
    cipher=b'age-encryption.org/v1\nsynthetic ciphertext for receiver validation'
    manifest={'sha256':hashlib.sha256(cipher).hexdigest(),'bytes':len(cipher),
              'decryption_verified':True,'private_identity_included':False}
    if mismatch:manifest['sha256']='0'*64
    files={'bundle.tar.age':cipher,'manifest.json':json.dumps(manifest).encode()}
    if extra:files[extra]=b'forbidden'
    result=io.BytesIO()
    with tarfile.open(fileobj=result,mode='w') as archive:
        for name,data in files.items():
            info=tarfile.TarInfo(name);info.size=len(data)
            archive.addfile(info,io.BytesIO(data))
    result.seek(0)
    return result


def test_ciphertext_copy_is_idempotent_and_detects_conflict(tmp_path):
    receipt=receiver.receive(stream(),tmp_path)
    assert receiver.receive(stream(),tmp_path)==receipt
    root=tmp_path/receipt['relative_directory']
    assert len(list((tmp_path/'ops-encrypted-backups').iterdir()))==1
    (root/'bundle.tar.age').write_bytes(b'altered')
    with pytest.raises(ValueError):receiver.receive(stream(),tmp_path)
    assert (root/'bundle.tar.age').read_bytes()==b'altered'


@pytest.mark.parametrize('extra',['../escape','/absolute','private-identity'])
def test_unknown_members_are_rejected_without_publication(tmp_path,extra):
    with pytest.raises(ValueError):receiver.receive(stream(extra),tmp_path)
    assert not list((tmp_path/'ops-encrypted-backups').iterdir())


def test_bad_digest_and_destination_symlink_are_rejected(tmp_path):
    with pytest.raises(ValueError):receiver.receive(stream(mismatch=True),tmp_path)
    (tmp_path/'ops-encrypted-backups').rmdir()
    elsewhere=tmp_path/'elsewhere';elsewhere.mkdir(mode=0o700)
    (tmp_path/'ops-encrypted-backups').symlink_to(elsewhere,target_is_directory=True)
    with pytest.raises(ValueError):receiver.receive(stream(),tmp_path)
    assert not list(elsewhere.iterdir())
