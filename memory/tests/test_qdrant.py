from __future__ import annotations

import pytest

from ops_memory.config import QdrantConfig
from ops_memory.errors import BackendUnavailableError
from ops_memory.qdrant import QdrantClient


def test_qdrant_search_has_deterministic_security_filters() -> None:
    client = QdrantClient(QdrantConfig(), 3)
    calls: list[tuple[str, str, object]] = []

    def request(method: str, path: str, payload=None):
        calls.append((method, path, payload))
        return {"result": [{"id": "00000000-0000-0000-0000-000000000001", "score": 0.9}]}

    client._request = request  # type: ignore[method-assign]
    hits = client.search(
        (1.0, 0.0, 0.0),
        project="minecraft",
        environment="production",
        max_classification="restricted",
        roles=("minecraft-ops",),
        categories=("incident",),
        kinds=("incident",),
        limit=20,
    )
    assert hits[0].score == 0.9
    payload = calls[0][2]
    must = payload["filter"]["must"]
    assert {"key": "project", "match": {"value": "minecraft"}} in must
    assert {"key": "environment", "match": {"value": "production"}} in must
    assert {"key": "allowed_roles", "match": {"any": ["minecraft-ops"]}} in must
    assert {"key": "classification_rank", "range": {"lte": 2}} in must


def test_collection_setup_creates_filter_indexes_once() -> None:
    client = QdrantClient(QdrantConfig(), 64)
    calls: list[tuple[str, str, object]] = []

    def request(method: str, path: str, payload=None):
        calls.append((method, path, payload))
        if method == "GET":
            return {"result": {"config": {"params": {"vectors": {"size": 64}}}}}
        return {"result": True}

    client._request = request  # type: ignore[method-assign]
    client.ensure_collection()
    client.ensure_collection()
    assert len([call for call in calls if call[0] == "GET"]) == 1
    indexes = [call[2]["field_name"] for call in calls if call[1].endswith("/index?wait=true")]
    assert indexes == [
        "project",
        "environment",
        "classification_rank",
        "allowed_roles",
        "category",
        "kind",
        "valid",
    ]


def test_qdrant_upsert_contains_vector_content_and_metadata() -> None:
    client = QdrantClient(QdrantConfig(), 3)
    calls = []
    client._request = lambda method, path, payload=None: calls.append((method, path, payload)) or {}  # type: ignore[method-assign]
    client.upsert(
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "project": "minecraft",
            "environment": "production",
            "classification": "internal",
            "classification_rank": 1,
            "category": "rule",
            "kind": "rule",
            "allowed_roles": ["minecraft-ops"],
            "content": "Mémoire non secrète",
            "source_type": "git-docs",
            "source_uri": "git://ops/docs/runbook.md",
            "source_ref": "commit:abc",
            "source_authority": "ops-repository",
            "authority_status": "authoritative",
            "metadata": {"service": "minecraft"},
            "importance": 0.8,
            "tier": "cold",
            "valid_from": "2026-09-04T00:00:00+00:00",
            "valid_until": None,
        },
        (1.0, 0.0, 0.0),
    )
    qdrant_payload = calls[0][2]["points"][0]["payload"]
    assert qdrant_payload["content"] == "Mémoire non secrète"
    assert qdrant_payload["metadata"] == {"service": "minecraft"}
    assert qdrant_payload["valid"] is True


def test_qdrant_key_is_loaded_from_ephemeral_systemd_credential(
    tmp_path, monkeypatch
) -> None:
    credential = tmp_path / "qdrant-api-key"
    credential.write_text("a" * 48 + "\n", encoding="ascii")
    credential.chmod(0o600)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    client = QdrantClient(
        QdrantConfig(api_key_credential="qdrant-api-key"), dimensions=64
    )
    assert client._api_key() == "a" * 48


def test_qdrant_key_accepts_systemd_non_root_credential_mode(
    tmp_path, monkeypatch
) -> None:
    credential = tmp_path / "qdrant-api-key"
    credential.write_text("a" * 48, encoding="ascii")
    credential.chmod(0o440)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    client = QdrantClient(
        QdrantConfig(api_key_credential="qdrant-api-key"), dimensions=64
    )
    assert client._api_key() == "a" * 48


def test_qdrant_key_rejects_world_accessible_credential(tmp_path, monkeypatch) -> None:
    credential = tmp_path / "qdrant-api-key"
    credential.write_text("a" * 48, encoding="ascii")
    credential.chmod(0o604)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    client = QdrantClient(
        QdrantConfig(api_key_credential="qdrant-api-key"), dimensions=64
    )
    with pytest.raises(BackendUnavailableError, match="world-accessible"):
        client._api_key()
