"""SQLite state store with an append-only hash-linked audit log."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from .util import GENESIS_HASH, canonical_json, scrub_payload


SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'paused', 'completed')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
    status TEXT NOT NULL CHECK (status IN ('open', 'monitoring', 'resolved')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS missions_status_updated_idx ON missions(status, updated_at DESC);
CREATE INDEX IF NOT EXISTS incidents_status_updated_idx ON incidents(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS actions (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    mission_id TEXT REFERENCES missions(id),
    incident_id TEXT REFERENCES incidents(id),
    project_id TEXT NOT NULL,
    runbook_id TEXT NOT NULL,
    runbook_version INTEGER NOT NULL,
    action_class TEXT NOT NULL CHECK (action_class IN ('A', 'B', 'C')),
    requested_by TEXT NOT NULL,
    reason TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    argv_json TEXT,
    budget_key TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'authorized', 'pending_approval', 'approved', 'rejected', 'approval_expired',
        'budget_exhausted', 'running', 'succeeded', 'failed'
    )),
    approval_deadline TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS actions_incident_idx ON actions(incident_id, created_at);
CREATE INDEX IF NOT EXISTS actions_status_idx ON actions(status, action_class);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    action_id TEXT NOT NULL REFERENCES actions(id),
    actor_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
    source TEXT NOT NULL,
    source_event_id TEXT UNIQUE,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS approvals_action_idx ON approvals(action_id, created_at);

CREATE TABLE IF NOT EXISTS budget_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    budget_key TEXT NOT NULL,
    action_id TEXT NOT NULL UNIQUE REFERENCES actions(id),
    used_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS budget_usage_lookup_idx ON budget_usage(budget_key, used_at);

CREATE TABLE IF NOT EXISTS mission_status_changes (
    request_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    actor_id TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    new_status TEXT NOT NULL CHECK (new_status IN ('open', 'paused', 'completed')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_status_changes (
    request_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    actor_id TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    new_status TEXT NOT NULL CHECK (new_status IN ('open', 'monitoring', 'resolved')),
    created_at TEXT NOT NULL
);

-- V1 mission state is deliberately split from the stable mission identity.
-- This keeps migrations additive and lets older MCP clients continue to use
-- the small create_mission contract while every mission still gains a durable
-- queue entry and resumable context.
CREATE TABLE IF NOT EXISTS mission_runtime (
    mission_id TEXT PRIMARY KEY REFERENCES missions(id) ON DELETE CASCADE,
    environment TEXT NOT NULL DEFAULT 'unknown',
    priority INTEGER NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
    queue_state TEXT NOT NULL DEFAULT 'queued'
        CHECK (queue_state IN ('queued', 'claimed', 'blocked', 'done')),
    objective TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    impact TEXT NOT NULL DEFAULT '',
    risk TEXT NOT NULL DEFAULT '',
    plan_json TEXT NOT NULL DEFAULT '[]',
    current_step TEXT NOT NULL DEFAULT '',
    remaining_steps_json TEXT NOT NULL DEFAULT '[]',
    resources_json TEXT NOT NULL DEFAULT '[]',
    files_json TEXT NOT NULL DEFAULT '[]',
    next_action TEXT NOT NULL DEFAULT '',
    current_model_role TEXT,
    current_model TEXT,
    iteration_count INTEGER NOT NULL DEFAULT 0 CHECK (iteration_count >= 0),
    max_iterations INTEGER NOT NULL DEFAULT 12 CHECK (max_iterations BETWEEN 1 AND 100),
    api_call_count INTEGER NOT NULL DEFAULT 0 CHECK (api_call_count >= 0),
    max_api_calls INTEGER NOT NULL DEFAULT 8 CHECK (max_api_calls BETWEEN 0 AND 100),
    deadline TEXT,
    claimed_by TEXT,
    claimed_at TEXT,
    lease_until TEXT,
    heartbeat_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS mission_runtime_queue_idx
    ON mission_runtime(queue_state, priority DESC, updated_at ASC);

CREATE TABLE IF NOT EXISTS mission_updates (
    request_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mission_checkpoints (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    actor_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    summary TEXT NOT NULL,
    decisions_json TEXT NOT NULL,
    completed_json TEXT NOT NULL,
    pending_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    model_role TEXT,
    model_name TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(mission_id, sequence)
);

CREATE INDEX IF NOT EXISTS mission_checkpoints_lookup_idx
    ON mission_checkpoints(mission_id, sequence DESC);

CREATE TABLE IF NOT EXISTS mission_records (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    incident_id TEXT REFERENCES incidents(id) ON DELETE SET NULL,
    actor_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN ('observation', 'analysis', 'decision', 'handoff', 'escalation')
    ),
    content TEXT NOT NULL,
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    evidence_json TEXT NOT NULL,
    model_trace_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS mission_records_lookup_idx
    ON mission_records(mission_id, created_at DESC);

CREATE TABLE IF NOT EXISTS resource_locks (
    resource_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    holder_actor_id TEXT NOT NULL,
    fencing_token INTEGER NOT NULL CHECK (fencing_token >= 1),
    acquired_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    lease_until TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS resource_locks_mission_idx
    ON resource_locks(mission_id, lease_until);

CREATE TABLE IF NOT EXISTS resource_lock_operations (
    request_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('acquire', 'renew', 'release')),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mission_claim_operations (
    request_id TEXT PRIMARY KEY,
    mission_id TEXT REFERENCES missions(id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('claim', 'heartbeat', 'release')),
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_traces (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    parent_trace_id TEXT REFERENCES model_traces(id),
    actor_id TEXT NOT NULL,
    model_role TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    reason TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (
        outcome IN ('selected', 'succeeded', 'failed', 'escalated', 'refused')
    ),
    confidence REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cost_microunits INTEGER NOT NULL DEFAULT 0 CHECK (cost_microunits >= 0),
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS model_traces_mission_idx
    ON model_traces(mission_id, started_at ASC);

CREATE TABLE IF NOT EXISTS action_artifacts (
    action_id TEXT PRIMARY KEY REFERENCES actions(id) ON DELETE CASCADE,
    before_digest TEXT,
    after_digest TEXT,
    target_version TEXT,
    rollback_ref TEXT,
    diff_artifact TEXT,
    healthcheck_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_artifact_updates (
    request_id TEXT PRIMARY KEY,
    action_id TEXT NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_details (
    incident_id TEXT PRIMARY KEY REFERENCES incidents(id) ON DELETE CASCADE,
    service TEXT NOT NULL DEFAULT '',
    resource_id TEXT NOT NULL DEFAULT '',
    environment TEXT NOT NULL DEFAULT 'unknown',
    symptoms TEXT NOT NULL DEFAULT '',
    impact TEXT NOT NULL DEFAULT '',
    probable_causes_json TEXT NOT NULL DEFAULT '[]',
    confirmed_cause TEXT NOT NULL DEFAULT '',
    resolution TEXT NOT NULL DEFAULT '',
    rollback TEXT NOT NULL DEFAULT '',
    recommendations_json TEXT NOT NULL DEFAULT '[]',
    versions_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incident_updates (
    request_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS component_heartbeats (
    component_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('healthy', 'degraded', 'failed')),
    detail TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS missions_runtime_after_insert
AFTER INSERT ON missions
BEGIN
    INSERT OR IGNORE INTO mission_runtime(mission_id, updated_at)
    VALUES (NEW.id, NEW.updated_at);
END;

CREATE TRIGGER IF NOT EXISTS incidents_details_after_insert
AFTER INSERT ON incidents
BEGIN
    INSERT OR IGNORE INTO incident_details(incident_id, updated_at)
    VALUES (NEW.id, NEW.updated_at);
END;

INSERT OR IGNORE INTO mission_runtime(mission_id, updated_at)
SELECT id, updated_at FROM missions;

INSERT OR IGNORE INTO incident_details(incident_id, updated_at)
SELECT id, updated_at FROM incidents;

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS audit_head (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    last_seq INTEGER NOT NULL,
    last_hash TEXT NOT NULL
);

INSERT OR IGNORE INTO audit_head(singleton, last_seq, last_hash)
VALUES (1, 0, '0000000000000000000000000000000000000000000000000000000000000000');
"""


@dataclass(frozen=True)
class AuditVerification:
    valid: bool
    event_count: int
    failure_seq: int | None = None
    reason: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "event_count": self.event_count,
            "failure_seq": self.failure_seq,
            "reason": self.reason,
        }


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        # synchronous is connection-local.  Keep every writer on the durable
        # setting rather than only the connection which created the schema.
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def raw_connection(self) -> sqlite3.Connection:
        """Return a configured connection for diagnostics and offline verification."""

        return self._connect()

    def _initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        existed = self.path.exists()
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, 1, 2, SCHEMA_VERSION}:
                raise RuntimeError(f"unsupported database schema version: {version}")
            if version == 0:
                user_tables = connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    LIMIT 1
                    """
                ).fetchone()
                if user_tables is not None:
                    raise RuntimeError(
                        "refusing to upgrade an unversioned non-empty database"
                    )
            if version == 1:
                self._migrate_v1_to_v2(connection)
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        finally:
            connection.close()
        if not existed:
            os.chmod(self.path, 0o600)

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        """Atomically widen lifecycle states while preserving all linked rows."""

        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            # The script intentionally leaves the explicit transaction open.
            # Validation and the schema-version write happen before commit, so
            # a failed migration cannot leave a converted but version-1 store.
            connection.executescript(
                """
                BEGIN IMMEDIATE;
                DROP TABLE IF EXISTS missions_v2;
                DROP TABLE IF EXISTS incidents_v2;

                CREATE TABLE missions_v2 (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('open', 'paused', 'completed')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO missions_v2(
                    id, request_id, project_id, title, status, created_by, created_at, updated_at
                )
                SELECT id, request_id, project_id, title,
                       CASE status WHEN 'closed' THEN 'completed' ELSE status END,
                       created_by, created_at, updated_at
                FROM missions;

                CREATE TABLE incidents_v2 (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    project_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'critical')),
                    status TEXT NOT NULL CHECK (status IN ('open', 'monitoring', 'resolved')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                INSERT INTO incidents_v2(
                    id, request_id, project_id, title, severity, status,
                    created_by, created_at, updated_at
                )
                SELECT id, request_id, project_id, title, severity, status,
                       created_by, created_at, updated_at
                FROM incidents;

                DROP TABLE missions;
                DROP TABLE incidents;
                ALTER TABLE missions_v2 RENAME TO missions;
                ALTER TABLE incidents_v2 RENAME TO incidents;

                CREATE INDEX IF NOT EXISTS missions_status_updated_idx
                    ON missions(status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS incidents_status_updated_idx
                    ON incidents(status, updated_at DESC);

                CREATE TABLE IF NOT EXISTS mission_status_changes (
                    request_id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL REFERENCES missions(id),
                    actor_id TEXT NOT NULL,
                    previous_status TEXT NOT NULL,
                    new_status TEXT NOT NULL CHECK (new_status IN ('open', 'paused', 'completed')),
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS incident_status_changes (
                    request_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL REFERENCES incidents(id),
                    actor_id TEXT NOT NULL,
                    previous_status TEXT NOT NULL,
                    new_status TEXT NOT NULL CHECK (new_status IN ('open', 'monitoring', 'resolved')),
                    created_at TEXT NOT NULL
                );
                """
            )
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError("foreign key validation failed during schema migration")
            connection.execute("PRAGMA user_version = 2")
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def append_event(
        self,
        connection: sqlite3.Connection,
        *,
        occurred_at: str,
        event_type: str,
        actor_id: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, Any],
    ) -> str:
        head = connection.execute(
            "SELECT last_seq, last_hash FROM audit_head WHERE singleton = 1"
        ).fetchone()
        if head is None:
            raise RuntimeError("audit head is missing")
        seq = int(head["last_seq"]) + 1
        previous_hash = str(head["last_hash"])
        event_id = str(uuid.uuid4())
        clean_payload = scrub_payload(payload)
        payload_json = canonical_json(clean_payload)
        material = {
            "seq": seq,
            "event_id": event_id,
            "occurred_at": occurred_at,
            "event_type": event_type,
            "actor_id": actor_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload": clean_payload,
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
        connection.execute(
            """
            INSERT INTO events(
                seq, event_id, occurred_at, event_type, actor_id, entity_type,
                entity_id, payload_json, previous_hash, event_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                seq,
                event_id,
                occurred_at,
                event_type,
                actor_id,
                entity_type,
                entity_id,
                payload_json,
                previous_hash,
                event_hash,
            ),
        )
        connection.execute(
            "UPDATE audit_head SET last_seq = ?, last_hash = ? WHERE singleton = 1",
            (seq, event_hash),
        )
        return event_id

    def verify_audit_chain(self) -> AuditVerification:
        with self.read() as connection:
            connection.execute("BEGIN")
            try:
                rows = connection.execute("SELECT * FROM events ORDER BY seq ASC").fetchall()
                head = connection.execute(
                    "SELECT last_seq, last_hash FROM audit_head WHERE singleton = 1"
                ).fetchone()
            finally:
                connection.rollback()

        return self._verify_snapshot(rows, head)

    @classmethod
    def verify_file(cls, path: Path) -> AuditVerification:
        """Verify an existing database through SQLite's read-only URI mode."""

        path = Path(path)
        if not path.is_file():
            return AuditVerification(False, 0, None, "database does not exist")
        uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("BEGIN")
            try:
                rows = connection.execute("SELECT * FROM events ORDER BY seq ASC").fetchall()
                head = connection.execute(
                    "SELECT last_seq, last_hash FROM audit_head WHERE singleton = 1"
                ).fetchone()
            finally:
                connection.rollback()
        except sqlite3.Error:
            return AuditVerification(False, 0, None, "database cannot be read or schema is invalid")
        finally:
            if "connection" in locals():
                connection.close()
        return cls._verify_snapshot(rows, head)

    @staticmethod
    def _verify_snapshot(
        rows: list[sqlite3.Row],
        head: sqlite3.Row | None,
    ) -> AuditVerification:

        previous_hash = GENESIS_HASH
        expected_seq = 1
        for row in rows:
            seq = int(row["seq"])
            if seq != expected_seq:
                return AuditVerification(False, len(rows), seq, "event sequence has a gap")
            if row["previous_hash"] != previous_hash:
                return AuditVerification(False, len(rows), seq, "previous hash mismatch")
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                return AuditVerification(False, len(rows), seq, "payload is not canonical JSON")
            if canonical_json(payload) != row["payload_json"]:
                return AuditVerification(False, len(rows), seq, "payload is not canonical JSON")
            material = {
                "seq": seq,
                "event_id": row["event_id"],
                "occurred_at": row["occurred_at"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "payload": payload,
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(material).encode("utf-8")).hexdigest()
            if calculated != row["event_hash"]:
                return AuditVerification(False, len(rows), seq, "event hash mismatch")
            previous_hash = calculated
            expected_seq += 1

        if head is None:
            return AuditVerification(False, len(rows), None, "audit head is missing")
        if int(head["last_seq"]) != len(rows) or head["last_hash"] != previous_hash:
            return AuditVerification(False, len(rows), None, "audit head mismatch")
        return AuditVerification(True, len(rows))
