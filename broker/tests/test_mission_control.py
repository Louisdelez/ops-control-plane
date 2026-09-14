from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ops_broker.errors import AuthorizationDenied, BrokerError, Conflict

from conftest import Harness, request_id


def _mission(harness: Harness, title: str = "mission") -> dict[str, object]:
    return harness.service.create_mission(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title=title,
    )


def test_rich_mission_state_is_idempotent_scoped_and_resumable(harness: Harness) -> None:
    mission = _mission(harness)
    control = harness.service.mission_control
    initial = control.get_mission_state("reader", str(mission["id"]))
    assert initial["queue_state"] == "queued"
    assert initial["priority"] == 50
    assert initial["max_iterations"] == 12

    operation_id = request_id()
    changed = control.update_mission_state(
        actor_id="operator",
        request_id=operation_id,
        mission_id=str(mission["id"]),
        environment="production",
        priority=90,
        objective="Restore the service",
        summary="Facts collected",
        impact="Users cannot connect",
        risk="Shared proxy",
        plan=["observe", "diagnose", "request change"],
        current_step="diagnose",
        remaining_steps=["request change"],
        resources=["server:one", "dns:example"],
        files=["reports/incident.json"],
        next_action="compare configuration",
        current_model_role="ROLE_LOCAL_OPS",
        current_model="local-test",
        max_iterations=20,
        max_api_calls=3,
    )
    assert changed["replayed"] is False
    assert changed["priority"] == 90
    assert changed["plan"] == ["observe", "diagnose", "request change"]
    replay = control.update_mission_state(
        actor_id="operator",
        request_id=operation_id,
        mission_id=str(mission["id"]),
        environment="production",
        priority=90,
        objective="Restore the service",
        summary="Facts collected",
        impact="Users cannot connect",
        risk="Shared proxy",
        plan=["observe", "diagnose", "request change"],
        current_step="diagnose",
        remaining_steps=["request change"],
        resources=["server:one", "dns:example"],
        files=["reports/incident.json"],
        next_action="compare configuration",
        current_model_role="ROLE_LOCAL_OPS",
        current_model="local-test",
        max_iterations=20,
        max_api_calls=3,
    )
    assert replay["replayed"] is True
    with pytest.raises(AuthorizationDenied):
        control.update_mission_state(
            actor_id="reader",
            request_id=request_id(),
            mission_id=str(mission["id"]),
            summary="unauthorized",
        )


def test_checkpoints_and_typed_records_are_durable_and_audited(harness: Harness) -> None:
    mission = _mission(harness)
    control = harness.service.mission_control
    checkpoint_request = request_id()
    checkpoint = control.create_checkpoint(
        actor_id="operator",
        request_id=checkpoint_request,
        mission_id=str(mission["id"]),
        phase="diagnosis",
        summary="Proxy configuration differs",
        decisions=["Do not restart twice"],
        completed=["Collected logs"],
        pending=["Request approval"],
        evidence=["artifact://incident/logs.json"],
        model_role="ROLE_REASONING",
        model_name="test-reasoner",
    )
    assert checkpoint["sequence"] == 1
    assert control.create_checkpoint(
        actor_id="operator",
        request_id=checkpoint_request,
        mission_id=str(mission["id"]),
        phase="diagnosis",
        summary="Proxy configuration differs",
        decisions=["Do not restart twice"],
        completed=["Collected logs"],
        pending=["Request approval"],
        evidence=["artifact://incident/logs.json"],
        model_role="ROLE_REASONING",
        model_name="test-reasoner",
    )["replayed"] is True
    record = control.add_record(
        actor_id="operator",
        request_id=request_id(),
        mission_id=str(mission["id"]),
        kind="observation",
        content="Endpoint returned 503",
        evidence=["check://endpoint/1"],
        confidence=1.0,
    )
    assert record["kind"] == "observation"
    assert control.list_checkpoints("reader", str(mission["id"]))[0]["summary"]
    assert control.list_records("reader", str(mission["id"]))[0]["content"] == "Endpoint returned 503"
    assert control.get_mission_state("reader", str(mission["id"]))["next_action"] == "Request approval"
    assert harness.database.verify_audit_chain().valid


def test_priority_queue_claim_heartbeat_release_and_completion(harness: Harness) -> None:
    instant = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    harness.service.clock = lambda: instant
    low = _mission(harness, "low")
    high = _mission(harness, "high")
    control = harness.service.mission_control
    control.update_mission_state(
        actor_id="operator", request_id=request_id(), mission_id=str(low["id"]), priority=10
    )
    control.update_mission_state(
        actor_id="operator", request_id=request_id(), mission_id=str(high["id"]), priority=99
    )
    claim = control.claim_next(actor_id="operator", request_id=request_id(), lease_seconds=60)
    assert claim is not None and claim["id"] == high["id"]
    heartbeat = control.mission_lease(
        actor_id="operator",
        request_id=request_id(),
        mission_id=str(high["id"]),
        operation="heartbeat",
        lease_seconds=120,
    )
    assert heartbeat["queue_state"] == "claimed"
    released = control.mission_lease(
        actor_id="operator",
        request_id=request_id(),
        mission_id=str(high["id"]),
        operation="release",
    )
    assert released["queue_state"] == "queued"
    harness.service.set_mission_status(
        actor_id="operator",
        request_id=request_id(),
        mission_id=str(high["id"]),
        status="completed",
    )
    assert control.get_mission_state("reader", str(high["id"]))["queue_state"] == "done"


def test_resource_lock_conflict_expiry_and_fencing(harness: Harness) -> None:
    instant = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    harness.service.clock = lambda: instant
    first = _mission(harness, "first")
    second = _mission(harness, "second")
    control = harness.service.mission_control
    lock = control.acquire_resource_lock(
        actor_id="operator",
        request_id=request_id(),
        mission_id=str(first["id"]),
        resource_id="server:prod-01",
        lease_seconds=60,
    )
    assert lock["fencing_token"] == 1
    with pytest.raises(Conflict, match="locked"):
        control.acquire_resource_lock(
            actor_id="operator",
            request_id=request_id(),
            mission_id=str(second["id"]),
            resource_id="server:prod-01",
            lease_seconds=60,
        )
    instant += timedelta(seconds=61)
    next_lock = control.acquire_resource_lock(
        actor_id="operator",
        request_id=request_id(),
        mission_id=str(second["id"]),
        resource_id="server:prod-01",
        lease_seconds=60,
    )
    assert next_lock["fencing_token"] == 2
    released = control.resource_lock_lease(
        actor_id="operator",
        request_id=request_id(),
        mission_id=str(second["id"]),
        resource_id="server:prod-01",
        operation="release",
    )
    assert released["released"] is True


def test_model_trace_lineage_and_per_mission_budgets(harness: Harness) -> None:
    mission = _mission(harness)
    control = harness.service.mission_control
    control.update_mission_state(
        actor_id="operator",
        request_id=request_id(),
        mission_id=str(mission["id"]),
        max_iterations=5,
        max_api_calls=1,
    )
    local = control.record_model_trace(
        actor_id="operator",
        request_id=request_id(),
        mission_id=str(mission["id"]),
        model_role="ROLE_TINY",
        provider="ollama",
        model_name="tiny-test",
        reason="classify",
        outcome="escalated",
        confidence=0.4,
        input_tokens=12,
        output_tokens=3,
    )
    remote = control.record_model_trace(
        actor_id="operator",
        request_id=request_id(),
        mission_id=str(mission["id"]),
        parent_trace_id=str(local["id"]),
        model_role="ROLE_REASONING",
        provider="deepseek",
        model_name="reasoner-test",
        reason="low confidence",
        outcome="succeeded",
        confidence=0.9,
        input_tokens=100,
        output_tokens=25,
        cost_microunits=7,
    )
    assert remote["parent_trace_id"] == local["id"]
    with pytest.raises(BrokerError, match="API call budget"):
        control.record_model_trace(
            actor_id="operator",
            request_id=request_id(),
            mission_id=str(mission["id"]),
            model_role="ROLE_REASONING",
            provider="deepseek",
            model_name="reasoner-test",
            reason="unbounded retry",
            outcome="failed",
        )
    traces = control.list_model_traces("reader", str(mission["id"]))
    assert [item["id"] for item in traces] == [local["id"], remote["id"]]
    assert control.get_mission_state("reader", str(mission["id"]))["api_call_count"] == 1


def test_structured_incident_report_is_project_scoped(harness: Harness) -> None:
    incident = harness.service.create_incident(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="database outage",
        severity="critical",
    )
    details = harness.service.mission_control.update_incident_details(
        actor_id="operator",
        request_id=request_id(),
        incident_id=str(incident["id"]),
        service="postgresql",
        resource_id="db:prod",
        environment="production",
        symptoms="Connections time out",
        impact="API unavailable",
        probable_causes=["storage latency"],
        confirmed_cause="volume full",
        resolution="freed bounded temporary data",
        rollback="not required",
        recommendations=["increase alert lead time"],
        versions={"postgresql": "17"},
    )
    assert details["confirmed_cause"] == "volume full"
    assert harness.service.mission_control.get_incident_details(
        "reader", str(incident["id"])
    )["versions"] == {"postgresql": "17"}


def test_action_before_after_health_and_rollback_evidence_is_immutable(harness: Harness) -> None:
    mission = _mission(harness)
    action = harness.service.request_action(
        actor_id="operator",
        request_id=request_id(),
        runbook_id="test.check.v1",
        parameters={},
        reason="artifact exercise",
        mission_id=str(mission["id"]),
    )
    operation_id = request_id()
    artifacts = harness.service.mission_control.record_action_artifacts(
        actor_id="operator",
        request_id=operation_id,
        action_id=str(action["id"]),
        before_digest="sha256:before",
        after_digest="sha256:after",
        target_version="release-2",
        rollback_ref="release-1",
        diff_artifact="artifact://diff/1",
        healthcheck={"status": "ok", "checks": 4},
    )
    assert artifacts["healthcheck"]["status"] == "ok"
    replay = harness.service.mission_control.record_action_artifacts(
        actor_id="operator",
        request_id=operation_id,
        action_id=str(action["id"]),
        before_digest="sha256:before",
        after_digest="sha256:after",
        target_version="release-2",
        rollback_ref="release-1",
        diff_artifact="artifact://diff/1",
        healthcheck={"status": "ok", "checks": 4},
    )
    assert replay["replayed"] is True
    assert harness.service.mission_control.get_action_artifacts(
        "reader", str(action["id"])
    ) == {**artifacts, "replayed": False}
    with pytest.raises(Conflict, match="immutable"):
        harness.service.mission_control.record_action_artifacts(
            actor_id="operator",
            request_id=request_id(),
            action_id=str(action["id"]),
            target_version="release-3",
        )


def test_restart_recovery_stops_unknown_actions_and_expires_leases(harness: Harness) -> None:
    instant = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    harness.service.clock = lambda: instant
    mission = _mission(harness)
    action = harness.service.request_action(
        actor_id="operator",
        request_id=request_id(),
        runbook_id="test.check.v1",
        parameters={},
        reason="recovery exercise",
        mission_id=str(mission["id"]),
    )
    harness.service.mission_control.claim_next(
        actor_id="operator", request_id=request_id(), lease_seconds=30
    )
    harness.service.mission_control.acquire_resource_lock(
        actor_id="operator",
        request_id=request_id(),
        mission_id=str(mission["id"]),
        resource_id="server:recovery",
        lease_seconds=30,
    )
    with harness.database.raw_connection() as connection:
        connection.execute("UPDATE actions SET status = 'running' WHERE id = ?", (action["id"],))
    instant += timedelta(seconds=31)
    report = harness.service.mission_control.recover_after_restart()
    assert report == {
        "interrupted_actions": 1,
        "requeued_missions": 1,
        "expired_resource_locks": 1,
    }
    assert harness.service.get_action("operator", str(action["id"]))["status"] == "failed"
    assert harness.service.mission_control.get_mission_state(
        "reader", str(mission["id"])
    )["queue_state"] == "queued"
    assert harness.database.verify_audit_chain().valid
