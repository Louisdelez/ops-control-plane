"""Narrow HTTP adapters for Zulip and the Unix-socket broker API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import re
import ssl
from typing import Any
import uuid

import httpx

from .config import Settings
from .errors import (
    BrokerRejected,
    ExternalServiceRejected,
    ProtocolError,
    QueueExpired,
    ResourceNotFound,
    RetryableBridgeError,
)


MAX_ZULIP_RESPONSE_BYTES = 1_048_576
MAX_BROKER_RESPONSE_BYTES = 65_536
MAX_ALERTMANAGER_RESPONSE_BYTES = 1_048_576
PUBLICATION_ACTOR_ID = "zulip-publisher"
MOBILE_ACTOR_ID = "zulip-mobile"
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


def _json_value(response: httpx.Response, maximum_bytes: int) -> Any:
    if len(response.content) > maximum_bytes:
        raise ProtocolError("an endpoint response exceeded the configured safety limit")
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise ProtocolError("an endpoint returned invalid JSON") from exc


def _json_object(response: httpx.Response, maximum_bytes: int) -> dict[str, Any]:
    payload = _json_value(response, maximum_bytes)
    if not isinstance(payload, dict):
        raise ProtocolError("an endpoint returned a non-object JSON response")
    return payload


@dataclass(frozen=True, slots=True)
class QueueCursor:
    queue_id: str
    last_event_id: int


@dataclass(frozen=True, slots=True)
class PendingApproval:
    action_id: str
    summary: str
    impact: str
    rollback: str
    deadline: str


@dataclass(frozen=True, slots=True)
class MobileMission:
    id: str
    project_id: str
    title: str
    status: str
    updated_at: str
    queue_state: str | None = None


class ZulipClient:
    """Minimal Zulip Events API client with no ``zuliprc`` support."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        if transport is None:
            context = ssl.create_default_context(
                cafile=str(settings.ca_bundle) if settings.ca_bundle is not None else None
            )
            transport = httpx.HTTPTransport(verify=context, retries=0)
        self._client = httpx.Client(
            base_url=settings.realm_url,
            auth=httpx.BasicAuth(settings.bot_email, settings.api_key),
            headers={"Accept": "application/json", "User-Agent": "zulip-approval-bridge/0.3"},
            timeout=httpx.Timeout(
                connect=10.0,
                read=settings.poll_timeout_seconds + 15.0,
                write=10.0,
                pool=10.0,
            ),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ZulipClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        allow_not_found: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RetryableBridgeError("the Zulip API is temporarily unreachable") from exc

        payload = _json_object(response, MAX_ZULIP_RESPONSE_BYTES)
        code = payload.get("code")
        if code == "BAD_EVENT_QUEUE_ID":
            raise QueueExpired("the Zulip event queue expired")
        if allow_not_found and response.status_code == 404:
            raise ResourceNotFound("the referenced Zulip resource does not exist")
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableBridgeError("the Zulip API returned a transient failure")
        if response.status_code >= 300:
            raise ExternalServiceRejected(
                f"the Zulip API rejected a request (HTTP {response.status_code})"
            )
        if payload.get("result") != "success":
            raise ExternalServiceRejected("the Zulip API returned a permanent application error")
        return payload

    def verify_bot_identity(self) -> None:
        profile = self._request("GET", "/api/v1/users/me")
        if (
            type(profile.get("user_id")) is not int
            or profile["user_id"] != self.settings.bot_user_id
            or profile.get("is_bot") is not True
            or profile.get("is_active") is not True
        ):
            raise ExternalServiceRejected(
                "the Zulip credential does not match the configured active bot user ID"
            )

    def verify_stream(self, stream: str, stream_id: int) -> None:
        payload = self._request(
            "GET",
            "/api/v1/get_stream_id",
            params={"stream": stream},
        )
        if (
            type(payload.get("stream_id")) is not int
            or payload["stream_id"] != stream_id
        ):
            raise ExternalServiceRejected(
                "the configured Zulip stream name and numeric stream ID do not match"
            )

    def verify_stream_identity(self) -> None:
        self.verify_stream(
            self.settings.approval_stream,
            self.settings.approval_stream_id,
        )

    def verify_alert_stream_identity(self) -> None:
        self.verify_stream(self.settings.alert_stream, self.settings.alert_stream_id)

    def register_queue(self) -> QueueCursor:
        payload = self._request(
            "POST",
            "/api/v1/register",
            data={
                "event_types": json.dumps(["message", "reaction"], separators=(",", ":")),
                "fetch_event_types": "[]",
                "narrow": json.dumps(
                    [["channel", self.settings.approval_stream]], separators=(",", ":")
                ),
                "client_gravatar": "false",
            },
        )
        queue_id = payload.get("queue_id")
        last_event_id = payload.get("last_event_id")
        if (
            not isinstance(queue_id, str)
            or not queue_id
            or len(queue_id) > 200
            or type(last_event_id) is not int
            or last_event_id < -1
        ):
            raise ProtocolError("Zulip returned an invalid event queue cursor")
        return QueueCursor(queue_id=queue_id, last_event_id=last_event_id)

    def get_events(self, cursor: QueueCursor) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/api/v1/events",
            params={
                "queue_id": cursor.queue_id,
                "last_event_id": str(cursor.last_event_id),
                "dont_block": "false",
            },
        )
        events = payload.get("events")
        if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
            raise ProtocolError("Zulip returned an invalid events collection")
        return events

    def get_user(self, user_id: int) -> dict[str, Any]:
        payload = self._request(
            "GET", f"/api/v1/users/{user_id}", allow_not_found=True
        )
        user = payload.get("user")
        if not isinstance(user, dict):
            raise ProtocolError("Zulip returned an invalid user object")
        return user

    def get_message(self, message_id: int) -> dict[str, Any]:
        payload = self._request(
            "GET", f"/api/v1/messages/{message_id}", allow_not_found=True
        )
        message = payload.get("message")
        if not isinstance(message, dict):
            raise ProtocolError("Zulip returned an invalid message object")
        return message

    def find_own_marker(
        self,
        *,
        stream: str,
        stream_id: int,
        topic: str,
        marker: str,
    ) -> int | None:
        """Find a previously accepted publication after an ambiguous send.

        Search results are not trusted by themselves: every returned message is
        checked against the configured numeric bot and stream identities.
        """

        payload = self._request(
            "GET",
            "/api/v1/messages",
            params={
                "anchor": "newest",
                "num_before": "50",
                "num_after": "0",
                "narrow": json.dumps(
                    [["channel", stream], ["topic", topic], ["search", marker]],
                    separators=(",", ":"),
                ),
            },
        )
        messages = payload.get("messages")
        if not isinstance(messages, list) or len(messages) > 50:
            raise ProtocolError("Zulip returned an invalid marker search result")
        matches: list[int] = []
        for message in messages:
            if not isinstance(message, dict):
                raise ProtocolError("Zulip returned a malformed marker search message")
            content = message.get("content")
            if (
                type(message.get("id")) is int
                and message["id"] > 0
                and message.get("type") == "stream"
                and type(message.get("stream_id")) is int
                and message["stream_id"] == stream_id
                and message.get("display_recipient") == stream
                and message.get("subject") == topic
                and type(message.get("sender_id")) is int
                and message["sender_id"] == self.settings.bot_user_id
                and isinstance(content, str)
                and content.count(marker) == 1
            ):
                matches.append(message["id"])
        if len(matches) > 1:
            raise ProtocolError("multiple Zulip messages contain one publication marker")
        return matches[0] if matches else None

    def send_stream_message(self, *, stream: str, topic: str, content: str) -> int:
        if not content or len(content) > 2_000:
            raise ProtocolError("a generated Zulip message exceeds its safety bound")
        payload = self._request(
            "POST",
            "/api/v1/messages",
            data={"type": "stream", "to": stream, "topic": topic, "content": content},
        )
        message_id = payload.get("id")
        if type(message_id) is not int or message_id <= 0:
            raise ProtocolError("Zulip returned an invalid published message ID")
        return message_id

    def upload_file(self, *, filename: str, content: bytes, content_type: str) -> str:
        if (
            not filename
            or len(filename) > 120
            or "/" in filename
            or "\\" in filename
            or not 1 <= len(content) <= 5_242_880
            or content_type not in {"application/json", "image/png"}
        ):
            raise ProtocolError("a requested Zulip upload is outside its safety bounds")
        payload = self._request(
            "POST",
            "/api/v1/user_uploads",
            files={"file": (filename, content, content_type)},
        )
        uri = payload.get("uri")
        if (
            not isinstance(uri, str)
            or not uri.startswith("/user_uploads/")
            or len(uri) > 1_000
            or any(character.isspace() for character in uri)
        ):
            raise ProtocolError("Zulip returned an invalid upload URI")
        return uri


class BrokerClient:
    """Approval-only adapter; production traffic can only use a Unix socket."""

    def __init__(
        self,
        socket_path: Path,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if transport is None:
            transport = httpx.HTTPTransport(uds=str(socket_path), retries=0)
        self._client = httpx.Client(
            base_url="http://ops-broker",
            headers={"Accept": "application/json", "User-Agent": "zulip-approval-bridge/0.3"},
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "BrokerClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RetryableBridgeError("the local Ops broker is temporarily unreachable") from exc
        payload = _json_object(response, MAX_BROKER_RESPONSE_BYTES)
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableBridgeError("the local Ops broker returned a transient failure")
        if response.status_code >= 300:
            raise BrokerRejected(response.status_code)
        return payload

    def verify_health(self) -> None:
        payload = self._request("GET", "/healthz")
        if payload != {"status": "ok"}:
            raise ProtocolError("the local Ops broker health response is invalid")

    def submit_approval(
        self,
        *,
        action_id: str,
        actor_id: str,
        request_id: str,
        decision: str,
        source_event_id: str,
        reason: str,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/actions/{action_id}/approvals",
            headers={"X-Actor-ID": actor_id, "Content-Type": "application/json"},
            json={
                "actor_id": actor_id,
                "request_id": request_id,
                "decision": decision,
                "source": "zulip",
                "source_event_id": source_event_id,
                "reason": reason,
            },
        )

    def list_pending_public(self, *, limit: int, offset: int) -> tuple[PendingApproval, ...]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("pending approval page size must be between 1 and 50")
        if type(offset) is not int or not 0 <= offset <= 10_000:
            raise ValueError("pending approval offset must be between 0 and 10000")
        payload = self._request(
            "GET",
            "/v1/zulip/approvals/pending",
            headers={"X-Actor-ID": PUBLICATION_ACTOR_ID},
            params={"limit": str(limit), "offset": str(offset)},
        )
        if set(payload) != {"items"} or not isinstance(payload["items"], list):
            raise ProtocolError("the local Ops broker returned an invalid public approval page")
        items = payload["items"]
        if len(items) > limit:
            raise ProtocolError("the local Ops broker exceeded the requested approval page size")
        parsed: list[PendingApproval] = []
        for item in items:
            if not isinstance(item, dict) or set(item) != {
                "action_id",
                "summary",
                "impact",
                "rollback",
                "deadline",
            }:
                raise ProtocolError("the local Ops broker returned unsafe approval fields")
            try:
                action_uuid = uuid.UUID(item["action_id"], version=4)
            except (AttributeError, TypeError, ValueError) as exc:
                raise ProtocolError("the local Ops broker returned an invalid action ID") from exc
            if str(action_uuid) != item["action_id"]:
                raise ProtocolError("the local Ops broker returned a non-canonical action ID")
            text_values = (item["summary"], item["impact"], item["rollback"])
            if any(not isinstance(value, str) or not 1 <= len(value) <= 1_000 for value in text_values):
                raise ProtocolError("the local Ops broker returned invalid public approval text")
            deadline = item["deadline"]
            if not isinstance(deadline, str) or len(deadline) > 64:
                raise ProtocolError("the local Ops broker returned an invalid approval deadline")
            try:
                parsed_deadline = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ProtocolError("the local Ops broker returned an invalid approval deadline") from exc
            if parsed_deadline.tzinfo is None:
                raise ProtocolError("the local Ops broker returned a timezone-free approval deadline")
            parsed.append(
                PendingApproval(
                    action_id=str(action_uuid),
                    summary=item["summary"],
                    impact=item["impact"],
                    rollback=item["rollback"],
                    deadline=deadline,
                )
            )
        return tuple(parsed)

    @staticmethod
    def _mobile_mission(payload: dict[str, Any], *, status_view: bool) -> MobileMission:
        required = {"id", "project_id", "title", "status", "created_at", "updated_at"}
        if status_view:
            required = {"id", "project_id", "title", "status", "queue_state", "updated_at"}
        if set(payload) != required:
            raise ProtocolError("the local Ops broker returned unsafe mobile mission fields")
        try:
            mission_id = str(uuid.UUID(payload["id"], version=4))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProtocolError("the local Ops broker returned an invalid mission ID") from exc
        project = payload.get("project_id")
        title = payload.get("title")
        status = payload.get("status")
        updated = payload.get("updated_at")
        created = payload.get("created_at") if not status_view else None
        queue_state = payload.get("queue_state") if status_view else None
        if (
            mission_id != payload["id"]
            or not isinstance(project, str)
            or not _PROJECT_ID.fullmatch(project)
            or not isinstance(title, str)
            or not 1 <= len(title) <= 240
            or status not in {"open", "paused", "completed"}
            or not isinstance(updated, str)
            or not 1 <= len(updated) <= 40
            or (not status_view and (not isinstance(created, str) or not 1 <= len(created) <= 40))
            or (status_view and queue_state not in {"queued", "claimed", "done"})
        ):
            raise ProtocolError("the local Ops broker returned an invalid mobile mission")
        for timestamp in (updated,) if status_view else (created, updated):
            assert isinstance(timestamp, str)
            try:
                parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ProtocolError("the local Ops broker returned an invalid mission timestamp") from exc
            if parsed_timestamp.tzinfo is None:
                raise ProtocolError("the local Ops broker returned a timezone-free mission timestamp")
        return MobileMission(mission_id, project, title, status, updated, queue_state)

    def create_mobile_mission(
        self, *, request_id: str, project_id: str, title: str
    ) -> MobileMission:
        payload = self._request(
            "POST",
            "/v1/zulip/missions",
            headers={"X-Actor-ID": MOBILE_ACTOR_ID, "Content-Type": "application/json"},
            json={"request_id": request_id, "project_id": project_id, "title": title},
        )
        return self._mobile_mission(payload, status_view=False)

    def get_mobile_mission(self, mission_id: str) -> MobileMission:
        payload = self._request(
            "GET",
            f"/v1/zulip/missions/{mission_id}/status",
            headers={"X-Actor-ID": MOBILE_ACTOR_ID},
        )
        return self._mobile_mission(payload, status_view=True)

    def resume_mobile_mission(self, *, mission_id: str, request_id: str) -> MobileMission:
        payload = self._request(
            "POST",
            f"/v1/zulip/missions/{mission_id}/resume",
            headers={"X-Actor-ID": MOBILE_ACTOR_ID, "Content-Type": "application/json"},
            json={"request_id": request_id},
        )
        return self._mobile_mission(payload, status_view=True)

    def answer_mobile_mission(
        self,
        *,
        mission_id: str,
        request_id: str,
        source_user_id: int,
        answer: str,
    ) -> None:
        payload = self._request(
            "POST",
            f"/v1/zulip/missions/{mission_id}/answers",
            headers={"X-Actor-ID": MOBILE_ACTOR_ID, "Content-Type": "application/json"},
            json={
                "request_id": request_id,
                "source_user_id": source_user_id,
                "answer": answer,
            },
        )
        if set(payload) != {"id", "mission_id", "kind", "created_at", "replayed"}:
            raise ProtocolError("the local Ops broker returned unsafe answer fields")
        try:
            record_id = str(uuid.UUID(payload["id"], version=4))
            returned_mission_id = str(uuid.UUID(payload["mission_id"], version=4))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProtocolError("the local Ops broker returned invalid answer IDs") from exc
        if (
            record_id != payload["id"]
            or returned_mission_id != mission_id
            or payload.get("kind") != "decision"
            or type(payload.get("replayed")) is not bool
            or not isinstance(payload.get("created_at"), str)
        ):
            raise ProtocolError("the local Ops broker returned an invalid answer receipt")
        try:
            created = datetime.fromisoformat(payload["created_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProtocolError("the local Ops broker returned an invalid answer timestamp") from exc
        if created.tzinfo is None:
            raise ProtocolError("the local Ops broker returned a timezone-free answer timestamp")


class AlertmanagerClient:
    """Read-only Alertmanager v2 adapter through a local Unix socket."""

    def __init__(
        self,
        socket_path: Path,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if transport is None:
            transport = httpx.HTTPTransport(uds=str(socket_path), retries=0)
        self._client = httpx.Client(
            base_url="http://alertmanager",
            headers={"Accept": "application/json", "User-Agent": "zulip-approval-bridge/0.3"},
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AlertmanagerClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _response(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise RetryableBridgeError("the local Alertmanager query socket is unreachable") from exc
        if len(response.content) > MAX_ALERTMANAGER_RESPONSE_BYTES:
            raise ProtocolError("the Alertmanager response exceeded the configured safety limit")
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableBridgeError("the local Alertmanager API returned a transient failure")
        if response.status_code >= 300:
            raise ExternalServiceRejected(
                f"the local Alertmanager API rejected a request (HTTP {response.status_code})"
            )
        return response

    def verify_ready(self) -> None:
        response = self._response("GET", "/-/ready")
        if len(response.content) > 1_024:
            raise ProtocolError("the Alertmanager readiness response is too large")

    def active_alerts(self) -> list[dict[str, Any]]:
        response = self._response(
            "GET",
            "/api/v2/alerts",
            params={
                "active": "true",
                "silenced": "true",
                "inhibited": "true",
                "unprocessed": "true",
            },
        )
        payload = _json_value(response, MAX_ALERTMANAGER_RESPONSE_BYTES)
        if not isinstance(payload, list) or len(payload) > 1_000:
            raise ProtocolError("Alertmanager returned an invalid or excessive alert collection")
        if any(not isinstance(alert, dict) for alert in payload):
            raise ProtocolError("Alertmanager returned a malformed alert")
        return payload
