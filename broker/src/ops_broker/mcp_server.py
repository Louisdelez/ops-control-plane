"""Official MCP Python SDK adapter for local supervised clients.

Approval decisions are deliberately absent: an MCP/model caller cannot turn a
class C request into an approved action.  A trusted human bridge must use the
HTTP approval endpoint and supply the authenticated actor id.
"""

from __future__ import annotations

from functools import lru_cache
import os
from typing import Annotated, Any, Callable, Literal

from pydantic import Field

try:
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations
except ImportError as exc:  # pragma: no cover - exercised by the optional install boundary
    raise SystemExit(
        "The MCP adapter is optional. Install it with: pip install 'ops-broker[mcp]'"
    ) from exc

from .errors import BrokerError
from .errors import AuthorizationDenied
from .factory import build_service
from .service import BrokerService


mcp = MCPServer(
    "ops-broker",
    title="Hermes operations broker",
    description="Fail-closed access to durable, allowlisted operational runbooks.",
    instructions=(
        "Use only runbooks returned for the actor. Never invent a command or claim an "
        "approval. A refusal, budget exhaustion, or approval requirement means escalate."
    ),
    version="0.1.4",
)


READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


@lru_cache(maxsize=1)
def _service() -> BrokerService:
    return build_service()


def _verified_actor(claimed_actor_id: str) -> str:
    configured_actor_id = os.environ.get("OPS_BROKER_MCP_ACTOR_ID")
    if not configured_actor_id:
        raise AuthorizationDenied("MCP actor identity is not configured")
    if claimed_actor_id != configured_actor_id:
        raise AuthorizationDenied("MCP actor identity does not match the configured identity")
    return configured_actor_id


def _invoke(function: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"ok": True, "data": function()}
    except BrokerError as exc:
        return {
            "ok": False,
            "error": {"code": exc.code, "message": exc.message, "details": exc.details},
        }
    except Exception:
        # Do not expose internal paths, command lines or exception values to a model.
        return {"ok": False, "error": {"code": "internal_error", "message": "broker failed"}}


@mcp.tool(annotations=READ)
def list_authorized_runbooks(actor_id: str) -> dict[str, Any]:
    """List only the operational runbooks explicitly granted to this actor."""

    return _invoke(lambda: _service().list_runbooks(_verified_actor(actor_id)))


@mcp.tool()
def create_mission(
    actor_id: str,
    request_id: str,
    project_id: str,
    title: str,
) -> dict[str, Any]:
    """Create an idempotent, durable operational mission."""

    return _invoke(
        lambda: _service().create_mission(
            actor_id=_verified_actor(actor_id),
            request_id=request_id,
            project_id=project_id,
            title=title,
        )
    )


@mcp.tool(annotations=READ)
def list_open_missions(
    actor_id: str,
    project_id: str | None = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """List recent non-completed missions from only the actor's granted projects."""

    return _invoke(
        lambda: _service().list_open_missions(
            _verified_actor(actor_id),
            project_id=project_id,
            limit=limit,
        )
    )


@mcp.tool(annotations=READ)
def get_mission(actor_id: str, mission_id: str) -> dict[str, Any]:
    """Read bounded public mission state when its project is granted."""

    return _invoke(lambda: _service().get_mission(_verified_actor(actor_id), mission_id))


@mcp.tool(annotations=READ)
def list_actions_for_mission(
    actor_id: str,
    mission_id: str,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
    offset: Annotated[int, Field(ge=0, le=1_000_000)] = 0,
) -> dict[str, Any]:
    """List recent redacted action summaries linked to a granted mission."""

    return _invoke(
        lambda: _service().list_actions_for_mission(
            _verified_actor(actor_id),
            mission_id,
            limit=limit,
            offset=offset,
        )
    )


@mcp.tool()
def set_mission_status(
    actor_id: str,
    request_id: str,
    mission_id: str,
    status: Literal["open", "paused", "completed"],
) -> dict[str, Any]:
    """Idempotently set a granted mission to open, paused or completed."""

    return _invoke(
        lambda: _service().set_mission_status(
            actor_id=_verified_actor(actor_id),
            request_id=request_id,
            mission_id=mission_id,
            status=status,
        )
    )


@mcp.tool()
def create_incident(
    actor_id: str,
    request_id: str,
    project_id: str,
    title: str,
    severity: str,
) -> dict[str, Any]:
    """Create an idempotent incident with severity info, warning or critical."""

    return _invoke(
        lambda: _service().create_incident(
            actor_id=_verified_actor(actor_id),
            request_id=request_id,
            project_id=project_id,
            title=title,
            severity=severity,
        )
    )


@mcp.tool(annotations=READ)
def list_open_incidents(
    actor_id: str,
    project_id: str | None = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """List recent unresolved incidents from only the actor's granted projects."""

    return _invoke(
        lambda: _service().list_open_incidents(
            _verified_actor(actor_id),
            project_id=project_id,
            limit=limit,
        )
    )


@mcp.tool(annotations=READ)
def get_incident(actor_id: str, incident_id: str) -> dict[str, Any]:
    """Read an incident when the actor has access to its project."""

    return _invoke(lambda: _service().get_incident(_verified_actor(actor_id), incident_id))


@mcp.tool(annotations=READ)
def list_actions_for_incident(
    actor_id: str,
    incident_id: str,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """List recent redacted action summaries linked to a granted incident."""

    return _invoke(
        lambda: _service().list_actions_for_incident(
            _verified_actor(actor_id),
            incident_id,
            limit=limit,
        )
    )


@mcp.tool()
def set_incident_status(
    actor_id: str,
    request_id: str,
    incident_id: str,
    status: Literal["open", "monitoring", "resolved"],
) -> dict[str, Any]:
    """Idempotently set a granted incident to open, monitoring or resolved."""

    return _invoke(
        lambda: _service().set_incident_status(
            actor_id=_verified_actor(actor_id),
            request_id=request_id,
            incident_id=incident_id,
            status=status,
        )
    )


@mcp.tool()
def request_runbook_action(
    actor_id: str,
    request_id: str,
    runbook_id: str,
    parameters: dict[str, str | int | bool],
    reason: str,
    incident_id: str | None = None,
    mission_id: str | None = None,
) -> dict[str, Any]:
    """Request a runbook action; A/B run automatically and C waits for a human."""

    return _invoke(
        lambda: _service().request_action(
            actor_id=_verified_actor(actor_id),
            request_id=request_id,
            runbook_id=runbook_id,
            parameters=parameters,
            reason=reason,
            incident_id=incident_id,
            mission_id=mission_id,
        )
    )


@mcp.tool(annotations=READ)
def get_action(actor_id: str, action_id: str) -> dict[str, Any]:
    """Read the public state and bounded result of an authorized action."""

    return _invoke(lambda: _service().get_action(_verified_actor(actor_id), action_id))


@mcp.tool()
def execute_approved_action(actor_id: str, action_id: str) -> dict[str, Any]:
    """Execute a class C action only after its separate human approval is durable."""

    return _invoke(
        lambda: _service().execute_action(
            actor_id=_verified_actor(actor_id),
            action_id=action_id,
        )
    )


@mcp.tool(annotations=READ)
def get_mission_state(actor_id: str, mission_id: str) -> dict[str, Any]:
    """Read the durable V1 context required to resume a granted mission."""

    return _invoke(
        lambda: _service().mission_control.get_mission_state(
            _verified_actor(actor_id), mission_id
        )
    )


@mcp.tool()
def update_mission_state(
    actor_id: str,
    request_id: str,
    mission_id: str,
    environment: str | None = None,
    priority: Annotated[int | None, Field(ge=0, le=100)] = None,
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
    max_iterations: Annotated[int | None, Field(ge=1, le=100)] = None,
    max_api_calls: Annotated[int | None, Field(ge=0, le=100)] = None,
    deadline: str | None = None,
) -> dict[str, Any]:
    """Idempotently update bounded resumable mission context."""

    return _invoke(
        lambda: _service().mission_control.update_mission_state(
            actor_id=_verified_actor(actor_id),
            request_id=request_id,
            mission_id=mission_id,
            environment=environment,
            priority=priority,
            objective=objective,
            summary=summary,
            impact=impact,
            risk=risk,
            plan=plan,
            current_step=current_step,
            remaining_steps=remaining_steps,
            resources=resources,
            files=files,
            next_action=next_action,
            current_model_role=current_model_role,
            current_model=current_model,
            max_iterations=max_iterations,
            max_api_calls=max_api_calls,
            deadline=deadline,
        )
    )


@mcp.tool()
def create_mission_checkpoint(
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
    """Persist a handoff-safe mission checkpoint outside model context."""

    return _invoke(
        lambda: _service().mission_control.create_checkpoint(
            actor_id=_verified_actor(actor_id),
            request_id=request_id,
            mission_id=mission_id,
            phase=phase,
            summary=summary,
            decisions=decisions,
            completed=completed,
            pending=pending,
            evidence=evidence,
            model_role=model_role,
            model_name=model_name,
        )
    )


@mcp.tool(annotations=READ)
def list_mission_checkpoints(
    actor_id: str,
    mission_id: str,
    limit: Annotated[int, Field(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """List recent durable checkpoints for a granted mission."""

    return _invoke(
        lambda: _service().mission_control.list_checkpoints(
            _verified_actor(actor_id), mission_id, limit=limit
        )
    )


@mcp.tool()
def add_mission_record(
    actor_id: str,
    request_id: str,
    mission_id: str,
    kind: Literal["observation", "analysis", "decision", "handoff", "escalation"],
    content: str,
    evidence: list[str],
    incident_id: str | None = None,
    confidence: Annotated[float | None, Field(ge=0.0, le=1.0)] = None,
    model_trace_id: str | None = None,
) -> dict[str, Any]:
    """Record observation, analysis or action reasoning as separate audited state."""

    return _invoke(
        lambda: _service().mission_control.add_record(
            actor_id=_verified_actor(actor_id),
            request_id=request_id,
            mission_id=mission_id,
            kind=kind,
            content=content,
            evidence=evidence,
            incident_id=incident_id,
            confidence=confidence,
            model_trace_id=model_trace_id,
        )
    )


@mcp.tool(annotations=READ)
def list_memory_missions(actor_id: str, project_id: str,
                         after: Annotated[int, Field(ge=0)] = 0,
                         limit: Annotated[int, Field(ge=1, le=50)] = 50) -> dict[str, Any]:
    """Discover scoped mission history, including missions already completed."""
    return _invoke(lambda: _service().list_memory_missions(
        _verified_actor(actor_id), project_id, after, limit))


@mcp.tool(annotations=READ)
def list_memory_history(actor_id: str, project_id: str,
                        after: Annotated[int, Field(ge=0)] = 0,
                        limit: Annotated[int, Field(ge=1, le=50)] = 50) -> dict[str, Any]:
    """Page source-attributed history for shared memory, including closed missions.

    Returns public records only, never action stdout, parameters or credentials.
    The cursor advances only after the caller has durably stored the page.
    """
    return _invoke(lambda: _service().list_memory_history(
        _verified_actor(actor_id), project_id, after, limit))


@mcp.tool(annotations=READ)
def list_mission_records(
    actor_id: str,
    mission_id: str,
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    """List bounded observations, analyses, decisions and handoffs."""

    return _invoke(
        lambda: _service().mission_control.list_records(
            _verified_actor(actor_id), mission_id, limit=limit
        )
    )


@mcp.tool()
def claim_next_mission(
    actor_id: str,
    request_id: str,
    project_id: str | None = None,
    lease_seconds: Annotated[int, Field(ge=30, le=3600)] = 300,
) -> dict[str, Any]:
    """Atomically claim the highest-priority granted mission with a lease."""

    return _invoke(
        lambda: _service().mission_control.claim_next(
            actor_id=_verified_actor(actor_id),
            request_id=request_id,
            project_id=project_id,
            lease_seconds=lease_seconds,
        )
    )


@mcp.tool()
def update_mission_lease(
    actor_id: str,
    request_id: str,
    mission_id: str,
    operation: Literal["heartbeat", "release"],
    lease_seconds: Annotated[int, Field(ge=30, le=3600)] = 300,
) -> dict[str, Any]:
    """Heartbeat or release a mission queue lease owned by this actor."""

    return _invoke(
        lambda: _service().mission_control.mission_lease(
            actor_id=_verified_actor(actor_id),
            request_id=request_id,
            mission_id=mission_id,
            operation=operation,
            lease_seconds=lease_seconds,
        )
    )


@mcp.tool()
def acquire_resource_lock(
    actor_id: str,
    request_id: str,
    mission_id: str,
    resource_id: str,
    lease_seconds: Annotated[int, Field(ge=30, le=3600)] = 300,
) -> dict[str, Any]:
    """Acquire an audited exclusive resource lease with a fencing token."""

    return _invoke(
        lambda: _service().mission_control.acquire_resource_lock(
            actor_id=_verified_actor(actor_id),
            request_id=request_id,
            mission_id=mission_id,
            resource_id=resource_id,
            lease_seconds=lease_seconds,
        )
    )


@mcp.tool()
def update_resource_lock(
    actor_id: str,
    request_id: str,
    mission_id: str,
    resource_id: str,
    operation: Literal["renew", "release"],
    lease_seconds: Annotated[int, Field(ge=30, le=3600)] = 300,
) -> dict[str, Any]:
    """Renew or release an exclusive resource lease owned by this mission."""

    return _invoke(
        lambda: _service().mission_control.resource_lock_lease(
            actor_id=_verified_actor(actor_id),
            request_id=request_id,
            mission_id=mission_id,
            resource_id=resource_id,
            operation=operation,
            lease_seconds=lease_seconds,
        )
    )


@mcp.tool()
def record_model_trace(
    actor_id: str,
    request_id: str,
    mission_id: str,
    model_role: str,
    provider: str,
    model_name: str,
    reason: str,
    outcome: Literal["selected", "succeeded", "failed", "escalated", "refused"],
    confidence: Annotated[float | None, Field(ge=0.0, le=1.0)] = None,
    input_tokens: Annotated[int, Field(ge=0)] = 0,
    output_tokens: Annotated[int, Field(ge=0)] = 0,
    cost_microunits: Annotated[int, Field(ge=0)] = 0,
    parent_trace_id: str | None = None,
    finished_at: str | None = None,
) -> dict[str, Any]:
    """Append a model selection or handoff to the central audited trace."""

    return _invoke(
        lambda: _service().mission_control.record_model_trace(
            actor_id=_verified_actor(actor_id),
            request_id=request_id,
            mission_id=mission_id,
            model_role=model_role,
            provider=provider,
            model_name=model_name,
            reason=reason,
            outcome=outcome,
            confidence=confidence,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_microunits=cost_microunits,
            parent_trace_id=parent_trace_id,
            finished_at=finished_at,
        )
    )


@mcp.tool(annotations=READ)
def list_model_traces(
    actor_id: str,
    mission_id: str,
    limit: Annotated[int, Field(ge=1, le=100)] = 50,
) -> dict[str, Any]:
    """List the ordered model/handoff chain for a granted mission."""

    return _invoke(
        lambda: _service().mission_control.list_model_traces(
            _verified_actor(actor_id), mission_id, limit=limit
        )
    )


@mcp.tool(annotations=READ)
def get_incident_details(actor_id: str, incident_id: str) -> dict[str, Any]:
    """Read the structured incident report for a granted project."""

    return _invoke(
        lambda: _service().mission_control.get_incident_details(
            _verified_actor(actor_id), incident_id
        )
    )


@mcp.tool()
def update_incident_details(
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
    """Idempotently enrich a granted incident with a durable structured report."""

    return _invoke(
        lambda: _service().mission_control.update_incident_details(
            actor_id=_verified_actor(actor_id),
            request_id=request_id,
            incident_id=incident_id,
            service=service,
            resource_id=resource_id,
            environment=environment,
            symptoms=symptoms,
            impact=impact,
            probable_causes=probable_causes,
            confirmed_cause=confirmed_cause,
            resolution=resolution,
            rollback=rollback,
            recommendations=recommendations,
            versions=versions,
        )
    )


@mcp.tool()
def record_action_artifacts(
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
    """Record immutable before/after, diff, health-check and rollback evidence."""

    return _invoke(
        lambda: _service().mission_control.record_action_artifacts(
            actor_id=_verified_actor(actor_id),
            request_id=request_id,
            action_id=action_id,
            before_digest=before_digest,
            after_digest=after_digest,
            target_version=target_version,
            rollback_ref=rollback_ref,
            diff_artifact=diff_artifact,
            healthcheck=healthcheck,
        )
    )


@mcp.tool(annotations=READ)
def get_action_artifacts(actor_id: str, action_id: str) -> dict[str, Any]:
    """Read immutable deployment/configuration evidence for an authorized action."""

    return _invoke(
        lambda: _service().mission_control.get_action_artifacts(
            _verified_actor(actor_id), action_id
        )
    )


@mcp.tool(annotations=READ)
def verify_audit_chain() -> dict[str, Any]:
    """Verify the broker's append-only event hash chain and durable head."""

    return _invoke(lambda: _service().database.verify_audit_chain().public())


def main() -> None:
    if not os.environ.get("OPS_BROKER_MCP_ACTOR_ID"):
        raise SystemExit("OPS_BROKER_MCP_ACTOR_ID is required")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
