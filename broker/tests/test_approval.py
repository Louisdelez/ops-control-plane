from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

import ops_broker.api as api_adapter
from ops_broker.api import create_app
from ops_broker.errors import ApprovalRequired, AuthorizationDenied, Conflict
from ops_broker.util import parse_timestamp

from conftest import Harness, request_id


def _request_change(harness: Harness) -> dict[str, object]:
    return harness.service.request_action(
        actor_id="operator",
        request_id=request_id(),
        runbook_id="test.change.v1",
        parameters={},
        reason="controlled production change",
    )


def test_class_c_requires_distinct_authorized_human(harness: Harness) -> None:
    action = _request_change(harness)
    assert action["status"] == "pending_approval"
    assert harness.executor.calls == []

    with pytest.raises(ApprovalRequired):
        harness.service.execute_action(actor_id="operator", action_id=str(action["id"]))

    with pytest.raises(AuthorizationDenied):
        harness.service.decide_approval(
            actor_id="zulip:unauthorized",
            request_id=request_id(),
            action_id=str(action["id"]),
            decision="approve",
            source="zulip",
            source_event_id="event-unauthorized",
            reason="not on the approver list",
        )

    with pytest.raises(AuthorizationDenied):
        harness.service.decide_approval(
            actor_id="operator",
            request_id=request_id(),
            action_id=str(action["id"]),
            decision="approve",
            source="zulip",
            source_event_id="event-self",
            reason="requester cannot self approve",
        )

    approval = harness.service.decide_approval(
        actor_id="zulip:42",
        request_id=request_id(),
        action_id=str(action["id"]),
        decision="approve",
        source="zulip",
        source_event_id="event-approved",
        reason="human reviewed impact and rollback",
    )
    assert approval["actor_id"] == "zulip:42"
    assert approval["decision"] == "approve"

    completed = harness.service.execute_action(
        actor_id="operator",
        action_id=str(action["id"]),
    )
    assert completed["status"] == "succeeded"
    assert len(harness.executor.calls) == 1


def test_approval_endpoint_verifies_header_actor(harness: Harness) -> None:
    action = _request_change(harness)
    client = TestClient(create_app(harness.service, api_mode="approvals-only"))
    response = client.post(
        f"/v1/actions/{action['id']}/approvals",
        headers={"X-Actor-ID": "zulip:99"},
        json={
            "actor_id": "zulip:42",
            "request_id": request_id(),
            "decision": "approve",
            "source": "zulip",
            "source_event_id": "event-header-mismatch",
            "reason": "identity mismatch must fail",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "authorization_denied"
    assert harness.service.get_action("operator", str(action["id"]))["status"] == "pending_approval"


def test_http_validation_does_not_echo_rejected_values(harness: Harness) -> None:
    client = TestClient(create_app(harness.service, api_mode="full"))
    opaque_value = "do-not-reflect-this-value"
    response = client.post(
        "/v1/incidents",
        headers={"X-Actor-ID": "reader"},
        json={
            "actor_id": "reader",
            "request_id": request_id(),
            "project_id": "infra",
            "title": "bad severity test",
            "severity": opaque_value,
        },
    )
    assert response.status_code == 422
    assert opaque_value not in response.text


def test_production_api_exposes_only_health_public_pending_and_approval(harness: Harness) -> None:
    client = TestClient(create_app(harness.service, api_mode="approvals-only"))
    assert client.get("/healthz").status_code == 200

    action = _request_change(harness)
    pending = client.get(
        "/v1/zulip/approvals/pending?limit=1&offset=0",
        headers={"X-Actor-ID": "zulip-publisher"},
    )
    assert pending.status_code == 200
    assert pending.json() == {
        "items": [
            {
                "action_id": action["id"],
                "summary": "Human-approved test change",
                "impact": "Impact à valider dans le runbook",
                "rollback": "Rollback défini par le runbook",
                "deadline": action["approval_deadline"],
            }
        ]
    }
    assert "controlled production change" not in pending.text
    assert "requested_by" not in pending.text
    assert client.get(
        "/v1/zulip/approvals/pending",
        headers={"X-Actor-ID": "zulip:42"},
    ).status_code == 403

    blocked = (
        ("GET", "/v1/audit/verify"),
        ("GET", "/v1/runbooks"),
        ("POST", "/v1/missions"),
        ("POST", "/v1/incidents"),
        ("POST", "/v1/actions"),
        ("GET", "/v1/approvals/pending"),
        ("GET", "/v1/zulip/approvals/pending/extra"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
    )
    for method, path in blocked:
        response = client.request(
            method,
            path,
            headers={"X-Actor-ID": "codex-supervised"},
            json={} if method == "POST" else None,
        )
        assert response.status_code == 404, (method, path, response.text)


def test_mobile_routes_are_narrow_idempotent_and_never_expose_context(
    harness: Harness,
) -> None:
    client = TestClient(create_app(harness.service, api_mode="approvals-only"))
    creation_id = request_id()
    created = client.post(
        "/v1/zulip/missions",
        headers={"X-Actor-ID": "zulip-mobile"},
        json={
            "request_id": creation_id,
            "project_id": "infra",
            "title": "Incident proxy",
        },
    )
    assert created.status_code == 201
    mission = created.json()
    assert set(mission) == {
        "id", "project_id", "title", "status", "created_at", "updated_at"
    }
    replay = client.post(
        "/v1/zulip/missions",
        headers={"X-Actor-ID": "zulip-mobile"},
        json={
            "request_id": creation_id,
            "project_id": "infra",
            "title": "Incident proxy",
        },
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == mission["id"]

    harness.service.mission_control.update_mission_state(
        actor_id="operator",
        request_id=request_id(),
        mission_id=mission["id"],
        summary="secret-looking internal context",
        files=["/private/report.txt"],
    )
    status = client.get(
        f"/v1/zulip/missions/{mission['id']}/status",
        headers={"X-Actor-ID": "zulip-mobile"},
    )
    assert status.status_code == 200
    assert set(status.json()) == {
        "id", "project_id", "title", "status", "queue_state", "updated_at"
    }
    assert "secret-looking" not in status.text
    assert "/private" not in status.text


def test_mobile_resume_is_bounded_to_paused_and_fixed_actor(harness: Harness) -> None:
    mission = harness.service.create_mission(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="Paused incident",
    )
    harness.service.set_mission_status(
        actor_id="operator",
        request_id=request_id(),
        mission_id=str(mission["id"]),
        status="paused",
    )
    client = TestClient(create_app(harness.service, api_mode="approvals-only"))
    denied = client.post(
        f"/v1/zulip/missions/{mission['id']}/resume",
        headers={"X-Actor-ID": "zulip:42"},
        json={"request_id": request_id()},
    )
    assert denied.status_code == 403
    resumed = client.post(
        f"/v1/zulip/missions/{mission['id']}/resume",
        headers={"X-Actor-ID": "zulip-mobile"},
        json={"request_id": request_id()},
    )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "open"

    harness.service.set_mission_status(
        actor_id="operator",
        request_id=request_id(),
        mission_id=str(mission["id"]),
        status="completed",
    )
    blocked = client.post(
        f"/v1/zulip/missions/{mission['id']}/resume",
        headers={"X-Actor-ID": "zulip-mobile"},
        json={"request_id": request_id()},
    )
    assert blocked.status_code == 403


def test_mobile_intake_cannot_reach_generic_or_execution_routes(harness: Harness) -> None:
    client = TestClient(create_app(harness.service, api_mode="approvals-only"))
    for method, path in (
        ("POST", "/v1/actions"),
        ("POST", "/v1/incidents"),
        ("GET", "/v1/runbooks"),
        ("GET", "/v1/audit/verify"),
    ):
        response = client.request(
            method,
            path,
            headers={"X-Actor-ID": "zulip-mobile"},
            json={} if method == "POST" else None,
        )
        assert response.status_code == 404


def test_mobile_answer_records_numeric_human_attribution_without_action_access(
    harness: Harness,
) -> None:
    mission = harness.service.create_mission(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="Question humaine",
    )
    client = TestClient(create_app(harness.service, api_mode="approvals-only"))
    answer_id = request_id()
    response = client.post(
        f"/v1/zulip/missions/{mission['id']}/answers",
        headers={"X-Actor-ID": "zulip-mobile"},
        json={
            "request_id": answer_id,
            "source_user_id": 42,
            "answer": "Oui, continuer.",
        },
    )
    assert response.status_code == 201
    assert set(response.json()) == {"id", "mission_id", "kind", "created_at", "replayed"}
    records = harness.service.mission_control.list_records(
        "operator", str(mission["id"])
    )
    assert records[0]["content"] == "Réponse Zulip utilisateur 42: Oui, continuer."
    assert records[0]["kind"] == "decision"

    replay = client.post(
        f"/v1/zulip/missions/{mission['id']}/answers",
        headers={"X-Actor-ID": "zulip-mobile"},
        json={
            "request_id": answer_id,
            "source_user_id": 42,
            "answer": "Oui, continuer.",
        },
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == response.json()["id"]
    assert replay.json()["replayed"] is True

    conflicting = client.post(
        f"/v1/zulip/missions/{mission['id']}/answers",
        headers={"X-Actor-ID": "zulip-mobile"},
        json={
            "request_id": answer_id,
            "source_user_id": 42,
            "answer": "Non, arrêter.",
        },
    )
    assert conflicting.status_code == 409


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        (
            "/v1/zulip/missions",
            {"request_id": "REQUEST", "project_id": "infra", "title": "bad\u200btitle"},
        ),
        (
            "/v1/zulip/missions/MISSION/answers",
            {"request_id": "REQUEST", "source_user_id": 42, "answer": "line one\nline two"},
        ),
    ],
)
def test_mobile_text_controls_fail_closed(
    harness: Harness, path: str, payload: dict[str, object]
) -> None:
    mission = harness.service.create_mission(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="Control validation",
    )
    payload["request_id"] = request_id()
    path = path.replace("MISSION", str(mission["id"]))
    response = TestClient(create_app(harness.service, api_mode="approvals-only")).post(
        path,
        headers={"X-Actor-ID": "zulip-mobile"},
        json=payload,
    )
    assert response.status_code == 422
    assert "bad" not in response.text
    assert "line one" not in response.text


def test_public_pending_is_bounded_and_excludes_expired(harness: Harness) -> None:
    action = _request_change(harness)
    client = TestClient(create_app(harness.service, api_mode="approvals-only"))
    assert client.get(
        "/v1/zulip/approvals/pending?limit=0",
        headers={"X-Actor-ID": "zulip-publisher"},
    ).status_code == 422
    assert client.get(
        "/v1/zulip/approvals/pending?offset=10001",
        headers={"X-Actor-ID": "zulip-publisher"},
    ).status_code == 422

    harness.service.clock = lambda: parse_timestamp(str(action["approval_deadline"]))
    response = client.get(
        "/v1/zulip/approvals/pending",
        headers={"X-Actor-ID": "zulip-publisher"},
    )
    assert response.status_code == 200
    assert response.json() == {"items": []}


def test_execution_rederives_argv_and_rejects_state_tampering(harness: Harness) -> None:
    action = _request_change(harness)
    harness.service.decide_approval(
        actor_id="zulip:42",
        request_id=request_id(),
        action_id=str(action["id"]),
        decision="approve",
        source="zulip",
        source_event_id="event-tamper-test",
        reason="reviewed before the simulated corruption",
    )
    with harness.database.raw_connection() as connection:
        connection.execute(
            "UPDATE actions SET argv_json = ? WHERE id = ?",
            ('["/bin/sh","-c","id"]', action["id"]),
        )

    with pytest.raises(Conflict, match="no longer matches"):
        harness.service.execute_action(actor_id="operator", action_id=str(action["id"]))
    assert harness.executor.calls == []


def test_pending_approval_expires_at_deadline(harness: Harness) -> None:
    action = _request_change(harness)
    assert action["approval_deadline"] is not None
    harness.service.clock = lambda: parse_timestamp(str(action["approval_deadline"]))

    with pytest.raises(ApprovalRequired, match="expired"):
        harness.service.execute_action(actor_id="operator", action_id=str(action["id"]))
    assert harness.service.get_action("operator", str(action["id"]))["status"] == "approval_expired"


def test_api_launcher_accepts_inherited_systemd_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.setenv("OPS_BROKER_FD", "3")
    monkeypatch.delenv("OPS_BROKER_SOCKET", raising=False)
    monkeypatch.setattr(api_adapter, "create_app", lambda: object())
    monkeypatch.setattr(
        api_adapter.uvicorn,
        "run",
        lambda application, **kwargs: calls.append({"application": application, **kwargs}),
    )

    api_adapter.main()
    assert len(calls) == 1
    call = calls[0]
    assert call["fd"] == 3
    assert call["access_log"] is False
    assert "host" not in call
