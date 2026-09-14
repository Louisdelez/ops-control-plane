"""Package verified component backups for transport; never copy the age identity.

No network access, remote mutation, retention deletion or live DB file copy.
The result is a component recovery bundle, not a full-machine restore claim.
"""
from pathlib import Path
import datetime
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
import time

ROOT = Path('/var/lib/ops-recovery-bundles')
RECIPIENT = Path('/etc/openbao-backup/recovery.age-recipient')
IDENTITY = Path('/home/ops-user/Documents/Recuperation-Ops-2026-09-11/PRIVE/cle-age.txt')
MAX_FILE = 512 * 1024 * 1024
MAX_TOTAL = 2 * 1024 * 1024 * 1024


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def regular(path):
    metadata = path.lstat()
    if path.resolve() != path.absolute() or not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o022 or not 0 < metadata.st_size <= MAX_FILE:
        raise ValueError('Unsafe backup member')
    return metadata.st_size


def latest(folder, pattern):
    candidates = list(folder.glob(pattern))
    if not candidates:
        raise ValueError('Missing component backup')
    selected = max(candidates, key=lambda p: p.lstat().st_mtime)
    if selected.is_symlink() or time.time() - selected.stat().st_mtime > 30 * 3600:
        raise ValueError('Stale or unsafe component backup')
    return selected


def sqlite_check(path):
    with sqlite3.connect('file:'+str(path)+'?mode=ro', uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('SQLite verification failed')


def sources():
    memory = latest(Path('/var/backups/ops-memory'), 'ops-memory-*')
    vault = latest(Path('/var/backups/openbao'), 'openbao-raft-*')
    broker = latest(Path('/var/backups/ops-broker'), 'ops-broker-*.db')
    native = latest(Path('/var/lib/ops-native-backups'), '[0-9]*Z')
    zulip = latest(Path('/var/lib/ops-zulip-backups'), '*.tar.age')
    selected = {'broker/state.db': broker, 'openbao/snapshot.age': vault/'snapshot.age',
                'openbao/manifest.json': vault/'manifest.json', 'zulip/bundle.tar.age': zulip,
                'pwa/session.key': Path('/var/lib/ops-approvals/session.key'),
                'pwa/webpush.pem': Path('/var/lib/ops-approvals/webpush.pem'),
                'openbao/unseal-shares.age': Path('/usr/local/share/ops-native/recovery-seed/unseal-shares.age')}
    for name in ('memory.sqlite3', 'qdrant.snapshot', 'manifest.json'):
        selected['memory/'+name] = memory/name
    for name in ('pilot.sqlite3', 'approvals.sqlite3', 'manifest.json'):
        selected['native/'+name] = native/name
    # Component backup services validate their formats; check SQLite again before packaging.
    for name, path in selected.items():
        regular(path)
        if name.endswith(('.db', '.sqlite3')):
            sqlite_check(path)
    return selected


def add_client_databases(work, selected):
    home = Path('/home/ops-user/.local/state')
    for name, relative in {'cli-pilot':'ops-cli-pilot/attempts.sqlite3',
                           'workflows':'ops-workflows/state.sqlite3',
                           'memory-sync':'ops-memory-sync/state.sqlite3'}.items():
        source = home/relative
        if not source.exists():
            raise ValueError('Missing client state')
        regular(source)
        target = work/(name+'.sqlite3')
        with sqlite3.connect('file:'+str(source)+'?mode=ro', uri=True) as original, sqlite3.connect(target) as backup:
            original.backup(backup)
        target.chmod(0o600)
        sqlite_check(target)
        selected['clients/'+target.name] = target
    source=Path('/var/lib/ops-approvals/sessions.db')
    regular(source)
    target=work/'pwa-sessions.sqlite3'
    with sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True) as original,sqlite3.connect(target) as backup:
        original.backup(backup)
    target.chmod(0o600);sqlite_check(target)
    selected['pwa/sessions.sqlite3']=target


def package(selected, work, output, recipient, identity):
    if any(path.resolve() == identity.resolve() for path in selected.values()):
        raise ValueError('Private recovery identity must never be bundled')
    if sum(regular(path) for path in selected.values()) > MAX_TOTAL:
        raise ValueError('Recovery bundle exceeds bound')
    plain = work/'components.tar'
    with tarfile.open(plain, 'w') as archive:
        for name, path in sorted(selected.items()):
            if name.startswith('/') or '..' in Path(name).parts:
                raise ValueError('Invalid archive member')
            info = archive.gettarinfo(str(path), arcname=name)
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            info.mode = 0o600
            with path.open('rb') as stream:
                archive.addfile(info, stream)
    plain.chmod(0o600)
    with output.open('xb') as encrypted:
        result = subprocess.run(['/usr/bin/age', '-R', str(recipient), str(plain)],
                                stdout=encrypted, stderr=subprocess.DEVNULL, timeout=180)
    output.chmod(0o600)
    if result.returncode:
        raise ValueError('Encryption failed')
    # Verify decryption by streaming its hash; never write decrypted content to logs.
    with subprocess.Popen(['/usr/bin/age', '-d', '-i', str(identity), str(output)],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        restored_hash = hashlib.file_digest(process.stdout, 'sha256').hexdigest()
        code = process.wait(timeout=180)
    if code or restored_hash != digest(plain):
        raise ValueError('Recovery decryption verification failed')
    return {'sha256': digest(output), 'bytes': output.stat().st_size,
            'members': sorted(selected), 'decryption_verified': True,
            'private_identity_included': False, 'full_stack_restore_verified': False}


def main():
    if os.geteuid() != 0:
        raise ValueError('Administrator service required')
    os.umask(0o077)
    ROOT.mkdir(mode=0o700, exist_ok=True)
    metadata = ROOT.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o077:
        raise ValueError('Private recovery directory required')
    with (ROOT/'bundle.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if shutil.disk_usage(ROOT).free < MAX_TOTAL * 2:
            raise ValueError('Insufficient recovery workspace')
        stamp = datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%S.%fZ')
        with tempfile.TemporaryDirectory(prefix='.work-', dir=ROOT) as raw:
            work = Path(raw)
            selected = sources()
            add_client_databases(work, selected)
            report = package(selected, work, work/'bundle.tar.age', RECIPIENT, IDENTITY)
            report.update(created_at=time.time(), bundle=stamp+'.tar.age')
            (work/'bundle.tar.age').replace(ROOT/report['bundle'])
            (ROOT/(stamp+'.json')).write_text(json.dumps(report, indent=2)+'\n')
            next_path = ROOT/'latest.next'
            next_path.write_text(json.dumps(report, indent=2)+'\n')
            next_path.replace(ROOT/'latest.json')
        print(json.dumps(report))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Recovery packaging failed; existing bundles preserved') from None
