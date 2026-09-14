from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from ops_broker.database import Database, SCHEMA_VERSION, _SCHEMA

from conftest import Harness, request_id


def test_hash_chain_detects_tampering(harness: Harness) -> None:
    harness.service.create_mission(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="audit chain exercise",
    )
    valid = harness.database.verify_audit_chain()
    assert valid.valid
    assert valid.event_count == 1

    with harness.database.raw_connection() as connection:
        connection.execute("UPDATE events SET payload_json = '{}' WHERE seq = 1")

    invalid = harness.database.verify_audit_chain()
    assert not invalid.valid
    assert invalid.failure_seq == 1
    assert invalid.reason == "event hash mismatch"


def test_offline_verifier_does_not_initialize_missing_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    result = Database.verify_file(missing)
    assert not result.valid
    assert not missing.exists()


def test_offline_verifier_does_not_modify_database_content(harness: Harness) -> None:
    harness.service.create_mission(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="read-only verification",
    )
    database_stat = harness.database.path.stat()
    with harness.database.raw_connection() as connection:
        before = connection.execute(
            "SELECT last_seq, last_hash FROM audit_head WHERE singleton = 1"
        ).fetchone()
    assert Database.verify_file(harness.database.path).valid
    with harness.database.raw_connection() as connection:
        after = connection.execute(
            "SELECT last_seq, last_hash FROM audit_head WHERE singleton = 1"
        ).fetchone()
    assert tuple(after) == tuple(before)
    assert harness.database.path.stat().st_mtime_ns == database_stat.st_mtime_ns


def test_schema_v1_lifecycle_states_migrate_atomically(tmp_path: Path) -> None:
    path = tmp_path / "v1.db"
    with sqlite3.connect(path) as connection:
        legacy_schema = _SCHEMA.replace(
            "status IN ('open', 'paused', 'completed')",
            "status IN ('open', 'closed')",
            1,
        ).replace(
            "status IN ('open', 'monitoring', 'resolved')",
            "status IN ('open', 'resolved')",
            1,
        )
        connection.executescript(legacy_schema)
        connection.executescript(
            """
            INSERT INTO missions VALUES (
                'mission-1', 'request-1', 'infra', 'legacy', 'closed',
                'operator', '2026-09-03T00:00:00Z', '2026-09-03T00:00:00Z'
            );
            INSERT INTO incidents VALUES (
                'incident-1', 'request-2', 'infra', 'legacy', 'warning', 'open',
                'operator', '2026-09-03T00:00:00Z', '2026-09-03T00:00:00Z'
            );
            INSERT INTO actions VALUES (
                'action-1', 'request-3', 'mission-1', 'incident-1', 'infra',
                'test.check.v1', 1, 'A', 'operator', 'legacy link', '{}',
                '["/usr/bin/true"]', NULL, 'succeeded', NULL, '{}',
                '2026-09-03T00:00:00Z', '2026-09-03T00:00:00Z'
            );
            PRAGMA user_version = 1;
            """
        )

    database = Database(path)
    with database.raw_connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("SELECT status FROM missions").fetchone()[0] == "completed"
        connection.execute("UPDATE missions SET status = 'paused'")
        connection.execute("UPDATE incidents SET status = 'monitoring'")
        linked = connection.execute(
            "SELECT mission_id, incident_id FROM actions WHERE id = 'action-1'"
        ).fetchone()
        assert tuple(linked) == ("mission-1", "incident-1")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert database.verify_audit_chain().valid


def test_unversioned_nonempty_database_is_not_silently_marked_current(tmp_path: Path) -> None:
    path = tmp_path / "unversioned.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE legacy_state(value TEXT)")

    with pytest.raises(RuntimeError, match="unversioned non-empty"):
        Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'legacy_state'"
        ).fetchone() is not None


def test_failed_v1_foreign_key_validation_rolls_back_migration(tmp_path: Path) -> None:
    path = tmp_path / "invalid-v1.db"
    with sqlite3.connect(path) as connection:
        legacy_schema = _SCHEMA.replace(
            "status IN ('open', 'paused', 'completed')",
            "status IN ('open', 'closed')",
            1,
        ).replace(
            "status IN ('open', 'monitoring', 'resolved')",
            "status IN ('open', 'resolved')",
            1,
        )
        connection.executescript(legacy_schema)
        connection.executescript(
            """
            INSERT INTO missions VALUES (
                'mission-1', 'request-1', 'infra', 'legacy', 'closed',
                'operator', '2026-09-03T00:00:00Z', '2026-09-03T00:00:00Z'
            );
            INSERT INTO actions VALUES (
                'action-1', 'request-3', 'missing-mission', NULL, 'infra',
                'test.check.v1', 1, 'A', 'operator', 'invalid legacy link', '{}',
                '["/usr/bin/true"]', NULL, 'succeeded', NULL, '{}',
                '2026-09-03T00:00:00Z', '2026-09-03T00:00:00Z'
            );
            PRAGMA user_version = 1;
            """
        )

    with pytest.raises(RuntimeError, match="foreign key validation"):
        Database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM missions WHERE id = 'mission-1'"
        ).fetchone()[0] == "closed"
