from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("mcp")
from mcp import Client  # noqa: E402

import ops_broker.mcp_server as adapter  # noqa: E402

from conftest import Harness, request_id


def test_official_mcp_adapter_has_no_approval_or_secret_tool(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter, "_service", lambda: harness.service)
    monkeypatch.setenv("OPS_BROKER_MCP_ACTOR_ID", "reader")

    async def exercise() -> tuple[
        list[str],
        dict[str, object] | None,
        dict[str, object] | None,
    ]:
        async with Client(adapter.mcp) as client:
            listed = await client.list_tools()
            called = await client.call_tool(
                "list_authorized_runbooks",
                {"actor_id": "reader"},
            )
            spoofed = await client.call_tool(
                "list_authorized_runbooks",
                {"actor_id": "operator"},
            )
            return (
                [tool.name for tool in listed.tools],
                called.structured_content,
                spoofed.structured_content,
            )

    names, result, spoofed = asyncio.run(exercise())
    assert "list_authorized_runbooks" in names
    assert {
        "list_open_incidents",
        "list_open_missions",
        "list_actions_for_incident",
        "list_actions_for_mission",
        "get_mission",
        "set_incident_status",
        "set_mission_status",
    } <= set(names)
    assert not any("approval" in name or "secret" in name for name in names)
    assert result is not None
    assert result["ok"] is True
    assert spoofed is not None
    assert spoofed["ok"] is False
    assert spoofed["error"]["code"] == "authorization_denied"  # type: ignore[index]


def test_mcp_lifecycle_schemas_publish_bounded_limits_and_status_enums() -> None:
    async def exercise() -> dict[str, dict[str, object]]:
        async with Client(adapter.mcp) as client:
            listed = await client.list_tools()
            return {
                tool.name: tool.input_schema
                for tool in listed.tools
                if tool.name
                in {
                    "list_open_missions",
                    "list_open_incidents",
                    "list_actions_for_mission",
                    "list_actions_for_incident",
                    "set_mission_status",
                    "set_incident_status",
                }
            }

    schemas = asyncio.run(exercise())
    for name in (
        "list_open_missions",
        "list_open_incidents",
        "list_actions_for_mission",
        "list_actions_for_incident",
    ):
        limit = schemas[name]["properties"]["limit"]  # type: ignore[index]
        assert limit["minimum"] == 1
        assert limit["maximum"] == 50
    mission_status = schemas["set_mission_status"]["properties"]["status"]  # type: ignore[index]
    incident_status = schemas["set_incident_status"]["properties"]["status"]  # type: ignore[index]
    assert mission_status["enum"] == ["open", "paused", "completed"]
    assert incident_status["enum"] == ["open", "monitoring", "resolved"]


def test_mcp_resume_tools_enforce_identity_project_limit_and_lifecycle(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mission = harness.service.create_mission(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="resume via MCP",
    )
    incident = harness.service.create_incident(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="resume incident via MCP",
        severity="warning",
    )
    linked_action = harness.service.request_action(
        actor_id="operator",
        request_id=request_id(),
        runbook_id="test.check.v1",
        parameters={},
        reason="MCP resume action",
        mission_id=str(mission["id"]),
        incident_id=str(incident["id"]),
    )
    other_mission = harness.service.create_mission(
        actor_id="other-reader",
        request_id=request_id(),
        project_id="other",
        title="must remain private",
    )
    monkeypatch.setattr(adapter, "_service", lambda: harness.service)
    monkeypatch.setenv("OPS_BROKER_MCP_ACTOR_ID", "operator")

    async def exercise() -> tuple[dict[str, dict[str, object] | None], bool]:
        async with Client(adapter.mcp) as client:
            bad_limit = await client.call_tool(
                "list_open_incidents",
                {"actor_id": "operator", "limit": 51},
            )
            calls = {
                "missions": await client.call_tool(
                    "list_open_missions",
                    {"actor_id": "operator", "limit": 1},
                ),
                "incidents": await client.call_tool(
                    "list_open_incidents",
                    {"actor_id": "operator", "limit": 1},
                ),
                "mission": await client.call_tool(
                    "get_mission",
                    {"actor_id": "operator", "mission_id": mission["id"]},
                ),
                "mission_actions": await client.call_tool(
                    "list_actions_for_mission",
                    {"actor_id": "operator", "mission_id": mission["id"], "limit": 1},
                ),
                "incident_actions": await client.call_tool(
                    "list_actions_for_incident",
                    {"actor_id": "operator", "incident_id": incident["id"], "limit": 1},
                ),
                "cross_project": await client.call_tool(
                    "get_mission",
                    {"actor_id": "operator", "mission_id": other_mission["id"]},
                ),
                "mission_status": await client.call_tool(
                    "set_mission_status",
                    {
                        "actor_id": "operator",
                        "request_id": request_id(),
                        "mission_id": mission["id"],
                        "status": "paused",
                    },
                ),
                "incident_status": await client.call_tool(
                    "set_incident_status",
                    {
                        "actor_id": "operator",
                        "request_id": request_id(),
                        "incident_id": incident["id"],
                        "status": "monitoring",
                    },
                ),
            }
            return (
                {name: result.structured_content for name, result in calls.items()},
                bool(bad_limit.is_error),
            )

    results, bad_limit_rejected = asyncio.run(exercise())
    assert results["missions"] is not None
    assert results["missions"]["ok"] is True
    assert results["missions"]["data"][0]["id"] == mission["id"]  # type: ignore[index]
    assert results["missions"]["data"][0]["status"] == "open"  # type: ignore[index]
    assert results["incidents"] is not None
    assert results["incidents"]["ok"] is True
    assert results["mission"] is not None
    assert results["mission"]["ok"] is True
    assert results["mission_actions"] is not None
    assert results["mission_actions"]["data"][0]["id"] == linked_action["id"]  # type: ignore[index]
    assert results["incident_actions"] is not None
    assert results["incident_actions"]["data"][0]["id"] == linked_action["id"]  # type: ignore[index]
    assert "stdout" not in results["incident_actions"]["data"][0]  # type: ignore[index]
    assert results["cross_project"] is not None
    assert results["cross_project"]["error"]["code"] == "authorization_denied"  # type: ignore[index]
    assert bad_limit_rejected
    assert results["mission_status"] is not None
    assert results["mission_status"]["data"]["status"] == "paused"  # type: ignore[index]
    assert results["incident_status"] is not None
    assert results["incident_status"]["data"]["status"] == "monitoring"  # type: ignore[index]
    assert harness.database.verify_audit_chain().valid
