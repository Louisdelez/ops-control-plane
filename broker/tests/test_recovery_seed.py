import io,runpy,tarfile
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
RECEIVER=runpy.run_path(str(ROOT/'broker/deploy/recovery/receive-encrypted-seed.py'))

def seed(extra=None):
 output=io.BytesIO()
 with tarfile.open(fileobj=output,mode='w') as tar:
  for name in sorted(RECEIVER['NAMES']):
   data=b'age-encryption.org/v1\nopaque-ciphertext';info=tarfile.TarInfo(name);info.size=len(data);tar.addfile(info,io.BytesIO(data))
  if extra is not None:tar.addfile(extra,io.BytesIO(b''))
 output.seek(0);return output

def test_receiver_preserves_existing_copy_and_rejects_escape(tmp_path):
 receive=RECEIVER['receive'];first=receive(seed(),tmp_path)
 assert receive(seed(),tmp_path)==first
 with pytest.raises(ValueError):receive(seed(tarfile.TarInfo('../escape')),tmp_path)
 assert not (tmp_path/'escape').exists()
 assert len(list((tmp_path/'ops-encrypted-backups').iterdir()))==1

def test_receiver_rejects_symlink_destination_and_conflicting_payload(tmp_path):
 root=tmp_path/'ops-encrypted-backups';root.symlink_to(tmp_path,target_is_directory=True)
 with pytest.raises(ValueError):RECEIVER['receive'](seed(),tmp_path)
 root.unlink();result=RECEIVER['receive'](seed(),tmp_path)
 final=tmp_path/result['relative_directory'];(final/'unseal-shares.age').write_bytes(b'changed')
 with pytest.raises(ValueError):RECEIVER['receive'](seed(),tmp_path)
 assert (final/'unseal-shares.age').read_bytes()==b'changed'
