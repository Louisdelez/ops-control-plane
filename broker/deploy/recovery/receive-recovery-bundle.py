"""Append-only ciphertext reception for the reviewed daily recovery transport."""
from pathlib import Path
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import tempfile

LIMIT = 256 * 1024 * 1024
QUOTA = 20 * 1024 * 1024 * 1024


def private_directory(path):
    metadata = path.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise ValueError('Unsafe directory')


def receive(stream, home):
    root = home/'ops-encrypted-backups'
    root.mkdir(mode=0o700, exist_ok=True)
    private_directory(root)
    stage = Path(tempfile.mkdtemp(prefix='.incoming-daily-', dir=root))
    try:
        seen = set()
        with tarfile.open(fileobj=stream, mode='r|') as archive:
            for entry in archive:
                maximum = LIMIT if entry.name == 'bundle.tar.age' else 16384
                if entry.name not in {'bundle.tar.age','manifest.json'} or entry.name in seen or not entry.isfile() or not 0 < entry.size <= maximum:
                    raise ValueError('Invalid archive member')
                if shutil.disk_usage(root).free < entry.size + 512*1024*1024:
                    raise ValueError('Insufficient destination space')
                with (stage/entry.name).open('xb') as output:
                    source = archive.extractfile(entry)
                    shutil.copyfileobj(source, output, 1024*1024)
                    output.flush();os.fsync(output.fileno())
                (stage/entry.name).chmod(0o600)
                seen.add(entry.name)
        if seen != {'bundle.tar.age','manifest.json'}:
            raise ValueError('Incomplete recovery bundle')
        manifest = json.loads((stage/'manifest.json').read_text())
        if not re.fullmatch('[0-9a-f]{64}', str(manifest.get('sha256',''))):
            raise ValueError('Invalid digest')
        bundle = stage/'bundle.tar.age'
        with bundle.open('rb') as f:
            if f.read(22) != b'age-encryption.org/v1\n':
                raise ValueError('Ciphertext required')
            f.seek(0);digest = hashlib.file_digest(f,'sha256').hexdigest()
        if digest != manifest['sha256'] or bundle.stat().st_size != manifest.get('bytes') or manifest.get('decryption_verified') is not True or manifest.get('private_identity_included') is not False:
            raise ValueError('Bundle verification failed')
        final = root/('dell-daily-'+digest)
        if final.exists() or final.is_symlink():
            private_directory(final)
            existing = final/'bundle.tar.age'
            meta = existing.lstat()
            if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_mode & 0o077 or meta.st_size != bundle.stat().st_size:
                raise ValueError('Conflicting existing copy')
            with existing.open('rb') as f:
                if hashlib.file_digest(f,'sha256').hexdigest() != digest:
                    raise ValueError('Conflicting existing copy')
            metadata_file = final/'manifest.json'
            metadata_stat = metadata_file.lstat()
            if not stat.S_ISREG(metadata_stat.st_mode) or metadata_stat.st_size > 16384 or metadata_stat.st_uid != os.geteuid() or metadata_stat.st_mode & 0o077:
                raise ValueError('Unsafe existing manifest')
            if metadata_file.read_bytes() != (stage/'manifest.json').read_bytes():
                raise ValueError('Conflicting existing manifest')
        else:
            used = 0
            for directory in root.glob('dell-daily-*'):
                private_directory(directory)
                existing = directory/'bundle.tar.age'
                meta = existing.lstat()
                if not stat.S_ISREG(meta.st_mode):raise ValueError('Unsafe existing backup')
                used += meta.st_size
            if used + bundle.stat().st_size > QUOTA:
                raise ValueError('Daily recovery quota reached; no deletion performed')
            stage.rename(final)
        return {'status':'copied_and_verified','sha256':digest,'bytes':manifest['bytes'],
                'relative_directory':'ops-encrypted-backups/'+final.name}
    finally:
        if stage.exists():shutil.rmtree(stage)


if __name__ == '__main__':
    os.umask(0o077)
    try:print(json.dumps(receive(sys.stdin.buffer,Path.home())))
    except Exception:raise SystemExit('Recovery receiver refused the operation') from None
