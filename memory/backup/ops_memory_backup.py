#!/usr/bin/python3
"""Deterministic backup and isolated restore test for ops-memory.

SQLite is copied with its online backup API. Qdrant is copied only through its
collection snapshot API. A backup is published atomically after both components
and their checksums have been verified.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from typing import Any, Callable, Iterator
import urllib.error
import urllib.parse
import urllib.request


FORMAT = "ops-memory-backup-v1"
BACKUP_RE = re.compile(r"\Aops-memory-(\d{8}T\d{6}\.\d{6}Z)\Z")
COLLECTION_RE = re.compile(r"\A[A-Za-z0-9_-]{1,128}\Z")
KEY_RE = re.compile(r"\A[A-Za-z0-9_-]{32,512}\Z")
SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
IMAGE_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")
SNAPSHOT_RE = re.compile(r"\A[A-Za-z0-9_.-]{1,255}\.snapshot\Z")
MAX_JSON_BYTES = 8_000_000


class BackupError(RuntimeError):
    """A fail-closed backup or verification failure."""


def _trusted_uids() -> set[int]:
    result = {0, os.geteuid()}
    try:
        result.add(pwd.getpwnam("opsmemory").pw_uid)
    except KeyError:
        pass
    return result


@dataclass(frozen=True)
class BackupConfig:
    database_path: Path
    destination: Path
    retention_count: int
    max_snapshot_bytes: int
    qdrant_url: str
    collection: str
    credential_name: str
    timeout_seconds: float
    image_env: Path
    restore_timeout_seconds: float
    restore_minimum_free_bytes: int

    @classmethod
    def load(cls, path: Path) -> "BackupConfig":
        raw = _read_bounded_regular(path, 262_144, allow_group_read=True)
        try:
            document = tomllib.loads(raw.decode("utf-8", errors="strict"))
        except (UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise BackupError("backup configuration is invalid") from exc
        if set(document) != {"backup", "qdrant", "restore_test"}:
            raise BackupError("backup configuration has unexpected sections")
        backup = _mapping(document.get("backup"), "backup")
        qdrant = _mapping(document.get("qdrant"), "qdrant")
        restore = _mapping(document.get("restore_test"), "restore_test")
        _require_keys(
            backup,
            {"database_path", "destination", "retention_count", "max_snapshot_bytes"},
            "backup",
        )
        _require_keys(
            qdrant,
            {"url", "collection", "credential_name", "timeout_seconds"},
            "qdrant",
        )
        _require_keys(
            restore,
            {"image_env", "timeout_seconds", "minimum_free_bytes"},
            "restore_test",
        )
        config = cls(
            database_path=_absolute_path(backup["database_path"], "backup.database_path"),
            destination=_absolute_path(backup["destination"], "backup.destination"),
            retention_count=_bounded_int(
                backup["retention_count"], 1, 3650, "backup.retention_count"
            ),
            max_snapshot_bytes=_bounded_int(
                backup["max_snapshot_bytes"],
                1_048_576,
                1_099_511_627_776,
                "backup.max_snapshot_bytes",
            ),
            qdrant_url=_validate_loopback_url(qdrant["url"]),
            collection=_collection(qdrant["collection"]),
            credential_name=_credential_name(qdrant["credential_name"]),
            timeout_seconds=_bounded_float(
                qdrant["timeout_seconds"], 1.0, 600.0, "qdrant.timeout_seconds"
            ),
            image_env=_absolute_path(restore["image_env"], "restore_test.image_env"),
            restore_timeout_seconds=_bounded_float(
                restore["timeout_seconds"],
                10.0,
                1800.0,
                "restore_test.timeout_seconds",
            ),
            restore_minimum_free_bytes=_bounded_int(
                restore["minimum_free_bytes"],
                268_435_456,
                68_719_476_736,
                "restore_test.minimum_free_bytes",
            ),
        )
        if config.destination == config.database_path.parent:
            raise BackupError("backup destination must differ from the live database directory")
        return config


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise BackupError(f"{name} must be a table")
    return value


def _require_keys(value: dict[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise BackupError(f"{name} has missing or unexpected fields")


def _absolute_path(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise BackupError(f"{name} must be a path")
    path = Path(value)
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise BackupError(f"{name} must be an absolute normalized path")
    return path


def _bounded_int(value: object, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise BackupError(f"{name} is outside its allowed range")
    return value


def _bounded_float(value: object, low: float, high: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BackupError(f"{name} must be numeric")
    result = float(value)
    if not low <= result <= high:
        raise BackupError(f"{name} is outside its allowed range")
    return result


def _collection(value: object) -> str:
    if not isinstance(value, str) or not COLLECTION_RE.fullmatch(value):
        raise BackupError("qdrant.collection is invalid")
    return value


def _credential_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value)
        or value in {".", ".."}
    ):
        raise BackupError("qdrant.credential_name is invalid")
    return value


def _validate_loopback_url(value: object) -> str:
    if not isinstance(value, str):
        raise BackupError("qdrant.url must be a URL")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.port is None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise BackupError("qdrant.url must be an explicit loopback HTTP endpoint")
    return value.rstrip("/")


def _read_bounded_regular(
    path: Path,
    maximum: int,
    *,
    allow_group_read: bool,
    allow_world_read: bool = False,
) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise BackupError("required file is unavailable") from exc
    unsafe_mask = 0o022 if allow_world_read else (0o027 if allow_group_read else 0o077)
    unsafe = before.st_mode & unsafe_mask
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_uid not in _trusted_uids()
        or unsafe
    ):
        raise BackupError("required file permissions are unsafe")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                raise BackupError("required file changed while opening")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(65_536, maximum + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum:
                    raise BackupError("required file exceeds its size limit")
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise BackupError("required file could not be read safely") from exc


def _load_credential(name: str) -> str:
    directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if not directory or not os.path.isabs(directory):
        raise BackupError("systemd credential directory is unavailable")
    raw = _read_bounded_regular(Path(directory) / name, 513, allow_group_read=True)
    try:
        key = raw.rstrip(b"\r\n").decode("ascii", errors="strict")
    except UnicodeError as exc:
        raise BackupError("Qdrant credential is invalid") from exc
    if not KEY_RE.fullmatch(key):
        raise BackupError("Qdrant credential is invalid")
    return key


def _safe_directory(path: Path, *, writable: bool = False) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupError("required directory is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid not in _trusted_uids()
        or info.st_mode & 0o002
    ):
        raise BackupError("required directory permissions are unsafe")
    if writable and not os.access(path, os.W_OK | os.X_OK):
        raise BackupError("backup destination is not writable")
    return path.resolve(strict=True)


def _safe_regular(path: Path, *, allow_group_read: bool = True) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupError("required backup file is unavailable") from exc
    unsafe = info.st_mode & (0o027 if allow_group_read else 0o077)
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid not in _trusted_uids()
        or unsafe
    ):
        raise BackupError("backup file permissions are unsafe")
    return info


def _sha256(path: Path) -> str:
    _safe_regular(path)
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        while True:
            block = os.read(descriptor, 1_048_576)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _fsync_path(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sqlite_integrity(path: Path) -> dict[str, int | str]:
    _safe_regular(path)
    quoted = urllib.parse.quote(str(path.resolve(strict=True)), safe="/")
    try:
        with sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=30.0) as db:
            db.execute("PRAGMA query_only=ON")
            integrity = db.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise BackupError("SQLite integrity check failed")
            schema = db.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if schema is None or not str(schema[0]).isdigit():
                raise BackupError("SQLite memory schema metadata is missing")
            memories = int(db.execute("SELECT COUNT(*) FROM memories").fetchone()[0])
            pages = int(db.execute("PRAGMA page_count").fetchone()[0])
    except sqlite3.Error as exc:
        raise BackupError("SQLite backup verification failed") from exc
    return {
        "integrity": "ok",
        "schema_version": str(schema[0]),
        "memories": memories,
        "pages": pages,
    }


def _backup_sqlite(source: Path, destination: Path) -> dict[str, int | str]:
    source_info = _safe_regular(source)
    if source_info.st_size < 1:
        raise BackupError("live SQLite catalogue is empty")
    descriptor = os.open(
        destination,
        os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o640,
    )
    os.close(descriptor)
    quoted = urllib.parse.quote(str(source.resolve(strict=True)), safe="/")
    try:
        with sqlite3.connect(f"file:{quoted}?mode=ro", uri=True, timeout=30.0) as source_db:
            source_db.execute("PRAGMA query_only=ON")
            source_db.execute("PRAGMA busy_timeout=30000")
            with sqlite3.connect(destination, timeout=30.0) as backup_db:
                source_db.backup(backup_db, pages=256, sleep=0.050)
                journal = backup_db.execute("PRAGMA journal_mode=DELETE").fetchone()
                if journal is None or str(journal[0]).lower() != "delete":
                    raise BackupError("SQLite backup is not standalone")
                backup_db.execute("PRAGMA synchronous=FULL")
    except sqlite3.Error as exc:
        raise BackupError("SQLite online backup failed") from exc
    os.chmod(destination, 0o640)
    details = _sqlite_integrity(destination)
    _fsync_path(destination)
    return details


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


class QdrantSnapshotClient:
    def __init__(self, url: str, collection: str, key: str, timeout: float) -> None:
        self.url = _validate_loopback_url(url)
        self.collection = _collection(collection)
        if not KEY_RE.fullmatch(key):
            raise BackupError("Qdrant credential is invalid")
        self._key = key
        self.timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _RejectRedirects()
        )

    @property
    def collection_path(self) -> str:
        return "/collections/" + urllib.parse.quote(self.collection, safe="")

    def _open(self, request: urllib.request.Request):
        try:
            return self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise BackupError(f"Qdrant request failed with HTTP {exc.code}") from exc
        except OSError as exc:
            raise BackupError("Qdrant request failed") from exc

    def json_request(
        self, method: str, path: str, payload: object | None = None
    ) -> dict[str, Any]:
        encoded = None
        if payload is not None:
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        request = urllib.request.Request(
            self.url + path,
            data=encoded,
            method=method,
            headers={"api-key": self._key, "Content-Type": "application/json"},
        )
        with self._open(request) as response:
            raw = response.read(MAX_JSON_BYTES + 1)
            if len(raw) > MAX_JSON_BYTES:
                raise BackupError("Qdrant JSON response is oversized")
        try:
            document = json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise BackupError("Qdrant returned invalid JSON") from exc
        if (
            not isinstance(document, dict)
            or document.get("status") != "ok"
            or "result" not in document
        ):
            raise BackupError("Qdrant returned an unsuccessful response")
        return document

    def collection_info(self) -> dict[str, int | str]:
        document = self.json_request("GET", self.collection_path)
        result = document.get("result")
        if not isinstance(result, dict):
            raise BackupError("Qdrant collection response is invalid")
        details: dict[str, int | str] = {}
        for name in ("points_count", "indexed_vectors_count"):
            value = result.get(name)
            if isinstance(value, int) and value >= 0:
                details[name] = value
        status_value = result.get("status")
        if isinstance(status_value, str) and len(status_value) <= 64:
            details["status"] = status_value
        if "points_count" not in details:
            raise BackupError("Qdrant collection point count is missing")
        return details

    def create_snapshot(self) -> dict[str, int | str]:
        document = self.json_request(
            "POST", f"{self.collection_path}/snapshots?wait=true"
        )
        result = document.get("result")
        if not isinstance(result, dict):
            raise BackupError("Qdrant snapshot response is invalid")
        name = result.get("name")
        size = result.get("size")
        checksum = result.get("checksum")
        creation_time = result.get("creation_time")
        if not isinstance(name, str) or not SNAPSHOT_RE.fullmatch(name):
            raise BackupError("Qdrant returned an unsafe snapshot name")
        if not isinstance(size, int) or size < 1:
            raise BackupError("Qdrant returned an invalid snapshot size")
        if not isinstance(checksum, str) or not SHA256_RE.fullmatch(checksum.lower()):
            raise BackupError("Qdrant returned an invalid snapshot checksum")
        if not isinstance(creation_time, str) or not 1 <= len(creation_time) <= 128:
            raise BackupError("Qdrant returned an invalid snapshot timestamp")
        return {
            "name": name,
            "size": size,
            "checksum": checksum.lower(),
            "creation_time": creation_time,
        }

    def download_snapshot(self, name: str, destination: Path, maximum: int) -> int:
        if not SNAPSHOT_RE.fullmatch(name):
            raise BackupError("refusing unsafe Qdrant snapshot name")
        request = urllib.request.Request(
            self.url
            + f"{self.collection_path}/snapshots/"
            + urllib.parse.quote(name, safe=""),
            method="GET",
            headers={"api-key": self._key},
        )
        descriptor = os.open(
            destination,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o640,
        )
        total = 0
        try:
            with self._open(request) as response:
                length = response.headers.get("Content-Length")
                if length is not None:
                    try:
                        announced = int(length)
                    except ValueError as exc:
                        raise BackupError("Qdrant snapshot has an invalid length") from exc
                    if announced < 1 or announced > maximum:
                        raise BackupError("Qdrant snapshot exceeds its allowed size")
                while True:
                    block = response.read(1_048_576)
                    if not block:
                        break
                    total += len(block)
                    if total > maximum:
                        raise BackupError("Qdrant snapshot exceeds its allowed size")
                    view = memoryview(block)
                    while view:
                        written = os.write(descriptor, view)
                        view = view[written:]
            if total < 1:
                raise BackupError("Qdrant snapshot download was empty")
            os.fsync(descriptor)
        except Exception:
            os.close(descriptor)
            destination.unlink(missing_ok=True)
            raise
        os.close(descriptor)
        os.chmod(destination, 0o640)
        return total

    def delete_snapshot(self, name: str) -> None:
        if not SNAPSHOT_RE.fullmatch(name):
            raise BackupError("refusing unsafe Qdrant snapshot name")
        document = self.json_request(
            "DELETE",
            f"{self.collection_path}/snapshots/{urllib.parse.quote(name, safe='')}?wait=true",
        )
        if document.get("result") is not True:
            raise BackupError("Qdrant did not confirm snapshot cleanup")


def _write_manifest(path: Path, document: dict[str, Any]) -> None:
    encoded = (
        json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o640,
    )
    try:
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _load_manifest(path: Path) -> dict[str, Any]:
    raw = _read_bounded_regular(path, 1_048_576, allow_group_read=True)
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError("backup manifest is invalid") from exc
    if not isinstance(document, dict):
        raise BackupError("backup manifest is invalid")
    return document


@contextmanager
def _exclusive_lock(destination: Path) -> Iterator[None]:
    lock = destination / ".backup.lock"
    descriptor = os.open(
        lock,
        os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o640,
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("another ops-memory backup operation is active") from exc
        yield
    finally:
        os.close(descriptor)


def _prune(destination: Path, retain: int, current: Path) -> list[str]:
    candidates: list[Path] = []
    for entry in destination.iterdir():
        if entry == current or entry.name.startswith(".pending-"):
            continue
        if BACKUP_RE.fullmatch(entry.name):
            info = entry.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise BackupError("refusing unsafe retention target")
            candidates.append(entry)
    candidates.sort(key=lambda item: item.name)
    expired = candidates[: max(0, len(candidates) + 1 - retain)]
    removed: list[str] = []
    for entry in expired:
        resolved = entry.resolve(strict=True)
        if resolved.parent != destination or not BACKUP_RE.fullmatch(resolved.name):
            raise BackupError("refusing retention outside the backup directory")
        tombstone = destination / f".expired-{resolved.name}-{secrets.token_hex(4)}"
        os.replace(resolved, tombstone)
        shutil.rmtree(tombstone)
        removed.append(entry.name)
    if removed:
        _fsync_directory(destination)
    return removed


def create_backup(
    config: BackupConfig,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    destination = _safe_directory(config.destination, writable=True)
    source_parent = _safe_directory(config.database_path.parent)
    source = config.database_path.resolve(strict=True)
    if source.parent != source_parent:
        raise BackupError("live database resolves outside its configured directory")
    key = _load_credential(config.credential_name)
    qdrant = QdrantSnapshotClient(
        config.qdrant_url, config.collection, key, config.timeout_seconds
    )
    key = ""
    instant = clock().astimezone(UTC)
    stamp = instant.strftime("%Y%m%dT%H%M%S.%fZ")
    backup_id = f"ops-memory-{stamp}"
    if not BACKUP_RE.fullmatch(backup_id):
        raise BackupError("generated backup identifier is invalid")
    final = destination / backup_id
    pending = destination / f".pending-{backup_id}-{secrets.token_hex(4)}"
    published = False
    created_snapshot: str | None = None
    try:
        with _exclusive_lock(destination):
            if final.exists() or final.is_symlink():
                raise BackupError("backup identifier already exists")
            pending.mkdir(mode=0o750)
            sqlite_path = pending / "memory.sqlite3"
            snapshot_path = pending / "qdrant.snapshot"
            sqlite_details = _backup_sqlite(source, sqlite_path)
            collection_details = qdrant.collection_info()
            snapshot_details = qdrant.create_snapshot()
            created_snapshot = str(snapshot_details["name"])
            try:
                downloaded = qdrant.download_snapshot(
                    created_snapshot, snapshot_path, config.max_snapshot_bytes
                )
                snapshot_sha256 = _sha256(snapshot_path)
                if snapshot_sha256 != snapshot_details["checksum"]:
                    raise BackupError("downloaded Qdrant snapshot checksum does not match")
            finally:
                if created_snapshot is not None:
                    qdrant.delete_snapshot(created_snapshot)
                    created_snapshot = None
            sqlite_info = sqlite_path.stat()
            snapshot_info = snapshot_path.stat()
            if downloaded != snapshot_info.st_size or downloaded != snapshot_details["size"]:
                raise BackupError("downloaded Qdrant snapshot size does not match")
            manifest: dict[str, Any] = {
                "format": FORMAT,
                "backup_id": backup_id,
                "created_at": instant.isoformat().replace("+00:00", "Z"),
                "consistency": {
                    "sqlite": "online-backup-api",
                    "qdrant": "collection-snapshot-api",
                    "cross_store": "sqlite-catalogue-authoritative-qdrant-index-rebuildable",
                },
                "sqlite": {
                    "file": "memory.sqlite3",
                    "bytes": sqlite_info.st_size,
                    "sha256": _sha256(sqlite_path),
                    **sqlite_details,
                },
                "qdrant": {
                    "file": "qdrant.snapshot",
                    "bytes": snapshot_info.st_size,
                    "sha256": snapshot_sha256,
                    "server_checksum": snapshot_details["checksum"],
                    "snapshot_creation_time": snapshot_details["creation_time"],
                    "collection": config.collection,
                    **collection_details,
                },
            }
            _write_manifest(pending / "manifest.json", manifest)
            _fsync_directory(pending)
            os.replace(pending, final)
            _fsync_directory(destination)
            try:
                verified = verify_backup(final)
            except (BackupError, OSError):
                quarantine = destination / f".invalid-{backup_id}-{secrets.token_hex(4)}"
                os.replace(final, quarantine)
                _fsync_directory(destination)
                raise
            published = True
            removed = _prune(destination, config.retention_count, final)
            return {
                "status": "ok",
                "backup_id": backup_id,
                "sqlite_memories": verified["sqlite_memories"],
                "qdrant_points": verified["qdrant_points"],
                "retention_removed": len(removed),
            }
    finally:
        if created_snapshot is not None:
            try:
                qdrant.delete_snapshot(created_snapshot)
            except BackupError:
                pass
        if not published and pending.exists() and not pending.is_symlink():
            try:
                if pending.resolve(strict=True).parent == destination:
                    shutil.rmtree(pending)
            except OSError:
                pass


def _backup_directory(path: Path) -> Path:
    if not path.is_absolute() or not BACKUP_RE.fullmatch(path.name):
        raise BackupError("backup path is invalid")
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupError("backup does not exist") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or info.st_uid not in _trusted_uids()
        or info.st_mode & 0o002
    ):
        raise BackupError("backup directory permissions are unsafe")
    return path.resolve(strict=True)


def latest_backup(destination: Path) -> Path:
    root = _safe_directory(destination)
    candidates = [
        item
        for item in root.iterdir()
        if BACKUP_RE.fullmatch(item.name) and item.is_dir() and not item.is_symlink()
    ]
    if not candidates:
        raise BackupError("no ops-memory backup is available")
    return sorted(candidates, key=lambda item: item.name)[-1]


def resolve_backup(config: BackupConfig, backup_id: str) -> Path:
    if backup_id == "latest":
        return latest_backup(config.destination)
    if not BACKUP_RE.fullmatch(backup_id):
        raise BackupError("backup identifier is invalid")
    root = _safe_directory(config.destination)
    candidate = root / backup_id
    resolved = _backup_directory(candidate)
    if resolved.parent != root:
        raise BackupError("backup resolves outside its configured destination")
    return resolved


def verify_backup(path: Path) -> dict[str, Any]:
    directory = _backup_directory(path)
    entries = {entry.name for entry in directory.iterdir()}
    if entries != {"manifest.json", "memory.sqlite3", "qdrant.snapshot"}:
        raise BackupError("backup contains missing or unexpected files")
    manifest = _load_manifest(directory / "manifest.json")
    if set(manifest) != {
        "format",
        "backup_id",
        "created_at",
        "consistency",
        "sqlite",
        "qdrant",
    }:
        raise BackupError("backup manifest has unexpected fields")
    if manifest.get("format") != FORMAT or manifest.get("backup_id") != directory.name:
        raise BackupError("backup manifest identity does not match")
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str) or not created_at.endswith("Z"):
        raise BackupError("backup timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(created_at.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise BackupError("backup timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise BackupError("backup timestamp has no timezone")
    consistency = manifest.get("consistency")
    if consistency != {
        "sqlite": "online-backup-api",
        "qdrant": "collection-snapshot-api",
        "cross_store": "sqlite-catalogue-authoritative-qdrant-index-rebuildable",
    }:
        raise BackupError("backup consistency declaration is invalid")
    sqlite_manifest = _mapping(manifest.get("sqlite"), "sqlite manifest")
    qdrant_manifest = _mapping(manifest.get("qdrant"), "qdrant manifest")
    expected_sqlite = {
        "file",
        "bytes",
        "sha256",
        "integrity",
        "schema_version",
        "memories",
        "pages",
    }
    expected_qdrant = {
        "file",
        "bytes",
        "sha256",
        "server_checksum",
        "snapshot_creation_time",
        "collection",
        "points_count",
    }
    if "indexed_vectors_count" in qdrant_manifest:
        expected_qdrant.add("indexed_vectors_count")
    if "status" in qdrant_manifest:
        expected_qdrant.add("status")
    if set(sqlite_manifest) != expected_sqlite or set(qdrant_manifest) != expected_qdrant:
        raise BackupError("backup component manifest has unexpected fields")
    if sqlite_manifest.get("file") != "memory.sqlite3":
        raise BackupError("SQLite backup filename is invalid")
    if qdrant_manifest.get("file") != "qdrant.snapshot":
        raise BackupError("Qdrant backup filename is invalid")
    sqlite_path = directory / "memory.sqlite3"
    qdrant_path = directory / "qdrant.snapshot"
    sqlite_info = _safe_regular(sqlite_path)
    qdrant_info = _safe_regular(qdrant_path)
    for component, info, file_path in (
        (sqlite_manifest, sqlite_info, sqlite_path),
        (qdrant_manifest, qdrant_info, qdrant_path),
    ):
        expected_bytes = component.get("bytes")
        expected_hash = component.get("sha256")
        if (
            not isinstance(expected_bytes, int)
            or expected_bytes < 1
            or expected_bytes != info.st_size
            or not isinstance(expected_hash, str)
            or not SHA256_RE.fullmatch(expected_hash)
            or _sha256(file_path) != expected_hash
        ):
            raise BackupError("backup component checksum or size does not match")
    sqlite_details = _sqlite_integrity(sqlite_path)
    for field in ("integrity", "schema_version", "memories", "pages"):
        if sqlite_manifest.get(field) != sqlite_details[field]:
            raise BackupError("SQLite backup metadata does not match")
    if (
        not isinstance(qdrant_manifest.get("server_checksum"), str)
        or not SHA256_RE.fullmatch(qdrant_manifest["server_checksum"])
        or qdrant_manifest["server_checksum"] != qdrant_manifest["sha256"]
        or not isinstance(qdrant_manifest.get("snapshot_creation_time"), str)
        or not COLLECTION_RE.fullmatch(str(qdrant_manifest.get("collection", "")))
        or not isinstance(qdrant_manifest.get("points_count"), int)
        or qdrant_manifest["points_count"] < 0
    ):
        raise BackupError("Qdrant backup metadata is invalid")
    return {
        "status": "ok",
        "backup_id": directory.name,
        "sqlite_memories": sqlite_details["memories"],
        "qdrant_points": qdrant_manifest["points_count"],
    }


def _read_image_id(path: Path) -> str:
    raw = _read_bounded_regular(
        path, 4096, allow_group_read=True, allow_world_read=True
    )
    try:
        lines = raw.decode("ascii", errors="strict").splitlines()
    except UnicodeError as exc:
        raise BackupError("Qdrant image environment is invalid") from exc
    if len(lines) != 1 or not lines[0].startswith("QDRANT_IMAGE="):
        raise BackupError("Qdrant image environment is invalid")
    image = lines[0].removeprefix("QDRANT_IMAGE=")
    if not IMAGE_RE.fullmatch(image):
        raise BackupError("Qdrant restore image is not immutable")
    return image


def _run_podman(arguments: list[str], *, timeout: float = 60.0) -> str:
    operation = arguments[0] if arguments else "missing-operation"
    try:
        completed = subprocess.run(
            ["/usr/bin/podman", *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=timeout,
            env={"PATH": "/usr/sbin:/usr/bin"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError("isolated Qdrant container command failed") from exc
    if completed.returncode != 0:
        # Report only the bounded verb and exit status. Podman stderr may
        # contain host paths or runtime details and deliberately stays hidden.
        raise BackupError(
            f"isolated Qdrant container command failed ({operation}, exit {completed.returncode})"
        )
    return completed.stdout.strip()


def _write_restore_config(path: Path, key: str) -> None:
    if not KEY_RE.fullmatch(key):
        raise BackupError("ephemeral restore credential is invalid")
    document = (
        "log_level: WARN\n"
        "storage:\n"
        "  storage_path: /qdrant/storage\n"
        "  snapshots_path: /qdrant/storage/snapshots\n"
        "service:\n"
        "  host: 0.0.0.0\n"
        "  http_port: 6333\n"
        "  grpc_port: 6334\n"
        "  enable_tls: false\n"
        f"  api_key: {json.dumps(key)}\n"
        "telemetry_disabled: true\n"
    ).encode("utf-8")
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        os.write(descriptor, document)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _container_port(name: str) -> int:
    output = _run_podman(["port", name, "6333/tcp"])
    matches = re.findall(r"^127\.0\.0\.1:(\d+)$", output, flags=re.MULTILINE)
    if len(matches) != 1:
        raise BackupError("isolated Qdrant port binding is unsafe")
    port = int(matches[0])
    if not 1024 <= port <= 65535:
        raise BackupError("isolated Qdrant port binding is invalid")
    return port


def _multipart_upload(
    url: str,
    collection: str,
    key: str,
    snapshot: Path,
    checksum: str,
    timeout: float,
) -> dict[str, Any]:
    base = urllib.parse.urlsplit(_validate_loopback_url(url))
    _safe_regular(snapshot)
    size = snapshot.stat().st_size
    boundary = "ops-memory-" + secrets.token_hex(24)
    prefix = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="snapshot"; filename="qdrant.snapshot"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("ascii")
    suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
    query = urllib.parse.urlencode(
        {"wait": "true", "priority": "snapshot", "checksum": checksum}
    )
    path = (
        "/collections/"
        + urllib.parse.quote(_collection(collection), safe="")
        + "/snapshots/upload?"
        + query
    )
    connection = http.client.HTTPConnection(base.hostname, base.port, timeout=timeout)
    try:
        connection.putrequest("POST", path, skip_accept_encoding=True)
        connection.putheader("api-key", key)
        connection.putheader("Content-Type", f"multipart/form-data; boundary={boundary}")
        connection.putheader("Content-Length", str(len(prefix) + size + len(suffix)))
        connection.endheaders()
        connection.send(prefix)
        descriptor = os.open(snapshot, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            while True:
                block = os.read(descriptor, 1_048_576)
                if not block:
                    break
                connection.send(block)
        finally:
            os.close(descriptor)
        connection.send(suffix)
        response = connection.getresponse()
        raw = response.read(MAX_JSON_BYTES + 1)
        if response.status != 200:
            raise BackupError(f"Qdrant restore upload failed with HTTP {response.status}")
        if len(raw) > MAX_JSON_BYTES:
            raise BackupError("Qdrant restore response is oversized")
    except OSError as exc:
        raise BackupError("Qdrant restore upload failed") from exc
    finally:
        connection.close()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackupError("Qdrant restore response is invalid") from exc
    if (
        not isinstance(document, dict)
        or document.get("status") != "ok"
        or document.get("result") is not True
    ):
        raise BackupError("Qdrant did not confirm snapshot restoration")
    return document


def restore_test(config: BackupConfig, backup: Path) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise BackupError("isolated Qdrant restore test must run as root")
    verification = verify_backup(backup)
    manifest = _load_manifest(backup / "manifest.json")
    qdrant_manifest = _mapping(manifest["qdrant"], "qdrant manifest")
    image = _read_image_id(config.image_env)
    if _run_podman(["image", "exists", image]).strip():
        # `podman image exists` normally has no stdout. Refuse surprising data,
        # which could otherwise end up in logs from a compromised wrapper.
        raise BackupError("Podman image validation returned unexpected output")
    work = Path(tempfile.mkdtemp(prefix="ops-memory-restore-test.", dir="/var/tmp"))
    os.chmod(work, 0o700)
    container = "ops-memory-restore-" + secrets.token_hex(8)
    key = secrets.token_urlsafe(48)
    started = False
    try:
        sqlite_copy = work / "memory.sqlite3"
        shutil.copyfile(backup / "memory.sqlite3", sqlite_copy)
        os.chmod(sqlite_copy, 0o600)
        copied_details = _sqlite_integrity(sqlite_copy)
        if copied_details["memories"] != verification["sqlite_memories"]:
            raise BackupError("restored SQLite catalogue count does not match")
        runtime_config = work / "qdrant.yaml"
        _write_restore_config(runtime_config, key)
        qdrant_storage = work / "qdrant-storage"
        qdrant_storage.mkdir(mode=0o700)
        snapshot_size = (backup / "qdrant.snapshot").stat().st_size
        required_free = config.restore_minimum_free_bytes + (snapshot_size * 3)
        if shutil.disk_usage(work).free < required_free:
            raise BackupError("insufficient free space for isolated Qdrant restore")
        _run_podman(
            [
                "run",
                "--detach",
                "--rm",
                "--name",
                container,
                "--pull=never",
                "--read-only",
                "--cap-drop=all",
                "--security-opt=no-new-privileges",
                "--pids-limit=256",
                "--memory=768m",
                "--cpus=2",
                "--publish=127.0.0.1::6333/tcp",
                f"--volume={qdrant_storage}:/qdrant/storage:Z,rw",
                f"--volume={runtime_config}:/qdrant/config/production.yaml:ro,Z",
                image,
            ],
            timeout=120.0,
        )
        started = True
        port = _container_port(container)
        url = f"http://127.0.0.1:{port}"
        client = QdrantSnapshotClient(
            url, "restore_test_" + secrets.token_hex(8), key, config.timeout_seconds
        )
        deadline = time.monotonic() + config.restore_timeout_seconds
        while True:
            try:
                client.json_request("GET", "/collections")
                break
            except BackupError:
                if time.monotonic() >= deadline:
                    raise BackupError("isolated Qdrant did not become ready")
                time.sleep(0.5)
        _multipart_upload(
            url,
            client.collection,
            key,
            backup / "qdrant.snapshot",
            str(qdrant_manifest["sha256"]),
            config.restore_timeout_seconds,
        )
        count_document = client.json_request(
            "POST", f"{client.collection_path}/points/count", {"exact": True}
        )
        result = count_document.get("result")
        restored_count = result.get("count") if isinstance(result, dict) else None
        if (
            not isinstance(restored_count, int)
            or restored_count != qdrant_manifest["points_count"]
        ):
            raise BackupError("restored Qdrant point count does not match")
        return {
            "status": "ok",
            "backup_id": backup.name,
            "sqlite_memories": copied_details["memories"],
            "qdrant_points": restored_count,
            "environment": "ephemeral",
        }
    finally:
        key = ""
        if started:
            try:
                _run_podman(["rm", "--force", container], timeout=60.0)
            except BackupError:
                pass
        if work.exists() and not work.is_symlink():
            try:
                if work.resolve(strict=True).parent == Path("/var/tmp"):
                    shutil.rmtree(work)
            except OSError:
                pass


def _output(document: dict[str, Any]) -> None:
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Back up and test-restore the local ops-memory stores"
    )
    parser.add_argument("--config", type=Path, default=Path("/etc/ops-memory/backup.toml"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("backup")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--backup-id", default="latest")
    restore_parser = subparsers.add_parser("restore-test")
    restore_parser.add_argument("--backup-id", default="latest")
    arguments = parser.parse_args(argv)
    previous_umask = os.umask(0o027)
    try:
        config = BackupConfig.load(arguments.config)
        if arguments.command == "backup":
            _output(create_backup(config))
        else:
            selected = resolve_backup(config, arguments.backup_id)
            if arguments.command == "verify":
                _output(verify_backup(selected))
            else:
                _output(restore_test(config, selected))
        return 0
    except (BackupError, OSError, ValueError) as exc:
        # Deliberately do not include nested HTTP bodies, subprocess stderr or
        # credential paths in journal output.
        print(f"ops-memory-backup: {exc}", file=sys.stderr)
        return 1
    finally:
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
