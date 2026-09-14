from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from .config import CLASSIFICATIONS, MEMORY_KINDS, TIERS
from .errors import SecretDetectedError, ValidationError


ALLOWED_METADATA_KEYS = frozenset(
    {
        "server",
        "service",
        "version",
        "severity",
        "agent",
        "status",
        "incident_id",
        "mission_id",
        "deployment_id",
        "owner",
        "tags",
    }
)
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(r"\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[:=]\s*[^\s,;]{8,}", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*", re.IGNORECASE),
    re.compile(r"\b(?:sk|ghp|glpat)-[A-Za-z0-9_-]{16,}\b", re.IGNORECASE),
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def parse_timestamp(value: object, name: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{name} must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _required_string(data: dict[str, Any], name: str, *, maximum: int = 256) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{name} must be a non-empty string")
    value = value.strip()
    if len(value) > maximum or "\x00" in value:
        raise ValidationError(f"{name} is invalid or too long")
    return value


def check_for_secrets(content: str, metadata: dict[str, Any]) -> None:
    scan = content + "\n" + json.dumps(metadata, sort_keys=True, ensure_ascii=False)
    if any(pattern.search(scan) for pattern in SECRET_PATTERNS):
        raise SecretDetectedError("content resembles a secret; store secrets in OpenBao")


def validate_source_uri(value: str) -> str:
    parsed = urlparse(value)
    if not parsed.scheme or any(character.isspace() for character in value):
        raise ValidationError("source_uri must be an absolute URI without whitespace")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValidationError("source_uri must not contain credentials, query string or fragment")
    return value


def validate_metadata(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValidationError("metadata must be an object")
    unknown = set(value) - ALLOWED_METADATA_KEYS
    if unknown:
        raise ValidationError(f"unsupported metadata keys: {sorted(unknown)}")
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > 16_384:
        raise ValidationError("metadata is too large")
    for key, item in value.items():
        if isinstance(item, (dict, float)) or item is None:
            raise ValidationError(f"metadata.{key} has an unsupported value")
        if isinstance(item, list):
            if key != "tags" or not all(isinstance(part, str) and 0 < len(part) <= 64 for part in item):
                raise ValidationError(f"metadata.{key} must be a list of short strings")
        elif not isinstance(item, (str, int, bool)):
            raise ValidationError(f"metadata.{key} has an unsupported value")
    return value


@dataclass(frozen=True)
class IngestRequest:
    actor: str
    content: str
    project: str
    environment: str
    classification: str
    category: str
    kind: str
    tier: str
    allowed_roles: tuple[str, ...]
    source_type: str
    source_uri: str
    source_ref: str
    source_authority: str
    metadata: dict[str, Any]
    importance: float
    valid_from: str
    valid_until: str | None
    supersedes_id: str | None
    reviewed: bool
    promote: bool

    @classmethod
    def from_dict(cls, data: object, *, max_content_bytes: int) -> "IngestRequest":
        if not isinstance(data, dict):
            raise ValidationError("request must be an object")
        allowed_fields = {
            "actor", "content", "project", "environment", "classification",
            "category", "kind", "tier", "allowed_roles", "source_type",
            "source_uri", "source_ref", "source_authority", "metadata",
            "importance", "valid_from", "valid_until", "supersedes_id",
            "reviewed", "promote",
        }
        unknown = set(data) - allowed_fields
        if unknown:
            raise ValidationError(f"unknown ingestion fields: {sorted(unknown)}")
        content = _required_string(data, "content", maximum=max_content_bytes)
        if len(content.encode("utf-8")) > max_content_bytes:
            raise ValidationError("content exceeds max_content_bytes")
        classification = _required_string(data, "classification", maximum=32)
        if classification not in CLASSIFICATIONS:
            raise ValidationError("unknown classification")
        kind = _required_string(data, "kind", maximum=32)
        if kind not in MEMORY_KINDS:
            raise ValidationError("unknown memory kind")
        tier = str(data.get("tier", "cold"))
        if tier not in TIERS:
            raise ValidationError("unknown memory tier")
        roles = data.get("allowed_roles")
        if (
            not isinstance(roles, list)
            or not 1 <= len(roles) <= 32
            or not all(isinstance(role, str) and 0 < len(role) <= 128 for role in roles)
        ):
            raise ValidationError("allowed_roles must be a non-empty string list")
        if len(roles) != len(set(roles)) or "*" in roles:
            raise ValidationError("allowed_roles contains duplicates or wildcard")
        metadata = validate_metadata(data.get("metadata"))
        source_type = _required_string(data, "source_type", maximum=64)
        source_uri = validate_source_uri(_required_string(data, "source_uri", maximum=2048))
        source_ref = _required_string(data, "source_ref", maximum=512)
        source_authority = _required_string(data, "source_authority", maximum=128)
        check_for_secrets(
            "\n".join((content, source_type, source_uri, source_ref, source_authority)), metadata
        )
        importance = data.get("importance", 0.5)
        if isinstance(importance, bool) or not isinstance(importance, (int, float)) or not 0 <= importance <= 1:
            raise ValidationError("importance must be between 0 and 1")
        supersedes_id = data.get("supersedes_id")
        if supersedes_id is not None:
            try:
                supersedes_id = str(uuid.UUID(str(supersedes_id)))
            except ValueError as exc:
                raise ValidationError("supersedes_id must be a UUID") from exc
        reviewed = data.get("reviewed", False)
        promote = data.get("promote", False)
        if not isinstance(reviewed, bool) or not isinstance(promote, bool):
            raise ValidationError("reviewed and promote must be booleans")
        now = utc_now()
        return cls(
            actor=_required_string(data, "actor", maximum=128),
            content=content,
            project=_required_string(data, "project", maximum=128),
            environment=_required_string(data, "environment", maximum=64),
            classification=classification,
            category=_required_string(data, "category", maximum=128),
            kind=kind,
            tier=tier,
            allowed_roles=tuple(roles),
            source_type=source_type,
            source_uri=source_uri,
            source_ref=source_ref,
            source_authority=source_authority,
            metadata=metadata,
            importance=float(importance),
            valid_from=parse_timestamp(data.get("valid_from", now), "valid_from") or now,
            valid_until=parse_timestamp(data.get("valid_until"), "valid_until", optional=True),
            supersedes_id=supersedes_id,
            reviewed=reviewed,
            promote=promote,
        )


@dataclass(frozen=True)
class SearchRequest:
    actor: str
    query: str
    project: str
    environment: str
    max_classification: str
    categories: tuple[str, ...] = field(default_factory=tuple)
    kinds: tuple[str, ...] = field(default_factory=tuple)
    top_k: int = 5
    candidate_limit: int = 20
    important: bool = False
    allow_api: bool = False

    @classmethod
    def from_dict(cls, data: object, *, result_limit: int, candidate_limit: int) -> "SearchRequest":
        if not isinstance(data, dict):
            raise ValidationError("request must be an object")
        allowed_fields = {
            "actor", "query", "project", "environment", "max_classification",
            "categories", "kinds", "top_k", "candidate_limit", "important",
            "allow_api",
        }
        unknown = set(data) - allowed_fields
        if unknown:
            raise ValidationError(f"unknown search fields: {sorted(unknown)}")
        classification = _required_string(data, "max_classification", maximum=32)
        if classification not in CLASSIFICATIONS:
            raise ValidationError("unknown classification")
        query = _required_string(data, "query", maximum=16_384)
        check_for_secrets(query, {})
        categories = data.get("categories", [])
        kinds = data.get("kinds", [])
        if not isinstance(categories, list) or not all(isinstance(item, str) and item for item in categories):
            raise ValidationError("categories must be a string list")
        if not isinstance(kinds, list) or not all(item in MEMORY_KINDS for item in kinds):
            raise ValidationError("kinds contains an invalid memory kind")
        if len(categories) > 32 or len(categories) != len(set(categories)):
            raise ValidationError("categories is too large or contains duplicates")
        if len(kinds) > 16 or len(kinds) != len(set(kinds)):
            raise ValidationError("kinds is too large or contains duplicates")
        top_k = data.get("top_k", result_limit)
        candidates = data.get("candidate_limit", candidate_limit)
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= result_limit:
            raise ValidationError(f"top_k must be between 1 and {result_limit}")
        if isinstance(candidates, bool) or not isinstance(candidates, int) or not 10 <= candidates <= candidate_limit:
            raise ValidationError(f"candidate_limit must be between 10 and {candidate_limit}")
        important = data.get("important", False)
        allow_api = data.get("allow_api", False)
        if not isinstance(important, bool) or not isinstance(allow_api, bool):
            raise ValidationError("important and allow_api must be booleans")
        return cls(
            actor=_required_string(data, "actor", maximum=128),
            query=query,
            project=_required_string(data, "project", maximum=128),
            environment=_required_string(data, "environment", maximum=64),
            max_classification=classification,
            categories=tuple(categories),
            kinds=tuple(kinds),
            top_k=top_k,
            candidate_limit=candidates,
            important=important,
            allow_api=allow_api,
        )
