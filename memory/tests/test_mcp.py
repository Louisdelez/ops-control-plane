from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")
from mcp import Client  # noqa: E402

import ops_memory.mcp_server as adapter  # noqa: E402


def test_official_mcp_adapter_has_fixed_actor_and_bounded_tools(monkeypatch) -> None:
    calls: list[dict] = []

    def request(_path, document, _timeout=15.0):
        calls.append(document)
        return {"ok": True, "result": {"results": [], "backend": "qdrant"}}

    monkeypatch.setattr(adapter, "socket_request", request)
    monkeypatch.setenv("OPS_MEMORY_MCP_ACTOR", "codex-supervised")

    async def exercise():
        async with Client(adapter.mcp) as client:
            listed = await client.list_tools()
            result = await client.call_tool(
                "memory_search",
                {
                    "query": "incident proxy",
                    "project": "minecraft",
                    "environment": "production",
                    "max_classification": "restricted",
                    "top_k": 3,
                    "candidate_limit": 20,
                },
            )
            return listed.tools, result.structured_content

    tools, result = asyncio.run(exercise())
    names = {tool.name for tool in tools}
    readers = {tool.name for tool in tools if tool.annotations and tool.annotations.read_only_hint is True}
    assert readers == {'memory_search', 'memory_get', 'memory_stats', 'memory_health'}
    assert names == {
        "memory_search",
        "memory_reindex_api",
        "memory_get",
        "memory_ingest",
        "memory_summarize",
        "memory_invalidate",
        "memory_stats",
        "memory_health",
    }
    assert result == {"ok": True, "data": {"results": [], "backend": "qdrant"}}
    assert calls[0]["actor"] == "codex-supervised"
    assert calls[0]["operation"] == "search"
    for tool in tools:
        assert "actor" not in tool.input_schema.get("properties", {})
    search_schema = next(tool.input_schema for tool in tools if tool.name == "memory_search")
    assert search_schema["properties"]["top_k"]["maximum"] == 5
    assert search_schema["properties"]["candidate_limit"]["maximum"] == 30


def test_mcp_adapter_hides_socket_errors(monkeypatch) -> None:
    monkeypatch.setenv("OPS_MEMORY_MCP_ACTOR", "codex-supervised")
    monkeypatch.setattr(
        adapter,
        "socket_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("sensitive/path")),
    )
    result = adapter.memory_stats()
    assert result == {
        "ok": False,
        "error": {
            "code": "memory_unavailable",
            "message": "local memory service unavailable",
        },
    }
