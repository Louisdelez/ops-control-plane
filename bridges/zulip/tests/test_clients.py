from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from zulip_approval_bridge.clients import QueueCursor, ZulipClient
from zulip_approval_bridge.config import Settings
from zulip_approval_bridge.errors import ExternalServiceRejected, QueueExpired

from helpers import zulip_transport


def test_startup_verifies_bot_and_stream_numeric_identity(settings: Settings) -> None:
    requests: list[httpx.Request] = []
    with ZulipClient(
        settings, transport=zulip_transport(settings, requests=requests)
    ) as client:
        client.verify_bot_identity()
        client.verify_stream_identity()
    assert [request.url.path for request in requests] == [
        "/api/v1/users/me",
        "/api/v1/get_stream_id",
    ]
    assert requests[1].url.params["stream"] == settings.approval_stream


def test_queue_requests_only_messages_and_reactions_in_configured_stream(settings: Settings) -> None:
    requests: list[httpx.Request] = []
    with ZulipClient(
        settings, transport=zulip_transport(settings, requests=requests)
    ) as client:
        cursor = client.register_queue()
    assert cursor == QueueCursor("queue-1", -1)
    form = parse_qs(requests[0].content.decode("utf-8"))
    assert form["event_types"] == ['["message","reaction"]']
    assert form["fetch_event_types"] == ["[]"]
    assert form["narrow"] == ['[["channel","Infrastructure"]]']


def test_bad_event_queue_id_requests_reregistration(settings: Settings) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "result": "error",
                "code": "BAD_EVENT_QUEUE_ID",
                "msg": "queue does not exist",
            },
        )

    with ZulipClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(QueueExpired):
            client.get_events(QueueCursor("expired", 4))


def test_auth_is_basic_and_request_never_redirects(settings: Settings) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(302, headers={"Location": "https://elsewhere.invalid/api"}, json={})

    with ZulipClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ExternalServiceRejected, match="rejected"):
            client.verify_bot_identity()
    assert len(captured) == 1
    assert captured[0].url.host == "ops.example.invalid"
    assert captured[0].headers["Authorization"].startswith("Basic ")
