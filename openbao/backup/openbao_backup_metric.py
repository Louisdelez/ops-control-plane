#!/usr/bin/python3
"""Atomically publish non-sensitive OpenBao backup health metrics."""

from __future__ import annotations

import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import sys
import tempfile
import time


BACKUPS = Path("/var/backups/openbao")
TEXTFILES = Path("/var/lib/node-exporter/textfile")
DESTINATION = TEXTFILES / "openbao-raft-backup.prom"
BACKUP_ID = re.compile(r"\Aopenbao-raft-[0-9]{8}T[0-9]{6}\.[0-9]{6}Z\Z")
SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
PREVIOUS_TIMESTAMP = re.compile(
    r"^ops_openbao_backup_last_success_timestamp_seconds ([0-9]+(?:\.[0-9]+)?)$",
    re.MULTILINE,
)
PREVIOUS_SIZE = re.compile(
    r"^ops_openbao_backup_last_size_bytes ([0-9]+)$", re.MULTILINE
)


def _safe_directory(path: Path, uid: int, gid: int | None = None) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
        raise OSError("unsafe directory")
    if gid is not None and info.st_gid != gid:
        raise OSError("unsafe directory group")
    if stat.S_IMODE(info.st_mode) & 0o022:
        raise OSError("writable directory")


def _regular(path: Path, uid: int, maximum: int) -> bytes:
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) & 0o077
        or not 1 <= info.st_size <= maximum
    ):
        raise OSError("unsafe backup file")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        raw = os.read(descriptor, maximum + 1)
        if len(raw) > maximum or os.read(descriptor, 1):
            raise OSError("oversized backup file")
        return raw
    finally:
        os.close(descriptor)


def _sha256(path: Path, uid: int, expected_size: int) -> str:
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) & 0o077
        or info.st_size != expected_size
    ):
        raise OSError("unsafe snapshot artifact")
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _latest_verified() -> int:
    backup_uid = pwd.getpwnam("openbao-backup").pw_uid
    _safe_directory(BACKUPS, backup_uid)
    candidates = sorted(
        (entry for entry in BACKUPS.iterdir() if BACKUP_ID.fullmatch(entry.name)),
        key=lambda entry: entry.name,
        reverse=True,
    )
    if not candidates:
        raise OSError("no backup")
    latest = candidates[0]
    _safe_directory(latest, backup_uid)
    manifest_raw = _regular(latest / "manifest.json", backup_uid, 64 * 1024)
    try:
        manifest = json.loads(manifest_raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OSError("invalid manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise OSError("invalid manifest")
    artifact = manifest.get("artifact")
    verification = manifest.get("verification")
    if not isinstance(artifact, dict) or not isinstance(verification, dict):
        raise OSError("invalid manifest")
    expected_hash = artifact.get("sha256")
    expected_size = artifact.get("encrypted_bytes")
    if (
        manifest.get("backup_id") != latest.name
        or artifact.get("name") != "snapshot.age"
        or artifact.get("encryption") != "age-x25519"
        or not isinstance(expected_hash, str)
        or not SHA256.fullmatch(expected_hash)
        or isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size < 128
        or verification.get("age_header") != "ok"
        or verification.get("sha256") != "ok"
        or verification.get("token_revoked") is not True
    ):
        raise OSError("invalid manifest")
    checksum_line = _regular(latest / "snapshot.age.sha256", backup_uid, 256)
    if checksum_line != f"{expected_hash}  snapshot.age\n".encode("ascii"):
        raise OSError("checksum file mismatch")
    if _sha256(latest / "snapshot.age", backup_uid, expected_size) != expected_hash:
        raise OSError("snapshot digest mismatch")
    return expected_size


def _previous() -> tuple[str, str]:
    try:
        info = DESTINATION.lstat()
    except FileNotFoundError:
        return "0", "0"
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise OSError("unsafe prior metric")
    raw = DESTINATION.read_text(encoding="ascii")
    timestamp = PREVIOUS_TIMESTAMP.search(raw)
    size = PREVIOUS_SIZE.search(raw)
    return (timestamp.group(1) if timestamp else "0", size.group(1) if size else "0")


def _publish(outcome: str) -> None:
    node_gid = grp.getgrnam("node-exporter").gr_gid
    _safe_directory(TEXTFILES, 0, node_gid)
    if outcome == "success":
        size = str(_latest_verified())
        timestamp = f"{time.time():.6f}"
        verified = "1"
    else:
        timestamp, size = _previous()
        verified = "0"
    content = (
        "# HELP ops_openbao_backup_last_success_timestamp_seconds Unix time of the last verified encrypted Raft backup.\n"
        "# TYPE ops_openbao_backup_last_success_timestamp_seconds gauge\n"
        f"ops_openbao_backup_last_success_timestamp_seconds {timestamp}\n"
        "# HELP ops_openbao_backup_last_verify_ok Whether the latest OpenBao backup attempt verified successfully.\n"
        "# TYPE ops_openbao_backup_last_verify_ok gauge\n"
        f"ops_openbao_backup_last_verify_ok {verified}\n"
        "# HELP ops_openbao_backup_last_size_bytes Size of the latest verified age-encrypted Raft snapshot.\n"
        "# TYPE ops_openbao_backup_last_size_bytes gauge\n"
        f"ops_openbao_backup_last_size_bytes {size}\n"
    )
    descriptor, name = tempfile.mkstemp(prefix=".openbao-backup.", suffix=".tmp", dir=TEXTFILES)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, DESTINATION)
        directory_fd = os.open(TEXTFILES, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def main(arguments: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) != 1 or arguments[0] not in {"success", "failure"}:
        return 64
    try:
        _publish(arguments[0])
    except (OSError, KeyError, ValueError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
