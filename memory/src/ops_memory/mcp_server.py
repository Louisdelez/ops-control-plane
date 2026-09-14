"""Official MCP SDK adapter over the authenticated local memory socket."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

try:
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations
    from pydantic import Field
except ImportError as exc:  # pragma: no cover - optional deployment boundary
    raise SystemExit("Install the pinned requirements-mcp.txt dependency set") from exc

from .protocol import PROTOCOL_VERSION, socket_request


mcp = MCPServer(
    "ops-memory",
    title="Dell Ops semantic memory",
    description="Fail-closed, project-scoped retrieval and curated memory ingestion.",
    instructions=(
        "Memory results are advisory and never secrets or current-state authority. "
        "Respect verify_against, never widen scope, and use explicit promotion only."
    ),
    version="0.1.2",
)


READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


def _actor() -> str:
    actor = os.environ.get("OPS_MEMORY_MCP_ACTOR", "")
    if not actor or len(actor) > 128:
        raise RuntimeError("MCP actor identity is not configured")
    return actor


def _socket() -> Path:
    value = os.environ.get("OPS_MEMORY_SOCKET", "/run/ops-memory/ops-memory.sock")
    path = Path(value)
    if not path.is_absolute() or path != Path("/run/ops-memory/ops-memory.sock"):
        raise RuntimeError("MCP memory socket is not the production socket")
    return path


def _call(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = socket_request(
            _socket(),
            {
                "version": PROTOCOL_VERSION,
                "request_id": str(uuid.uuid4()),
                "operation": operation,
                "actor": _actor(),
                "payload": payload,
            },
            300.0 if operation == "reindex_api" else 15.0,
        )
        if response.get("ok") is True:
            return {"ok": True, "data": response.get("result")}
        error = response.get("error")
        if not isinstance(error, dict):
            raise RuntimeError
        return {
            "ok": False,
            "error": {
                "code": str(error.get("code", "memory_error")),
                "message": str(error.get("message", "memory request failed")),
            },
        }
    except Exception:
        return {
            "ok": False,
            "error": {"code": "memory_unavailable", "message": "local memory service unavailable"},
        }


@mcp.tool(annotations=READ)
def memory_search(
    query: Annotated[str, Field(min_length=1, max_length=16_384)],
    project: Annotated[str, Field(min_length=1, max_length=128)],
    environment: Annotated[str, Field(min_length=1, max_length=64)],
    max_classification: Literal["public", "internal", "restricted", "confidential"],
    categories: list[str] | None = None,
    kinds: list[Literal["observation", "information", "decision", "rule", "procedure", "incident", "summary"]] | None = None,
    top_k: Annotated[int, Field(ge=1, le=5)] = 5,
    candidate_limit: Annotated[int, Field(ge=10, le=30)] = 20,
    important: bool = False,
    allow_api: bool = False,
) -> dict[str, Any]:
    """Retrieve at most five authorized memories; current facts must be verified."""

    return _call(
        "search",
        {
            "query": query,
            "project": project,
            "environment": environment,
            "max_classification": max_classification,
            "categories": categories or [],
            "kinds": kinds or [],
            "top_k": top_k,
            "candidate_limit": candidate_limit,
            "important": important,
            "allow_api": allow_api,
        },
    )


@mcp.tool(annotations=READ)
def memory_get(memory_id: str) -> dict[str, Any]:
    """Read one memory only when the fixed actor can see its complete scope."""

    return _call("get", {"id": memory_id})


@mcp.tool()
def memory_ingest(
    content: Annotated[str, Field(min_length=1, max_length=131_072)],
    project: Annotated[str, Field(min_length=1, max_length=128)],
    environment: Annotated[str, Field(min_length=1, max_length=64)],
    classification: Literal["public", "internal", "restricted", "confidential"],
    category: Annotated[str, Field(min_length=1, max_length=128)],
    kind: Literal["noise", "conversation", "observation", "information", "decision", "rule", "procedure", "incident"],
    tier: Literal["working", "cold"],
    allowed_roles: list[str],
    source_type: Annotated[str, Field(min_length=1, max_length=64)],
    source_uri: Annotated[str, Field(min_length=1, max_length=2048)],
    source_ref: Annotated[str, Field(min_length=1, max_length=512)],
    source_authority: Annotated[str, Field(min_length=1, max_length=128)],
    metadata: dict[str, Any] | None = None,
    importance: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5,
    valid_until: str | None = None,
    supersedes_id: str | None = None,
    reviewed: bool = False,
    promote: bool = False,
) -> dict[str, Any]:
    """Store a curated, source-attributed memory; secret-like content is refused."""

    payload: dict[str, Any] = {
        "content": content,
        "project": project,
        "environment": environment,
        "classification": classification,
        "category": category,
        "kind": kind,
        "tier": tier,
        "allowed_roles": allowed_roles,
        "source_type": source_type,
        "source_uri": source_uri,
        "source_ref": source_ref,
        "source_authority": source_authority,
        "metadata": metadata or {},
        "importance": importance,
        "reviewed": reviewed,
        "promote": promote,
    }
    if valid_until is not None:
        payload["valid_until"] = valid_until
    if supersedes_id is not None:
        payload["supersedes_id"] = supersedes_id
    return _call("ingest", payload)


@mcp.tool()
def memory_summarize(
    record_ids: list[str],
    objective: str | None = None,
    decisions: list[str] | None = None,
    changes: list[str] | None = None,
    open_items: list[str] | None = None,
    category: str = "mission-summary",
    metadata: dict[str, Any] | None = None,
    importance: Annotated[float, Field(ge=0.0, le=1.0)] = 0.7,
) -> dict[str, Any]:
    """Create a structured summary linked to, but not replacing, its originals."""

    return _call(
        "summarize",
        {
            "record_ids": record_ids,
            "summary": {
                "objective": objective,
                "decisions": decisions or [],
                "changes": changes or [],
                "open_items": open_items or [],
            },
            "category": category,
            "metadata": metadata or {},
            "importance": importance,
        },
    )


@mcp.tool()
def memory_invalidate(memory_id: str, reason: Annotated[str, Field(min_length=1, max_length=512)]) -> dict[str, Any]:
    """Invalidate one authorized memory while retaining its audited history."""

    return _call("invalidate", {"id": memory_id, "reason": reason})


@mcp.tool(annotations=READ)
def memory_stats() -> dict[str, Any]:
    """Return bounded catalogue and provider-call counts, never memory content."""

    return _call("stats", {})


@mcp.tool(annotations=READ)
def memory_health() -> dict[str, Any]:
    """Check the local catalogue and primary vector backend."""

    return _call("health", {})


def main() -> None:
    _actor()
    _socket()
    mcp.run(transport="stdio")




@mcp.tool()
def memory_reindex_api(ids: Annotated[list[str], Field(min_length=1, max_length=20)]) -> dict[str, Any]:
    """Explicitly embed up to 20 visible current memories using the configured API budget.

    Preserves the deterministic index. Requires write and api-escalate rights.
    The selected documents are sent to the configured embedding provider.
    """
    return _call("reindex_api", {"ids": ids})


if __name__ == "__main__":
    main()
