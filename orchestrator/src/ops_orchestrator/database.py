from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Iterator
import uuid
from urllib.parse import quote

from .errors import ConfigurationError, ValidationError


SCHEMA_VERSION = 4
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROVIDER_ACCOUNT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_REGISTRY_REVISION = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_FINANCE_STATUSES = {
    "ok",
    "credential_unavailable",
    "provider_unavailable",
    "unsupported_schema",
}
MAX_FINANCE_SNAPSHOTS_PER_ACCOUNT = 50_000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Database:
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists():
            info = self.path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ConfigurationError("database path must be a regular non-symlink file")
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS usage_reservations (
                    call_id TEXT PRIMARY KEY,
                    route_id TEXT NOT NULL,
                    mission_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    provider_account_id TEXT,
                    role TEXT NOT NULL,
                    model TEXT NOT NULL,
                    location TEXT NOT NULL CHECK(location IN ('local', 'remote')),
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL CHECK(status IN ('reserved', 'completed', 'uncertain')),
                    reserved_input_tokens INTEGER NOT NULL CHECK(reserved_input_tokens >= 0),
                    reserved_output_tokens INTEGER NOT NULL CHECK(reserved_output_tokens >= 0),
                    actual_input_tokens INTEGER CHECK(actual_input_tokens >= 0),
                    actual_output_tokens INTEGER CHECK(actual_output_tokens >= 0),
                    reserved_cost_microusd INTEGER NOT NULL CHECK(reserved_cost_microusd >= 0),
                    actual_cost_microusd INTEGER CHECK(actual_cost_microusd >= 0),
                    error_code TEXT
                );
                CREATE INDEX IF NOT EXISTS usage_provider_created
                    ON usage_reservations(provider_id, created_at);
                CREATE INDEX IF NOT EXISTS usage_mission
                    ON usage_reservations(mission_id, provider_id);

                CREATE TABLE IF NOT EXISTS model_trace (
                    event_id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL,
                    route_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    role TEXT,
                    provider_id TEXT,
                    model TEXT,
                    location TEXT,
                    reason TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    complexity REAL NOT NULL,
                    impact REAL NOT NULL,
                    confidence_in REAL,
                    confidence_out REAL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_microusd INTEGER NOT NULL DEFAULT 0,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    UNIQUE(mission_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS trace_route ON model_trace(route_id, sequence);

                CREATE TABLE IF NOT EXISTS mission_runs (
                    route_id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    complexity REAL NOT NULL,
                    impact REAL NOT NULL,
                    requested_role TEXT,
                    final_role TEXT,
                    final_provider TEXT,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    duration_ms INTEGER
                );
                CREATE INDEX IF NOT EXISTS mission_runs_started ON mission_runs(started_at);

                CREATE TABLE IF NOT EXISTS metric_counters (
                    metric_key TEXT NOT NULL,
                    labels_json TEXT NOT NULL,
                    value INTEGER NOT NULL CHECK(value >= 0),
                    PRIMARY KEY(metric_key, labels_json)
                );
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif existing[0] == "1":
                columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(usage_reservations)"
                    ).fetchall()
                }
                if "provider_account_id" not in columns:
                    connection.execute(
                        "ALTER TABLE usage_reservations ADD COLUMN provider_account_id TEXT"
                    )
                connection.execute(
                    "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )
            elif existing[0] == "2":
                connection.execute(
                    "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )
            elif existing[0] == "3":
                connection.execute(
                    "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )
            elif existing[0] != str(SCHEMA_VERSION):
                connection.rollback()
                raise ConfigurationError("database schema version mismatch")
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS usage_account_created
                ON usage_reservations(provider_account_id, created_at)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS usage_account_route
                ON usage_reservations(provider_account_id, route_id)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS model_result_validations (
                    route_id TEXT PRIMARY KEY,
                    mission_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    role TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    provider_account_id TEXT,
                    model TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN ('succeeded', 'failed')),
                    quality_milli INTEGER NOT NULL CHECK(quality_milli BETWEEN 0 AND 1000),
                    corrections_required INTEGER NOT NULL
                        CHECK(corrections_required BETWEEN 0 AND 1000),
                    evidence_kind TEXT NOT NULL CHECK(evidence_kind IN (
                        'deterministic_test', 'human_review', 'production_observation'
                    )),
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(route_id) REFERENCES mission_runs(route_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS validation_category_provider
                ON model_result_validations(task_type, provider_id, model, recorded_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS adaptive_score_audit (
                    audit_id TEXT PRIMARY KEY,
                    route_id TEXT NOT NULL,
                    task_type TEXT NOT NULL,
                    role TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    provider_account_id TEXT,
                    model TEXT NOT NULL,
                    estimated_cost_microusd INTEGER NOT NULL
                        CHECK(estimated_cost_microusd >= 0),
                    attempt_count INTEGER NOT NULL CHECK(attempt_count >= 0),
                    completed_attempts INTEGER NOT NULL CHECK(completed_attempts >= 0),
                    uncertain_attempts INTEGER NOT NULL CHECK(uncertain_attempts >= 0),
                    validation_count INTEGER NOT NULL CHECK(validation_count >= 0),
                    successful_validations INTEGER NOT NULL
                        CHECK(successful_validations >= 0),
                    posterior_quality_milli INTEGER NOT NULL
                        CHECK(posterior_quality_milli BETWEEN 0 AND 1000),
                    quality_adjustment_ppm INTEGER NOT NULL DEFAULT 0
                        CHECK(quality_adjustment_ppm BETWEEN -150000 AND 150000),
                    observed_latency_ms INTEGER
                        CHECK(observed_latency_ms IS NULL OR
                              observed_latency_ms BETWEEN 0 AND 300000),
                    latency_sample_count INTEGER NOT NULL DEFAULT 0
                        CHECK(latency_sample_count BETWEEN 0 AND 500),
                    latency_target_ms INTEGER
                        CHECK(latency_target_ms IS NULL OR
                              latency_target_ms BETWEEN 1 AND 300000),
                    latency_adjustment_ppm INTEGER NOT NULL DEFAULT 0
                        CHECK(latency_adjustment_ppm BETWEEN -150000 AND 150000),
                    adjustment_ppm INTEGER NOT NULL
                        CHECK(adjustment_ppm BETWEEN -150000 AND 150000),
                    adaptive_cost_microusd INTEGER NOT NULL
                        CHECK(adaptive_cost_microusd >= 0),
                    rank INTEGER NOT NULL CHECK(rank >= 1),
                    algorithm_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(route_id) REFERENCES mission_runs(route_id),
                    UNIQUE(route_id, role, provider_id)
                )
                """
            )
            adaptive_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(adaptive_score_audit)"
                ).fetchall()
            }
            adaptive_migrations = {
                "quality_adjustment_ppm": (
                    "INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(quality_adjustment_ppm BETWEEN -150000 AND 150000)"
                ),
                "observed_latency_ms": (
                    "INTEGER CHECK(observed_latency_ms IS NULL OR "
                    "observed_latency_ms BETWEEN 0 AND 300000)"
                ),
                "latency_sample_count": (
                    "INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(latency_sample_count BETWEEN 0 AND 500)"
                ),
                "latency_target_ms": (
                    "INTEGER CHECK(latency_target_ms IS NULL OR "
                    "latency_target_ms BETWEEN 1 AND 300000)"
                ),
                "latency_adjustment_ppm": (
                    "INTEGER NOT NULL DEFAULT 0 "
                    "CHECK(latency_adjustment_ppm BETWEEN -150000 AND 150000)"
                ),
            }
            for column, declaration in adaptive_migrations.items():
                if column not in adaptive_columns:
                    connection.execute(
                        f"ALTER TABLE adaptive_score_audit ADD COLUMN {column} {declaration}"
                    )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS provider_finance_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    provider_account_id TEXT NOT NULL,
                    registry_revision TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    cash_balance_mode TEXT NOT NULL CHECK(cash_balance_mode = 'direct_api'),
                    status TEXT NOT NULL CHECK(status IN (
                        'ok', 'credential_unavailable', 'provider_unavailable',
                        'unsupported_schema'
                    )),
                    is_available INTEGER CHECK(is_available IN (0, 1)),
                    balances_json TEXT NOT NULL CHECK(length(balances_json) <= 32768),
                    error_code TEXT CHECK(error_code IS NULL OR length(error_code) <= 64),
                    duration_ms INTEGER NOT NULL CHECK(duration_ms BETWEEN 0 AND 60000)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS provider_finance_account_captured
                ON provider_finance_snapshots(provider_account_id, captured_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS adaptive_audit_route
                ON adaptive_score_audit(route_id, rank)
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS validation_append_only_update
                BEFORE UPDATE ON model_result_validations
                BEGIN
                    SELECT RAISE(ABORT, 'model result validations are append-only');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS validation_append_only_delete
                BEFORE DELETE ON model_result_validations
                BEGIN
                    SELECT RAISE(ABORT, 'model result validations are append-only');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS adaptive_audit_append_only_update
                BEFORE UPDATE ON adaptive_score_audit
                BEGIN
                    SELECT RAISE(ABORT, 'adaptive score audit is append-only');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS adaptive_audit_append_only_delete
                BEFORE DELETE ON adaptive_score_audit
                BEGIN
                    SELECT RAISE(ABORT, 'adaptive score audit is append-only');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS provider_finance_append_only_update
                BEFORE UPDATE ON provider_finance_snapshots
                BEGIN
                    SELECT RAISE(ABORT, 'provider finance snapshots are append-only');
                END
                """
            )
            connection.execute(
                """
                CREATE TRIGGER IF NOT EXISTS provider_finance_append_only_delete
                BEFORE DELETE ON provider_finance_snapshots
                BEGIN
                    SELECT RAISE(ABORT, 'provider finance snapshots are append-only');
                END
                """
            )
            conflicting_mission = connection.execute(
                """
                SELECT mission_id FROM mission_runs
                GROUP BY mission_id
                HAVING COUNT(DISTINCT project_id) > 1
                LIMIT 1
                """
            ).fetchone()
            if conflicting_mission is not None:
                connection.rollback()
                raise ConfigurationError(
                    "database contains a mission_id bound to multiple projects"
                )
            connection.commit()
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def connect_readonly(self) -> Iterator[sqlite3.Connection]:
        """Open the live database without asking SQLite to mutate WAL state."""
        uri = f"file:{quote(str(self.path), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, timeout=10, isolation_level=None, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
        finally:
            connection.close()

    def increment(self, metric_key: str, labels: dict[str, str] | None = None, amount: int = 1) -> None:
        if amount < 0:
            raise ValueError("metric increment must not be negative")
        labels_json = json.dumps(labels or {}, sort_keys=True, separators=(",", ":"))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO metric_counters(metric_key, labels_json, value)
                VALUES (?, ?, ?)
                ON CONFLICT(metric_key, labels_json)
                DO UPDATE SET value = value + excluded.value
                """,
                (metric_key, labels_json, amount),
            )
            connection.commit()

    def start_run(
        self,
        *,
        route_id: str,
        mission_id: str,
        project_id: str,
        task_type: str,
        risk: str,
        complexity: float,
        impact: float,
        requested_role: str | None,
        reason: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT project_id FROM mission_runs
                WHERE mission_id = ? LIMIT 1
                """,
                (mission_id,),
            ).fetchone()
            if existing is not None and str(existing["project_id"]) != project_id:
                connection.rollback()
                raise ValidationError("mission_id is already bound to another project")
            connection.execute(
                """
                INSERT INTO mission_runs(
                    route_id, mission_id, project_id, task_type, risk, complexity, impact,
                    requested_role, status, reason, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (route_id, mission_id, project_id, task_type, risk, complexity, impact, requested_role, reason, utc_now()),
            )
            connection.commit()

    def finish_run(
        self,
        *,
        route_id: str,
        status: str,
        reason: str,
        duration_ms: int,
        final_role: str | None = None,
        final_provider: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE mission_runs
                SET status = ?, reason = ?, completed_at = ?, duration_ms = ?,
                    final_role = ?, final_provider = ?
                WHERE route_id = ? AND status = 'running'
                """,
                (status, reason, utc_now(), duration_ms, final_role, final_provider, route_id),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise RuntimeError("mission run state transition failed")
            connection.commit()

    def record_signal(self, signal: str, project_id: str) -> None:
        if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
            raise ValidationError("project_id must be a safe bounded identifier")
        allowed = {
            "mission_succeeded": ("mission_outcomes", {"outcome": "succeeded"}),
            "mission_failed": ("mission_outcomes", {"outcome": "failed"}),
            "tool_error": ("tool_errors", {}),
            "memory_search": ("memory_searches", {}),
        }
        try:
            metric, labels = allowed[signal]
        except KeyError as exc:
            raise ValidationError("unsupported metric signal") from exc
        self.increment(metric, {**labels, "project": project_id})

    def latest_run(self, mission_id: str):
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT route_id, project_id, risk, complexity, impact FROM mission_runs
                WHERE mission_id = ? ORDER BY started_at DESC LIMIT 1
                """,
                (mission_id,),
            ).fetchone()

    def bind_provider_accounts(self, provider_accounts: dict[str, str | None]) -> None:
        """Backfill account ownership for pre-v2 reservations without weakening caps."""

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for provider_id, account_id in sorted(provider_accounts.items()):
                if account_id is None:
                    conflicting = connection.execute(
                        """
                        SELECT provider_account_id FROM usage_reservations
                        WHERE provider_id = ? AND provider_account_id IS NOT NULL
                        LIMIT 1
                        """,
                        (provider_id,),
                    ).fetchone()
                    if conflicting is not None:
                        connection.rollback()
                        raise ConfigurationError(
                            f"historical account binding removed for provider {provider_id}"
                        )
                    continue
                conflicting = connection.execute(
                    """
                    SELECT provider_account_id FROM usage_reservations
                    WHERE provider_id = ? AND provider_account_id IS NOT NULL
                          AND provider_account_id != ?
                    LIMIT 1
                    """,
                    (provider_id, account_id),
                ).fetchone()
                if conflicting is not None:
                    connection.rollback()
                    raise ConfigurationError(
                        f"historical account binding changed for provider {provider_id}"
                    )
                connection.execute(
                    """
                    UPDATE usage_reservations
                    SET provider_account_id = ?
                    WHERE provider_id = ? AND provider_account_id IS NULL
                    """,
                    (account_id, provider_id),
                )
            connection.commit()

    def record_model_validation(
        self,
        *,
        route_id: str,
        mission_id: str,
        project_id: str,
        outcome: str,
        quality_milli: int,
        corrections_required: int,
        evidence_kind: str,
        provider_accounts: dict[str, str | None],
    ) -> dict[str, object]:
        """Append one externally validated outcome bound to recorded route facts."""

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                """
                SELECT mission_id, project_id, task_type, final_role, final_provider,
                       status
                FROM mission_runs WHERE route_id = ?
                """,
                (route_id,),
            ).fetchone()
            if run is None:
                connection.rollback()
                raise ValidationError("route_id does not identify a recorded task")
            if str(run["mission_id"]) != mission_id or str(run["project_id"]) != project_id:
                connection.rollback()
                raise ValidationError("outcome identity does not match the recorded route")
            provider_id = run["final_provider"]
            role = run["final_role"]
            if run["status"] != "routed" or not provider_id or not role:
                connection.rollback()
                raise ValidationError("only a routed model result can receive an outcome")
            usage = connection.execute(
                """
                SELECT model FROM usage_reservations
                WHERE route_id = ? AND provider_id = ? AND status = 'completed'
                ORDER BY completed_at DESC LIMIT 1
                """,
                (route_id, provider_id),
            ).fetchone()
            if usage is None:
                connection.rollback()
                raise ValidationError("routed model result has no completed usage record")
            account_id = provider_accounts.get(str(provider_id))
            try:
                connection.execute(
                    """
                    INSERT INTO model_result_validations(
                        route_id, mission_id, project_id, task_type, role,
                        provider_id, provider_account_id, model, outcome,
                        quality_milli, corrections_required, evidence_kind, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        route_id,
                        mission_id,
                        project_id,
                        run["task_type"],
                        role,
                        provider_id,
                        account_id,
                        usage["model"],
                        outcome,
                        quality_milli,
                        corrections_required,
                        evidence_kind,
                        utc_now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ValidationError("an outcome is already recorded for this route") from exc
            labels = json.dumps(
                {
                    "outcome": outcome,
                    "project": project_id,
                    "task_type": str(run["task_type"]),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO metric_counters(metric_key, labels_json, value)
                VALUES ('validated_model_outcomes', ?, 1)
                ON CONFLICT(metric_key, labels_json)
                DO UPDATE SET value = value + 1
                """,
                (labels,),
            )
            connection.commit()
        return {
            "route_id": route_id,
            "mission_id": mission_id,
            "project_id": project_id,
            "task_type": str(run["task_type"]),
            "provider_id": str(provider_id),
            "provider_account_id": account_id,
            "model": str(usage["model"]),
            "outcome": outcome,
            "quality_milli": quality_milli,
            "corrections_required": corrections_required,
            "evidence_kind": evidence_kind,
        }

    def model_performance_history(
        self,
        *,
        task_type: str,
        provider_id: str,
        model: str,
        maximum_rows: int = 500,
        usage_role: str | None = None,
    ) -> dict[str, object]:
        """Read bounded execution and validation history for one exact candidate."""

        if not 1 <= maximum_rows <= 500:
            raise ValueError("maximum_rows must be between 1 and 500")
        with self.connect_readonly() as connection:
            attempts = connection.execute(
                """
                SELECT status,
                       CASE
                           WHEN status = 'completed' AND completed_at IS NOT NULL
                           THEN MAX(0, MIN(300000, CAST(ROUND(
                               (julianday(completed_at) - julianday(created_at))
                               * 86400000
                           ) AS INTEGER)))
                           ELSE NULL
                       END AS duration_ms
                FROM usage_reservations
                WHERE call_id IN (
                    SELECT usage.call_id
                    FROM usage_reservations AS usage
                    JOIN mission_runs AS run ON run.route_id = usage.route_id
                    WHERE run.task_type = ? AND usage.provider_id = ?
                          AND usage.model = ?
                          AND (? IS NULL OR usage.role = ?)
                    ORDER BY usage.created_at DESC
                    LIMIT ?
                )
                """,
                (
                    task_type,
                    provider_id,
                    model,
                    usage_role,
                    usage_role,
                    maximum_rows,
                ),
            ).fetchall()
            validations = connection.execute(
                """
                SELECT outcome, quality_milli, corrections_required
                FROM model_result_validations
                WHERE task_type = ? AND provider_id = ? AND model = ?
                      AND (? IS NULL OR role = ?)
                ORDER BY recorded_at DESC LIMIT ?
                """,
                (
                    task_type,
                    provider_id,
                    model,
                    usage_role,
                    usage_role,
                    maximum_rows,
                ),
            ).fetchall()
        return {
            "attempt_count": len(attempts),
            "completed_attempts": sum(row["status"] == "completed" for row in attempts),
            "uncertain_attempts": sum(row["status"] != "completed" for row in attempts),
            "completed_latency_ms": [
                int(row["duration_ms"])
                for row in attempts
                if row["status"] == "completed" and row["duration_ms"] is not None
            ],
            "validations": [dict(row) for row in validations],
        }

    def record_adaptive_score_audit(
        self,
        *,
        route_id: str,
        task_type: str,
        role: str,
        scores: list[dict[str, object]],
    ) -> None:
        if not scores or len(scores) > 64:
            raise ValueError("adaptive score audit batch is outside bounds")
        created_at = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for score in scores:
                connection.execute(
                    """
                    INSERT INTO adaptive_score_audit(
                        audit_id, route_id, task_type, role, provider_id,
                        provider_account_id, model, estimated_cost_microusd,
                        attempt_count, completed_attempts, uncertain_attempts,
                        validation_count, successful_validations,
                        posterior_quality_milli, quality_adjustment_ppm,
                        observed_latency_ms, latency_sample_count,
                        latency_target_ms, latency_adjustment_ppm, adjustment_ppm,
                        adaptive_cost_microusd, rank, algorithm_version, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        route_id,
                        task_type,
                        role,
                        score["provider_id"],
                        score["provider_account_id"],
                        score["model"],
                        score["estimated_cost_microusd"],
                        score["attempt_count"],
                        score["completed_attempts"],
                        score["uncertain_attempts"],
                        score["validation_count"],
                        score["successful_validations"],
                        score["posterior_quality_milli"],
                        score["quality_adjustment_ppm"],
                        score["observed_latency_ms"],
                        score["latency_sample_count"],
                        score["latency_target_ms"],
                        score["latency_adjustment_ppm"],
                        score["adjustment_ppm"],
                        score["adaptive_cost_microusd"],
                        score["rank"],
                        score["algorithm_version"],
                        created_at,
                    ),
                )
            connection.commit()

    def adaptive_score_audit(self, route_id: str, limit: int = 100) -> list[dict[str, object]]:
        if not 1 <= limit <= 100:
            raise ValueError("adaptive score audit limit is outside bounds")
        with self.connect_readonly() as connection:
            rows = connection.execute(
                """
                SELECT task_type, role, provider_id, provider_account_id, model,
                       estimated_cost_microusd, attempt_count, completed_attempts,
                       uncertain_attempts, validation_count, successful_validations,
                       posterior_quality_milli, quality_adjustment_ppm,
                       observed_latency_ms, latency_sample_count,
                       latency_target_ms, latency_adjustment_ppm, adjustment_ppm,
                       adaptive_cost_microusd, rank, algorithm_version, created_at
                FROM adaptive_score_audit WHERE route_id = ?
                ORDER BY created_at, role, rank LIMIT ?
                """,
                (route_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def model_performance_summary(self, limit: int = 200) -> list[dict[str, object]]:
        if not 1 <= limit <= 200:
            raise ValueError("model performance summary limit is outside bounds")
        with self.connect_readonly() as connection:
            rows = connection.execute(
                """
                WITH route_usage AS (
                    SELECT route_id, COUNT(*) AS attempts,
                           SUM(CASE WHEN status = 'uncertain' THEN 1 ELSE 0 END)
                               AS uncertain_attempts,
                           SUM(CASE WHEN status = 'completed'
                               THEN actual_cost_microusd
                               ELSE reserved_cost_microusd END) AS cost_microusd
                    FROM usage_reservations GROUP BY route_id
                )
                SELECT validation.task_type, validation.provider_id,
                       validation.provider_account_id, validation.model,
                       COUNT(*) AS validation_count,
                       SUM(CASE WHEN validation.outcome = 'succeeded' THEN 1 ELSE 0 END)
                           AS successful_validations,
                       CAST(ROUND(AVG(validation.quality_milli)) AS INTEGER)
                           AS average_quality_milli,
                       CAST(ROUND(AVG(validation.corrections_required)) AS INTEGER)
                           AS average_corrections_required,
                       CAST(ROUND(AVG(route_usage.attempts)) AS INTEGER)
                           AS average_attempts,
                       CAST(ROUND(AVG(route_usage.uncertain_attempts)) AS INTEGER)
                           AS average_uncertain_attempts,
                       CAST(ROUND(AVG(route_usage.cost_microusd)) AS INTEGER)
                           AS average_cost_microusd,
                       CAST(ROUND(AVG(run.duration_ms)) AS INTEGER)
                           AS average_duration_ms,
                       MIN(validation.recorded_at) AS first_recorded_at,
                       MAX(validation.recorded_at) AS last_recorded_at
                FROM model_result_validations AS validation
                JOIN mission_runs AS run ON run.route_id = validation.route_id
                JOIN route_usage ON route_usage.route_id = validation.route_id
                GROUP BY validation.task_type, validation.provider_id,
                         validation.provider_account_id, validation.model
                ORDER BY validation.task_type, validation.provider_id, validation.model
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _finance_balances_json(balances: object) -> str:
        if not isinstance(balances, list) or len(balances) > 8:
            raise ValidationError("provider finance balances are outside bounds")
        allowed_fields = {
            "currency",
            "available",
            "granted",
            "topped_up",
            "cash",
            "voucher",
            "total_cash",
            "total_voucher",
            "billing_type",
        }
        amount = re.compile(r"(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,12})?")
        for entry in balances:
            if (
                not isinstance(entry, dict)
                or not {"currency", "available"} <= set(entry)
                or not set(entry) <= allowed_fields
            ):
                raise ValidationError("provider finance balance entry is invalid")
            for key, value in entry.items():
                if key == "currency":
                    if value is not None and (
                        not isinstance(value, str)
                        or not re.fullmatch(r"[A-Z]{3}", value)
                    ):
                        raise ValidationError("provider finance currency is invalid")
                elif key == "billing_type":
                    if value not in {"prepaid", "postpaid"}:
                        raise ValidationError("provider finance billing type is invalid")
                elif not isinstance(value, str) or not amount.fullmatch(value):
                    raise ValidationError("provider finance amount is invalid")
        rendered = json.dumps(
            balances,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        if len(rendered.encode("utf-8")) > 32768:
            raise ValidationError("provider finance balances exceed storage bounds")
        return rendered

    @staticmethod
    def _finance_row(row: sqlite3.Row) -> dict[str, object]:
        balances = json.loads(str(row["balances_json"]))
        return {
            "snapshot_id": str(row["snapshot_id"]),
            "provider_account_id": str(row["provider_account_id"]),
            "registry_revision": str(row["registry_revision"]),
            "captured_at": str(row["captured_at"]),
            "cash_balance_mode": str(row["cash_balance_mode"]),
            "status": str(row["status"]),
            "is_available": (
                None if row["is_available"] is None else bool(row["is_available"])
            ),
            "balances": balances,
            "error_code": (
                None if row["error_code"] is None else str(row["error_code"])
            ),
            "duration_ms": int(row["duration_ms"]),
        }

    def record_provider_finance_snapshot(
        self,
        *,
        provider_account_id: str,
        registry_revision: str,
        cash_balance_mode: str,
        status: str,
        is_available: bool | None,
        balances: object,
        error_code: str | None,
        duration_ms: int,
    ) -> dict[str, object]:
        """Append one bounded, normalized finance observation.

        Credentials and raw provider bodies are intentionally absent from this
        contract.  Only normalized monetary strings and a finite error code can
        cross the persistence boundary.
        """

        if not isinstance(provider_account_id, str) or not _PROVIDER_ACCOUNT_ID.fullmatch(provider_account_id):
            raise ValidationError("provider finance account id is invalid")
        if not isinstance(registry_revision, str) or not _REGISTRY_REVISION.fullmatch(registry_revision):
            raise ValidationError("provider finance registry revision is invalid")
        if cash_balance_mode != "direct_api":
            raise ValidationError("only direct cash-balance snapshots are persisted")
        if status not in _FINANCE_STATUSES:
            raise ValidationError("provider finance snapshot status is invalid")
        if is_available is not None and not isinstance(is_available, bool):
            raise ValidationError("provider finance availability is invalid")
        if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or not 0 <= duration_ms <= 60000:
            raise ValidationError("provider finance duration is invalid")
        if error_code is not None and (
            not isinstance(error_code, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", error_code)
        ):
            raise ValidationError("provider finance error code is invalid")
        balances_json = self._finance_balances_json(balances)
        if status == "ok":
            if error_code is not None or balances_json == "[]":
                raise ValidationError("successful provider finance snapshot is incomplete")
        elif error_code is None or is_available is not None or balances_json != "[]":
            raise ValidationError("failed provider finance snapshot contains balance data")

        snapshot_id = str(uuid.uuid4())
        captured_at = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_count = connection.execute(
                """
                SELECT COUNT(*) FROM provider_finance_snapshots
                WHERE provider_account_id = ?
                """,
                (provider_account_id,),
            ).fetchone()[0]
            if existing_count >= MAX_FINANCE_SNAPSHOTS_PER_ACCOUNT:
                connection.rollback()
                raise ValidationError(
                    "provider finance append-only snapshot capacity is exhausted"
                )
            connection.execute(
                """
                INSERT INTO provider_finance_snapshots(
                    snapshot_id, provider_account_id, registry_revision,
                    captured_at, cash_balance_mode, status, is_available,
                    balances_json, error_code, duration_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    provider_account_id,
                    registry_revision,
                    captured_at,
                    cash_balance_mode,
                    status,
                    None if is_available is None else int(is_available),
                    balances_json,
                    error_code,
                    duration_ms,
                ),
            )
            row = connection.execute(
                "SELECT * FROM provider_finance_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._finance_row(row)

    def latest_provider_finance_snapshots(
        self, provider_account_ids: tuple[str, ...]
    ) -> list[dict[str, object]]:
        if (
            not isinstance(provider_account_ids, tuple)
            or not 1 <= len(provider_account_ids) <= 64
            or len(set(provider_account_ids)) != len(provider_account_ids)
            or any(
                not isinstance(account_id, str)
                or not _PROVIDER_ACCOUNT_ID.fullmatch(account_id)
                for account_id in provider_account_ids
            )
        ):
            raise ValidationError("provider finance account query is invalid")
        placeholders = ",".join("?" for _ in provider_account_ids)
        with self.connect_readonly() as connection:
            rows = connection.execute(
                f"""
                WITH ranked AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY provider_account_id
                        ORDER BY captured_at DESC, snapshot_id DESC
                    ) AS row_rank
                    FROM provider_finance_snapshots
                    WHERE provider_account_id IN ({placeholders})
                )
                SELECT * FROM ranked WHERE row_rank = 1
                ORDER BY provider_account_id
                """,
                provider_account_ids,
            ).fetchall()
        return [self._finance_row(row) for row in rows]

    def provider_finance_history(
        self, provider_account_id: str, limit: int = 50
    ) -> list[dict[str, object]]:
        if not isinstance(provider_account_id, str) or not _PROVIDER_ACCOUNT_ID.fullmatch(provider_account_id):
            raise ValidationError("provider finance account id is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValidationError("provider finance history limit is outside bounds")
        with self.connect_readonly() as connection:
            rows = connection.execute(
                """
                SELECT * FROM provider_finance_snapshots
                WHERE provider_account_id = ?
                ORDER BY captured_at DESC, snapshot_id DESC LIMIT ?
                """,
                (provider_account_id, limit),
            ).fetchall()
        return [self._finance_row(row) for row in rows]
