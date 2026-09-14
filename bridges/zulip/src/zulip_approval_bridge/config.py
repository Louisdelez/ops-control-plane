"""Strict, environment-only bridge configuration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ConfigurationError


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None or not value.strip():
        raise ConfigurationError(f"required environment variable {name} is not configured")
    return value.strip()


def _positive_integer(raw: str, name: str) -> int:
    if not raw.isascii() or not raw.isdecimal():
        raise ConfigurationError(f"{name} must contain one positive decimal integer")
    value = int(raw)
    if value <= 0:
        raise ConfigurationError(f"{name} must contain one positive decimal integer")
    return value


def _positive_integer_set(raw: str, name: str) -> frozenset[int]:
    pieces = [piece.strip() for piece in raw.split(",")]
    if not pieces or any(not piece for piece in pieces):
        raise ConfigurationError(f"{name} must be a comma-separated list of numeric user IDs")
    values = frozenset(_positive_integer(piece, name) for piece in pieces)
    if not values:
        raise ConfigurationError(f"{name} must configure at least one numeric user ID")
    return values


def _bounded_float(environment: Mapping[str, str], name: str, default: str, low: float, high: float) -> float:
    raw = environment.get(name, default).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not low <= value <= high:
        raise ConfigurationError(f"{name} must be between {low:g} and {high:g}")
    return value


def _bounded_integer(
    environment: Mapping[str, str], name: str, default: str, low: int, high: int
) -> int:
    value = _positive_integer(environment.get(name, default).strip(), name)
    if not low <= value <= high:
        raise ConfigurationError(f"{name} must be between {low} and {high}")
    return value


def _scope_value(environment: Mapping[str, str], name: str) -> str:
    value = _required(environment, name)
    if len(value) > 200 or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigurationError(f"{name} must be bounded text without control characters")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """All trust boundaries required by the bridge.

    The two Zulip credentials are excluded from ``repr`` so an accidental
    settings log cannot disclose them. The application has no credential-file
    parser: an OpenBao integration must inject these values into the process
    environment at launch.
    """

    realm_url: str
    bot_email: str = field(repr=False)
    api_key: str = field(repr=False)
    bot_user_id: int
    approver_user_ids: frozenset[int]
    approval_stream: str
    approval_stream_id: int
    approval_topic: str
    alert_stream: str
    alert_stream_id: int
    alert_topic: str
    daily_stream: str
    daily_stream_id: int
    daily_topic: str
    broker_socket: Path = Path("/run/ops-broker/api.sock")
    alertmanager_socket: Path = Path("/run/zulip-alertmanager-query/api.sock")
    daily_report_path: Path = Path("/var/lib/ops-reports/daily/latest.json")
    capture_dir: Path = Path("/var/lib/ops-reports/captures")
    state_path: Path = Path("/var/lib/zulip-approval-bridge/state.db")
    ca_bundle: Path | None = None
    poll_timeout_seconds: float = 90.0
    retry_max_seconds: float = 60.0
    producer_poll_seconds: float = 15.0
    pending_page_size: int = 50

    @property
    def realm_fingerprint(self) -> str:
        return hashlib.sha256(self.realm_url.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def from_env(cls, environment: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if environment is None else environment

        raw_realm = _required(env, "ZULIP_REALM_URL")
        parsed = urlsplit(raw_realm)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ConfigurationError(
                "ZULIP_REALM_URL must be one HTTPS origin without credentials, path, query, or fragment"
            )
        realm_url = raw_realm.rstrip("/")

        bot_email = _required(env, "ZULIP_BOT_EMAIL")
        api_key = _required(env, "ZULIP_API_KEY")
        if len(api_key) < 16:
            raise ConfigurationError("ZULIP_API_KEY is not shaped like an API credential")

        approvers = _positive_integer_set(
            _required(env, "ZULIP_APPROVER_USER_IDS"),
            "ZULIP_APPROVER_USER_IDS",
        )
        bot_user_id = _positive_integer(
            _required(env, "ZULIP_BOT_USER_ID"), "ZULIP_BOT_USER_ID"
        )
        if bot_user_id in approvers:
            raise ConfigurationError("ZULIP_BOT_USER_ID cannot be a human approver")
        stream = _scope_value(env, "ZULIP_APPROVAL_STREAM")
        topic = _scope_value(env, "ZULIP_APPROVAL_TOPIC")
        alert_stream = _scope_value(env, "ZULIP_ALERT_STREAM")
        alert_topic = _scope_value(env, "ZULIP_ALERT_TOPIC")
        daily_stream = _scope_value(env, "ZULIP_DAILY_STREAM")
        daily_topic = _scope_value(env, "ZULIP_DAILY_TOPIC")

        broker_socket = Path(env.get("OPS_BROKER_SOCKET", "/run/ops-broker/api.sock"))
        alertmanager_socket = Path(
            env.get(
                "ALERTMANAGER_QUERY_SOCKET",
                "/run/zulip-alertmanager-query/api.sock",
            )
        )
        daily_report_path = Path(
            env.get("OPS_DAILY_REPORT_PATH", "/var/lib/ops-reports/daily/latest.json")
        )
        capture_dir = Path(
            env.get("OPS_CAPTURE_DIR", "/var/lib/ops-reports/captures")
        )
        state_path = Path(env.get("ZULIP_BRIDGE_STATE", "/var/lib/zulip-approval-bridge/state.db"))
        if not broker_socket.is_absolute():
            raise ConfigurationError("OPS_BROKER_SOCKET must be an absolute Unix-socket path")
        if not alertmanager_socket.is_absolute():
            raise ConfigurationError("ALERTMANAGER_QUERY_SOCKET must be an absolute Unix-socket path")
        if alertmanager_socket == broker_socket:
            raise ConfigurationError("broker and Alertmanager Unix sockets must be distinct")
        if not daily_report_path.is_absolute():
            raise ConfigurationError("OPS_DAILY_REPORT_PATH must be an absolute file path")
        if not capture_dir.is_absolute():
            raise ConfigurationError("OPS_CAPTURE_DIR must be an absolute directory path")
        if not state_path.is_absolute():
            raise ConfigurationError("ZULIP_BRIDGE_STATE must be an absolute path")

        raw_ca = env.get("ZULIP_CA_BUNDLE", "").strip()
        ca_bundle = Path(raw_ca) if raw_ca else None
        if ca_bundle is not None and not ca_bundle.is_absolute():
            raise ConfigurationError("ZULIP_CA_BUNDLE must be an absolute path")

        return cls(
            realm_url=realm_url,
            bot_email=bot_email,
            api_key=api_key,
            bot_user_id=bot_user_id,
            approver_user_ids=approvers,
            approval_stream=stream,
            approval_stream_id=_positive_integer(
                _required(env, "ZULIP_APPROVAL_STREAM_ID"), "ZULIP_APPROVAL_STREAM_ID"
            ),
            approval_topic=topic,
            alert_stream=alert_stream,
            alert_stream_id=_positive_integer(
                _required(env, "ZULIP_ALERT_STREAM_ID"), "ZULIP_ALERT_STREAM_ID"
            ),
            alert_topic=alert_topic,
            daily_stream=daily_stream,
            daily_stream_id=_positive_integer(
                _required(env, "ZULIP_DAILY_STREAM_ID"), "ZULIP_DAILY_STREAM_ID"
            ),
            daily_topic=daily_topic,
            broker_socket=broker_socket,
            alertmanager_socket=alertmanager_socket,
            daily_report_path=daily_report_path,
            capture_dir=capture_dir,
            state_path=state_path,
            ca_bundle=ca_bundle,
            poll_timeout_seconds=_bounded_float(
                env, "ZULIP_POLL_TIMEOUT_SECONDS", "90", 30, 300
            ),
            retry_max_seconds=_bounded_float(
                env, "ZULIP_RETRY_MAX_SECONDS", "60", 5, 300
            ),
            producer_poll_seconds=_bounded_float(
                env, "ZULIP_PRODUCER_POLL_SECONDS", "15", 5, 300
            ),
            pending_page_size=_bounded_integer(
                env, "OPS_BROKER_PENDING_PAGE_SIZE", "50", 1, 50
            ),
        )
