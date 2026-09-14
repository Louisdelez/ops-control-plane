from __future__ import annotations

import pytest

from zulip_approval_bridge.config import Settings
from zulip_approval_bridge.errors import ConfigurationError


def valid_environment() -> dict[str, str]:
    return {
        "ZULIP_REALM_URL": "https://ops.example.invalid/",
        "ZULIP_BOT_EMAIL": "bot@example.invalid",
        "ZULIP_API_KEY": "s" * 32,
        "ZULIP_BOT_USER_ID": "900",
        "ZULIP_APPROVER_USER_IDS": "42, 73",
        "ZULIP_APPROVAL_STREAM": "Infrastructure",
        "ZULIP_APPROVAL_STREAM_ID": "77",
        "ZULIP_APPROVAL_TOPIC": "Approbations",
        "ZULIP_ALERT_STREAM": "Monitoring",
        "ZULIP_ALERT_STREAM_ID": "88",
        "ZULIP_ALERT_TOPIC": "Alertes",
        "ZULIP_DAILY_STREAM": "Infrastructure",
        "ZULIP_DAILY_STREAM_ID": "77",
        "ZULIP_DAILY_TOPIC": "Rapport quotidien",
    }


@pytest.mark.parametrize(
    "missing",
    [
        "ZULIP_BOT_USER_ID",
        "ZULIP_APPROVER_USER_IDS",
        "ZULIP_APPROVAL_STREAM",
        "ZULIP_APPROVAL_STREAM_ID",
        "ZULIP_APPROVAL_TOPIC",
        "ZULIP_ALERT_STREAM",
        "ZULIP_ALERT_STREAM_ID",
        "ZULIP_ALERT_TOPIC",
        "ZULIP_DAILY_STREAM",
        "ZULIP_DAILY_STREAM_ID",
        "ZULIP_DAILY_TOPIC",
    ],
)
def test_trust_boundaries_are_required(missing: str) -> None:
    environment = valid_environment()
    del environment[missing]
    with pytest.raises(ConfigurationError, match=missing):
        Settings.from_env(environment)


@pytest.mark.parametrize(
    "realm",
    [
        "http://ops.example.invalid",
        "https://user:pass@ops.example.invalid",
        "https://ops.example.invalid/a/path",
        "https://ops.example.invalid?other=realm",
    ],
)
def test_realm_must_be_one_https_origin(realm: str) -> None:
    environment = valid_environment()
    environment["ZULIP_REALM_URL"] = realm
    with pytest.raises(ConfigurationError, match="HTTPS origin"):
        Settings.from_env(environment)


def test_approvers_are_numeric_and_nonempty() -> None:
    for value in ("", "zulip:42", "42,", "-1"):
        environment = valid_environment()
        environment["ZULIP_APPROVER_USER_IDS"] = value
        with pytest.raises(ConfigurationError):
            Settings.from_env(environment)


def test_secrets_are_not_in_settings_representation() -> None:
    environment = valid_environment()
    settings = Settings.from_env(environment)
    rendered = repr(settings)
    assert environment["ZULIP_API_KEY"] not in rendered
    assert environment["ZULIP_BOT_EMAIL"] not in rendered
    assert settings.realm_url == "https://ops.example.invalid"
    assert settings.approver_user_ids == frozenset({42, 73})


def test_bot_id_cannot_be_approver() -> None:
    environment = valid_environment()
    environment["ZULIP_APPROVER_USER_IDS"] = "42,900"
    with pytest.raises(ConfigurationError, match="cannot be a human approver"):
        Settings.from_env(environment)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ALERTMANAGER_QUERY_SOCKET", "relative.sock"),
        ("OPS_DAILY_REPORT_PATH", "latest.json"),
        ("OPS_BROKER_PENDING_PAGE_SIZE", "51"),
        ("ZULIP_PRODUCER_POLL_SECONDS", "1"),
    ],
)
def test_producer_paths_and_bounds_are_fail_closed(name: str, value: str) -> None:
    environment = valid_environment()
    environment[name] = value
    with pytest.raises(ConfigurationError):
        Settings.from_env(environment)
