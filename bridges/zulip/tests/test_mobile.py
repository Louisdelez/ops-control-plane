from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from zulip_approval_bridge.bridge import ApprovalProcessor
from zulip_approval_bridge.clients import BrokerClient, ZulipClient
from zulip_approval_bridge.errors import ProtocolError
from zulip_approval_bridge.mobile import MobileCommandProcessor
from zulip_approval_bridge.publishers import DurableZulipPublisher
from zulip_approval_bridge.state import EventState

from zulip_approval_bridge.config import Settings


MISSION_ID = "223e4567-e89b-42d3-a456-426614174000"


def _message(content: str, *, sender_id: int = 42, stream_id: int = 77) -> dict[str, object]:
    return {
        "id": 700,
        "type": "stream",
        "stream_id": stream_id,
        "display_recipient": "Infrastructure",
        "subject": "Incident proxy",
        "sender_id": sender_id,
        "content": f"<p>{content}</p>",
    }


def _event() -> dict[str, object]:
    return {"id": 21, "type": "message", "message": {"id": 700}}


def _zulip_transport(
    message: dict[str, object], requests: list[httpx.Request]
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/messages/700":
            return httpx.Response(200, json={"result": "success", "message": message})
        if request.url.path == "/api/v1/users/42":
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "user": {
                        "user_id": 42,
                        "is_bot": False,
                        "is_active": True,
                        "is_deleted": False,
                        "is_imported_stub": False,
                    },
                },
            )
        if request.url.path == "/api/v1/user_uploads":
            return httpx.Response(
                200,
                json={"result": "success", "uri": "/user_uploads/1/mobile/file"},
            )
        if request.url.path == "/api/v1/messages" and request.method == "POST":
            return httpx.Response(200, json={"result": "success", "id": 701})
        raise AssertionError(f"unexpected Zulip request: {request.method} {request.url.path}")

    return httpx.MockTransport(handler)


def _broker_transport(
    requests: list[tuple[str, str, dict[str, object] | None]], *, refusal: bool = False
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Actor-ID") == "zulip-mobile"
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if refusal:
            return httpx.Response(403, json={"error": {"code": "denied"}})
        common = {
            "id": MISSION_ID,
            "project_id": "infra-shared",
            "title": "Incident proxy",
            "status": "open",
            "updated_at": "2026-09-04T15:00:00Z",
        }
        if request.method == "POST" and request.url.path == "/v1/zulip/missions":
            return httpx.Response(
                201,
                json={**common, "created_at": "2026-09-04T15:00:00Z"},
            )
        if request.url.path.endswith("/status"):
            return httpx.Response(200, json={**common, "queue_state": "queued"})
        if request.url.path.endswith("/resume"):
            return httpx.Response(200, json={**common, "queue_state": "queued"})
        if request.url.path.endswith("/answers"):
            return httpx.Response(
                201,
                json={
                    "id": "323e4567-e89b-42d3-a456-426614174000",
                    "mission_id": MISSION_ID,
                    "kind": "decision",
                    "created_at": "2026-09-04T15:00:00Z",
                    "replayed": False,
                },
            )
        raise AssertionError(f"unexpected broker request: {request.method} {request.url.path}")

    return httpx.MockTransport(handler)


def _processor(
    settings: Settings,
    message: dict[str, object],
    zulip_requests: list[httpx.Request],
    broker_requests: list[tuple[str, str, dict[str, object] | None]],
    *,
    refusal: bool = False,
) -> tuple[ApprovalProcessor, ZulipClient, BrokerClient]:
    zulip = ZulipClient(settings, transport=_zulip_transport(message, zulip_requests))
    broker = BrokerClient(
        settings.broker_socket,
        transport=_broker_transport(broker_requests, refusal=refusal),
    )
    state = EventState(settings.state_path)
    mobile = MobileCommandProcessor(
        settings, zulip, broker, DurableZulipPublisher(state, zulip)
    )
    return ApprovalProcessor(settings, zulip, broker, mobile), zulip, broker


@pytest.mark.parametrize(
    ("command", "expected_path", "expected_outcome"),
    [
        (
            "!ops mission infra-shared Incident proxy",
            "/v1/zulip/missions",
            "mobile_mission_created",
        ),
        (
            f"!ops statut {MISSION_ID}",
            f"/v1/zulip/missions/{MISSION_ID}/status",
            "mobile_status_returned",
        ),
        (
            f"!ops reprendre {MISSION_ID}",
            f"/v1/zulip/missions/{MISSION_ID}/resume",
            "mobile_mission_resumed",
        ),
        (
            f"!ops répondre {MISSION_ID} Oui, applique le rollback.",
            f"/v1/zulip/missions/{MISSION_ID}/answers",
            "mobile_answer_recorded",
        ),
    ],
)
def test_mobile_mission_commands_are_forwarded_with_fixed_identity_and_durable_reply(
    settings: Settings,
    command: str,
    expected_path: str,
    expected_outcome: str,
) -> None:
    zulip_requests: list[httpx.Request] = []
    broker_requests: list[tuple[str, str, dict[str, object] | None]] = []
    processor, zulip, broker = _processor(
        settings, _message(command), zulip_requests, broker_requests
    )
    with zulip, broker:
        assert processor.process(_event(), "queue-1") == expected_outcome
        # A duplicate delivery returns the durable message ID without another
        # broker mutation or Zulip POST.
        assert processor.process(_event(), "queue-1") == expected_outcome

    assert len(broker_requests) == 2
    assert {request[1] for request in broker_requests} == {expected_path}
    assert broker_requests[0][2] == broker_requests[1][2]
    sends = [
        request
        for request in zulip_requests
        if request.method == "POST" and request.url.path == "/api/v1/messages"
    ]
    assert len(sends) == 1
    form = parse_qs(sends[0].content.decode("utf-8"))
    assert form["topic"] == ["Incident proxy"]
    assert "OPS-MOBILE-V1:" in form["content"][0]


def test_mobile_command_requires_allowlisted_fresh_human_and_exact_stream(
    settings: Settings,
) -> None:
    for message, expected in (
        (_message("!ops rapport", sender_id=73), "ignored_unauthorized_actor"),
        (_message("!ops rapport", stream_id=78), "ignored_wrong_message_scope"),
        (_message("bonjour"), "ignored_non_command_message"),
    ):
        zulip_requests: list[httpx.Request] = []
        broker_requests: list[tuple[str, str, dict[str, object] | None]] = []
        processor, zulip, broker = _processor(
            settings, message, zulip_requests, broker_requests
        )
        with zulip, broker:
            assert processor.process(_event(), "queue-1") == expected
        assert broker_requests == []
        assert not any(
            request.method == "POST" and request.url.path == "/api/v1/messages"
            for request in zulip_requests
        )


def test_invalid_explicit_command_gets_bounded_help(settings: Settings) -> None:
    zulip_requests: list[httpx.Request] = []
    broker_requests: list[tuple[str, str, dict[str, object] | None]] = []
    processor, zulip, broker = _processor(
        settings, _message("!ops efface tout"), zulip_requests, broker_requests
    )
    with zulip, broker:
        assert processor.process(_event(), "queue-1") == "mobile_invalid_command"
    assert broker_requests == []
    send = next(
        request
        for request in zulip_requests
        if request.method == "POST" and request.url.path == "/api/v1/messages"
    )
    assert "Commandes" in parse_qs(send.content.decode("utf-8"))["content"][0]


def test_report_and_capture_upload_only_protected_bounded_files(
    settings: Settings, tmp_path: Path
) -> None:
    report = tmp_path / "daily.json"
    report.write_text('{"summary":"ok"}\n', encoding="utf-8")
    report.chmod(0o640)
    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()
    capture = capture_dir / f"{MISSION_ID}.png"
    capture.write_bytes(b"\x89PNG\r\n\x1a\ncontent")
    capture.chmod(0o640)
    configured = Settings(
        **{
            field: getattr(settings, field)
            for field in settings.__dataclass_fields__
            if field not in {"daily_report_path", "capture_dir", "state_path"}
        },
        daily_report_path=report,
        capture_dir=capture_dir,
        state_path=tmp_path / "mobile-state.db",
    )
    for command in ("!ops rapport", f"!ops capture {MISSION_ID}"):
        # Distinct event IDs avoid intentionally idempotent reply reuse.
        event = _event()
        event["id"] = 21 if command == "!ops rapport" else 22
        zulip_requests: list[httpx.Request] = []
        broker_requests: list[tuple[str, str, dict[str, object] | None]] = []
        processor, zulip, broker = _processor(
            configured, _message(command), zulip_requests, broker_requests
        )
        with zulip, broker:
            assert processor.process(event, "queue-1") in {
                "mobile_report_returned", "mobile_capture_returned"
            }
        uploads = [r for r in zulip_requests if r.url.path == "/api/v1/user_uploads"]
        assert len(uploads) == 1
        if command == "!ops rapport":
            assert broker_requests == []
        else:
            assert [request[1] for request in broker_requests] == [
                f"/v1/zulip/missions/{MISSION_ID}/status"
            ]

    capture.chmod(0o666)
    event = _event()
    event["id"] = 23
    processor, zulip, broker = _processor(
        configured, _message(f"!ops capture {MISSION_ID}"), [], []
    )
    with zulip, broker, pytest.raises(ProtocolError, match="protected bounded"):
        processor.process(event, "queue-1")


def test_broker_refusal_is_reported_without_leaking_details(settings: Settings) -> None:
    zulip_requests: list[httpx.Request] = []
    broker_requests: list[tuple[str, str, dict[str, object] | None]] = []
    processor, zulip, broker = _processor(
        settings,
        _message("!ops mission infra-shared Refused"),
        zulip_requests,
        broker_requests,
        refusal=True,
    )
    with zulip, broker:
        assert processor.process(_event(), "queue-1") == "mobile_broker_rejected"
    send = next(r for r in zulip_requests if r.method == "POST" and r.url.path == "/api/v1/messages")
    assert "refusée" in parse_qs(send.content.decode("utf-8"))["content"][0]
