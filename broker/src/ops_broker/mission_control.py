"""Durable V1 mission context, checkpoints, queueing and resource leases.

The broker keeps this state outside every model context.  Every mutating call
is project-scoped, idempotent where it can be retried over a transport, and
appended to the central hash-linked audit log.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from .database import Database
from .errors import AuthorizationDenied, BrokerError, Conflict, NotFound
from .policy import RBACPolicy
from .util import canonical_json, isoformat, parse_timestamp, redact_text, scrub_payload, utc_now


_ACTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")
_RESOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9:._/-]{1,191}$")
_MODEL_ROLE = re.compile(r"^ROLE_(?:TINY|LOCAL_OPS|REASONING|CODER|EMBEDDING|RERANKER)$")
_PROVIDER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ENVIRONMENT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_RECORD_KINDS = frozenset({"observation", "analysis", "decision", "handoff", "escalation"})
_TRACE_OUTCOMES = frozenset({"selected", "succeeded", "failed", "escalated", "refused"})


class MissionControl:
    """Project-scoped durable control plane state used by Hermes and MCP clients."""

    def __init__(
        self,
        database: Database,
        rbac: RBACPolicy,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.database = database
        self.rbac = rbac
        self.clock = clock

    def _now(self) -> tuple[datetime, str]:
        value = self.clock()
        return value, isoformat(value)

    @staticmethod
    def _actor(actor_id: str) -> str:
        if not isinstance(actor_id, str) or not _ACTOR_ID.fullmatch(actor_id):
            raise AuthorizationDenied("actor id is malformed")
        return actor_id

    @staticmethod
    def _request_id(request_id: str) -> str:
        try:
            return str(uuid.UUID(request_id))
        except (TypeError, ValueError) as exc:
            raise BrokerError(
                "invalid_request_id", "request_id must be a UUID", status_code=422
            ) from exc

    @staticmethod
    def _text(value: str, field: str, maximum: int, *, minimum: int = 0) -> str:
        if (
            not isinstance(value, str)
            or "\x00" in value
            or not minimum <= len(value) <= maximum
        ):
            raise BrokerError(
                "invalid_request", f"{field} must be bounded text", status_code=422
            )
        return redact_text(value)

    @staticmethod
    def _string_list(value: Iterable[str], field: str, *, maximum: int = 64) -> list[str]:
        if isinstance(value, (str, bytes)):
            raise BrokerError("invalid_request", f"{field} must be a list", status_code=422)
        try:
            result = list(value)
        except TypeError as exc:
            raise BrokerError(
                "invalid_request", f"{field} must be a list", status_code=422
            ) from exc
        if len(result) > maximum or not all(isinstance(item, str) for item in result):
            raise BrokerError("invalid_request", f"{field} is invalid", status_code=422)
        return [MissionControl._text(item, field, 1000) for item in result]

    @staticmethod
    def _json(value: Any, field: str, *, maximum_bytes: int = 32_768) -> str:
        clean = scrub_payload(value)
        try:
            encoded = canonical_json(clean)
        except (TypeError, ValueError) as exc:
            raise BrokerError(
                "invalid_request", f"{field} must be JSON-compatible", status_code=422
            ) from exc
        if len(encoded.encode("utf-8")) > maximum_bytes:
            raise BrokerError("invalid_request", f"{field} is too large", status_code=422)
        return encoded

    def _mission(self, connection: sqlite3.Connection, mission_id: str) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT m.*, r.*
            FROM missions AS m
            JOIN mission_runtime AS r ON r.mission_id = m.id
            WHERE m.id = ?
            """,
            (mission_id,),
        ).fetchone()
        if row is None:
            raise NotFound("mission")
        return row

    @staticmethod
    def _runtime_public(row: sqlite3.Row, *, replayed: bool | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": str(row["id"])[:36],
            "project_id": str(row["project_id"])[:64],
            "title": str(row["title"])[:240],
            "status": str(row["status"])[:16],
            "environment": str(row["environment"])[:64],
            "priority": int(row["priority"]),
            "queue_state": str(row["queue_state"])[:16],
            "objective": str(row["objective"])[:4000],
            "summary": str(row["summary"])[:4000],
            "impact": str(row["impact"])[:2000],
            "risk": str(row["risk"])[:2000],
            "plan": json.loads(row["plan_json"]),
            "current_step": str(row["current_step"])[:1000],
            "remaining_steps": json.loads(row["remaining_steps_json"]),
            "resources": json.loads(row["resources_json"]),
            "files": json.loads(row["files_json"]),
            "next_action": str(row["next_action"])[:1000],
            "current_model_role": row["current_model_role"],
            "current_model": row["current_model"],
            "iteration_count": int(row["iteration_count"]),
            "max_iterations": int(row["max_iterations"]),
            "api_call_count": int(row["api_call_count"]),
            "max_api_calls": int(row["max_api_calls"]),
            "deadline": row["deadline"],
            "claimed_by": row["claimed_by"],
            "lease_until": row["lease_until"],
            "created_at": str(row["created_at"])[:40],
            "updated_at": str(row["updated_at"])[:40],
        }
        if replayed is not None:
            result["replayed"] = replayed
        return result

    def get_mission_state(self, actor_id: str, mission_id: str) -> dict[str, Any]:
        actor_id = self._actor(actor_id)
        with self.database.read() as connection:
            row = self._mission(connection, mission_id)
        self.rbac.require_project(actor_id, str(row["project_id"]))
        return self._runtime_public(row)

    def update_mission_state(
        self,
        *,
        actor_id: str,
        request_id: str,
        mission_id: str,
        environment: str | None = None,
        priority: int | None = None,
        objective: str | None = None,
        summary: str | None = None,
        impact: str | None = None,
        risk: str | None = None,
        plan: list[str] | None = None,
        current_step: str | None = None,
        remaining_steps: list[str] | None = None,
        resources: list[str] | None = None,
        files: list[str] | None = None,
        next_action: str | None = None,
        current_model_role: str | None = None,
        current_model: str | None = None,
        max_iterations: int | None = None,
        max_api_calls: int | None = None,
        deadline: str | None = None,
    ) -> dict[str, Any]:
        actor_id = self._actor(actor_id)
        request_id = self._request_id(request_id)
        values: dict[str, Any] = {}
        if environment is not None:
            if not isinstance(environment, str) or not _ENVIRONMENT.fullmatch(environment):
                raise BrokerError("invalid_request", "environment is malformed", status_code=422)
            values["environment"] = environment
        if priority is not None:
            if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 100:
                raise BrokerError("invalid_request", "priority must be 0..100", status_code=422)
            values["priority"] = priority
        for name, value, maximum in (
            ("objective", objective, 4000),
            ("summary", summary, 4000),
            ("impact", impact, 2000),
            ("risk", risk, 2000),
            ("current_step", current_step, 1000),
            ("next_action", next_action, 1000),
            ("current_model", current_model, 200),
        ):
            if value is not None:
                values[name] = self._text(value, name, maximum)
        for name, value in (
            ("plan_json", plan),
            ("remaining_steps_json", remaining_steps),
            ("resources_json", resources),
            ("files_json", files),
        ):
            if value is not None:
                values[name] = self._json(self._string_list(value, name), name)
        if current_model_role is not None:
            if not isinstance(current_model_role, str) or not _MODEL_ROLE.fullmatch(current_model_role):
                raise BrokerError("invalid_request", "current_model_role is invalid", status_code=422)
            values["current_model_role"] = current_model_role
        if max_iterations is not None:
            if isinstance(max_iterations, bool) or not isinstance(max_iterations, int) or not 1 <= max_iterations <= 100:
                raise BrokerError("invalid_request", "max_iterations must be 1..100", status_code=422)
            values["max_iterations"] = max_iterations
        if max_api_calls is not None:
            if isinstance(max_api_calls, bool) or not isinstance(max_api_calls, int) or not 0 <= max_api_calls <= 100:
                raise BrokerError("invalid_request", "max_api_calls must be 0..100", status_code=422)
            values["max_api_calls"] = max_api_calls
        if deadline is not None:
            try:
                parse_timestamp(deadline)
            except (TypeError, ValueError) as exc:
                raise BrokerError("invalid_request", "deadline is invalid", status_code=422) from exc
            values["deadline"] = deadline
        if not values:
            raise BrokerError("invalid_request", "at least one mission field is required", status_code=422)

        payload_json = self._json(values, "mission update")
        _, now = self._now()
        with self.database.transaction() as connection:
            row = self._mission(connection, mission_id)
            self.rbac.require_lifecycle(actor_id, str(row["project_id"]), "mission.context.write")
            prior = connection.execute(
                "SELECT * FROM mission_updates WHERE request_id = ?", (request_id,)
            ).fetchone()
            if prior is not None:
                if (
                    prior["mission_id"] == mission_id
                    and prior["actor_id"] == actor_id
                    and prior["payload_json"] == payload_json
                ):
                    return self._runtime_public(row, replayed=True)
                raise Conflict("request_id was already used for another mission update")
            assignments = ", ".join(f"{column} = ?" for column in values)
            connection.execute(
                f"UPDATE mission_runtime SET {assignments}, updated_at = ? WHERE mission_id = ?",
                (*values.values(), now, mission_id),
            )
            connection.execute(
                """
                INSERT INTO mission_updates(request_id, mission_id, actor_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (request_id, mission_id, actor_id, payload_json, now),
            )
            self.database.append_event(
                connection,
                occurred_at=now,
                event_type="mission.context_updated",
                actor_id=actor_id,
                entity_type="mission",
                entity_id=mission_id,
                payload={"request_id": request_id, "fields": sorted(values)},
            )
            updated = self._mission(connection, mission_id)
        return self._runtime_public(updated, replayed=False)

    def create_checkpoint(
        self,
        *,
        actor_id: str,
        request_id: str,
        mission_id: str,
        phase: str,
        summary: str,
        decisions: list[str],
        completed: list[str],
        pending: list[str],
        evidence: list[str],
        model_role: str | None = None,
        model_name: str | None = None,
    ) -> dict[str, Any]:
        actor_id = self._actor(actor_id)
        request_id = self._request_id(request_id)
        phase = self._text(phase, "phase", 120, minimum=1)
        summary = self._text(summary, "summary", 4000, minimum=1)
        decisions_json = self._json(self._string_list(decisions, "decisions"), "decisions")
        completed_json = self._json(self._string_list(completed, "completed"), "completed")
        pending_list = self._string_list(pending, "pending")
        pending_json = self._json(pending_list, "pending")
        evidence_json = self._json(self._string_list(evidence, "evidence"), "evidence")
        if model_role is not None and not _MODEL_ROLE.fullmatch(model_role):
            raise BrokerError("invalid_request", "model_role is invalid", status_code=422)
        if model_name is not None:
            model_name = self._text(model_name, "model_name", 200)
        checkpoint_id = str(uuid.uuid4())
        _, now = self._now()
        with self.database.transaction() as connection:
            mission = self._mission(connection, mission_id)
            self.rbac.require_lifecycle(
                actor_id, str(mission["project_id"]), "mission.checkpoint.write"
            )
            prior = connection.execute(
                "SELECT * FROM mission_checkpoints WHERE request_id = ?", (request_id,)
            ).fetchone()
            if prior is not None:
                expected = (
                    mission_id,
                    actor_id,
                    phase,
                    summary,
                    decisions_json,
                    completed_json,
                    pending_json,
                    evidence_json,
                    model_role,
                    model_name,
                )
                actual = tuple(
                    prior[key]
                    for key in (
                        "mission_id", "actor_id", "phase", "summary", "decisions_json",
                        "completed_json", "pending_json", "evidence_json", "model_role", "model_name",
                    )
                )
                if actual != expected:
                    raise Conflict("request_id was already used for another checkpoint")
                return self._checkpoint_public(prior, replayed=True)
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM mission_checkpoints WHERE mission_id = ?",
                    (mission_id,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO mission_checkpoints(
                    id, request_id, mission_id, sequence, actor_id, phase, summary,
                    decisions_json, completed_json, pending_json, evidence_json,
                    model_role, model_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id, request_id, mission_id, sequence, actor_id, phase, summary,
                    decisions_json, completed_json, pending_json, evidence_json,
                    model_role, model_name, now,
                ),
            )
            connection.execute(
                """
                UPDATE mission_runtime
                SET summary = ?, current_step = ?, remaining_steps_json = ?,
                    next_action = ?, current_model_role = COALESCE(?, current_model_role),
                    current_model = COALESCE(?, current_model), updated_at = ?
                WHERE mission_id = ?
                """,
                (
                    summary,
                    phase,
                    pending_json,
                    pending_list[0] if pending_list else "",
                    model_role,
                    model_name,
                    now,
                    mission_id,
                ),
            )
            self.database.append_event(
                connection,
                occurred_at=now,
                event_type="mission.checkpoint_created",
                actor_id=actor_id,
                entity_type="mission",
                entity_id=mission_id,
                payload={"checkpoint_id": checkpoint_id, "sequence": sequence, "phase": phase},
            )
            row = connection.execute(
                "SELECT * FROM mission_checkpoints WHERE id = ?", (checkpoint_id,)
            ).fetchone()
        assert row is not None
        return self._checkpoint_public(row, replayed=False)

    @staticmethod
    def _checkpoint_public(row: sqlite3.Row, *, replayed: bool = False) -> dict[str, Any]:
        return {
            "id": str(row["id"])[:36],
            "mission_id": str(row["mission_id"])[:36],
            "sequence": int(row["sequence"]),
            "phase": str(row["phase"])[:120],
            "summary": str(row["summary"])[:4000],
            "decisions": json.loads(row["decisions_json"]),
            "completed": json.loads(row["completed_json"]),
            "pending": json.loads(row["pending_json"]),
            "evidence": json.loads(row["evidence_json"]),
            "model_role": row["model_role"],
            "model_name": row["model_name"],
            "created_at": str(row["created_at"])[:40],
            "replayed": replayed,
        }

    def list_checkpoints(
        self, actor_id: str, mission_id: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        actor_id = self._actor(actor_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise BrokerError("invalid_request", "limit must be 1..50", status_code=422)
        with self.database.read() as connection:
            mission = self._mission(connection, mission_id)
            self.rbac.require_project(actor_id, str(mission["project_id"]))
            rows = connection.execute(
                """
                SELECT * FROM mission_checkpoints WHERE mission_id = ?
                ORDER BY sequence DESC LIMIT ?
                """,
                (mission_id, limit),
            ).fetchall()
        return [self._checkpoint_public(row) for row in rows]

    def add_record(
        self,
        *,
        actor_id: str,
        request_id: str,
        mission_id: str,
        kind: str,
        content: str,
        evidence: list[str],
        incident_id: str | None = None,
        confidence: float | None = None,
        model_trace_id: str | None = None,
    ) -> dict[str, Any]:
        actor_id = self._actor(actor_id)
        request_id = self._request_id(request_id)
        if kind not in _RECORD_KINDS:
            raise BrokerError("invalid_request", "record kind is invalid", status_code=422)
        content = self._text(content, "content", 8000, minimum=1)
        evidence_json = self._json(self._string_list(evidence, "evidence"), "evidence")
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (float, int))
            or not 0.0 <= float(confidence) <= 1.0
        ):
            raise BrokerError("invalid_request", "confidence must be 0..1", status_code=422)
        record_id = str(uuid.uuid4())
        _, now = self._now()
        with self.database.transaction() as connection:
            mission = self._mission(connection, mission_id)
            project_id = str(mission["project_id"])
            self.rbac.require_lifecycle(actor_id, project_id, "mission.record.write")
            if incident_id is not None:
                incident = connection.execute(
                    "SELECT project_id FROM incidents WHERE id = ?", (incident_id,)
                ).fetchone()
                if incident is None:
                    raise NotFound("incident")
                if incident["project_id"] != project_id:
                    raise Conflict("incident belongs to another project")
            prior = connection.execute(
                "SELECT * FROM mission_records WHERE request_id = ?", (request_id,)
            ).fetchone()
            if prior is not None:
                expected_confidence = None if confidence is None else float(confidence)
                if (
                    prior["mission_id"] != mission_id
                    or prior["incident_id"] != incident_id
                    or prior["actor_id"] != actor_id
                    or prior["kind"] != kind
                    or prior["content"] != content
                    or prior["confidence"] != expected_confidence
                    or prior["evidence_json"] != evidence_json
                    or prior["model_trace_id"] != model_trace_id
                ):
                    raise Conflict("request_id was already used for another mission record")
                return self._record_public(prior, replayed=True)
            connection.execute(
                """
                INSERT INTO mission_records(
                    id, request_id, mission_id, incident_id, actor_id, kind, content,
                    confidence, evidence_json, model_trace_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id, request_id, mission_id, incident_id, actor_id, kind, content,
                    None if confidence is None else float(confidence), evidence_json,
                    model_trace_id, now,
                ),
            )
            self.database.append_event(
                connection,
                occurred_at=now,
                event_type=f"mission.{kind}_recorded",
                actor_id=actor_id,
                entity_type="mission",
                entity_id=mission_id,
                payload={"record_id": record_id, "confidence": confidence},
            )
            row = connection.execute(
                "SELECT * FROM mission_records WHERE id = ?", (record_id,)
            ).fetchone()
        assert row is not None
        return self._record_public(row, replayed=False)

    @staticmethod
    def _record_public(row: sqlite3.Row, *, replayed: bool = False) -> dict[str, Any]:
        return {
            "id": str(row["id"])[:36],
            "mission_id": str(row["mission_id"])[:36],
            "incident_id": row["incident_id"],
            "kind": str(row["kind"])[:16],
            "content": str(row["content"])[:8000],
            "confidence": row["confidence"],
            "evidence": json.loads(row["evidence_json"]),
            "model_trace_id": row["model_trace_id"],
            "created_at": str(row["created_at"])[:40],
            "replayed": replayed,
        }

    def list_records(
        self, actor_id: str, mission_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        actor_id = self._actor(actor_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise BrokerError("invalid_request", "limit must be 1..100", status_code=422)
        with self.database.read() as connection:
            mission = self._mission(connection, mission_id)
            self.rbac.require_project(actor_id, str(mission["project_id"]))
            rows = connection.execute(
                """
                SELECT * FROM mission_records WHERE mission_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (mission_id, limit),
            ).fetchall()
        return [self._record_public(row) for row in rows]

    def claim_next(
        self,
        *,
        actor_id: str,
        request_id: str,
        lease_seconds: int = 300,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        actor_id = self._actor(actor_id)
        request_id = self._request_id(request_id)
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 30 <= lease_seconds <= 3600:
            raise BrokerError("invalid_request", "lease_seconds must be 30..3600", status_code=422)
        projects = self.rbac.projects_for_actor(actor_id)
        if project_id is not None:
            self.rbac.require_project(actor_id, project_id)
            projects = frozenset({project_id})
        if not projects:
            return None
        now_dt, now = self._now()
        lease_until = isoformat(now_dt + timedelta(seconds=lease_seconds))
        with self.database.transaction() as connection:
            prior = connection.execute(
                "SELECT * FROM mission_claim_operations WHERE request_id = ?", (request_id,)
            ).fetchone()
            if prior is not None:
                result = json.loads(prior["result_json"])
                result["replayed"] = True
                return result or None
            placeholders = ",".join("?" for _ in projects)
            candidates = connection.execute(
                f"""
                SELECT m.id, m.project_id
                FROM missions AS m JOIN mission_runtime AS r ON r.mission_id = m.id
                WHERE m.status = 'open' AND m.project_id IN ({placeholders})
                  AND (r.queue_state = 'queued' OR (r.queue_state = 'claimed' AND r.lease_until <= ?))
                ORDER BY r.priority DESC, m.created_at ASC, m.id ASC
                LIMIT 1
                """,
                (*sorted(projects), now),
            ).fetchone()
            if candidates is None:
                result_json = "null"
                connection.execute(
                    """
                    INSERT INTO mission_claim_operations(
                        request_id, mission_id, actor_id, operation, result_json, created_at
                    ) VALUES (?, NULL, ?, 'claim', ?, ?)
                    """,
                    (request_id, actor_id, result_json, now),
                )
                return None
            self.rbac.require_lifecycle(
                actor_id, str(candidates["project_id"]), "mission.queue.claim"
            )
            connection.execute(
                """
                UPDATE mission_runtime
                SET queue_state = 'claimed', claimed_by = ?, claimed_at = ?,
                    heartbeat_at = ?, lease_until = ?, updated_at = ?
                WHERE mission_id = ?
                """,
                (actor_id, now, now, lease_until, now, candidates["id"]),
            )
            claimed = self._mission(connection, str(candidates["id"]))
            result = self._runtime_public(claimed, replayed=False)
            connection.execute(
                """
                INSERT INTO mission_claim_operations(
                    request_id, mission_id, actor_id, operation, result_json, created_at
                ) VALUES (?, ?, ?, 'claim', ?, ?)
                """,
                (request_id, candidates["id"], actor_id, self._json(result, "claim result"), now),
            )
            self.database.append_event(
                connection,
                occurred_at=now,
                event_type="mission.claimed",
                actor_id=actor_id,
                entity_type="mission",
                entity_id=str(candidates["id"]),
                payload={"request_id": request_id, "lease_until": lease_until},
            )
        return result

    def mission_lease(
        self,
        *,
        actor_id: str,
        request_id: str,
        mission_id: str,
        operation: str,
        lease_seconds: int = 300,
    ) -> dict[str, Any]:
        actor_id = self._actor(actor_id)
        request_id = self._request_id(request_id)
        if operation not in {"heartbeat", "release"}:
            raise BrokerError("invalid_request", "lease operation is invalid", status_code=422)
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 30 <= lease_seconds <= 3600:
            raise BrokerError("invalid_request", "lease_seconds must be 30..3600", status_code=422)
        now_dt, now = self._now()
        with self.database.transaction() as connection:
            mission = self._mission(connection, mission_id)
            self.rbac.require_lifecycle(
                actor_id, str(mission["project_id"]), "mission.queue.claim"
            )
            prior = connection.execute(
                "SELECT * FROM mission_claim_operations WHERE request_id = ?", (request_id,)
            ).fetchone()
            if prior is not None:
                if prior["mission_id"] != mission_id or prior["actor_id"] != actor_id or prior["operation"] != operation:
                    raise Conflict("request_id was already used for another lease operation")
                result = json.loads(prior["result_json"])
                result["replayed"] = True
                return result
            if mission["queue_state"] != "claimed" or mission["claimed_by"] != actor_id:
                raise Conflict("mission is not claimed by this actor")
            if operation == "heartbeat":
                lease_until = isoformat(now_dt + timedelta(seconds=lease_seconds))
                connection.execute(
                    """
                    UPDATE mission_runtime SET heartbeat_at = ?, lease_until = ?, updated_at = ?
                    WHERE mission_id = ?
                    """,
                    (now, lease_until, now, mission_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE mission_runtime
                    SET queue_state = CASE WHEN (SELECT status FROM missions WHERE id = ?) = 'completed'
                                         THEN 'done' ELSE 'queued' END,
                        claimed_by = NULL, claimed_at = NULL, heartbeat_at = NULL,
                        lease_until = NULL, updated_at = ?
                    WHERE mission_id = ?
                    """,
                    (mission_id, now, mission_id),
                )
            updated = self._mission(connection, mission_id)
            result = self._runtime_public(updated, replayed=False)
            connection.execute(
                """
                INSERT INTO mission_claim_operations(
                    request_id, mission_id, actor_id, operation, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (request_id, mission_id, actor_id, operation, self._json(result, "lease result"), now),
            )
            self.database.append_event(
                connection,
                occurred_at=now,
                event_type=f"mission.{operation}",
                actor_id=actor_id,
                entity_type="mission",
                entity_id=mission_id,
                payload={"request_id": request_id},
            )
        return result

    def acquire_resource_lock(
        self,
        *,
        actor_id: str,
        request_id: str,
        mission_id: str,
        resource_id: str,
        lease_seconds: int = 300,
    ) -> dict[str, Any]:
        return self._resource_lock_operation(
            actor_id=actor_id,
            request_id=request_id,
            mission_id=mission_id,
            resource_id=resource_id,
            operation="acquire",
            lease_seconds=lease_seconds,
        )

    def resource_lock_lease(
        self,
        *,
        actor_id: str,
        request_id: str,
        mission_id: str,
        resource_id: str,
        operation: str,
        lease_seconds: int = 300,
    ) -> dict[str, Any]:
        if operation not in {"renew", "release"}:
            raise BrokerError("invalid_request", "lock operation is invalid", status_code=422)
        return self._resource_lock_operation(
            actor_id=actor_id,
            request_id=request_id,
            mission_id=mission_id,
            resource_id=resource_id,
            operation=operation,
            lease_seconds=lease_seconds,
        )

    def _resource_lock_operation(
        self,
        *,
        actor_id: str,
        request_id: str,
        mission_id: str,
        resource_id: str,
        operation: str,
        lease_seconds: int,
    ) -> dict[str, Any]:
        actor_id = self._actor(actor_id)
        request_id = self._request_id(request_id)
        if not isinstance(resource_id, str) or not _RESOURCE_ID.fullmatch(resource_id):
            raise BrokerError("invalid_request", "resource_id is malformed", status_code=422)
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 30 <= lease_seconds <= 3600:
            raise BrokerError("invalid_request", "lease_seconds must be 30..3600", status_code=422)
        now_dt, now = self._now()
        lease_until = isoformat(now_dt + timedelta(seconds=lease_seconds))
        with self.database.transaction() as connection:
            mission = self._mission(connection, mission_id)
            project_id = str(mission["project_id"])
            self.rbac.require_lifecycle(actor_id, project_id, "resource.lock")
            prior_op = connection.execute(
                "SELECT * FROM resource_lock_operations WHERE request_id = ?", (request_id,)
            ).fetchone()
            if prior_op is not None:
                if (
                    prior_op["resource_id"] != resource_id
                    or prior_op["mission_id"] != mission_id
                    or prior_op["actor_id"] != actor_id
                    or prior_op["operation"] != operation
                ):
                    raise Conflict("request_id was already used for another lock operation")
                result = json.loads(prior_op["result_json"])
                result["replayed"] = True
                return result
            lock = connection.execute(
                "SELECT * FROM resource_locks WHERE resource_id = ?", (resource_id,)
            ).fetchone()
            if operation == "acquire":
                if lock is not None and parse_timestamp(str(lock["lease_until"])) > now_dt and (
                    lock["mission_id"] != mission_id or lock["holder_actor_id"] != actor_id
                ):
                    raise Conflict("resource is locked by another mission")
                token = 1 if lock is None else int(lock["fencing_token"])
                if lock is not None and (
                    lock["mission_id"] != mission_id or lock["holder_actor_id"] != actor_id
                ):
                    token += 1
                connection.execute(
                    """
                    INSERT INTO resource_locks(
                        resource_id, project_id, mission_id, holder_actor_id, fencing_token,
                        acquired_at, heartbeat_at, lease_until
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(resource_id) DO UPDATE SET
                        project_id=excluded.project_id, mission_id=excluded.mission_id,
                        holder_actor_id=excluded.holder_actor_id,
                        fencing_token=excluded.fencing_token, acquired_at=excluded.acquired_at,
                        heartbeat_at=excluded.heartbeat_at, lease_until=excluded.lease_until
                    """,
                    (resource_id, project_id, mission_id, actor_id, token, now, now, lease_until),
                )
                lock = connection.execute(
                    "SELECT * FROM resource_locks WHERE resource_id = ?", (resource_id,)
                ).fetchone()
                assert lock is not None
                result = self._lock_public(lock, replayed=False)
            else:
                if lock is None:
                    raise NotFound("resource lock")
                if lock["mission_id"] != mission_id or lock["holder_actor_id"] != actor_id:
                    raise AuthorizationDenied("resource lock belongs to another mission or actor")
                if operation == "renew":
                    connection.execute(
                        "UPDATE resource_locks SET heartbeat_at = ?, lease_until = ? WHERE resource_id = ?",
                        (now, lease_until, resource_id),
                    )
                    lock = connection.execute(
                        "SELECT * FROM resource_locks WHERE resource_id = ?", (resource_id,)
                    ).fetchone()
                    assert lock is not None
                    result = self._lock_public(lock, replayed=False)
                else:
                    result = self._lock_public(lock, replayed=False)
                    result["released"] = True
                    connection.execute("DELETE FROM resource_locks WHERE resource_id = ?", (resource_id,))
            result_json = self._json(result, "lock result")
            connection.execute(
                """
                INSERT INTO resource_lock_operations(
                    request_id, resource_id, mission_id, actor_id, operation, result_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (request_id, resource_id, mission_id, actor_id, operation, result_json, now),
            )
            self.database.append_event(
                connection,
                occurred_at=now,
                event_type=f"resource_lock.{operation}",
                actor_id=actor_id,
                entity_type="resource",
                entity_id=resource_id,
                payload={"mission_id": mission_id, "request_id": request_id},
            )
        return result

    @staticmethod
    def _lock_public(row: sqlite3.Row, *, replayed: bool = False) -> dict[str, Any]:
        return {
            "resource_id": str(row["resource_id"])[:192],
            "project_id": str(row["project_id"])[:64],
            "mission_id": str(row["mission_id"])[:36],
            "holder_actor_id": str(row["holder_actor_id"])[:128],
            "fencing_token": int(row["fencing_token"]),
            "acquired_at": str(row["acquired_at"])[:40],
            "heartbeat_at": str(row["heartbeat_at"])[:40],
            "lease_until": str(row["lease_until"])[:40],
            "replayed": replayed,
        }

    def record_model_trace(
        self,
        *,
        actor_id: str,
        request_id: str,
        mission_id: str,
        model_role: str,
        provider: str,
        model_name: str,
        reason: str,
        outcome: str,
        confidence: float | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_microunits: int = 0,
        parent_trace_id: str | None = None,
        finished_at: str | None = None,
    ) -> dict[str, Any]:
        actor_id = self._actor(actor_id)
        request_id = self._request_id(request_id)
        if not _MODEL_ROLE.fullmatch(model_role):
            raise BrokerError("invalid_request", "model_role is invalid", status_code=422)
        if not isinstance(provider, str) or not _PROVIDER.fullmatch(provider):
            raise BrokerError("invalid_request", "provider is malformed", status_code=422)
        model_name = self._text(model_name, "model_name", 200, minimum=1)
        reason = self._text(reason, "reason", 1000, minimum=1)
        if outcome not in _TRACE_OUTCOMES:
            raise BrokerError("invalid_request", "outcome is invalid", status_code=422)
        if confidence is not None and (
            isinstance(confidence, bool)
            or not isinstance(confidence, (float, int))
            or not 0.0 <= float(confidence) <= 1.0
        ):
            raise BrokerError("invalid_request", "confidence must be 0..1", status_code=422)
        for name, value in (
            ("input_tokens", input_tokens),
            ("output_tokens", output_tokens),
            ("cost_microunits", cost_microunits),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BrokerError("invalid_request", f"{name} must be non-negative", status_code=422)
        if finished_at is not None:
            try:
                parse_timestamp(finished_at)
            except (TypeError, ValueError) as exc:
                raise BrokerError("invalid_request", "finished_at is invalid", status_code=422) from exc
        trace_id = str(uuid.uuid4())
        _, now = self._now()
        remote = provider not in {"local", "ollama", "deterministic"}
        with self.database.transaction() as connection:
            mission = self._mission(connection, mission_id)
            project_id = str(mission["project_id"])
            self.rbac.require_lifecycle(actor_id, project_id, "model.trace.write")
            prior = connection.execute(
                "SELECT * FROM model_traces WHERE request_id = ?", (request_id,)
            ).fetchone()
            if prior is not None:
                return self._trace_public(prior, replayed=True)
            if int(mission["iteration_count"]) >= int(mission["max_iterations"]):
                raise BrokerError(
                    "iteration_budget_exhausted",
                    "mission iteration budget is exhausted; human escalation is required",
                    status_code=429,
                )
            if remote and int(mission["api_call_count"]) >= int(mission["max_api_calls"]):
                raise BrokerError(
                    "api_call_budget_exhausted",
                    "mission API call budget is exhausted; human escalation is required",
                    status_code=429,
                )
            if parent_trace_id is not None:
                parent = connection.execute(
                    "SELECT mission_id FROM model_traces WHERE id = ?", (parent_trace_id,)
                ).fetchone()
                if parent is None or parent["mission_id"] != mission_id:
                    raise Conflict("parent trace is missing or belongs to another mission")
            connection.execute(
                """
                INSERT INTO model_traces(
                    id, request_id, mission_id, parent_trace_id, actor_id, model_role,
                    provider, model_name, reason, outcome, confidence, input_tokens,
                    output_tokens, cost_microunits, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id, request_id, mission_id, parent_trace_id, actor_id, model_role,
                    provider, model_name, reason, outcome,
                    None if confidence is None else float(confidence), input_tokens,
                    output_tokens, cost_microunits, now, finished_at,
                ),
            )
            connection.execute(
                """
                UPDATE mission_runtime
                SET current_model_role = ?, current_model = ?,
                    iteration_count = iteration_count + 1,
                    api_call_count = api_call_count + ?, updated_at = ?
                WHERE mission_id = ?
                """,
                (model_role, model_name, 1 if remote else 0, now, mission_id),
            )
            self.database.append_event(
                connection,
                occurred_at=now,
                event_type="model.trace_recorded",
                actor_id=actor_id,
                entity_type="mission",
                entity_id=mission_id,
                payload={
                    "trace_id": trace_id,
                    "parent_trace_id": parent_trace_id,
                    "model_role": model_role,
                    "provider": provider,
                    "model_name": model_name,
                    "reason": reason,
                    "outcome": outcome,
                    "confidence": confidence,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost_microunits": cost_microunits,
                },
            )
            row = connection.execute(
                "SELECT * FROM model_traces WHERE id = ?", (trace_id,)
            ).fetchone()
        assert row is not None
        return self._trace_public(row, replayed=False)

    @staticmethod
    def _trace_public(row: sqlite3.Row, *, replayed: bool = False) -> dict[str, Any]:
        return {
            "id": str(row["id"])[:36],
            "mission_id": str(row["mission_id"])[:36],
            "parent_trace_id": row["parent_trace_id"],
            "model_role": str(row["model_role"])[:32],
            "provider": str(row["provider"])[:64],
            "model_name": str(row["model_name"])[:200],
            "reason": str(row["reason"])[:1000],
            "outcome": str(row["outcome"])[:16],
            "confidence": row["confidence"],
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "cost_microunits": int(row["cost_microunits"]),
            "started_at": str(row["started_at"])[:40],
            "finished_at": row["finished_at"],
            "replayed": replayed,
        }

    def list_model_traces(
        self, actor_id: str, mission_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        actor_id = self._actor(actor_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise BrokerError("invalid_request", "limit must be 1..100", status_code=422)
        with self.database.read() as connection:
            mission = self._mission(connection, mission_id)
            self.rbac.require_project(actor_id, str(mission["project_id"]))
            rows = connection.execute(
                """
                SELECT * FROM model_traces WHERE mission_id = ?
                ORDER BY started_at ASC, id ASC LIMIT ?
                """,
                (mission_id, limit),
            ).fetchall()
        return [self._trace_public(row) for row in rows]

    def update_incident_details(
        self,
        *,
        actor_id: str,
        request_id: str,
        incident_id: str,
        service: str | None = None,
        resource_id: str | None = None,
        environment: str | None = None,
        symptoms: str | None = None,
        impact: str | None = None,
        probable_causes: list[str] | None = None,
        confirmed_cause: str | None = None,
        resolution: str | None = None,
        rollback: str | None = None,
        recommendations: list[str] | None = None,
        versions: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        actor_id = self._actor(actor_id)
        request_id = self._request_id(request_id)
        values: dict[str, Any] = {}
        for name, value, maximum in (
            ("service", service, 200),
            ("resource_id", resource_id, 192),
            ("symptoms", symptoms, 8000),
            ("impact", impact, 4000),
            ("confirmed_cause", confirmed_cause, 4000),
            ("resolution", resolution, 8000),
            ("rollback", rollback, 4000),
        ):
            if value is not None:
                values[name] = self._text(value, name, maximum)
        if environment is not None:
            if not _ENVIRONMENT.fullmatch(environment):
                raise BrokerError("invalid_request", "environment is malformed", status_code=422)
            values["environment"] = environment
        for name, value in (
            ("probable_causes_json", probable_causes),
            ("recommendations_json", recommendations),
        ):
            if value is not None:
                values[name] = self._json(self._string_list(value, name), name)
        if versions is not None:
            if not isinstance(versions, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in versions.items()
            ):
                raise BrokerError("invalid_request", "versions must map strings", status_code=422)
            values["versions_json"] = self._json(versions, "versions")
        if not values:
            raise BrokerError("invalid_request", "at least one incident field is required", status_code=422)
        payload_json = self._json(values, "incident update")
        _, now = self._now()
        with self.database.transaction() as connection:
            incident = connection.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,)
            ).fetchone()
            if incident is None:
                raise NotFound("incident")
            self.rbac.require_lifecycle(
                actor_id, str(incident["project_id"]), "incident.detail.write"
            )
            prior = connection.execute(
                "SELECT * FROM incident_updates WHERE request_id = ?", (request_id,)
            ).fetchone()
            if prior is not None:
                if prior["incident_id"] != incident_id or prior["actor_id"] != actor_id or prior["payload_json"] != payload_json:
                    raise Conflict("request_id was already used for another incident update")
                row = connection.execute(
                    """
                    SELECT i.*, d.* FROM incidents AS i JOIN incident_details AS d
                    ON d.incident_id = i.id WHERE i.id = ?
                    """,
                    (incident_id,),
                ).fetchone()
                assert row is not None
                return self._incident_public(row, replayed=True)
            assignments = ", ".join(f"{column} = ?" for column in values)
            connection.execute(
                f"UPDATE incident_details SET {assignments}, updated_at = ? WHERE incident_id = ?",
                (*values.values(), now, incident_id),
            )
            connection.execute(
                """
                INSERT INTO incident_updates(request_id, incident_id, actor_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (request_id, incident_id, actor_id, payload_json, now),
            )
            self.database.append_event(
                connection,
                occurred_at=now,
                event_type="incident.details_updated",
                actor_id=actor_id,
                entity_type="incident",
                entity_id=incident_id,
                payload={"request_id": request_id, "fields": sorted(values)},
            )
            row = connection.execute(
                """
                SELECT i.*, d.* FROM incidents AS i JOIN incident_details AS d
                ON d.incident_id = i.id WHERE i.id = ?
                """,
                (incident_id,),
            ).fetchone()
        assert row is not None
        return self._incident_public(row, replayed=False)

    def get_incident_details(self, actor_id: str, incident_id: str) -> dict[str, Any]:
        actor_id = self._actor(actor_id)
        with self.database.read() as connection:
            row = connection.execute(
                """
                SELECT i.*, d.* FROM incidents AS i JOIN incident_details AS d
                ON d.incident_id = i.id WHERE i.id = ?
                """,
                (incident_id,),
            ).fetchone()
        if row is None:
            raise NotFound("incident")
        self.rbac.require_project(actor_id, str(row["project_id"]))
        return self._incident_public(row)

    def record_action_artifacts(
        self,
        *,
        actor_id: str,
        request_id: str,
        action_id: str,
        before_digest: str | None = None,
        after_digest: str | None = None,
        target_version: str | None = None,
        rollback_ref: str | None = None,
        diff_artifact: str | None = None,
        healthcheck: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Attach immutable before/after evidence to one authorized action."""

        actor_id = self._actor(actor_id)
        request_id = self._request_id(request_id)
        values: dict[str, Any] = {}
        for name, value, maximum in (
            ("before_digest", before_digest, 128),
            ("after_digest", after_digest, 128),
            ("target_version", target_version, 200),
            ("rollback_ref", rollback_ref, 1000),
            ("diff_artifact", diff_artifact, 1000),
        ):
            if value is not None:
                values[name] = self._text(value, name, maximum, minimum=1)
        if healthcheck is not None:
            if not isinstance(healthcheck, dict):
                raise BrokerError("invalid_request", "healthcheck must be an object", status_code=422)
            values["healthcheck_json"] = self._json(healthcheck, "healthcheck")
        if not values:
            raise BrokerError("invalid_request", "at least one action artifact is required", status_code=422)
        payload_json = self._json(values, "action artifacts")
        _, now = self._now()
        with self.database.transaction() as connection:
            action = connection.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
            if action is None:
                raise NotFound("action")
            self.rbac.require_project(actor_id, str(action["project_id"]))
            if action["requested_by"] != actor_id:
                raise AuthorizationDenied("only the action requester may attach execution artifacts")
            prior_update = connection.execute(
                "SELECT * FROM action_artifact_updates WHERE request_id = ?", (request_id,)
            ).fetchone()
            if prior_update is not None:
                if (
                    prior_update["action_id"] != action_id
                    or prior_update["actor_id"] != actor_id
                    or prior_update["payload_json"] != payload_json
                ):
                    raise Conflict("request_id was already used for other action artifacts")
                row = connection.execute(
                    "SELECT * FROM action_artifacts WHERE action_id = ?", (action_id,)
                ).fetchone()
                assert row is not None
                return self._action_artifacts_public(row, replayed=True)
            existing = connection.execute(
                "SELECT * FROM action_artifacts WHERE action_id = ?", (action_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO action_artifacts(action_id, updated_at) VALUES (?, ?)",
                    (action_id, now),
                )
            else:
                for column, value in values.items():
                    current = existing[column]
                    if current not in {None, "", "{}"} and current != value:
                        raise Conflict(f"{column} is immutable once recorded")
            assignments = ", ".join(f"{column} = ?" for column in values)
            connection.execute(
                f"UPDATE action_artifacts SET {assignments}, updated_at = ? WHERE action_id = ?",
                (*values.values(), now, action_id),
            )
            connection.execute(
                """
                INSERT INTO action_artifact_updates(request_id, action_id, actor_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (request_id, action_id, actor_id, payload_json, now),
            )
            self.database.append_event(
                connection,
                occurred_at=now,
                event_type="action.artifacts_recorded",
                actor_id=actor_id,
                entity_type="action",
                entity_id=action_id,
                payload={"request_id": request_id, "fields": sorted(values)},
            )
            row = connection.execute(
                "SELECT * FROM action_artifacts WHERE action_id = ?", (action_id,)
            ).fetchone()
        assert row is not None
        return self._action_artifacts_public(row, replayed=False)

    @staticmethod
    def _action_artifacts_public(row: sqlite3.Row, *, replayed: bool = False) -> dict[str, Any]:
        return {
            "action_id": str(row["action_id"])[:36],
            "before_digest": row["before_digest"],
            "after_digest": row["after_digest"],
            "target_version": row["target_version"],
            "rollback_ref": row["rollback_ref"],
            "diff_artifact": row["diff_artifact"],
            "healthcheck": json.loads(row["healthcheck_json"]),
            "updated_at": str(row["updated_at"])[:40],
            "replayed": replayed,
        }

    def get_action_artifacts(self, actor_id: str, action_id: str) -> dict[str, Any] | None:
        actor_id = self._actor(actor_id)
        with self.database.read() as connection:
            action = connection.execute(
                "SELECT project_id FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
            if action is None:
                raise NotFound("action")
            self.rbac.require_project(actor_id, str(action["project_id"]))
            row = connection.execute(
                "SELECT * FROM action_artifacts WHERE action_id = ?", (action_id,)
            ).fetchone()
        return None if row is None else self._action_artifacts_public(row)

    @staticmethod
    def _incident_public(row: sqlite3.Row, *, replayed: bool | None = None) -> dict[str, Any]:
        result = {
            "id": str(row["id"])[:36],
            "project_id": str(row["project_id"])[:64],
            "title": str(row["title"])[:240],
            "severity": str(row["severity"])[:16],
            "status": str(row["status"])[:16],
            "service": str(row["service"])[:200],
            "resource_id": str(row["resource_id"])[:192],
            "environment": str(row["environment"])[:64],
            "symptoms": str(row["symptoms"])[:8000],
            "impact": str(row["impact"])[:4000],
            "probable_causes": json.loads(row["probable_causes_json"]),
            "confirmed_cause": str(row["confirmed_cause"])[:4000],
            "resolution": str(row["resolution"])[:8000],
            "rollback": str(row["rollback"])[:4000],
            "recommendations": json.loads(row["recommendations_json"]),
            "versions": json.loads(row["versions_json"]),
            "created_at": str(row["created_at"])[:40],
            "updated_at": str(row["updated_at"])[:40],
        }
        if replayed is not None:
            result["replayed"] = replayed
        return result

    def recover_after_restart(self, *, actor_id: str = "system-recovery") -> dict[str, int]:
        """Fail interrupted actions and requeue expired claims after a process restart."""

        actor_id = self._actor(actor_id)
        _, now = self._now()
        with self.database.transaction() as connection:
            running = connection.execute(
                "SELECT id FROM actions WHERE status = 'running' ORDER BY id"
            ).fetchall()
            for row in running:
                result = canonical_json(
                    {
                        "return_code": -1,
                        "stdout": "",
                        "stderr": "interrupted by control-plane restart; state requires verification",
                        "timed_out": False,
                        "truncated": False,
                        "interrupted": True,
                    }
                )
                connection.execute(
                    "UPDATE actions SET status = 'failed', result_json = ?, updated_at = ? WHERE id = ?",
                    (result, now, row["id"]),
                )
                self.database.append_event(
                    connection,
                    occurred_at=now,
                    event_type="action.interrupted",
                    actor_id=actor_id,
                    entity_type="action",
                    entity_id=str(row["id"]),
                    payload={"decision": "stop_and_verify"},
                )
            expired_claims = connection.execute(
                """
                SELECT mission_id FROM mission_runtime
                WHERE queue_state = 'claimed' AND lease_until IS NOT NULL AND lease_until <= ?
                ORDER BY mission_id
                """,
                (now,),
            ).fetchall()
            for row in expired_claims:
                connection.execute(
                    """
                    UPDATE mission_runtime SET queue_state = 'queued', claimed_by = NULL,
                        claimed_at = NULL, heartbeat_at = NULL, lease_until = NULL,
                        updated_at = ? WHERE mission_id = ?
                    """,
                    (now, row["mission_id"]),
                )
                self.database.append_event(
                    connection,
                    occurred_at=now,
                    event_type="mission.requeued_after_restart",
                    actor_id=actor_id,
                    entity_type="mission",
                    entity_id=str(row["mission_id"]),
                    payload={"reason": "expired_lease"},
                )
            expired_locks = connection.execute(
                "SELECT resource_id FROM resource_locks WHERE lease_until <= ? ORDER BY resource_id",
                (now,),
            ).fetchall()
            for row in expired_locks:
                connection.execute(
                    "DELETE FROM resource_locks WHERE resource_id = ?", (row["resource_id"],)
                )
                self.database.append_event(
                    connection,
                    occurred_at=now,
                    event_type="resource_lock.expired",
                    actor_id=actor_id,
                    entity_type="resource",
                    entity_id=str(row["resource_id"]),
                    payload={"reason": "expired_lease"},
                )
        return {
            "interrupted_actions": len(running),
            "requeued_missions": len(expired_claims),
            "expired_resource_locks": len(expired_locks),
        }
