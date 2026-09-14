from __future__ import annotations

from copy import deepcopy
import uuid

import pytest

from zulip_approval_bridge.bridge import ApprovalProcessor
from zulip_approval_bridge.clients import BrokerClient, ZulipClient
from zulip_approval_bridge.config import Settings

from helpers import broker_transport, valid_message, valid_user, zulip_transport


@pytest.mark.parametrize(
    ("emoji_name", "emoji_code", "expected_decision", "expected_outcome"),
    [
        ("check", "2705", "approve", "approved"),
        ("cross_mark", "274c", "reject", "rejected"),
    ],
)
def test_unicode_reaction_maps_stable_user_id_to_broker(
    settings: Settings,
    approval_event: dict[str, object],
    emoji_name: str,
    emoji_code: str,
    expected_decision: str,
    expected_outcome: str,
) -> None:
    event = deepcopy(approval_event)
    event["emoji_name"] = emoji_name
    event["emoji_code"] = emoji_code
    broker_requests: list[dict[str, object]] = []
    with ZulipClient(settings, transport=zulip_transport(settings)) as zulip, BrokerClient(
        settings.broker_socket, transport=broker_transport(broker_requests)
    ) as broker:
        processor = ApprovalProcessor(settings, zulip, broker)
        outcome = processor.process(event, "queue-1")

    assert outcome == expected_outcome
    assert len(broker_requests) == 1
    request = broker_requests[0]
    assert request["actor"] == "zulip:42"
    assert request["path"].endswith("/123e4567-e89b-42d3-a456-426614174000/approvals")
    body = request["body"]
    assert isinstance(body, dict)
    assert body["actor_id"] == "zulip:42"
    assert body["decision"] == expected_decision
    assert body["source"] == "zulip"
    assert body["source_event_id"].endswith(":12")
    uuid.UUID(body["request_id"])


def test_retry_uses_the_same_request_and_source_ids(
    settings: Settings, approval_event: dict[str, object]
) -> None:
    broker_requests: list[dict[str, object]] = []
    with ZulipClient(settings, transport=zulip_transport(settings)) as zulip, BrokerClient(
        settings.broker_socket, transport=broker_transport(broker_requests)
    ) as broker:
        processor = ApprovalProcessor(settings, zulip, broker)
        assert processor.process(approval_event, "queue-1") == "approved"
        assert processor.process(approval_event, "queue-1") == "approved"

    first = broker_requests[0]["body"]
    second = broker_requests[1]["body"]
    assert first["request_id"] == second["request_id"]
    assert first["source_event_id"] == second["source_event_id"]


def test_custom_emoji_named_check_is_ignored_without_identity_lookup(
    settings: Settings, approval_event: dict[str, object]
) -> None:
    event = deepcopy(approval_event)
    event["reaction_type"] = "realm_emoji"
    requests = []
    broker_requests: list[dict[str, object]] = []
    with ZulipClient(
        settings, transport=zulip_transport(settings, requests=requests)
    ) as zulip, BrokerClient(
        settings.broker_socket, transport=broker_transport(broker_requests)
    ) as broker:
        outcome = ApprovalProcessor(settings, zulip, broker).process(event, "queue-1")
    assert outcome == "ignored_reaction"
    assert requests == []
    assert broker_requests == []


def test_unconfigured_actor_is_ignored_before_user_lookup(
    settings: Settings, approval_event: dict[str, object]
) -> None:
    event = deepcopy(approval_event)
    event["user_id"] = 43
    requests = []
    with ZulipClient(
        settings, transport=zulip_transport(settings, requests=requests)
    ) as zulip, BrokerClient(
        settings.broker_socket, transport=broker_transport([])
    ) as broker:
        outcome = ApprovalProcessor(settings, zulip, broker).process(event, "queue-1")
    assert outcome == "ignored_unauthorized_actor"
    assert requests == []


@pytest.mark.parametrize(
    "user_patch",
    [
        {"is_bot": True},
        {"is_active": False},
        {"is_imported_stub": True},
        {"user_id": 99},
    ],
)
def test_bots_and_unverifiable_identities_are_ignored(
    settings: Settings,
    approval_event: dict[str, object],
    user_patch: dict[str, object],
) -> None:
    user = valid_user()
    user.update(user_patch)
    broker_requests: list[dict[str, object]] = []
    with ZulipClient(settings, transport=zulip_transport(settings, user=user)) as zulip, BrokerClient(
        settings.broker_socket, transport=broker_transport(broker_requests)
    ) as broker:
        outcome = ApprovalProcessor(settings, zulip, broker).process(
            approval_event, "queue-1"
        )
    assert outcome == "ignored_non_human_actor"
    assert broker_requests == []


@pytest.mark.parametrize(
    ("message_patch", "expected"),
    [
        ({"display_recipient": "Minecraft"}, "ignored_wrong_message_scope"),
        ({"stream_id": 78}, "ignored_wrong_message_scope"),
        ({"subject": "Autre sujet"}, "ignored_wrong_message_scope"),
        ({"sender_id": 901}, "ignored_wrong_message_scope"),
        ({"id": 556}, "ignored_wrong_message_scope"),
        ({"type": "private"}, "ignored_wrong_message_scope"),
        ({"content": "<p>No marker</p>"}, "ignored_invalid_approval_marker"),
        (
            {
                "content": (
                    "OPS-APPROVAL-V1:123e4567-e89b-42d3-a456-426614174000 "
                    "OPS-APPROVAL-V1:223e4567-e89b-42d3-a456-426614174000"
                )
            },
            "ignored_invalid_approval_marker",
        ),
    ],
)
def test_message_scope_and_single_marker_are_mandatory(
    settings: Settings,
    approval_event: dict[str, object],
    message_patch: dict[str, object],
    expected: str,
) -> None:
    message = valid_message()
    message.update(message_patch)
    broker_requests: list[dict[str, object]] = []
    with ZulipClient(
        settings, transport=zulip_transport(settings, message=message)
    ) as zulip, BrokerClient(
        settings.broker_socket, transport=broker_transport(broker_requests)
    ) as broker:
        outcome = ApprovalProcessor(settings, zulip, broker).process(
            approval_event, "queue-1"
        )
    assert outcome == expected
    assert broker_requests == []


def test_permanent_broker_refusal_is_safe_outcome(
    settings: Settings, approval_event: dict[str, object]
) -> None:
    broker_requests: list[dict[str, object]] = []
    with ZulipClient(settings, transport=zulip_transport(settings)) as zulip, BrokerClient(
        settings.broker_socket,
        transport=broker_transport(broker_requests, status_code=403),
    ) as broker:
        outcome = ApprovalProcessor(settings, zulip, broker).process(
            approval_event, "queue-1"
        )
    assert outcome == "broker_rejected"
    assert len(broker_requests) == 1
