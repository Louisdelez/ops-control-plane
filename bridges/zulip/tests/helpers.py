from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

import httpx

from conftest import ACTION_ID
from zulip_approval_bridge.config import Settings


def valid_user() -> dict[str, Any]:
    return {
        "user_id": 42,
        "is_bot": False,
        "is_active": True,
        "is_deleted": False,
        "is_imported_stub": False,
    }


def valid_message() -> dict[str, Any]:
    return {
        "id": 555,
        "type": "stream",
        "stream_id": 77,
        "display_recipient": "Infrastructure",
        "subject": "Approbations",
        "sender_id": 900,
        "content": f"<p>Réagir.</p><p>OPS-APPROVAL-V1:{ACTION_ID}</p>",
    }


def zulip_transport(
    settings: Settings,
    *,
    user: dict[str, Any] | None = None,
    message: dict[str, Any] | None = None,
    requests: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    selected_user = deepcopy(valid_user() if user is None else user)
    selected_message = deepcopy(valid_message() if message is None else message)

    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        path = request.url.path
        if path == "/api/v1/users/me":
            return httpx.Response(
                200,
                json={
                    "result": "success",
                    "user_id": settings.bot_user_id,
                    "is_bot": True,
                    "is_active": True,
                },
            )
        if path == "/api/v1/get_stream_id":
            return httpx.Response(
                200,
                json={"result": "success", "stream_id": settings.approval_stream_id},
            )
        if path == "/api/v1/users/42":
            return httpx.Response(200, json={"result": "success", "user": selected_user})
        if path == "/api/v1/messages/555":
            return httpx.Response(
                200, json={"result": "success", "message": selected_message}
            )
        if path == "/api/v1/register":
            return httpx.Response(
                200,
                json={"result": "success", "queue_id": "queue-1", "last_event_id": -1},
            )
        raise AssertionError(f"unexpected Zulip test request: {request.method} {path}")

    return httpx.MockTransport(handler)


def broker_transport(
    requests: list[dict[str, Any]], *, status_code: int = 201
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "POST" and request.url.path.endswith("/approvals"):
            body = json.loads(request.content.decode("utf-8"))
            requests.append(
                {
                    "path": request.url.path,
                    "actor": request.headers.get("X-Actor-ID"),
                    "body": body,
                }
            )
            if status_code >= 300:
                return httpx.Response(
                    status_code,
                    json={"error": {"code": "test_refusal", "message": "not exposed"}},
                )
            return httpx.Response(status_code, json={"id": "approval-id"})
        raise AssertionError(f"unexpected broker test request: {request.method} {request.url.path}")

    return httpx.MockTransport(handler)
