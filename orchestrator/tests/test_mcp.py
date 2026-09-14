from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")
from mcp import Client  # noqa: E402

import ops_orchestrator.mcp_server as adapter  # noqa: E402


def test_official_stdio_mcp_exposes_bounded_non_executing_tools(monkeypatch):
    calls = []

    def fake_request(socket_path, method, path, payload=None, *, timeout=240):
        calls.append((socket_path, method, path, payload, timeout))
        return {"status": "ok"}

    monkeypatch.setattr(adapter, "_request", fake_request)
    monkeypatch.setenv("OPS_ORCHESTRATOR_MCP_ACTOR_ID", "codex-supervised")
    monkeypatch.setenv(
        "OPS_ORCHESTRATOR_MCP_PROJECTS",
        "minecraft,infra-shared,network-shared,monitoring-shared,backup-shared",
    )

    async def exercise():
        async with Client(adapter.mcp) as client:
            listed = await client.list_tools()
            health = await client.call_tool(
                "get_orchestrator_health", {"actor_id": "codex-supervised"}
            )
            denied = await client.call_tool(
                "get_orchestrator_health", {"actor_id": "invented-agent"}
            )
            routed = await client.call_tool(
                "route_model_task",
                {
                    "actor_id": "codex-supervised",
                    "mission_id": "incident-483",
                    "project_id": "minecraft",
                    "task_type": "ANALYZE",
                    "risk": "LOW",
                    "complexity": 0.1,
                    "impact": 0.1,
                    "confidence": 0.9,
                    "urgency": "NORMAL",
                    "deterministic_available": True,
                    "instructions": "Use deterministic evidence.",
                    "mission": "Classify one alert.",
                },
            )
            return listed.tools, health.structured_content, denied.structured_content, routed.structured_content

    tools, health, denied, routed = asyncio.run(exercise())
    names = {tool.name for tool in tools}
    assert names == {
        "get_orchestrator_health",
        "get_model_budgets",
        "get_model_performance",
        "get_model_catalogue",
        "get_model_trace",
        "preview_model_candidates",
        "route_model_task",
        "record_orchestrator_signal",
        "record_model_outcome",
    }
    assert not any("approval" in name or "secret" in name or "execute" in name for name in names)
    assert health["ok"] is True
    assert denied["ok"] is False
    assert denied["error"]["code"] == "authorization_denied"
    assert routed["ok"] is True
    assert calls[-1][2] == "/v1/route"
    assert calls[-1][3]["deterministic_available"] is True
    assert calls[-1][3]["remote_allowed"] is True


def test_mcp_route_denies_cross_project_before_socket_call(monkeypatch):
    calls = []
    monkeypatch.setattr(adapter, "_request", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setenv("OPS_ORCHESTRATOR_MCP_ACTOR_ID", "minecraft-ops")
    monkeypatch.setenv("OPS_ORCHESTRATOR_MCP_PROJECTS", "minecraft")

    result = adapter.route_model_task(
        actor_id="minecraft-ops",
        mission_id="incident-483",
        project_id="infra-shared",
        task_type="ANALYZE",
        risk="LOW",
        complexity=0.1,
        impact=0.1,
        confidence=0.9,
        urgency="NORMAL",
        deterministic_available=False,
        instructions="Use bounded evidence.",
        mission="Classify one alert.",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "authorization_denied"
    assert calls == []


def test_mcp_signal_checks_project_allowlist_before_socket_call(monkeypatch):
    calls = []
    monkeypatch.setattr(adapter, "_request", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setenv("OPS_ORCHESTRATOR_MCP_ACTOR_ID", "minecraft-ops")
    monkeypatch.setenv("OPS_ORCHESTRATOR_MCP_PROJECTS", "minecraft")

    denied = adapter.record_orchestrator_signal(
        actor_id="minecraft-ops",
        project_id="infra-shared",
        signal="tool_error",
    )

    assert denied["ok"] is False
    assert denied["error"]["code"] == "authorization_denied"
    assert calls == []


def test_mcp_signal_submits_exact_project_scoped_payload(monkeypatch):
    calls = []

    def fake_request(socket_path, method, path, payload=None, *, timeout=240):
        calls.append((socket_path, method, path, payload, timeout))
        return {"status": "recorded"}

    monkeypatch.setattr(adapter, "_request", fake_request)
    monkeypatch.setenv("OPS_ORCHESTRATOR_MCP_ACTOR_ID", "minecraft-ops")
    monkeypatch.setenv("OPS_ORCHESTRATOR_MCP_PROJECTS", "minecraft")

    result = adapter.record_orchestrator_signal(
        actor_id="minecraft-ops",
        project_id="minecraft",
        signal="memory_search",
    )

    assert result["ok"] is True
    assert calls == [
        (
            "/run/ops-orchestrator/api.sock",
            "POST",
            "/v1/signals",
            {"signal": "memory_search", "project_id": "minecraft"},
            10,
        )
    ]


def test_mcp_model_outcome_is_bounded_and_project_scoped(monkeypatch):
    calls = []

    def fake_request(socket_path, method, path, payload=None, *, timeout=240):
        calls.append((socket_path, method, path, payload, timeout))
        return {"status": "recorded"}

    monkeypatch.setattr(adapter, "_request", fake_request)
    monkeypatch.setenv("OPS_ORCHESTRATOR_MCP_ACTOR_ID", "minecraft-ops")
    monkeypatch.setenv("OPS_ORCHESTRATOR_MCP_PROJECTS", "minecraft")

    denied = adapter.record_model_outcome(
        actor_id="minecraft-ops",
        route_id="route-1",
        mission_id="mission-1",
        project_id="infra-shared",
        outcome="succeeded",
        quality=0.9,
        corrections_required=0,
        evidence_kind="deterministic_test",
    )
    assert denied["ok"] is False
    assert calls == []

    accepted = adapter.record_model_outcome(
        actor_id="minecraft-ops",
        route_id="route-1",
        mission_id="mission-1",
        project_id="minecraft",
        outcome="succeeded",
        quality=0.9,
        corrections_required=0,
        evidence_kind="deterministic_test",
    )
    assert accepted["ok"] is True
    assert calls == [
        (
            "/run/ops-orchestrator/api.sock",
            "POST",
            "/v1/model-outcomes",
            {
                "route_id": "route-1",
                "mission_id": "mission-1",
                "project_id": "minecraft",
                "outcome": "succeeded",
                "quality": 0.9,
                "corrections_required": 0,
                "evidence_kind": "deterministic_test",
            },
            10,
        )
    ]


def test_mcp_trace_denies_cross_project_after_bounded_lookup(monkeypatch):
    monkeypatch.setattr(
        adapter,
        "_request",
        lambda *_args, **_kwargs: {
            "mission_id": "incident-483",
            "project_id": "infra-shared",
            "chain_valid": True,
            "events": [],
        },
    )
    monkeypatch.setenv("OPS_ORCHESTRATOR_MCP_ACTOR_ID", "minecraft-ops")
    monkeypatch.setenv("OPS_ORCHESTRATOR_MCP_PROJECTS", "minecraft")

    result = adapter.get_model_trace("minecraft-ops", "incident-483")

    assert result["ok"] is False
    assert result["error"]["code"] == "authorization_denied"


def test_mcp_schema_publishes_score_context_and_enum_bounds():
    async def exercise():
        async with Client(adapter.mcp) as client:
            listed = await client.list_tools()
            return {tool.name: tool for tool in listed.tools}

    tools = asyncio.run(exercise())
    route = tools["route_model_task"]
    properties = route.input_schema["properties"]
    for score in ("complexity", "impact", "confidence"):
        assert properties[score]["minimum"] == 0.0
        assert properties[score]["maximum"] == 1.0
    assert properties["risk"]["enum"] == ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    assert "remote_allowed" not in properties
    assert route.annotations.destructive_hint is False
    assert tools["get_model_trace"].annotations.read_only_hint is True
    signal = tools["record_orchestrator_signal"]
    assert "project_id" in signal.input_schema["required"]
    assert signal.input_schema["properties"]["project_id"]["maxLength"] == 128
    outcome = tools["record_model_outcome"]
    assert outcome.input_schema["properties"]["quality"]["minimum"] == 0.0
    assert outcome.input_schema["properties"]["quality"]["maximum"] == 1.0
    assert outcome.input_schema["properties"]["corrections_required"]["maximum"] == 1000
