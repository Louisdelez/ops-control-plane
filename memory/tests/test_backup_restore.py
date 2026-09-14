from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
from typing import Iterator

import pytest


ROOT = Path(__file__).parents[2]
MODULE_PATH = ROOT / "memory" / "backup" / "ops_memory_backup.py"
SPEC = importlib.util.spec_from_file_location("ops_memory_backup", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
backup_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backup_module
SPEC.loader.exec_module(backup_module)


def _database(path: Path, memories: int = 2) -> None:
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO schema_meta VALUES('schema_version', '1')")
        db.execute("CREATE TABLE memories(id TEXT PRIMARY KEY, content TEXT NOT NULL)")
        db.executemany(
            "INSERT INTO memories VALUES(?, ?)",
            [(str(index), f"memory-{index}") for index in range(memories)],
        )
        db.commit()
    path.chmod(0o600)


class _SnapshotHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    key = "k" * 48
    snapshot = b"qdrant-snapshot-test-data\x00" * 32
    snapshot_name = "ops_memory_v1-2026-09-04.snapshot"
    deleted = 0

    def log_message(self, format: str, *args: object) -> None:
        return

    def _authorized(self) -> bool:
        if self.headers.get("api-key") != self.key:
            self.send_error(401)
            return False
        return True

    def _json(self, document: object) -> None:
        encoded = json.dumps(document).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if not self._authorized():
            return
        if self.path == "/collections/ops_memory_v1":
            self._json(
                {
                    "status": "ok",
                    "result": {
                        "status": "green",
                        "points_count": 2,
                        "indexed_vectors_count": 2,
                    },
                }
            )
            return
        if self.path == f"/collections/ops_memory_v1/snapshots/{self.snapshot_name}":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(self.snapshot)))
            self.end_headers()
            self.wfile.write(self.snapshot)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if not self._authorized():
            return
        if self.path == "/collections/ops_memory_v1/snapshots?wait=true":
            self._json(
                {
                    "status": "ok",
                    "result": {
                        "name": self.snapshot_name,
                        "size": len(self.snapshot),
                        "creation_time": "2026-09-04T03:35:00Z",
                        "checksum": hashlib.sha256(self.snapshot).hexdigest(),
                    },
                }
            )
            return
        self.send_error(404)

    def do_DELETE(self) -> None:
        if not self._authorized():
            return
        expected = (
            f"/collections/ops_memory_v1/snapshots/{self.snapshot_name}?wait=true"
        )
        if self.path == expected:
            type(self).deleted += 1
            self._json({"status": "ok", "result": True})
            return
        self.send_error(404)


class _Server:
    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir(mode=0o700)
    (credentials / "qdrant-api-key").write_text(_SnapshotHandler.key, encoding="ascii")
    (credentials / "qdrant-api-key").chmod(0o600)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))


def _config(tmp_path: Path, url: str, retain: int = 14):
    database = tmp_path / "state" / "memory.sqlite3"
    database.parent.mkdir(mode=0o700)
    _database(database)
    destination = tmp_path / "backups"
    destination.mkdir(mode=0o700)
    image_env = tmp_path / "qdrant.env"
    image_env.write_text("QDRANT_IMAGE=sha256:" + "a" * 64 + "\n", encoding="ascii")
    image_env.chmod(0o600)
    return backup_module.BackupConfig(
        database_path=database,
        destination=destination,
        retention_count=retain,
        max_snapshot_bytes=1_048_576,
        qdrant_url=url,
        collection="ops_memory_v1",
        credential_name="qdrant-api-key",
        timeout_seconds=2.0,
        image_env=image_env,
        restore_timeout_seconds=10.0,
        restore_minimum_free_bytes=268_435_456,
    )


def test_backup_is_atomic_private_verified_and_retained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _SnapshotHandler.deleted = 0
    _credential(tmp_path, monkeypatch)
    with _Server(_SnapshotHandler) as server:
        config = _config(tmp_path, server.url, retain=1)
        first_time = datetime(2026, 9, 4, 3, 35, tzinfo=UTC)
        first = backup_module.create_backup(config, clock=lambda: first_time)
        first_path = config.destination / first["backup_id"]
        assert first["status"] == "ok"
        assert first["sqlite_memories"] == 2
        assert first["qdrant_points"] == 2
        assert backup_module.verify_backup(first_path)["status"] == "ok"
        assert {item.name for item in first_path.iterdir()} == {
            "manifest.json",
            "memory.sqlite3",
            "qdrant.snapshot",
        }
        assert all(item.stat().st_mode & 0o007 == 0 for item in first_path.iterdir())

        second = backup_module.create_backup(
            config, clock=lambda: first_time + timedelta(seconds=1)
        )
        second_path = config.destination / second["backup_id"]
        assert second["retention_removed"] == 1
        assert not first_path.exists()
        assert second_path.is_dir()
        assert _SnapshotHandler.deleted == 2
        assert not list(config.destination.glob(".pending-*"))


def test_verify_detects_snapshot_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _credential(tmp_path, monkeypatch)
    with _Server(_SnapshotHandler) as server:
        config = _config(tmp_path, server.url)
        result = backup_module.create_backup(
            config, clock=lambda: datetime(2026, 9, 4, 3, 35, tzinfo=UTC)
        )
    selected = config.destination / result["backup_id"]
    snapshot = selected / "qdrant.snapshot"
    snapshot.write_bytes(snapshot.read_bytes() + b"tampered")
    snapshot.chmod(0o640)
    with pytest.raises(backup_module.BackupError, match="checksum or size"):
        backup_module.verify_backup(selected)


def test_failed_download_is_not_published_and_server_snapshot_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class OversizedHandler(_SnapshotHandler):
        deleted = 0
        snapshot = b"x" * (1_048_576 + 1)

    _credential(tmp_path, monkeypatch)
    with _Server(OversizedHandler) as server:
        config = _config(tmp_path, server.url)
        with pytest.raises(backup_module.BackupError, match="allowed size"):
            backup_module.create_backup(
                config, clock=lambda: datetime(2026, 9, 4, 3, 35, tzinfo=UTC)
            )
    assert OversizedHandler.deleted == 1
    assert not list(config.destination.glob("ops-memory-*"))
    assert not list(config.destination.glob(".pending-*"))


def test_config_rejects_non_loopback_qdrant(tmp_path: Path) -> None:
    config = tmp_path / "backup.toml"
    config.write_text(
        """
[backup]
database_path = "/var/lib/ops-memory/memory.sqlite3"
destination = "/var/backups/ops-memory"
retention_count = 14
max_snapshot_bytes = 8589934592
[qdrant]
url = "https://example.invalid:6333"
collection = "ops_memory_v1"
credential_name = "qdrant-api-key"
timeout_seconds = 60.0
[restore_test]
image_env = "/etc/ops-memory/qdrant.env"
timeout_seconds = 300.0
minimum_free_bytes = 2147483648
""",
        encoding="utf-8",
    )
    config.chmod(0o600)
    with pytest.raises(backup_module.BackupError, match="loopback"):
        backup_module.BackupConfig.load(config)


def test_image_must_be_immutable_and_environment_exact(tmp_path: Path) -> None:
    image = tmp_path / "qdrant.env"
    image.write_text("QDRANT_IMAGE=qdrant:latest\n", encoding="ascii")
    image.chmod(0o600)
    with pytest.raises(backup_module.BackupError, match="not immutable"):
        backup_module._read_image_id(image)
    image.write_text("QDRANT_IMAGE=sha256:" + "b" * 64 + "\n", encoding="ascii")
    assert backup_module._read_image_id(image) == "sha256:" + "b" * 64


def test_backup_directory_symlink_is_rejected(tmp_path: Path) -> None:
    actual = tmp_path / "ops-memory-20260904T033500.000000Z"
    actual.mkdir()
    alias = tmp_path / "elsewhere" / actual.name
    alias.parent.mkdir()
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(backup_module.BackupError, match="permissions are unsafe"):
        backup_module.verify_backup(alias)


def test_multipart_restore_streams_snapshot_with_checksum(tmp_path: Path) -> None:
    snapshot = tmp_path / "qdrant.snapshot"
    snapshot.write_bytes(b"snapshot-body" * 100)
    snapshot.chmod(0o600)
    expected_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()

    class UploadHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"
        received = b""
        request_path = ""

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            assert self.headers.get("api-key") == "r" * 48
            length = int(self.headers["Content-Length"])
            type(self).received = self.rfile.read(length)
            type(self).request_path = self.path
            encoded = b'{"status":"ok","result":true}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    with _Server(UploadHandler) as server:
        result = backup_module._multipart_upload(
            server.url,
            "restore_test_1234",
            "r" * 48,
            snapshot,
            expected_hash,
            2.0,
        )
    assert result["result"] is True
    assert snapshot.read_bytes() in UploadHandler.received
    assert f"checksum={expected_hash}" in UploadHandler.request_path
    assert "priority=snapshot" in UploadHandler.request_path


def test_restore_test_requires_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.geteuid() == 0:
        pytest.skip("test is intended for the unprivileged developer test run")
    config = _config(tmp_path, "http://127.0.0.1:6333")
    monkeypatch.setattr(backup_module, "verify_backup", lambda path: {"status": "ok"})
    with pytest.raises(backup_module.BackupError, match="must run as root"):
        backup_module.restore_test(config, tmp_path)
