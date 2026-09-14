from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .config import classification_rank
from .embedding import tokenize
from .errors import ConflictError, NotFoundError
from .models import IngestRequest, SearchRequest, utc_now


SCHEMA_VERSION = 1


def _normalize_content(content: str) -> str:
    return " ".join(content.casefold().split())


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _row_to_record(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for name in ("allowed_roles", "metadata"):
        result[name] = json.loads(result.pop(f"{name}_json"))
    result["reviewed"] = bool(result["reviewed"])
    result["stale"] = result.get("stale_at") is not None
    return result


class MemoryCatalog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._prepare_path()
        self.initialize()

    def _prepare_path(self) -> None:
        if self.path.exists() and self.path.is_symlink():
            raise OSError("database path must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except PermissionError:
            pass

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as db:
            db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY,
                    content TEXT NOT NULL,
                    project TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    classification_rank INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    allowed_roles_json TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_uri TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    source_authority TEXT NOT NULL,
                    authority_status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    importance REAL NOT NULL CHECK(importance BETWEEN 0 AND 1),
                    content_hash TEXT NOT NULL,
                    dedup_key TEXT NOT NULL,
                    vector_model TEXT NOT NULL,
                    vector_json TEXT NOT NULL,
                    qdrant_state TEXT NOT NULL,
                    qdrant_error TEXT,
                    valid_from TEXT NOT NULL,
                    valid_until TEXT,
                    invalidated_at TEXT,
                    invalid_reason TEXT,
                    stale_at TEXT,
                    supersedes_id TEXT REFERENCES memories(id),
                    superseded_by_id TEXT REFERENCES memories(id),
                    reviewed INTEGER NOT NULL CHECK(reviewed IN (0, 1)),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS memories_scope_idx
                    ON memories(project, environment, classification_rank, category, kind);
                CREATE INDEX IF NOT EXISTS memories_current_idx
                    ON memories(invalidated_at, superseded_by_id, valid_until, stale_at);
                CREATE INDEX IF NOT EXISTS memories_qdrant_idx ON memories(qdrant_state);
                CREATE INDEX IF NOT EXISTS memories_dedup_idx ON memories(dedup_key);
                CREATE TABLE IF NOT EXISTS memory_sources (
                    memory_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                    source_type TEXT NOT NULL,
                    source_uri TEXT NOT NULL,
                    source_ref TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    PRIMARY KEY(memory_id, source_type, source_uri, source_ref)
                );
                CREATE TABLE IF NOT EXISTS summary_members (
                    summary_id TEXT NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
                    member_id TEXT NOT NULL REFERENCES memories(id),
                    PRIMARY KEY(summary_id, member_id)
                );
                CREATE TABLE IF NOT EXISTS access_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    project TEXT,
                    environment TEXT,
                    classification TEXT,
                    target_id TEXT,
                    query_hash TEXT,
                    outcome TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS access_audit_time_idx ON access_audit(occurred_at);
                CREATE TABLE IF NOT EXISTS provider_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model TEXT NOT NULL,
                    outcome TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS maintenance_runs (
                    id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    expired INTEGER NOT NULL,
                    marked_stale INTEGER NOT NULL,
                    reindexed INTEGER NOT NULL,
                    failures INTEGER NOT NULL
                );
                """
            )
            current = db.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            if current is not None and int(current["value"]) != SCHEMA_VERSION:
                raise RuntimeError("unsupported memory database schema")
            db.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            db.commit()
        os.chmod(self.path, 0o600)

    def audit(
        self,
        actor: str,
        operation: str,
        outcome: str,
        *,
        project: str | None = None,
        environment: str | None = None,
        classification: str | None = None,
        target_id: str | None = None,
        query: str | None = None,
        detail: str = "",
    ) -> None:
        query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest() if query is not None else None
        with self.connect() as db:
            db.execute(
                """INSERT INTO access_audit(
                    occurred_at, actor, operation, project, environment, classification,
                    target_id, query_hash, outcome, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    utc_now(), actor, operation, project, environment, classification,
                    target_id, query_hash, outcome, detail[:512],
                ),
            )
            db.commit()

    def provider_call(self, actor: str, operation: str, provider: str, model: str, outcome: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO provider_calls(occurred_at, actor, operation, provider, model, outcome) VALUES(?, ?, ?, ?, ?, ?)",
                (utc_now(), actor, operation, provider, model, outcome),
            )
            db.commit()

    def provider_calls_today(self, provider: str, model: str) -> int:
        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        with self.connect() as db:
            row = db.execute(
                """SELECT COUNT(*) count FROM provider_calls
                   WHERE provider=? AND model=? AND occurred_at>=?
                     AND outcome!='budget_denied'""",
                (provider, model, day_start),
            ).fetchone()
        return int(row["count"])

    def reserve_provider_call(
        self,
        actor: str,
        operation: str,
        provider: str,
        model: str,
        daily_budget: int,
    ) -> int | None:
        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            used = db.execute(
                """SELECT COUNT(*) count FROM provider_calls
                   WHERE provider=? AND model=? AND occurred_at>=?
                     AND outcome!='budget_denied'""",
                (provider, model, day_start),
            ).fetchone()["count"]
            outcome = "reserved" if daily_budget > 0 and int(used) < daily_budget else "budget_denied"
            cursor = db.execute(
                """INSERT INTO provider_calls(
                    occurred_at, actor, operation, provider, model, outcome
                ) VALUES(?, ?, ?, ?, ?, ?)""",
                (utc_now(), actor, operation, provider, model, outcome),
            )
            db.commit()
        return int(cursor.lastrowid) if outcome == "reserved" else None

    def complete_provider_call(self, call_id: int, outcome: str) -> None:
        if outcome not in {"success", "failed"}:
            raise ValueError("invalid provider outcome")
        with self.connect() as db:
            changed = db.execute(
                "UPDATE provider_calls SET outcome=? WHERE id=? AND outcome='reserved'",
                (outcome, call_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError("provider call reservation is not active")
            db.commit()

    @staticmethod
    def _dedup_key(request: IngestRequest) -> tuple[str, str]:
        content_hash = hashlib.sha256(_normalize_content(request.content).encode("utf-8")).hexdigest()
        scope = "\x1f".join(
            (
                request.project,
                request.environment,
                request.classification,
                request.category,
                request.kind,
                request.tier,
                ",".join(sorted(request.allowed_roles)),
                content_hash,
            )
        )
        return content_hash, hashlib.sha256(scope.encode("utf-8")).hexdigest()

    def ingest(
        self,
        request: IngestRequest,
        vector: tuple[float, ...],
        vector_model: str,
        authority_status: str,
    ) -> tuple[dict[str, Any], bool, str | None]:
        now = utc_now()
        content_hash, dedup_key = self._dedup_key(request)
        record_id = str(uuid.uuid4())
        superseded_id: str | None = None
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = None
            if request.supersedes_id is None:
                duplicate = db.execute(
                    """SELECT * FROM memories WHERE dedup_key=?
                       AND invalidated_at IS NULL AND superseded_by_id IS NULL
                       AND valid_from<=? AND (valid_until IS NULL OR valid_until>?)
                       ORDER BY created_at DESC LIMIT 1""",
                    (dedup_key, now, now),
                ).fetchone()
            if duplicate is not None:
                db.execute(
                    """INSERT OR IGNORE INTO memory_sources(
                        memory_id, source_type, source_uri, source_ref, first_seen_at
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (duplicate["id"], request.source_type, request.source_uri, request.source_ref, now),
                )
                db.commit()
                return _row_to_record(duplicate), True, None

            if request.supersedes_id:
                old = db.execute("SELECT * FROM memories WHERE id=?", (request.supersedes_id,)).fetchone()
                if old is None:
                    raise NotFoundError("superseded memory does not exist")
                if old["project"] != request.project or old["environment"] != request.environment:
                    raise ConflictError("supersession cannot cross project or environment")
                if old["superseded_by_id"] is not None or old["invalidated_at"] is not None:
                    raise ConflictError("superseded memory is not current")
                superseded_id = str(old["id"])

            db.execute(
                """INSERT INTO memories(
                    id, content, project, environment, classification, classification_rank,
                    category, kind, tier, allowed_roles_json, source_type, source_uri,
                    source_ref, source_authority, authority_status, metadata_json, importance,
                    content_hash, dedup_key, vector_model, vector_json, qdrant_state,
                    valid_from, valid_until, supersedes_id, reviewed, created_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'pending', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record_id,
                    request.content,
                    request.project,
                    request.environment,
                    request.classification,
                    classification_rank(request.classification),
                    request.category,
                    request.kind,
                    request.tier,
                    _json(request.allowed_roles),
                    request.source_type,
                    request.source_uri,
                    request.source_ref,
                    request.source_authority,
                    authority_status,
                    _json(request.metadata),
                    request.importance,
                    content_hash,
                    dedup_key,
                    vector_model,
                    _json(vector),
                    request.valid_from,
                    request.valid_until,
                    request.supersedes_id,
                    int(request.reviewed),
                    request.actor,
                    now,
                    now,
                ),
            )
            db.execute(
                """INSERT INTO memory_sources(
                    memory_id, source_type, source_uri, source_ref, first_seen_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (record_id, request.source_type, request.source_uri, request.source_ref, now),
            )
            if superseded_id:
                db.execute(
                    "UPDATE memories SET superseded_by_id=?, qdrant_state='delete_pending', updated_at=? WHERE id=?",
                    (record_id, now, superseded_id),
                )
            row = db.execute("SELECT * FROM memories WHERE id=?", (record_id,)).fetchone()
            db.commit()
        assert row is not None
        return _row_to_record(row), False, superseded_id

    def get(self, record_id: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM memories WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFoundError("memory does not exist")
        return _row_to_record(row)

    def get_many(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as db:
            rows = db.execute(f"SELECT * FROM memories WHERE id IN ({placeholders})", ids).fetchall()
        by_id = {row["id"]: _row_to_record(row) for row in rows}
        return [by_id[item] for item in ids if item in by_id]

    def mark_qdrant(self, record_id: str, state: str, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE memories SET qdrant_state=?, qdrant_error=?, updated_at=? WHERE id=?",
                (state, error[:512] if error else None, utc_now(), record_id),
            )
            db.commit()

    def invalidate(self, record_id: str, reason: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM memories WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFoundError("memory does not exist")
            if row["invalidated_at"] is None:
                db.execute(
                    """UPDATE memories SET invalidated_at=?, invalid_reason=?,
                       qdrant_state='delete_pending', updated_at=? WHERE id=?""",
                    (now, reason, now, record_id),
                )
            result = db.execute("SELECT * FROM memories WHERE id=?", (record_id,)).fetchone()
            db.commit()
        assert result is not None
        return _row_to_record(result)

    @staticmethod
    def is_current(record: dict[str, Any], now: str | None = None) -> bool:
        current = now or utc_now()
        return (
            record["invalidated_at"] is None
            and record["superseded_by_id"] is None
            and record["valid_from"] <= current
            and (record["valid_until"] is None or record["valid_until"] > current)
        )

    def fallback_candidates(self, request: SearchRequest, roles: tuple[str, ...], scan_limit: int) -> list[dict[str, Any]]:
        now = utc_now()
        clauses = [
            "project=?",
            "environment=?",
            "classification_rank<=?",
            "invalidated_at IS NULL",
            "superseded_by_id IS NULL",
            "valid_from<=?",
            "(valid_until IS NULL OR valid_until>?)",
        ]
        parameters: list[Any] = [
            request.project,
            request.environment,
            classification_rank(request.max_classification),
            now,
            now,
        ]
        if request.categories:
            clauses.append(f"category IN ({','.join('?' for _ in request.categories)})")
            parameters.extend(request.categories)
        if request.kinds:
            clauses.append(f"kind IN ({','.join('?' for _ in request.kinds)})")
            parameters.extend(request.kinds)
        parameters.append(scan_limit)
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM memories WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?",
                parameters,
            ).fetchall()
        records = [_row_to_record(row) for row in rows]
        allowed = set(roles)
        query_tokens = set(tokenize(request.query))
        for record in records:
            record_tokens = set(tokenize(str(record["content"])))
            union = query_tokens | record_tokens
            record["semantic_score"] = len(query_tokens & record_tokens) / len(union) if union else 0.0
        visible = [
            record
            for record in records
            if allowed.intersection(record["allowed_roles"])
            and float(record["semantic_score"]) > 0
        ]
        visible.sort(
            key=lambda item: (
                -float(item["semantic_score"]),
                -float(item["importance"]),
                str(item["id"]),
            )
        )
        return visible[:scan_limit]

    def pending_for_reindex(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM memories
                   WHERE qdrant_state IN ('pending','failed')
                     AND invalidated_at IS NULL AND superseded_by_id IS NULL
                   ORDER BY updated_at LIMIT ?""",
                (limit,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def pending_deletions(self, limit: int = 500) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM memories WHERE qdrant_state='delete_pending' ORDER BY updated_at LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def link_summary(self, summary_id: str, member_ids: list[str]) -> None:
        with self.connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO summary_members(summary_id, member_id) VALUES(?, ?)",
                ((summary_id, item) for item in member_ids),
            )
            db.commit()

    def consolidate_lifecycle(self, stale_after_days: int) -> tuple[int, int]:
        now = datetime.now(UTC)
        now_text = now.isoformat(timespec="seconds")
        stale_before = (now - timedelta(days=stale_after_days)).isoformat(timespec="seconds")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            expired = db.execute(
                """UPDATE memories SET invalidated_at=?, invalid_reason='validity_expired',
                   qdrant_state='delete_pending', updated_at=?
                   WHERE valid_until IS NOT NULL AND valid_until<=? AND invalidated_at IS NULL""",
                (now_text, now_text, now_text),
            ).rowcount
            stale = db.execute(
                """UPDATE memories SET stale_at=?, updated_at=?
                   WHERE updated_at<? AND stale_at IS NULL AND invalidated_at IS NULL
                     AND superseded_by_id IS NULL AND kind IN ('observation','information','incident','summary')""",
                (now_text, now_text, stale_before),
            ).rowcount
            db.commit()
        return expired, stale

    def record_maintenance(self, started_at: str, expired: int, stale: int, reindexed: int, failures: int) -> str:
        run_id = str(uuid.uuid4())
        with self.connect() as db:
            db.execute(
                "INSERT INTO maintenance_runs VALUES(?, ?, ?, ?, ?, ?, ?)",
                (run_id, started_at, utc_now(), expired, stale, reindexed, failures),
            )
            db.commit()
        return run_id

    def stats(self) -> dict[str, Any]:
        with self.connect() as db:
            totals = db.execute(
                """SELECT COUNT(*) total,
                    SUM(CASE WHEN invalidated_at IS NULL AND superseded_by_id IS NULL THEN 1 ELSE 0 END) current_count,
                    SUM(CASE WHEN qdrant_state='indexed' THEN 1 ELSE 0 END) indexed_count,
                    SUM(CASE WHEN qdrant_state IN ('pending','failed') THEN 1 ELSE 0 END) pending,
                    SUM(CASE WHEN stale_at IS NOT NULL THEN 1 ELSE 0 END) stale
                    FROM memories"""
            ).fetchone()
            providers = db.execute(
                "SELECT provider, model, outcome, COUNT(*) count FROM provider_calls GROUP BY provider, model, outcome"
            ).fetchall()
        return {
            "total": int(totals["total"] or 0),
            "current": int(totals["current_count"] or 0),
            "indexed": int(totals["indexed_count"] or 0),
            "pending": int(totals["pending"] or 0),
            "stale": int(totals["stale"] or 0),
            "provider_calls": [dict(row) for row in providers],
        }
