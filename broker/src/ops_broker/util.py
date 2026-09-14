"""Small deterministic helpers shared by persistence and execution."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any


GENESIS_HASH = "0" * 64

_SENSITIVE_KEY = re.compile(
    r"(?:password|passwd|pwd|token|secret|credential|authorization|api[_-]?key|private[_-]?key)",
    re.IGNORECASE,
)
_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|credential|authorization|api[_-]?key|private[_-]?key)"
    r"(\s*[:=]\s*)([^\s,;]+)",
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PEM = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def redact_text(value: str) -> str:
    """Best-effort defense in depth; runbooks must not intentionally print secrets."""

    value = _PEM.sub("[PRIVATE_KEY_REDACTED]", value)
    value = _BEARER.sub("Bearer [REDACTED]", value)
    value = _JWT.sub("[JWT_REDACTED]", value)
    return _ASSIGNMENT.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)


def scrub_payload(value: Any) -> Any:
    """Remove values under secret-like keys before they enter the audit chain."""

    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _SENSITIVE_KEY.search(str(key)) else scrub_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [scrub_payload(item) for item in value]
    if isinstance(value, tuple):
        return [scrub_payload(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def contains_sensitive_name(value: str) -> bool:
    return bool(_SENSITIVE_KEY.search(value))
