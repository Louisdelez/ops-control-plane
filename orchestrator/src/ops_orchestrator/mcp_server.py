"""Official MCP SDK stdio adapter for Codex and supervised local clients.

The adapter forwards bounded requests to the permission-restricted Unix API.
It has no operations executor, no secret tool and no human-approval tool.
"""

from __future__ import annotations

import os
import re
from typing import Annotated, Any, Callable, Literal

try:
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations
    from pydantic import Field
except ImportError as exc:  # pragma: no cover - optional dependency boundary
    raise SystemExit("Install the MCP adapter with: pip install 'ops-orchestrator[mcp]'") from exc

from .cli import _request


Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
BoundedText = Annotated[str, Field(min_length=1, max_length=6000)]
Score = Annotated[float, Field(ge=0.0, le=1.0)]
CapabilityRating = Annotated[int, Field(ge=0, le=10)]
ContextItems = Annotated[list[Annotated[str, Field(min_length=1, max_length=6000)]], Field(max_length=8)]
Specialties = Annotated[
    list[Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]],
    Field(max_length=16),
]


mcp = MCPServer(
    "ops-orchestrator",
    title="Dell Ops model orchestrator",
    description="Fail-closed API-first routing, budgets and model traces over a local Unix API.",
    instructions=(
        "This server never authorizes or executes production actions. Prefer a broker-authorized "
        "deterministic tool; use route_model_task only for bounded reasoning. A refusal, exhausted "
        "budget, low confidence, approval requirement, or unavailable provider means stop and "
        "escalate. External processing is authorized by the root-owned Unix-peer policy, never by "
        "a model argument; send only minimal context containing no secrets. Coder output requires "
        "tests, human review and Git. "
        "Use get_model_trace when resuming a model handoff."
    ),
    version="0.2.0",
)


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
ADVISORY_CALL = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
LOCAL_RECORD = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


class IdentityDenied(Exception):
    pass


_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _socket_path() -> str:
    value = os.environ.get("OPS_ORCHESTRATOR_SOCKET", "/run/ops-orchestrator/api.sock")
    if not value.startswith("/") or "\x00" in value:
        raise RuntimeError("invalid orchestrator socket configuration")
    return value


def _verified_actor(claimed_actor_id: str) -> str:
    configured = os.environ.get("OPS_ORCHESTRATOR_MCP_ACTOR_ID")
    if not configured:
        raise IdentityDenied("MCP actor identity is not configured")
    if claimed_actor_id != configured:
        raise IdentityDenied("MCP actor identity does not match the fixed local identity")
    return configured


def _allowed_projects() -> frozenset[str]:
    raw = os.environ.get("OPS_ORCHESTRATOR_MCP_PROJECTS", "")
    projects = raw.split(",") if raw else []
    if (
        not projects
        or len(projects) > 32
        or any(not _PROJECT_RE.fullmatch(project) for project in projects)
        or len(set(projects)) != len(projects)
    ):
        raise IdentityDenied("MCP project grants are not configured")
    return frozenset(projects)


def _verified_project(project_id: str) -> str:
    if project_id not in _allowed_projects():
        raise IdentityDenied("MCP actor has no grant for this project")
    return project_id


def _verified_trace(result: dict[str, Any]) -> dict[str, Any]:
    project_id = result.get("project_id")
    if not isinstance(project_id, str):
        raise IdentityDenied("model trace has no authorized project")
    _verified_project(project_id)
    return result


def _invoke(actor_id: str, function: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        _verified_actor(actor_id)
        return {"ok": True, "data": function()}
    except IdentityDenied as exc:
        return {"ok": False, "error": {"code": "authorization_denied", "message": str(exc)}}
    except Exception:
        # Socket paths, provider details, context and exception bodies are not exposed to a model.
        return {"ok": False, "error": {"code": "orchestrator_unavailable", "message": "local orchestrator request failed"}}


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def get_orchestrator_health(actor_id: Identifier) -> dict[str, Any]:
    """Read local router health and provider availability; makes no model call."""

    return _invoke(actor_id, lambda: _request(_socket_path(), "GET", "/v1/health", timeout=10))


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def get_model_budgets(actor_id: Identifier) -> dict[str, Any]:
    """Read durable daily/monthly call, token and cost budget usage."""

    return _invoke(actor_id, lambda: _request(_socket_path(), "GET", "/v1/budgets", timeout=10))


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def get_model_performance(actor_id: Identifier) -> dict[str, Any]:
    """Read bounded per-category outcomes and the exact adaptive scoring policy."""

    return _invoke(
        actor_id,
        lambda: _request(_socket_path(), "GET", "/v1/model-performance", timeout=10),
    )


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def get_model_catalogue(
    actor_id: Identifier,
    offset: Annotated[int, Field(ge=0, le=999999)] = 0,
    limit: Annotated[int, Field(ge=1, le=200)] = 100,
) -> dict[str, Any]:
    """List public model cards, capabilities, prices and non-secret runtime state."""

    return _invoke(
        actor_id,
        lambda: _request(
            _socket_path(),
            "GET",
            f"/v1/catalogue?offset={offset}&limit={limit}",
            timeout=10,
        ),
    )


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def preview_model_candidates(
    actor_id: Identifier,
    project_id: Identifier,
    reasoning: CapabilityRating = 0,
    code: CapabilityRating = 0,
    math: CapabilityRating = 0,
    research: CapabilityRating = 0,
    tools: CapabilityRating = 0,
    agentic: CapabilityRating = 0,
    instructions: CapabilityRating = 0,
    reliability: CapabilityRating = 0,
    factuality: CapabilityRating = 0,
    speed: CapabilityRating = 0,
    multilingual: CapabilityRating = 0,
    required_specialties: Specialties | None = None,
    maximum_monthly_cost_usd: Annotated[
        str | None,
        Field(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$"),
    ] = None,
    runtime_only: bool = True,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict[str, Any]:
    """Preview cheapest cards meeting hard capabilities; never invokes or activates a model."""

    try:
        _verified_actor(actor_id)
        _verified_project(project_id)
    except IdentityDenied as exc:
        return {
            "ok": False,
            "error": {"code": "authorization_denied", "message": str(exc)},
        }
    requirements = {
        name: value
        for name, value in {
            "reasoning": reasoning,
            "code": code,
            "math": math,
            "research": research,
            "tools": tools,
            "agentic": agentic,
            "instructions": instructions,
            "reliability": reliability,
            "factuality": factuality,
            "speed": speed,
            "multilingual": multilingual,
        }.items()
        if value > 0
    }
    payload: dict[str, Any] = {
        "project_id": project_id,
        "requirements": requirements,
        "required_specialties": required_specialties or [],
        "runtime_only": runtime_only,
        "limit": limit,
    }
    if maximum_monthly_cost_usd is not None:
        payload["maximum_monthly_cost_usd"] = maximum_monthly_cost_usd
    return _invoke(
        actor_id,
        lambda: _request(
            _socket_path(),
            "POST",
            "/v1/catalogue/route-preview",
            payload,
            timeout=10,
        ),
    )


@mcp.tool(annotations=READ_ONLY, structured_output=True)
def get_model_trace(actor_id: Identifier, mission_id: Identifier) -> dict[str, Any]:
    """Read and verify the hash-linked model chain for one exact mission."""

    from urllib.parse import quote

    return _invoke(
        actor_id,
        lambda: _verified_trace(
            _request(
                _socket_path(),
                "GET",
                "/v1/traces/" + quote(mission_id, safe=""),
                timeout=10,
            )
        ),
    )


@mcp.tool(annotations=ADVISORY_CALL, structured_output=True)
def route_model_task(
    actor_id: Identifier,
    mission_id: Identifier,
    project_id: Identifier,
    task_type: Literal["OBSERVE", "ANALYZE", "PLAN", "SUMMARIZE", "CODE", "EMBED", "RERANK"],
    risk: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"],
    complexity: Score,
    impact: Score,
    confidence: Score,
    urgency: Literal["ROUTINE", "NORMAL", "URGENT", "EMERGENCY"],
    deterministic_available: bool,
    instructions: BoundedText,
    mission: BoundedText,
    current_state: Annotated[str, Field(max_length=6000)] = "",
    relevant_memories: ContextItems | None = None,
    tool_results: ContextItems | None = None,
    requested_max_cost_usd: Annotated[
        str | None,
        Field(pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?$"),
    ] = None,
) -> dict[str, Any]:
    """Route bounded context to a logical role; returns advice, never an executed action."""

    try:
        _verified_actor(actor_id)
        _verified_project(project_id)
    except IdentityDenied as exc:
        return {
            "ok": False,
            "error": {"code": "authorization_denied", "message": str(exc)},
        }

    payload = {
        "mission_id": mission_id,
        "project_id": project_id,
        "task_type": task_type,
        "risk": risk,
        "complexity": complexity,
        "impact": impact,
        "confidence": confidence,
        "urgency": urgency,
        "deterministic_available": deterministic_available,
        # This is a request, not the authorization decision. The Unix API
        # intersects it with its SO_PEERCRED-derived, root-owned route grant.
        "remote_allowed": True,
        "context": {
            "instructions": instructions,
            "mission": mission,
            "current_state": current_state,
            "relevant_memories": relevant_memories or [],
            "tool_results": tool_results or [],
        },
    }
    if requested_max_cost_usd is not None:
        payload["requested_max_cost_usd"] = requested_max_cost_usd
    return _invoke(
        actor_id,
        lambda: _request(_socket_path(), "POST", "/v1/route", payload, timeout=240),
    )


@mcp.tool(annotations=LOCAL_RECORD, structured_output=True)
def record_orchestrator_signal(
    actor_id: Identifier,
    project_id: Identifier,
    signal: Literal["mission_succeeded", "mission_failed", "tool_error", "memory_search"],
) -> dict[str, Any]:
    """Record one bounded observability signal; human approval is deliberately unavailable."""

    try:
        _verified_actor(actor_id)
        _verified_project(project_id)
    except IdentityDenied as exc:
        return {
            "ok": False,
            "error": {"code": "authorization_denied", "message": str(exc)},
        }

    return _invoke(
        actor_id,
        lambda: _request(
            _socket_path(),
            "POST",
            "/v1/signals",
            {"signal": signal, "project_id": project_id},
            timeout=10,
        ),
    )


@mcp.tool(annotations=LOCAL_RECORD, structured_output=True)
def record_model_outcome(
    actor_id: Identifier,
    route_id: Identifier,
    mission_id: Identifier,
    project_id: Identifier,
    outcome: Literal["succeeded", "failed"],
    quality: Score,
    corrections_required: Annotated[int, Field(ge=0, le=1000)],
    evidence_kind: Literal[
        "deterministic_test", "human_review", "production_observation"
    ],
) -> dict[str, Any]:
    """Record external validation for one exact routed result; never self-evaluate."""

    try:
        _verified_actor(actor_id)
        _verified_project(project_id)
    except IdentityDenied as exc:
        return {
            "ok": False,
            "error": {"code": "authorization_denied", "message": str(exc)},
        }
    return _invoke(
        actor_id,
        lambda: _request(
            _socket_path(),
            "POST",
            "/v1/model-outcomes",
            {
                "route_id": route_id,
                "mission_id": mission_id,
                "project_id": project_id,
                "outcome": outcome,
                "quality": quality,
                "corrections_required": corrections_required,
                "evidence_kind": evidence_kind,
            },
            timeout=10,
        ),
    )


def main() -> None:
    if not os.environ.get("OPS_ORCHESTRATOR_MCP_ACTOR_ID") or not os.environ.get(
        "OPS_ORCHESTRATOR_MCP_PROJECTS"
    ):
        raise SystemExit(
            "OPS_ORCHESTRATOR_MCP_ACTOR_ID and OPS_ORCHESTRATOR_MCP_PROJECTS are required"
        )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
