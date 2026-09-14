from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from zulip_approval_bridge.clients import AlertmanagerClient, BrokerClient, ZulipClient
from zulip_approval_bridge.config import Settings
from zulip_approval_bridge.errors import ProtocolError


def test_broker_public_pending_uses_fixed_actor_and_exact_bounds(settings: Settings) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "action_id": "123e4567-e89b-42d3-a456-426614174000",
                        "summary": "Changer DNS",
                        "impact": "Aucun arrêt",
                        "rollback": "Supprimer l'entrée",
                        "deadline": "2026-09-04T12:00:00Z",
                    }
                ]
            },
        )

    with BrokerClient(settings.broker_socket, transport=httpx.MockTransport(handler)) as client:
        items = client.list_pending_public(limit=25, offset=50)
    assert len(items) == 1
    assert captured[0].url.path == "/v1/zulip/approvals/pending"
    assert dict(captured[0].url.params) == {"limit": "25", "offset": "50"}
    assert captured[0].headers["X-Actor-ID"] == "zulip-publisher"


def test_broker_public_pending_rejects_unexpected_fields(settings: Settings) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "action_id": "123e4567-e89b-42d3-a456-426614174000",
                        "summary": "Changer DNS",
                        "impact": "Aucun",
                        "rollback": "Retirer",
                        "deadline": "2026-09-04T12:00:00Z",
                        "reason": "must never cross this boundary",
                    }
                ]
            },
        )

    with BrokerClient(settings.broker_socket, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProtocolError, match="unsafe approval fields"):
            client.list_pending_public(limit=50, offset=0)


def test_alertmanager_is_queried_read_only_through_adapter(settings: Settings) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/-/ready":
            return httpx.Response(200, text="OK")
        if request.url.path == "/api/v2/alerts":
            return httpx.Response(200, json=[])
        raise AssertionError(request.url.path)

    with AlertmanagerClient(
        settings.alertmanager_socket,
        transport=httpx.MockTransport(handler),
    ) as client:
        client.verify_ready()
        assert client.active_alerts() == []
    assert [request.method for request in captured] == ["GET", "GET"]
    assert dict(captured[1].url.params) == {
        "active": "true",
        "silenced": "true",
        "inhibited": "true",
        "unprocessed": "true",
    }


def test_zulip_marker_recovery_revalidates_numeric_scope(settings: Settings) -> None:
    marker = "OPS-APPROVAL-V1:123e4567-e89b-42d3-a456-426614174000"
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={
                "result": "success",
                "messages": [
                    {
                        "id": 9,
                        "type": "stream",
                        "stream_id": 77,
                        "display_recipient": "Infrastructure",
                        "subject": "Approbations",
                        "sender_id": 900,
                        "content": f"<p>{marker}</p>",
                    },
                    {
                        "id": 10,
                        "type": "stream",
                        "stream_id": 999,
                        "display_recipient": "Infrastructure",
                        "subject": "Approbations",
                        "sender_id": 900,
                        "content": f"<p>{marker}</p>",
                    },
                ],
            },
        )

    with ZulipClient(settings, transport=httpx.MockTransport(handler)) as client:
        assert client.find_own_marker(
            stream="Infrastructure",
            stream_id=77,
            topic="Approbations",
            marker=marker,
        ) == 9
    narrow = json.loads(captured[0].url.params["narrow"])
    assert narrow == [
        ["channel", "Infrastructure"],
        ["topic", "Approbations"],
        ["search", marker],
    ]


def test_zulip_send_is_short_stream_message(settings: Settings) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"result": "success", "id": 123})

    with ZulipClient(settings, transport=httpx.MockTransport(handler)) as client:
        assert client.send_stream_message(
            stream="Monitoring",
            topic="Alertes",
            content="Alerte courte",
        ) == 123
    form = parse_qs(captured[0].content.decode("utf-8"))
    assert form == {
        "type": ["stream"],
        "to": ["Monitoring"],
        "topic": ["Alertes"],
        "content": ["Alerte courte"],
    }
