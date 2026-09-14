from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ops_broker.errors import AuthorizationDenied, BrokerError, Conflict

from conftest import Harness, request_id


def test_memory_history_pages_closed_missions_without_cross_project_access(harness: Harness) -> None:
    mission = harness.service.create_mission(actor_id='operator', request_id=request_id(),
        project_id='infra', title='memory history')
    ids = []
    for number in range(3):
        record = harness.service.mission_control.add_record(actor_id='operator',
            request_id=request_id(), mission_id=mission['id'], kind='observation',
            content='Historical fact '+str(number), evidence=[])
        ids.append(record['id'])
    with harness.database.transaction() as connection:
        connection.execute("UPDATE missions SET status='completed' WHERE id=?", (mission['id'],))
    first = harness.service.list_memory_history('reader', 'infra', limit=2)
    discovered = harness.service.list_memory_missions('reader', 'infra')
    assert [m['id'] for m in discovered['missions']] == [mission['id']]
    assert not harness.service.list_memory_missions('reader', 'infra', after=discovered['next_cursor'])['missions']
    with pytest.raises(AuthorizationDenied):
        harness.service.list_memory_missions('other-reader', 'infra')
    assert [r['id'] for r in first['records']] == ids[:2]
    second = harness.service.list_memory_history('reader', 'infra', after=first['next_cursor'])
    assert [r['id'] for r in second['records']] == ids[2:]
    assert not harness.service.list_memory_history('reader', 'infra', after=second['next_cursor'])['records']
    assert all(r['project_id'] == 'infra' for r in first['records'])
    assert not any('result' in r or 'parameters' in r for r in first['records'])
    with pytest.raises(AuthorizationDenied):
        harness.service.list_memory_history('other-reader', 'infra')
    with pytest.raises(BrokerError):
        harness.service.list_memory_history('reader', 'infra', after=-1)


def test_open_lists_are_ordered_limited_public_and_project_filtered(harness: Harness) -> None:
    instant = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
    harness.service.clock = lambda: instant
    older_mission = harness.service.create_mission(
        actor_id="reader",
        request_id=request_id(),
        project_id="infra",
        title="older mission",
    )
    older_incident = harness.service.create_incident(
        actor_id="reader",
        request_id=request_id(),
        project_id="infra",
        title="older incident",
        severity="warning",
    )

    instant += timedelta(minutes=1)
    other_mission = harness.service.create_mission(
        actor_id="other-reader",
        request_id=request_id(),
        project_id="other",
        title="private other mission",
    )
    other_incident = harness.service.create_incident(
        actor_id="other-reader",
        request_id=request_id(),
        project_id="other",
        title="private other incident",
        severity="critical",
    )

    instant += timedelta(minutes=1)
    newest_mission = harness.service.create_mission(
        actor_id="reader",
        request_id=request_id(),
        project_id="infra",
        title="newest mission",
    )
    newest_incident = harness.service.create_incident(
        actor_id="reader",
        request_id=request_id(),
        project_id="infra",
        title="newest incident",
        severity="info",
    )

    missions = harness.service.list_open_missions("reader")
    incidents = harness.service.list_open_incidents("reader")
    assert [item["id"] for item in missions] == [newest_mission["id"], older_mission["id"]]
    assert [item["id"] for item in incidents] == [newest_incident["id"], older_incident["id"]]
    assert other_mission["id"] not in {item["id"] for item in missions}
    assert other_incident["id"] not in {item["id"] for item in incidents}
    assert harness.service.list_open_missions("reader", limit=1)[0]["id"] == newest_mission["id"]
    assert harness.service.list_open_incidents("reader", project_id="infra", limit=1)[0]["id"] == newest_incident["id"]
    assert set(missions[0]) == {"id", "project_id", "title", "status", "created_at", "updated_at"}
    assert set(incidents[0]) == {
        "id",
        "project_id",
        "title",
        "severity",
        "status",
        "created_at",
        "updated_at",
    }

    with pytest.raises(AuthorizationDenied):
        harness.service.list_open_missions("reader", project_id="other")
    with pytest.raises(AuthorizationDenied):
        harness.service.list_open_incidents("reader", project_id="other")
    with pytest.raises(AuthorizationDenied):
        harness.service.get_mission("reader", str(other_mission["id"]))
    with pytest.raises(AuthorizationDenied):
        harness.service.list_open_incidents("unknown")


@pytest.mark.parametrize("limit", [0, 51, True, "20"])
def test_open_list_limit_is_strict(harness: Harness, limit: object) -> None:
    with pytest.raises(BrokerError, match="limit"):
        harness.service.list_open_incidents("reader", limit=limit)  # type: ignore[arg-type]
    with pytest.raises(BrokerError, match="limit"):
        harness.service.list_open_missions("reader", limit=limit)  # type: ignore[arg-type]


def test_linked_action_lists_are_recent_bounded_project_scoped_and_redacted(
    harness: Harness,
) -> None:
    instant = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    harness.service.clock = lambda: instant
    mission = harness.service.create_mission(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="resumable mission",
    )
    incident = harness.service.create_incident(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="resumable incident",
        severity="warning",
    )
    older = harness.service.request_action(
        actor_id="operator",
        request_id=request_id(),
        runbook_id="test.check.v1",
        parameters={},
        reason="sensitive operational detail must stay out of summaries",
        mission_id=str(mission["id"]),
        incident_id=str(incident["id"]),
    )
    instant += timedelta(minutes=1)
    newer = harness.service.request_action(
        actor_id="operator",
        request_id=request_id(),
        runbook_id="test.check.v1",
        parameters={},
        reason="second private reason",
        mission_id=str(mission["id"]),
    )

    mission_actions = harness.service.list_actions_for_mission("reader", str(mission["id"]))
    incident_actions = harness.service.list_actions_for_incident(
        "reader", str(incident["id"]), limit=1
    )
    assert [item["id"] for item in mission_actions] == [newer["id"], older["id"]]
    assert harness.service.list_actions_for_mission('reader', str(mission['id']), limit=1, offset=1)[0]['id'] == older['id']
    with pytest.raises(BrokerError):
        harness.service.list_actions_for_mission('reader', str(mission['id']), offset=-1)
    assert [item["id"] for item in incident_actions] == [older["id"]]
    assert set(mission_actions[0]) == {
        "id",
        "mission_id",
        "incident_id",
        "project_id",
        "runbook_id",
        "runbook_version",
        "action_class",
        "requested_by",
        "status",
        "approval_deadline",
        "result",
        "created_at",
        "updated_at",
    }
    assert mission_actions[0]["result"] == {
        "return_code": 0,
        "timed_out": False,
        "truncated": False,
    }
    assert not {
        "reason",
        "parameters",
        "request_id",
        "stdout",
        "stderr",
    } & set(mission_actions[0])

    other_mission = harness.service.create_mission(
        actor_id="other-reader",
        request_id=request_id(),
        project_id="other",
        title="private mission",
    )
    with pytest.raises(AuthorizationDenied):
        harness.service.list_actions_for_mission("reader", str(other_mission["id"]))
    with pytest.raises(BrokerError, match="limit"):
        harness.service.list_actions_for_incident(
            "reader", str(incident["id"]), limit=51
        )


def test_status_changes_are_idempotent_authorized_and_audited(harness: Harness) -> None:
    mission = harness.service.create_mission(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="lifecycle mission",
    )
    incident = harness.service.create_incident(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="lifecycle incident",
        severity="critical",
    )

    mission_change_id = request_id()
    first = harness.service.set_mission_status(
        actor_id="operator",
        request_id=mission_change_id,
        mission_id=str(mission["id"]),
        status="paused",
    )
    replay = harness.service.set_mission_status(
        actor_id="operator",
        request_id=mission_change_id,
        mission_id=str(mission["id"]),
        status="paused",
    )
    assert first["changed"] is True
    assert first["replayed"] is False
    assert first["current_status"] == "paused"
    assert replay == {**first, "replayed": True}
    assert harness.service.get_mission("operator", str(mission["id"]))["status"] == "paused"
    assert harness.service.list_open_missions("reader")[0]["status"] == "paused"
    with pytest.raises(Conflict):
        harness.service.set_mission_status(
            actor_id="operator",
            request_id=mission_change_id,
            mission_id=str(mission["id"]),
            status="completed",
        )
    harness.service.set_mission_status(
        actor_id="operator",
        request_id=request_id(),
        mission_id=str(mission["id"]),
        status="completed",
    )
    assert harness.service.list_open_missions("reader") == []

    incident_change_id = request_id()
    harness.service.set_incident_status(
        actor_id="operator",
        request_id=incident_change_id,
        incident_id=str(incident["id"]),
        status="monitoring",
    )
    assert harness.service.list_open_incidents("reader")[0]["status"] == "monitoring"
    harness.service.set_incident_status(
        actor_id="operator",
        request_id=request_id(),
        incident_id=str(incident["id"]),
        status="resolved",
    )
    assert harness.service.get_incident("operator", str(incident["id"]))["status"] == "resolved"
    assert harness.service.list_open_incidents("reader") == []

    with harness.database.read() as connection:
        mission_events = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'mission.status_changed'"
        ).fetchone()[0]
        incident_events = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'incident.status_changed'"
        ).fetchone()[0]
    assert mission_events == 2
    assert incident_events == 2
    assert harness.database.verify_audit_chain().valid


def test_status_change_denies_cross_project_and_invalid_states(harness: Harness) -> None:
    mission = harness.service.create_mission(
        actor_id="other-reader",
        request_id=request_id(),
        project_id="other",
        title="other mission",
    )
    incident = harness.service.create_incident(
        actor_id="other-reader",
        request_id=request_id(),
        project_id="other",
        title="other incident",
        severity="warning",
    )
    with pytest.raises(AuthorizationDenied):
        harness.service.set_mission_status(
            actor_id="operator",
            request_id=request_id(),
            mission_id=str(mission["id"]),
            status="completed",
        )
    with pytest.raises(AuthorizationDenied):
        harness.service.set_incident_status(
            actor_id="operator",
            request_id=request_id(),
            incident_id=str(incident["id"]),
            status="resolved",
        )
    with pytest.raises(BrokerError, match="invalid mission status"):
        harness.service.set_mission_status(
            actor_id="other-reader",
            request_id=request_id(),
            mission_id=str(mission["id"]),
            status="closed",
        )
    with pytest.raises(BrokerError, match="invalid incident status"):
        harness.service.set_incident_status(
            actor_id="other-reader",
            request_id=request_id(),
            incident_id=str(incident["id"]),
            status="closed",
        )
    assert harness.database.verify_audit_chain().valid


def test_project_read_grant_does_not_grant_lifecycle_write(harness: Harness) -> None:
    mission = harness.service.create_mission(
        actor_id="reader",
        request_id=request_id(),
        project_id="infra",
        title="readable but not writable",
    )
    assert harness.service.get_mission("reader", str(mission["id"]))["status"] == "open"
    with pytest.raises(AuthorizationDenied, match="lifecycle"):
        harness.service.set_mission_status(
            actor_id="reader",
            request_id=request_id(),
            mission_id=str(mission["id"]),
            status="completed",
        )
    assert harness.service.get_mission("reader", str(mission["id"]))["status"] == "open"


def test_terminal_states_cannot_reopen_and_noop_does_not_reorder(harness: Harness) -> None:
    mission = harness.service.create_mission(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="terminal mission",
    )
    completed = harness.service.set_mission_status(
        actor_id="operator",
        request_id=request_id(),
        mission_id=str(mission["id"]),
        status="completed",
    )
    before = harness.service.get_mission("operator", str(mission["id"]))

    noop = harness.service.set_mission_status(
        actor_id="operator",
        request_id=request_id(),
        mission_id=str(mission["id"]),
        status="completed",
    )
    after = harness.service.get_mission("operator", str(mission["id"]))
    assert noop["changed"] is False
    assert noop["replayed"] is False
    assert noop["current_status"] == "completed"
    assert after["updated_at"] == before["updated_at"] == completed["updated_at"]

    with pytest.raises(Conflict, match="cannot transition"):
        harness.service.set_mission_status(
            actor_id="operator",
            request_id=request_id(),
            mission_id=str(mission["id"]),
            status="open",
        )

    incident = harness.service.create_incident(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="terminal incident",
        severity="critical",
    )
    harness.service.set_incident_status(
        actor_id="operator",
        request_id=request_id(),
        incident_id=str(incident["id"]),
        status="resolved",
    )
    with pytest.raises(Conflict, match="cannot transition"):
        harness.service.set_incident_status(
            actor_id="operator",
            request_id=request_id(),
            incident_id=str(incident["id"]),
            status="monitoring",
        )


def test_replay_reports_original_result_and_current_state(harness: Harness) -> None:
    mission = harness.service.create_mission(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="stale replay guard",
    )
    pause_request = request_id()
    original = harness.service.set_mission_status(
        actor_id="operator",
        request_id=pause_request,
        mission_id=str(mission["id"]),
        status="paused",
    )
    harness.service.set_mission_status(
        actor_id="operator",
        request_id=request_id(),
        mission_id=str(mission["id"]),
        status="completed",
    )
    replay = harness.service.set_mission_status(
        actor_id="operator",
        request_id=pause_request,
        mission_id=str(mission["id"]),
        status="paused",
    )
    assert replay["status"] == original["status"] == "paused"
    assert replay["previous_status"] == original["previous_status"] == "open"
    assert replay["current_status"] == "completed"
    assert replay["replayed"] is True


def test_status_and_audit_event_commit_atomically(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mission = harness.service.create_mission(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="atomic lifecycle write",
    )
    before_events = harness.database.verify_audit_chain().event_count

    def fail_audit(*args: object, **kwargs: object) -> str:
        raise RuntimeError("simulated audit failure")

    monkeypatch.setattr(harness.database, "append_event", fail_audit)
    with pytest.raises(RuntimeError, match="simulated audit failure"):
        harness.service.set_mission_status(
            actor_id="operator",
            request_id=request_id(),
            mission_id=str(mission["id"]),
            status="paused",
        )

    with harness.database.read() as connection:
        status = connection.execute(
            "SELECT status FROM missions WHERE id = ?", (mission["id"],)
        ).fetchone()[0]
        changes = connection.execute(
            "SELECT COUNT(*) FROM mission_status_changes"
        ).fetchone()[0]
    assert status == "open"
    assert changes == 0
    assert harness.database.verify_audit_chain().event_count == before_events
