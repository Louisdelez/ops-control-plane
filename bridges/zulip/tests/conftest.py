from __future__ import annotations

from pathlib import Path

import pytest

from zulip_approval_bridge.config import Settings


ACTION_ID = "123e4567-e89b-42d3-a456-426614174000"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        realm_url="https://ops.example.invalid",
        bot_email="approval-bot@example.invalid",
        api_key="a" * 32,
        bot_user_id=900,
        approver_user_ids=frozenset({42}),
        approval_stream="Infrastructure",
        approval_stream_id=77,
        approval_topic="Approbations",
        alert_stream="Monitoring",
        alert_stream_id=88,
        alert_topic="Alertes",
        daily_stream="Infrastructure",
        daily_stream_id=77,
        daily_topic="Rapport quotidien",
        broker_socket=Path("/run/ops-broker/api.sock"),
        alertmanager_socket=Path("/run/zulip-alertmanager-query/api.sock"),
        daily_report_path=tmp_path / "reports" / "latest.json",
        state_path=tmp_path / "state" / "bridge.db",
        poll_timeout_seconds=30.0,
        retry_max_seconds=5.0,
        producer_poll_seconds=5.0,
        pending_page_size=50,
    )


@pytest.fixture
def approval_event() -> dict[str, object]:
    return {
        "id": 12,
        "type": "reaction",
        "op": "add",
        "emoji_name": "check",
        "emoji_code": "2705",
        "reaction_type": "unicode_emoji",
        "user_id": 42,
        "message_id": 555,
    }
