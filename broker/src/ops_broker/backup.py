"""Coherent, single-file SQLite backups with audit-chain verification."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import fcntl
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Callable
from urllib.parse import quote

from .database import Database


def backup_sqlite(
    source: Path,
    destination_directory: Path,
    *,
    retain: int = 14,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Path:
    """Use SQLite's online-backup API, verify, fsync, then atomically publish."""

    source = Path(source)
    destination_directory = Path(destination_directory)
    if not source.is_file():
        raise FileNotFoundError(f"source database does not exist: {source}")
    if source.is_symlink():
        raise ValueError("source database must not be a symbolic link")
    if not 1 <= retain <= 3650:
        raise ValueError("retain must be between 1 and 3650")
    if destination_directory.is_symlink():
        raise ValueError("backup directory must not be a symbolic link")
    destination_directory.mkdir(mode=0o700, parents=False, exist_ok=True)

    source_resolved = source.resolve(strict=True)
    destination_resolved = destination_directory.resolve(strict=True)
    if source_resolved.parent == destination_resolved:
        raise ValueError("backup directory must differ from the live database directory")

    previous_umask = os.umask(0o077)
    lock_descriptor = -1
    temporary_path: Path | None = None
    try:
        lock_path = destination_resolved / ".backup.lock"
        lock_descriptor = os.open(
            lock_path,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC,
            0o600,
        )
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)

        timestamp = clock().astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        final_path = destination_resolved / f"ops-broker-{timestamp}.db"
        if final_path.exists():
            raise FileExistsError(f"backup already exists: {final_path.name}")

        temporary_descriptor, temporary_name = tempfile.mkstemp(
            prefix=".ops-broker-",
            suffix=".tmp",
            dir=destination_resolved,
        )
        os.close(temporary_descriptor)
        temporary_path = Path(temporary_name)
        os.chmod(temporary_path, 0o600)

        source_uri = f"file:{quote(str(source_resolved), safe='/')}?mode=ro"
        with sqlite3.connect(source_uri, uri=True, timeout=30.0) as source_connection:
            source_connection.execute("PRAGMA query_only = ON")
            source_connection.execute("PRAGMA busy_timeout = 30000")
            with sqlite3.connect(temporary_path, timeout=30.0) as backup_connection:
                source_connection.backup(backup_connection, pages=256, sleep=0.050)
                journal_mode = backup_connection.execute("PRAGMA journal_mode = DELETE").fetchone()
                if journal_mode is None or str(journal_mode[0]).lower() != "delete":
                    raise RuntimeError("backup could not be converted to a standalone database")
                integrity = backup_connection.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise RuntimeError("SQLite integrity_check failed for the backup")

        audit = Database.verify_file(temporary_path)
        if not audit.valid:
            raise RuntimeError(f"audit-chain verification failed for backup: {audit.reason}")

        with temporary_path.open("rb") as backup_file:
            os.fsync(backup_file.fileno())
        os.replace(temporary_path, final_path)
        temporary_path = None
        os.chmod(final_path, 0o600)
        directory_descriptor = os.open(destination_resolved, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

        backups = sorted(destination_resolved.glob("ops-broker-*.db"))
        for expired in backups[:-retain]:
            if expired.is_file() and not expired.is_symlink():
                expired.unlink()
        return final_path
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if lock_descriptor >= 0:
            os.close(lock_descriptor)
        os.umask(previous_umask)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a coherent ops-broker SQLite backup")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--retain", type=int, default=14)
    args = parser.parse_args()
    backup = backup_sqlite(args.database, args.destination, retain=args.retain)
    print(backup)


if __name__ == "__main__":
    main()
