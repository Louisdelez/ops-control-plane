#!/usr/bin/env python3
"""Scan the exact staged tree before committing; never print credential values."""
import argparse
import io
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gitleaks', default='gitleaks', help='Path to the Gitleaks executable')
    args = parser.parse_args()
    root = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip())
    tree = subprocess.check_output(['git', 'write-tree'], cwd=root, text=True).strip()
    archive = subprocess.check_output(['git', 'archive', tree], cwd=root)
    forbidden_dirs = {'.git', '.ssh', '.aws', 'secrets', 'credentials', 'backups', 'artifacts', 'runtime', 'private', 'sessions'}
    forbidden_suffixes = {'.key', '.pem', '.p12', '.pfx', '.kdbx', '.age', '.db', '.sqlite', '.sqlite3', '.log', '.dump'}
    count = 0
    with tempfile.TemporaryDirectory(prefix='ops-publication-') as temporary:
        destination = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            for entry in bundle:
                path = PurePosixPath(entry.name)
                if entry.isdir():
                    continue
                if not entry.isfile() or path.is_absolute() or '..' in path.parts:
                    raise SystemExit('Publication refused: unsupported staged entry: ' + entry.name)
                template = path.name in {'.env.example', '.env.sample', '.env.template'}
                if (forbidden_dirs.intersection(path.parts) or path.suffix in forbidden_suffixes
                        or (path.name.startswith('.env') and not template)
                        or path.name in {'auth.json', 'hosts.yml', 'id_rsa', 'id_ed25519'}
                        or path.name.endswith('.age-recipient')):
                    raise SystemExit('Publication refused: sensitive staged path: ' + entry.name)
                content = bundle.extractfile(entry).read()
                if re.search(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{64,}', content):
                    raise SystemExit('Publication refused: private key material in ' + entry.name)
                target = destination.joinpath(*path.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                count += 1
        config = destination / '.gitleaks.toml'
        if not config.is_file():
            raise SystemExit('Publication refused: staged Gitleaks configuration missing')
        result = subprocess.run([args.gitleaks, 'dir', '.', '--config', str(config),
                                 '--redact', '--no-banner'], cwd=destination)
        if result.returncode:
            raise SystemExit(result.returncode)
    print(f'Publication check passed: {count} staged files; tree {tree}')


if __name__ == '__main__':
    main()
