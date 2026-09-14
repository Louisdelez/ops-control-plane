"""Fail-closed reaction validation and long-poll orchestration."""

from __future__ import annotations

import hashlib
import logging
import random
import re
import time
from typing import Any
import uuid

from .clients import BrokerClient, QueueCursor, ZulipClient
from .config import Settings
from .errors import (
    BrokerRejected,
    ProtocolError,
    QueueExpired,
    ResourceNotFound,
    RetryableBridgeError,
)
from .state import EventState
from .mobile import MobileCommandProcessor


LOG = logging.getLogger("zulip_approval_bridge")

APPROVAL_MARKER = re.compile(
    r"(?<![A-Za-z0-9_-])OPS-APPROVAL-V1:"
    r"([0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
    r"(?![A-Za-z0-9_-])"
)

# Emoji names alone are insufficient: a realm emoji can reuse a trusted-looking
# name. Both decisions require the official Unicode namespace and codepoint.
REACTIONS: dict[tuple[str, str, str], tuple[str, str]] = {
    ("unicode_emoji", "check", "2705"): ("approve", "✅"),
    ("unicode_emoji", "cross_mark", "274c"): ("reject", "❌"),
}


def _event_integer(event: dict[str, Any], key: str, *, allow_zero: bool = False) -> int:
    value = event.get(key)
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        raise ProtocolError(f"a Zulip event contains an invalid {key}")
    return value


class ApprovalProcessor:
    def __init__(
        self,
        settings: Settings,
        zulip: ZulipClient,
        broker: BrokerClient,
        mobile: MobileCommandProcessor | None = None,
    ) -> None:
        self.settings = settings
        self.zulip = zulip
        self.broker = broker
        self.mobile = mobile

    def verify_startup(self) -> None:
        self.zulip.verify_bot_identity()
        self.zulip.verify_stream_identity()
        self.broker.verify_health()

    def process(self, event: dict[str, Any], queue_id: str) -> str:
        """Validate and forward one event, returning a non-sensitive outcome."""

        event_id = _event_integer(event, "id", allow_zero=True)
        if event.get("type") == "message":
            if self.mobile is None:
                return "ignored_event_type"
            return self.mobile.process(event, queue_id)
        if event.get("type") != "reaction" or event.get("op") != "add":
            return "ignored_event_type"

        reaction = REACTIONS.get(
            (
                event.get("reaction_type"),
                event.get("emoji_name"),
                event.get("emoji_code"),
            )
        )
        if reaction is None:
            return "ignored_reaction"
        decision, glyph = reaction

        user_id = _event_integer(event, "user_id")
        if user_id not in self.settings.approver_user_ids:
            return "ignored_unauthorized_actor"

        try:
            user = self.zulip.get_user(user_id)
        except ResourceNotFound:
            return "ignored_missing_actor"
        if (
            type(user.get("user_id")) is not int
            or user["user_id"] != user_id
            or user.get("is_bot") is not False
            or user.get("is_active") is not True
            or user.get("is_deleted") is True
            or user.get("is_imported_stub") is True
        ):
            return "ignored_non_human_actor"

        message_id = _event_integer(event, "message_id")
        try:
            message = self.zulip.get_message(message_id)
        except ResourceNotFound:
            return "ignored_missing_message"
        if not self._is_target_message(message, message_id):
            return "ignored_wrong_message_scope"

        content = message.get("content")
        if not isinstance(content, str):
            raise ProtocolError("Zulip returned a message without string content")
        markers = APPROVAL_MARKER.findall(content)
        if len(markers) != 1:
            return "ignored_invalid_approval_marker"
        action_id = str(uuid.UUID(markers[0]))

        queue_fingerprint = hashlib.sha256(queue_id.encode("utf-8")).hexdigest()[:16]
        source_event_id = (
            f"zulip:{self.settings.realm_fingerprint}:{queue_fingerprint}:{event_id}"
        )
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, source_event_id))
        actor_id = f"zulip:{user_id}"
        reason = f"Réaction Zulip {glyph}, message {message_id}."

        try:
            self.broker.submit_approval(
                action_id=action_id,
                actor_id=actor_id,
                request_id=request_id,
                decision=decision,
                source_event_id=source_event_id,
                reason=reason,
            )
        except BrokerRejected as exc:
            LOG.warning(
                "broker refusal event_id=%d user_id=%d message_id=%d status=%d",
                event_id,
                user_id,
                message_id,
                exc.status_code,
            )
            return "broker_rejected"
        return "approved" if decision == "approve" else "rejected"

    def _is_target_message(self, message: dict[str, Any], message_id: int) -> bool:
        return (
            type(message.get("id")) is int
            and message["id"] == message_id
            and message.get("type") == "stream"
            and type(message.get("stream_id")) is int
            and message["stream_id"] == self.settings.approval_stream_id
            and message.get("display_recipient") == self.settings.approval_stream
            and message.get("subject") == self.settings.approval_topic
            and type(message.get("sender_id")) is int
            and message["sender_id"] == self.settings.bot_user_id
        )


class BridgeRunner:
    """Maintain one reaction queue and acknowledge only durable outcomes."""

    def __init__(
        self,
        settings: Settings,
        state: EventState,
        zulip: ZulipClient,
        processor: ApprovalProcessor,
        *,
        sleeper: Any = time.sleep,
    ) -> None:
        self.settings = settings
        self.state = state
        self.zulip = zulip
        self.processor = processor
        self.sleeper = sleeper

    def cursor(self) -> QueueCursor:
        stored = self.state.load_cursor(self.settings.realm_fingerprint)
        if stored is not None:
            return QueueCursor(stored.queue_id, stored.last_event_id)
        registered = self.zulip.register_queue()
        self.state.save_cursor(
            self.settings.realm_fingerprint,
            registered.queue_id,
            registered.last_event_id,
        )
        LOG.info("registered a new Zulip reaction queue")
        return registered

    def poll_once(self, cursor: QueueCursor) -> QueueCursor:
        events = self.zulip.get_events(cursor)
        current = cursor
        for event in events:
            event_id = _event_integer(event, "id", allow_zero=True)
            if event_id <= current.last_event_id:
                continue
            if self.state.is_processed(
                self.settings.realm_fingerprint, current.queue_id, event_id
            ):
                outcome = "duplicate_event"
            else:
                outcome = self.processor.process(event, current.queue_id)
            stored = self.state.commit_event(
                realm_fingerprint=self.settings.realm_fingerprint,
                queue_id=current.queue_id,
                event_id=event_id,
                outcome=outcome,
            )
            current = QueueCursor(stored.queue_id, stored.last_event_id)
            if outcome in {"approved", "rejected"}:
                LOG.info("decision recorded event_id=%d outcome=%s", event_id, outcome)
            elif outcome == "broker_rejected":
                LOG.warning("decision not recorded event_id=%d", event_id)
        return current

    def run_forever(
        self,
        *,
        verify_startup: bool = True,
        stop_event: Any | None = None,
    ) -> None:
        if verify_startup:
            self.processor.verify_startup()
        current = self.cursor()
        backoff = 1.0
        while stop_event is None or not stop_event.is_set():
            try:
                current = self.poll_once(current)
                backoff = 1.0
            except QueueExpired:
                self.state.clear_cursor(self.settings.realm_fingerprint, current.queue_id)
                current = self.cursor()
                backoff = 1.0
            except RetryableBridgeError as exc:
                LOG.warning("transient bridge failure: %s; retrying", exc)
                delay = min(backoff, self.settings.retry_max_seconds)
                delay += random.uniform(0.0, min(delay * 0.2, 1.0))
                if stop_event is None:
                    self.sleeper(delay)
                else:
                    stop_event.wait(delay)
                backoff = min(backoff * 2.0, self.settings.retry_max_seconds)
