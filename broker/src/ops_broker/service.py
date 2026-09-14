"""Broker domain service: state transitions, budgets, approvals and execution."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable

from .database import Database
from .errors import (
    ApprovalRequired,
    AuthorizationDenied,
    BrokerError,
    BudgetExceeded,
    Conflict,
    NotFound,
)
from .executor import CommandExecutor, ExecutionResult, Executor
from .mission_control import MissionControl
from .policy import ActionPolicy, RBACPolicy
from .runbooks import Runbook, RunbookRegistry
from .util import canonical_json, isoformat, parse_timestamp, redact_text, scrub_payload, utc_now


_ACTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_SOURCE = re.compile(r"^[a-z][a-z0-9._-]{0,31}$")
_MISSION_STATUSES = frozenset({"open", "paused", "completed"})
_INCIDENT_STATUSES = frozenset({"open", "monitoring", "resolved"})
_MISSION_TRANSITIONS = {
    "open": frozenset({"paused", "completed"}),
    "paused": frozenset({"open", "completed"}),
    "completed": frozenset(),
}
_INCIDENT_TRANSITIONS = {
    "open": frozenset({"monitoring", "resolved"}),
    "monitoring": frozenset({"open", "resolved"}),
    "resolved": frozenset(),
}


class BrokerService:
    def __init__(
        self,
        database: Database,
        registry: RunbookRegistry,
        rbac: RBACPolicy,
        action_policy: ActionPolicy,
        *,
        executor: Executor | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.database = database
        self.registry = registry
        self.rbac = rbac
        self.action_policy = action_policy
        self.executor = executor or CommandExecutor()
        self.clock = clock
        # Keep V1 mission state outside LLM context.  The indirection through
        # self.clock preserves deterministic test clocks changed after init.
        self.mission_control = MissionControl(
            database,
            rbac,
            clock=lambda: self.clock(),
        )
        self.recovery_report = self.mission_control.recover_after_restart()
        for runbook in registry.all():
            if runbook.action_class == "B":
                assert runbook.budget is not None
                action_policy.validate_runbook_budget(
                    runbook.budget.max_attempts,
                    runbook.budget.window_seconds,
                )

    def _now(self) -> tuple[datetime, str]:
        value = self.clock()
        return value, isoformat(value)

    @staticmethod
    def _validate_actor(actor_id: str) -> None:
        if not isinstance(actor_id, str) or not _ACTOR_ID.fullmatch(actor_id):
            raise AuthorizationDenied("actor id is malformed")

    @staticmethod
    def _validate_request_id(request_id: str) -> str:
        try:
            return str(uuid.UUID(request_id))
        except (ValueError, TypeError) as exc:
            raise BrokerError("invalid_request_id", "request_id must be a UUID", status_code=422) from exc

    @staticmethod
    def _bounded_text(value: str, name: str, minimum: int, maximum: int) -> str:
        if not isinstance(value, str) or not minimum <= len(value) <= maximum or "\x00" in value:
            raise BrokerError("invalid_request", f"{name} must be bounded text", status_code=422)
        return redact_text(value)

    @staticmethod
    def _validate_limit(limit: int) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise BrokerError("invalid_request", "limit must be an integer from 1 to 50", status_code=422)
        return limit

    @staticmethod
    def _validate_offset(offset: int) -> int:
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 10_000:
            raise BrokerError("invalid_request", "offset must be an integer from 0 to 10000", status_code=422)
        return offset

    @staticmethod
    def _mission_public(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"])[:36],
            "project_id": str(row["project_id"])[:64],
            "title": str(row["title"])[:240],
            "status": str(row["status"])[:16],
            "created_at": str(row["created_at"])[:40],
            "updated_at": str(row["updated_at"])[:40],
        }

    @staticmethod
    def _incident_public(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["id"])[:36],
            "project_id": str(row["project_id"])[:64],
            "title": str(row["title"])[:240],
            "severity": str(row["severity"])[:16],
            "status": str(row["status"])[:16],
            "created_at": str(row["created_at"])[:40],
            "updated_at": str(row["updated_at"])[:40],
        }

    @staticmethod
    def _status_change_public(
        row: sqlite3.Row,
        *,
        entity_key: str,
        current_status: str,
        replayed: bool,
    ) -> dict[str, Any]:
        previous_status = str(row["previous_status"])[:16]
        requested_status = str(row["new_status"])[:16]
        return {
            "request_id": str(row["request_id"])[:36],
            entity_key: str(row[entity_key])[:36],
            "previous_status": previous_status,
            "status": requested_status,
            "current_status": current_status[:16],
            "changed": previous_status != requested_status,
            "replayed": replayed,
            "updated_at": str(row["created_at"])[:40],
        }

    def _audit_denial(
        self,
        *,
        actor_id: str,
        event_type: str,
        entity_type: str,
        entity_id: str,
        code: str,
        request_id: str | None,
    ) -> None:
        _, now = self._now()
        with self.database.transaction() as connection:
            self.database.append_event(
                connection,
                occurred_at=now,
                event_type=event_type,
                actor_id=actor_id if isinstance(actor_id, str) else "invalid-actor",
                entity_type=entity_type,
                entity_id=entity_id,
                payload={"decision": "deny", "code": code, "request_id": request_id},
            )

    def list_runbooks(self, actor_id: str) -> list[dict[str, Any]]:
        self._validate_actor(actor_id)
        if not self.rbac.is_known_actor(actor_id):
            raise AuthorizationDenied("unknown actor")
        return [
            runbook.public_metadata()
            for runbook in self.registry.all()
            if self.rbac.can_runbook(actor_id, runbook.project, runbook.id, runbook.action_class)
        ]

    def create_mission(
        self,
        *,
        actor_id: str,
        request_id: str,
        project_id: str,
        title: str,
    ) -> dict[str, Any]:
        self._validate_actor(actor_id)
        request_id = self._validate_request_id(request_id)
        if not _PROJECT_ID.fullmatch(project_id):
            raise BrokerError("invalid_request", "project_id is malformed", status_code=422)
        title = self._bounded_text(title, "title", 1, 240)
        try:
            self.rbac.require_project(actor_id, project_id)
        except AuthorizationDenied as exc:
            self._audit_denial(
                actor_id=actor_id,
                event_type="mission.denied",
                entity_type="mission",
                entity_id=request_id,
                code=exc.code,
                request_id=request_id,
            )
            raise

        with self.database.read() as connection:
            existing = connection.execute(
                "SELECT * FROM missions WHERE request_id = ?", (request_id,)
            ).fetchone()
        if existing is not None:
            if (
                existing["created_by"] == actor_id
                and existing["project_id"] == project_id
                and existing["title"] == title
            ):
                return dict(existing)
            raise Conflict("request_id was already used for a different mission")

        mission_id = str(uuid.uuid4())
        _, now = self._now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO missions(id, request_id, project_id, title, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                (mission_id, request_id, project_id, title, actor_id, now, now),
            )
            self.database.append_event(
                connection,
                occurred_at=now,
                event_type="mission.created",
                actor_id=actor_id,
                entity_type="mission",
                entity_id=mission_id,
                payload={"request_id": request_id, "project_id": project_id},
            )
            row = connection.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()
        assert row is not None
        return dict(row)

    def create_incident(
        self,
        *,
        actor_id: str,
        request_id: str,
        project_id: str,
        title: str,
        severity: str,
    ) -> dict[str, Any]:
        self._validate_actor(actor_id)
        request_id = self._validate_request_id(request_id)
        if not _PROJECT_ID.fullmatch(project_id):
            raise BrokerError("invalid_request", "project_id is malformed", status_code=422)
        if severity not in {"info", "warning", "critical"}:
            raise BrokerError("invalid_request", "severity is invalid", status_code=422)
        title = self._bounded_text(title, "title", 1, 240)
        try:
            self.rbac.require_project(actor_id, project_id)
        except AuthorizationDenied as exc:
            self._audit_denial(
                actor_id=actor_id,
                event_type="incident.denied",
                entity_type="incident",
                entity_id=request_id,
                code=exc.code,
                request_id=request_id,
            )
            raise

        with self.database.read() as connection:
            existing = connection.execute(
                "SELECT * FROM incidents WHERE request_id = ?", (request_id,)
            ).fetchone()
        if existing is not None:
            if (
                existing["created_by"] == actor_id
                and existing["project_id"] == project_id
                and existing["title"] == title
                and existing["severity"] == severity
            ):
                return dict(existing)
            raise Conflict("request_id was already used for a different incident")

        incident_id = str(uuid.uuid4())
        _, now = self._now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO incidents(
                    id, request_id, project_id, title, severity, status, created_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?)
                """,
                (incident_id, request_id, project_id, title, severity, actor_id, now, now),
            )
            self.database.append_event(
                connection,
                occurred_at=now,
                event_type="incident.created",
                actor_id=actor_id,
                entity_type="incident",
                entity_id=incident_id,
                payload={"request_id": request_id, "project_id": project_id, "severity": severity},
            )
            row = connection.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        assert row is not None
        return dict(row)

    def _authorized_project_filter(
        self,
        actor_id: str,
        project_id: str | None,
    ) -> tuple[str, ...]:
        self._validate_actor(actor_id)
        if project_id is not None:
            if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
                raise BrokerError("invalid_request", "project_id is malformed", status_code=422)
            self.rbac.require_project(actor_id, project_id)
            return (project_id,)
        return tuple(sorted(self.rbac.projects_for_actor(actor_id)))

    def list_open_missions(
        self,
        actor_id: str,
        project_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        limit = self._validate_limit(limit)
        projects = self._authorized_project_filter(actor_id, project_id)
        if not projects:
            return []
        placeholders = ",".join("?" for _ in projects)
        with self.database.read() as connection:
            rows = connection.execute(
                f"""
                SELECT m.id, m.project_id, m.title, m.status, m.created_at, m.updated_at
                FROM missions AS m
                JOIN mission_runtime AS r ON r.mission_id = m.id
                WHERE m.status IN ('open', 'paused') AND m.project_id IN ({placeholders})
                ORDER BY r.priority DESC, m.updated_at DESC, m.id DESC
                LIMIT ?
                """,
                (*projects, limit),
            ).fetchall()
        return [self._mission_public(row) for row in rows]

    def list_memory_missions(self, actor_id: str, project_id: str, after: int = 0,
                             limit: int = 50) -> dict[str, Any]:
        """Discover even missions completed between two collector runs."""
        limit = self._validate_limit(limit)
        projects = self._authorized_project_filter(actor_id, project_id)
        if isinstance(after, bool) or not isinstance(after, int) or not 0 <= after < 2**63:
            raise BrokerError("invalid_request", "invalid mission cursor", status_code=422)
        if not projects:
            return {"missions": [], "next_cursor": after}
        with self.database.read() as connection:
            rows = connection.execute(
                "SELECT *, rowid AS memory_cursor FROM missions WHERE project_id=? AND rowid>? ORDER BY rowid LIMIT ?",
                (project_id, after, limit),
            ).fetchall()
        return {"missions": [self._mission_public(row) for row in rows],
                "next_cursor": rows[-1]["memory_cursor"] if rows else after}

    def list_memory_history(self, actor_id: str, project_id: str, after: int = 0,
                            limit: int = 50) -> dict[str, Any]:
        """Page immutable public records, including closed missions, within RBAC."""
        limit = self._validate_limit(limit)
        projects = self._authorized_project_filter(actor_id, project_id)
        if isinstance(after, bool) or not isinstance(after, int) or not 0 <= after < 2**63:
            raise BrokerError("invalid_request", "invalid history cursor", status_code=422)
        if not projects:
            return {"records": [], "next_cursor": after}
        with self.database.read() as connection:
            rows = connection.execute(
                """SELECT r.*, r.rowid AS history_cursor, m.project_id
                   FROM mission_records r JOIN missions m ON m.id=r.mission_id
                   WHERE m.project_id=? AND r.rowid>? ORDER BY r.rowid LIMIT ?""",
                (project_id, after, limit),
            ).fetchall()
        return {"records": [{**self.mission_control._record_public(row),
                              "project_id": row["project_id"]} for row in rows],
                "next_cursor": rows[-1]["history_cursor"] if rows else after}

    def list_open_incidents(
        self,
        actor_id: str,
        project_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        limit = self._validate_limit(limit)
        projects = self._authorized_project_filter(actor_id, project_id)
        if not projects:
            return []
        placeholders = ",".join("?" for _ in projects)
        with self.database.read() as connection:
            rows = connection.execute(
                f"""
                SELECT id, project_id, title, severity, status, created_at, updated_at
                FROM incidents
                WHERE status IN ('open', 'monitoring') AND project_id IN ({placeholders})
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (*projects, limit),
            ).fetchall()
        return [self._incident_public(row) for row in rows]

    def get_mission(self, actor_id: str, mission_id: str) -> dict[str, Any]:
        self._validate_actor(actor_id)
        with self.database.read() as connection:
            row = connection.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()
        if row is None:
            raise NotFound("mission")
        self.rbac.require_project(actor_id, row["project_id"])
        return self._mission_public(row)

    def get_incident(self, actor_id: str, incident_id: str) -> dict[str, Any]:
        self._validate_actor(actor_id)
        with self.database.read() as connection:
            row = connection.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        if row is None:
            raise NotFound("incident")
        self.rbac.require_project(actor_id, row["project_id"])
        return self._incident_public(row)

    def list_actions_for_mission(
        self,
        actor_id: str,
        mission_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return self._list_actions_for_entity(
            actor_id=actor_id,
            entity_id=mission_id,
            entity_type="mission",
            limit=limit,
            offset=offset,
        )

    def list_actions_for_incident(
        self,
        actor_id: str,
        incident_id: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return self._list_actions_for_entity(
            actor_id=actor_id,
            entity_id=incident_id,
            entity_type="incident",
            limit=limit,
        )

    def _list_actions_for_entity(
        self,
        *,
        actor_id: str,
        entity_id: str,
        entity_type: str,
        limit: int,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        self._validate_actor(actor_id)
        limit = self._validate_limit(limit)
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 1_000_000:
            raise BrokerError("invalid_request", "invalid action page offset", status_code=422)
        if entity_type == "mission":
            entity_table = "missions"
            link_column = "mission_id"
        elif entity_type == "incident":
            entity_table = "incidents"
            link_column = "incident_id"
        else:  # pragma: no cover - all callers use fixed literals
            raise AssertionError("unsupported action-list entity")

        with self.database.read() as connection:
            entity = connection.execute(
                f"SELECT project_id FROM {entity_table} WHERE id = ?",
                (entity_id,),
            ).fetchone()
        if entity is None:
            raise NotFound(entity_type)
        self.rbac.require_project(actor_id, entity["project_id"])

        with self.database.read() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM actions
                WHERE {link_column} = ? AND project_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (entity_id, entity["project_id"], limit, offset),
            ).fetchall()
        return [self._action_summary_public(row) for row in rows]

    def set_mission_status(
        self,
        *,
        actor_id: str,
        request_id: str,
        mission_id: str,
        status: str,
    ) -> dict[str, Any]:
        return self._set_entity_status(
            actor_id=actor_id,
            request_id=request_id,
            entity_id=mission_id,
            status=status,
            entity_type="mission",
        )

    def set_incident_status(
        self,
        *,
        actor_id: str,
        request_id: str,
        incident_id: str,
        status: str,
    ) -> dict[str, Any]:
        return self._set_entity_status(
            actor_id=actor_id,
            request_id=request_id,
            entity_id=incident_id,
            status=status,
            entity_type="incident",
        )

    def _set_entity_status(
        self,
        *,
        actor_id: str,
        request_id: str,
        entity_id: str,
        status: str,
        entity_type: str,
    ) -> dict[str, Any]:
        self._validate_actor(actor_id)
        request_id = self._validate_request_id(request_id)
        if entity_type == "mission":
            allowed_statuses = _MISSION_STATUSES
            allowed_transitions = _MISSION_TRANSITIONS
            entity_table = "missions"
            changes_table = "mission_status_changes"
            entity_key = "mission_id"
        elif entity_type == "incident":
            allowed_statuses = _INCIDENT_STATUSES
            allowed_transitions = _INCIDENT_TRANSITIONS
            entity_table = "incidents"
            changes_table = "incident_status_changes"
            entity_key = "incident_id"
        else:  # pragma: no cover - all callers use a fixed literal
            raise AssertionError("unsupported status entity")
        if not isinstance(status, str) or status not in allowed_statuses:
            raise BrokerError("invalid_request", f"invalid {entity_type} status", status_code=422)

        with self.database.read() as connection:
            entity = connection.execute(
                f"SELECT project_id FROM {entity_table} WHERE id = ?",
                (entity_id,),
            ).fetchone()
        if entity is None:
            raise NotFound(entity_type)
        try:
            self.rbac.require_lifecycle(
                actor_id,
                entity["project_id"],
                f"{entity_type}.status.write",
            )
        except AuthorizationDenied as exc:
            self._audit_denial(
                actor_id=actor_id,
                event_type=f"{entity_type}.status_denied",
                entity_type=entity_type,
                entity_id=entity_id,
                code=exc.code,
                request_id=request_id,
            )
            raise

        with self.database.transaction() as connection:
            current = connection.execute(
                f"SELECT project_id, status FROM {entity_table} WHERE id = ?",
                (entity_id,),
            ).fetchone()
            if current is None:
                raise NotFound(entity_type)
            existing = connection.execute(
                f"SELECT * FROM {changes_table} WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing[entity_key] == entity_id
                    and existing["actor_id"] == actor_id
                    and existing["new_status"] == status
                ):
                    return self._status_change_public(
                        existing,
                        entity_key=entity_key,
                        current_status=str(current["status"]),
                        replayed=True,
                    )
                raise Conflict(f"request_id was already used for a different {entity_type} status")

            current_status = str(current["status"])
            changed = current_status != status
            if changed and status not in allowed_transitions[current_status]:
                raise Conflict(
                    f"{entity_type} cannot transition from {current_status} to {status}"
                )
            _, now = self._now()
            connection.execute(
                f"""
                INSERT INTO {changes_table}(
                    request_id, {entity_key}, actor_id, previous_status, new_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (request_id, entity_id, actor_id, current_status, status, now),
            )
            if changed:
                connection.execute(
                    f"UPDATE {entity_table} SET status = ?, updated_at = ? WHERE id = ?",
                    (status, now, entity_id),
                )
                if entity_type == "mission":
                    queue_state = {
                        "open": "queued",
                        "paused": "blocked",
                        "completed": "done",
                    }[status]
                    connection.execute(
                        """
                        UPDATE mission_runtime
                        SET queue_state = ?, claimed_by = NULL, claimed_at = NULL,
                            heartbeat_at = NULL, lease_until = NULL, updated_at = ?
                        WHERE mission_id = ?
                        """,
                        (queue_state, now, entity_id),
                    )
                    if status in {"paused", "completed"}:
                        held_locks = connection.execute(
                            "SELECT resource_id FROM resource_locks WHERE mission_id = ?",
                            (entity_id,),
                        ).fetchall()
                        connection.execute(
                            "DELETE FROM resource_locks WHERE mission_id = ?", (entity_id,)
                        )
                        for held_lock in held_locks:
                            self.database.append_event(
                                connection,
                                occurred_at=now,
                                event_type="resource_lock.released_on_mission_status",
                                actor_id=actor_id,
                                entity_type="resource",
                                entity_id=str(held_lock["resource_id"]),
                                payload={"mission_id": entity_id, "status": status},
                            )
            self.database.append_event(
                connection,
                occurred_at=now,
                event_type=(
                    f"{entity_type}.status_changed"
                    if changed
                    else f"{entity_type}.status_unchanged"
                ),
                actor_id=actor_id,
                entity_type=entity_type,
                entity_id=entity_id,
                payload={
                    "request_id": request_id,
                    "project_id": current["project_id"],
                    "previous_status": current_status,
                    "status": status,
                    "changed": changed,
                },
            )
            stored = connection.execute(
                f"SELECT * FROM {changes_table} WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        assert stored is not None
        return self._status_change_public(
            stored,
            entity_key=entity_key,
            current_status=status,
            replayed=False,
        )

    def request_action(
        self,
        *,
        actor_id: str,
        request_id: str,
        runbook_id: str,
        parameters: dict[str, Any],
        reason: str,
        incident_id: str | None = None,
        mission_id: str | None = None,
    ) -> dict[str, Any]:
        self._validate_actor(actor_id)
        request_id = self._validate_request_id(request_id)
        reason = self._bounded_text(reason, "reason", 3, 1000)

        try:
            runbook = self.registry.get(runbook_id)
            validated = runbook.validate_parameters(parameters)
            self.rbac.require_runbook(actor_id, runbook.project, runbook.id, runbook.action_class)
            argv = runbook.render_argv(validated)
        except BrokerError as exc:
            self._audit_denial(
                actor_id=actor_id,
                event_type="action.denied",
                entity_type="action_request",
                entity_id=request_id,
                code=exc.code,
                request_id=request_id,
            )
            raise

        parameter_incident = validated.get("incident_id")
        if parameter_incident is not None:
            if incident_id is None:
                incident_id = str(parameter_incident)
            elif incident_id != parameter_incident:
                raise BrokerError(
                    "invalid_parameters",
                    "incident_id does not match the runbook parameter",
                    status_code=422,
                )

        try:
            self._validate_linked_entities(runbook.project, incident_id, mission_id)
        except BrokerError as exc:
            self._audit_denial(
                actor_id=actor_id,
                event_type="action.denied",
                entity_type="action_request",
                entity_id=request_id,
                code=exc.code,
                request_id=request_id,
            )
            raise

        parameters_json = canonical_json(scrub_payload(validated))
        argv_json = canonical_json(list(argv)) if argv is not None else None
        with self.database.read() as connection:
            existing = connection.execute(
                "SELECT * FROM actions WHERE request_id = ?", (request_id,)
            ).fetchone()
        if existing is not None:
            same_request = (
                existing["requested_by"] == actor_id
                and existing["runbook_id"] == runbook.id
                and existing["runbook_version"] == runbook.version
                and existing["project_id"] == runbook.project
                and existing["reason"] == reason
                and existing["parameters_json"] == parameters_json
                and existing["argv_json"] == argv_json
                and existing["incident_id"] == incident_id
                and existing["mission_id"] == mission_id
            )
            if not same_request:
                raise Conflict("request_id was already used for a different action")
            if existing["status"] == "budget_exhausted":
                raise BudgetExceeded(existing["id"])
            return self._action_public(existing)

        action_id = str(uuid.uuid4())
        now_dt, now = self._now()
        approval_deadline: str | None = None
        status = "authorized"
        if runbook.action_class == "C":
            ttl = self.action_policy.classes["C"].approval_ttl_seconds
            assert ttl is not None
            approval_deadline = isoformat(now_dt + timedelta(seconds=ttl))
            status = "pending_approval"

        budget_key = runbook.budget_key(validated)
        exhausted = False
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO actions(
                    id, request_id, mission_id, incident_id, project_id, runbook_id,
                    runbook_version, action_class, requested_by, reason, parameters_json,
                    argv_json, budget_key, status, approval_deadline, result_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    action_id,
                    request_id,
                    mission_id,
                    incident_id,
                    runbook.project,
                    runbook.id,
                    runbook.version,
                    runbook.action_class,
                    actor_id,
                    reason,
                    parameters_json,
                    argv_json,
                    budget_key,
                    status,
                    approval_deadline,
                    now,
                    now,
                ),
            )
            self.database.append_event(
                connection,
                occurred_at=now,
                event_type="action.requested",
                actor_id=actor_id,
                entity_type="action",
                entity_id=action_id,
                payload={
                    "request_id": request_id,
                    "runbook_id": runbook.id,
                    "project_id": runbook.project,
                    "action_class": runbook.action_class,
                },
            )

            if runbook.action_class == "B":
                assert runbook.budget is not None and budget_key is not None
                cutoff = isoformat(now_dt - timedelta(seconds=runbook.budget.window_seconds))
                used = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM budget_usage WHERE budget_key = ? AND used_at >= ?",
                        (budget_key, cutoff),
                    ).fetchone()[0]
                )
                if used >= runbook.budget.max_attempts:
                    exhausted = True
                    connection.execute(
                        "UPDATE actions SET status = 'budget_exhausted', updated_at = ? WHERE id = ?",
                        (now, action_id),
                    )
                    self.database.append_event(
                        connection,
                        occurred_at=now,
                        event_type="action.denied",
                        actor_id=actor_id,
                        entity_type="action",
                        entity_id=action_id,
                        payload={"decision": "escalate", "code": "budget_exhausted"},
                    )
                else:
                    connection.execute(
                        "INSERT INTO budget_usage(budget_key, action_id, used_at) VALUES (?, ?, ?)",
                        (budget_key, action_id, now),
                    )
                    self.database.append_event(
                        connection,
                        occurred_at=now,
                        event_type="action.budget_reserved",
                        actor_id=actor_id,
                        entity_type="action",
                        entity_id=action_id,
                        payload={"max_attempts": runbook.budget.max_attempts},
                    )

            row = connection.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()

        if exhausted:
            raise BudgetExceeded(action_id)
        assert row is not None
        if runbook.action_class in {"A", "B"}:
            return self.execute_action(actor_id=actor_id, action_id=action_id)
        return self._action_public(row)

    def _validate_linked_entities(
        self,
        project_id: str,
        incident_id: str | None,
        mission_id: str | None,
    ) -> None:
        with self.database.read() as connection:
            if incident_id is not None:
                incident = connection.execute(
                    "SELECT project_id FROM incidents WHERE id = ?", (incident_id,)
                ).fetchone()
                if incident is None:
                    raise NotFound("incident")
                if incident["project_id"] != project_id:
                    raise Conflict("incident belongs to a different project")
            if mission_id is not None:
                mission = connection.execute(
                    "SELECT project_id FROM missions WHERE id = ?", (mission_id,)
                ).fetchone()
                if mission is None:
                    raise NotFound("mission")
                if mission["project_id"] != project_id:
                    raise Conflict("mission belongs to a different project")

    def execute_action(self, *, actor_id: str, action_id: str) -> dict[str, Any]:
        self._validate_actor(actor_id)
        with self.database.read() as connection:
            row = connection.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        if row is None:
            raise NotFound("action")
        if actor_id != row["requested_by"]:
            self._audit_denial(
                actor_id=actor_id,
                event_type="action.execution_denied",
                entity_type="action",
                entity_id=action_id,
                code="actor_mismatch",
                request_id=row["request_id"],
            )
            raise AuthorizationDenied("only the requesting actor may execute this action")

        runbook = self.registry.get(row["runbook_id"])
        try:
            self.rbac.require_runbook(
                actor_id,
                row["project_id"],
                row["runbook_id"],
                row["action_class"],
            )
        except AuthorizationDenied as exc:
            self._audit_denial(
                actor_id=actor_id,
                event_type="action.execution_denied",
                entity_type="action",
                entity_id=action_id,
                code=exc.code,
                request_id=row["request_id"],
            )
            raise
        if row["status"] in {"succeeded", "failed"}:
            return self._action_public(row)

        try:
            stored_parameters = json.loads(row["parameters_json"])
            validated_parameters = runbook.validate_parameters(stored_parameters)
            expected_argv = runbook.render_argv(validated_parameters)
            expected_argv_json = (
                canonical_json(list(expected_argv)) if expected_argv is not None else None
            )
        except (BrokerError, json.JSONDecodeError, TypeError, ValueError):
            self._audit_denial(
                actor_id=actor_id,
                event_type="action.execution_denied",
                entity_type="action",
                entity_id=action_id,
                code="action_integrity_mismatch",
                request_id=row["request_id"],
            )
            raise Conflict("stored action no longer matches its runbook") from None
        if (
            runbook.version != row["runbook_version"]
            or runbook.project != row["project_id"]
            or runbook.action_class != row["action_class"]
            or expected_argv_json != row["argv_json"]
        ):
            self._audit_denial(
                actor_id=actor_id,
                event_type="action.execution_denied",
                entity_type="action",
                entity_id=action_id,
                code="action_integrity_mismatch",
                request_id=row["request_id"],
            )
            raise Conflict("stored action no longer matches its runbook")
        if row["status"] == "pending_approval":
            now_dt, now = self._now()
            deadline = row["approval_deadline"]
            if deadline is not None and parse_timestamp(deadline) <= now_dt:
                with self.database.transaction() as connection:
                    changed = connection.execute(
                        """
                        UPDATE actions SET status = 'approval_expired', updated_at = ?
                        WHERE id = ? AND status = 'pending_approval'
                        """,
                        (now, action_id),
                    ).rowcount
                    if changed == 1:
                        self.database.append_event(
                            connection,
                            occurred_at=now,
                            event_type="action.approval_expired",
                            actor_id=actor_id,
                            entity_type="action",
                            entity_id=action_id,
                            payload={"decision": "deny"},
                        )
                raise ApprovalRequired("the human approval request expired")
            raise ApprovalRequired()
        if row["status"] == "rejected":
            raise AuthorizationDenied("the action was rejected")
        if row["status"] == "approval_expired":
            raise ApprovalRequired("the human approval expired")
        if row["status"] == "budget_exhausted":
            raise BudgetExceeded(action_id)
        if row["status"] == "running":
            raise Conflict("action is already running")
        if row["status"] not in {"authorized", "approved"}:
            raise Conflict("action cannot be executed from its current state")

        if row["action_class"] == "C":
            now_dt, now = self._now()
            with self.database.read() as connection:
                approval = connection.execute(
                    """
                    SELECT * FROM approvals
                    WHERE action_id = ? AND decision = 'approve'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (action_id,),
                ).fetchone()
            if approval is None:
                raise ApprovalRequired()
            if parse_timestamp(approval["expires_at"]) <= now_dt:
                with self.database.transaction() as connection:
                    connection.execute(
                        "UPDATE actions SET status = 'approval_expired', updated_at = ? WHERE id = ?",
                        (now, action_id),
                    )
                    self.database.append_event(
                        connection,
                        occurred_at=now,
                        event_type="action.approval_expired",
                        actor_id=actor_id,
                        entity_type="action",
                        entity_id=action_id,
                        payload={"decision": "deny"},
                    )
                raise ApprovalRequired("the human approval expired")

        _, now = self._now()
        with self.database.transaction() as connection:
            changed = connection.execute(
                """
                UPDATE actions SET status = 'running', updated_at = ?
                WHERE id = ? AND status IN ('authorized', 'approved')
                """,
                (now, action_id),
            ).rowcount
            if changed != 1:
                raise Conflict("action was concurrently claimed")
            self.database.append_event(
                connection,
                occurred_at=now,
                event_type="action.started",
                actor_id=actor_id,
                entity_type="action",
                entity_id=action_id,
                payload={"runbook_id": runbook.id},
            )

        argv = expected_argv or ()
        try:
            result = self.executor.execute(argv, runbook.timeout_seconds, runbook.output_max_bytes)
        except Exception:  # The internal exception must not leak through the API or MCP.
            result = ExecutionResult(-1, "", "executor failed", False, False)
        stdout = redact_text(result.stdout)
        stderr = redact_text(result.stderr)
        truncated = (
            result.truncated
            or len(stdout.encode("utf-8")) > runbook.output_max_bytes
            or len(stderr.encode("utf-8")) > runbook.output_max_bytes
        )
        result = ExecutionResult(
            result.return_code,
            stdout.encode("utf-8")[: runbook.output_max_bytes].decode("utf-8", errors="ignore"),
            stderr.encode("utf-8")[: runbook.output_max_bytes].decode("utf-8", errors="ignore"),
            result.timed_out,
            truncated,
        )
        status = "succeeded" if result.return_code == 0 and not result.timed_out else "failed"
        result_payload = {
            "return_code": result.return_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "truncated": result.truncated,
        }
        _, finished_at = self._now()
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE actions SET status = ?, result_json = ?, updated_at = ? WHERE id = ?",
                (status, canonical_json(result_payload), finished_at, action_id),
            )
            self.database.append_event(
                connection,
                occurred_at=finished_at,
                event_type=f"action.{status}",
                actor_id=actor_id,
                entity_type="action",
                entity_id=action_id,
                payload={
                    "return_code": result.return_code,
                    "timed_out": result.timed_out,
                    "truncated": result.truncated,
                },
            )
            finished = connection.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        assert finished is not None
        return self._action_public(finished)

    def decide_approval(
        self,
        *,
        actor_id: str,
        request_id: str,
        action_id: str,
        decision: str,
        source: str,
        source_event_id: str | None,
        reason: str,
    ) -> dict[str, Any]:
        self._validate_actor(actor_id)
        request_id = self._validate_request_id(request_id)
        if decision not in {"approve", "reject"}:
            raise BrokerError("invalid_request", "decision must be approve or reject", status_code=422)
        if not _SOURCE.fullmatch(source):
            raise BrokerError("invalid_request", "source is malformed", status_code=422)
        if source_event_id is not None:
            source_event_id = self._bounded_text(source_event_id, "source_event_id", 1, 200)
        if source == "zulip" and source_event_id is None:
            raise BrokerError(
                "invalid_request",
                "source_event_id is required for Zulip approvals",
                status_code=422,
            )
        reason = self._bounded_text(reason, "reason", 1, 500)

        now_dt, now = self._now()
        approval_id = str(uuid.uuid4())
        action_status = "approved" if decision == "approve" else "rejected"
        denial: BrokerError | None = None
        expired = False
        row: sqlite3.Row | None = None
        with self.database.transaction() as connection:
            action = connection.execute(
                "SELECT * FROM actions WHERE id = ?", (action_id,)
            ).fetchone()
            if action is None:
                raise NotFound("action")

            duplicate = connection.execute(
                "SELECT * FROM approvals WHERE request_id = ?", (request_id,)
            ).fetchone()
            source_duplicate = None
            if source_event_id is not None:
                source_duplicate = connection.execute(
                    "SELECT * FROM approvals WHERE source_event_id = ?", (source_event_id,)
                ).fetchone()
            for prior in (duplicate, source_duplicate):
                if prior is not None:
                    if (
                        prior["action_id"] == action_id
                        and prior["actor_id"] == actor_id
                        and prior["decision"] == decision
                        and prior["source"] == source
                        and prior["source_event_id"] == source_event_id
                        and prior["reason"] == reason
                    ):
                        return dict(prior)
                    raise Conflict("approval request or source event was already consumed")

            try:
                self.action_policy.require_approval_actor(actor_id)
                if actor_id == action["requested_by"]:
                    raise AuthorizationDenied("an action requester cannot approve its own action")
                if action["action_class"] != "C":
                    raise AuthorizationDenied("only class C actions accept human approval")
                if action["status"] != "pending_approval":
                    raise Conflict("action is not awaiting approval")
            except BrokerError as exc:
                denial = exc
                self.database.append_event(
                    connection,
                    occurred_at=now,
                    event_type="approval.denied",
                    actor_id=actor_id,
                    entity_type="action",
                    entity_id=action_id,
                    payload={
                        "decision": "deny",
                        "code": exc.code,
                        "request_id": request_id,
                    },
                )

            if denial is None:
                expires_at = action["approval_deadline"]
                if expires_at is None:
                    denial = Conflict("class C action has no approval deadline")
                elif parse_timestamp(expires_at) <= now_dt:
                    expired = True
                    connection.execute(
                        "UPDATE actions SET status = 'approval_expired', updated_at = ? WHERE id = ?",
                        (now, action_id),
                    )
                    self.database.append_event(
                        connection,
                        occurred_at=now,
                        event_type="approval.expired",
                        actor_id=actor_id,
                        entity_type="action",
                        entity_id=action_id,
                        payload={"decision": "deny", "request_id": request_id},
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO approvals(
                            id, request_id, action_id, actor_id, decision, source,
                            source_event_id, reason, created_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            approval_id,
                            request_id,
                            action_id,
                            actor_id,
                            decision,
                            source,
                            source_event_id,
                            reason,
                            now,
                            expires_at,
                        ),
                    )
                    changed = connection.execute(
                        """
                        UPDATE actions SET status = ?, updated_at = ?
                        WHERE id = ? AND status = 'pending_approval'
                        """,
                        (action_status, now, action_id),
                    ).rowcount
                    if changed != 1:
                        raise Conflict("action approval was concurrently decided")
                    self.database.append_event(
                        connection,
                        occurred_at=now,
                        event_type={
                            "approve": "approval.approved",
                            "reject": "approval.rejected",
                        }[decision],
                        actor_id=actor_id,
                        entity_type="action",
                        entity_id=action_id,
                        payload={
                            "approval_id": approval_id,
                            "request_id": request_id,
                            "source": source,
                            "source_event_id": source_event_id,
                        },
                    )
                    row = connection.execute(
                        "SELECT * FROM approvals WHERE id = ?", (approval_id,)
                    ).fetchone()

        if denial is not None:
            raise denial
        if expired:
            raise ApprovalRequired("the approval request expired")
        assert row is not None
        return dict(row)

    def pending_approvals(self, actor_id: str) -> list[dict[str, Any]]:
        self._validate_actor(actor_id)
        self.action_policy.require_approval_actor(actor_id)
        _, now = self._now()
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT * FROM actions
                WHERE action_class = 'C'
                  AND status = 'pending_approval'
                  AND approval_deadline > ?
                ORDER BY created_at
                """,
                (now,),
            ).fetchall()
        return [self._action_public(row) for row in rows]

    def pending_public_approvals(self, *, limit: int, offset: int) -> list[dict[str, str]]:
        """Return the minimal, scrubbed publication view for the UDS bridge.

        Authorization for this method is the exact production route plus its
        Unix peer; no caller-provided actor is accepted here. Corrupt or stale
        rows are omitted rather than leaking raw database fields.
        """

        limit = self._validate_limit(limit)
        offset = self._validate_offset(offset)
        _, now = self._now()
        with self.database.read() as connection:
            rows = connection.execute(
                """
                SELECT id, project_id, runbook_id, runbook_version, action_class,
                       status, parameters_json, approval_deadline, created_at
                FROM actions
                WHERE action_class = 'C'
                  AND status = 'pending_approval'
                  AND approval_deadline IS NOT NULL
                  AND approval_deadline > ?
                ORDER BY created_at ASC, id ASC
                LIMIT ? OFFSET ?
                """,
                (now, limit, offset),
            ).fetchall()

        public: list[dict[str, str]] = []
        for row in rows:
            try:
                action_id = str(uuid.UUID(str(row["id"]), version=4))
                if action_id != row["id"]:
                    continue
                runbook = self.registry.get(str(row["runbook_id"]))
                if (
                    runbook.action_class != "C"
                    or runbook.version != row["runbook_version"]
                    or runbook.project != row["project_id"]
                ):
                    continue
                stored_parameters = json.loads(row["parameters_json"])
                parameters = runbook.validate_parameters(stored_parameters)
            except (BrokerError, json.JSONDecodeError, TypeError, ValueError):
                continue

            summary_source = parameters.get("summary")
            impact_source = parameters.get("impact")
            rollback_source = parameters.get("rollback")
            summary = self._publication_text(
                summary_source if isinstance(summary_source, str) else runbook.description,
                maximum=240,
                fallback="Changement de classe C à valider",
            )
            impact = self._publication_text(
                impact_source if isinstance(impact_source, str) else "Impact à valider dans le runbook",
                maximum=1_000,
                fallback="Impact à valider dans le runbook",
            )
            rollback = self._publication_text(
                rollback_source if isinstance(rollback_source, str) else "Rollback défini par le runbook",
                maximum=1_000,
                fallback="Rollback défini par le runbook",
            )
            deadline = str(row["approval_deadline"])
            try:
                if parse_timestamp(deadline) <= parse_timestamp(now):
                    continue
            except ValueError:
                continue
            public.append(
                {
                    "action_id": action_id,
                    "summary": summary,
                    "impact": impact,
                    "rollback": rollback,
                    "deadline": deadline[:64],
                }
            )
        return public

    @staticmethod
    def _publication_text(value: str, *, maximum: int, fallback: str) -> str:
        collapsed = " ".join(redact_text(value).split())
        if not collapsed:
            collapsed = fallback
        return collapsed[:maximum]

    def get_action(self, actor_id: str, action_id: str) -> dict[str, Any]:
        self._validate_actor(actor_id)
        with self.database.read() as connection:
            row = connection.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        if row is None:
            raise NotFound("action")
        if actor_id not in self.action_policy.approval_actor_ids:
            self.rbac.require_runbook(actor_id, row["project_id"], row["runbook_id"], row["action_class"])
        return self._action_public(row)

    @staticmethod
    def _action_public(row: sqlite3.Row) -> dict[str, Any]:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        return {
            "id": row["id"],
            "request_id": row["request_id"],
            "mission_id": row["mission_id"],
            "incident_id": row["incident_id"],
            "project_id": row["project_id"],
            "runbook_id": row["runbook_id"],
            "runbook_version": row["runbook_version"],
            "action_class": row["action_class"],
            "requested_by": row["requested_by"],
            "reason": row["reason"],
            "parameters": json.loads(row["parameters_json"]),
            "status": row["status"],
            "approval_deadline": row["approval_deadline"],
            "result": result,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _action_summary_public(row: sqlite3.Row) -> dict[str, Any]:
        stored_result = json.loads(row["result_json"]) if row["result_json"] else None
        result = None
        if isinstance(stored_result, dict):
            result = {
                "return_code": stored_result.get("return_code"),
                "timed_out": stored_result.get("timed_out"),
                "truncated": stored_result.get("truncated"),
            }
        return {
            "id": str(row["id"])[:36],
            "mission_id": str(row["mission_id"])[:36] if row["mission_id"] else None,
            "incident_id": str(row["incident_id"])[:36] if row["incident_id"] else None,
            "project_id": str(row["project_id"])[:64],
            "runbook_id": str(row["runbook_id"])[:128],
            "runbook_version": int(row["runbook_version"]),
            "action_class": str(row["action_class"])[:1],
            "requested_by": str(row["requested_by"])[:128],
            "status": str(row["status"])[:32],
            "approval_deadline": (
                str(row["approval_deadline"])[:40] if row["approval_deadline"] else None
            ),
            "result": result,
            "created_at": str(row["created_at"])[:40],
            "updated_at": str(row["updated_at"])[:40],
        }
