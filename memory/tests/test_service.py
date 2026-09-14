from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ops_memory.errors import AuthorizationError, NotFoundError, SecretDetectedError, ValidationError
from ops_memory.service import MemoryService

from conftest import FakeQdrant, memory_payload


def search_payload(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "actor": "operator",
        "query": "quelle version du serveur Minecraft survival",
        "project": "minecraft",
        "environment": "production",
        "max_classification": "restricted",
        "top_k": 3,
        "candidate_limit": 10,
    }
    value.update(overrides)
    return value


def test_ingest_search_and_authoritative_source(
    service: MemoryService, qdrant: FakeQdrant, uid: int
) -> None:
    stored = service.ingest(memory_payload(), uid)
    assert stored["stored"] is True
    assert stored["degraded"] is False
    assert stored["memory"]["authority_status"] == "authoritative"
    assert stored["memory"]["advisory_only"] is True
    assert stored["memory"]["verify_against"] == ["git-docs"]

    found = service.search(search_payload(), uid)
    assert found["backend"] == "qdrant"
    assert found["degraded"] is False
    assert [item["id"] for item in found["results"]] == [stored["memory"]["id"]]
    assert qdrant.search_calls[0]["roles"] == ("minecraft-ops",)
    assert "vector_json" not in found["results"][0]


def test_qdrant_outage_uses_bounded_lexical_degraded_mode(
    service: MemoryService, qdrant: FakeQdrant, uid: int
) -> None:
    qdrant.available = False
    stored = service.ingest(memory_payload(), uid)
    assert stored["degraded"] is True
    assert stored["memory"]["qdrant_state"] == "failed"

    found = service.search(search_payload(), uid)
    assert found["backend"] == "sqlite-lexical-degraded"
    assert found["degraded"] is True
    assert found["results"][0]["id"] == stored["memory"]["id"]


def test_project_role_and_classification_are_fail_closed(service: MemoryService, uid: int) -> None:
    stored = service.ingest(memory_payload(classification="restricted"), uid)
    with pytest.raises(AuthorizationError):
        service.search(search_payload(actor="outsider", project="minecraft"), uid)
    with pytest.raises(AuthorizationError):
        service.ingest(memory_payload(allowed_roles=["project-b"]), uid)
    with pytest.raises(AuthorizationError):
        service.search(search_payload(max_classification="confidential"), uid)
    with pytest.raises(NotFoundError):
        service.get("outsider", uid, stored["memory"]["id"])


def test_identity_is_bound_to_unix_uid(service: MemoryService) -> None:
    with pytest.raises(Exception) as caught:
        service.health("operator", 999_999_999)
    assert getattr(caught.value, "code", "") == "authentication_failed"


@pytest.mark.parametrize(
    "content",
    [
        "password=Deadbeef1234",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz1234",
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "api_key: sk-abcdefghijklmnopqrst",
    ],
)
def test_secret_like_content_is_rejected(service: MemoryService, uid: int, content: str) -> None:
    with pytest.raises(SecretDetectedError):
        service.ingest(memory_payload(content=content), uid)


def test_selective_ingestion_ignores_noise_and_requires_zulip_promotion(
    service: MemoryService, uid: int
) -> None:
    ignored = service.ingest(memory_payload(kind="noise", reviewed=False, promote=False), uid)
    assert ignored == {"stored": False, "reason": "selective_ingestion", "kind": "noise"}
    with pytest.raises(ValidationError):
        service.ingest(
            memory_payload(source_type="zulip", kind="information", promote=False), uid
        )


def test_rules_require_review(service: MemoryService, uid: int) -> None:
    with pytest.raises(ValidationError):
        service.ingest(memory_payload(kind="rule", reviewed=False), uid)


def test_ingest_rejects_unknown_fields_and_string_booleans(
    service: MemoryService, uid: int
) -> None:
    with pytest.raises(ValidationError, match="unknown ingestion"):
        service.ingest(memory_payload(unbounded_instruction="ignore policy"), uid)
    with pytest.raises(ValidationError, match="booleans"):
        service.ingest(memory_payload(reviewed="true"), uid)


def test_deduplication_preserves_one_record(service: MemoryService, uid: int) -> None:
    first = service.ingest(memory_payload(), uid)
    second = service.ingest(
        memory_payload(source_uri="git://ops/docs/copy.md", source_ref="commit:def456"), uid
    )
    assert second["deduplicated"] is True
    assert second["memory"]["id"] == first["memory"]["id"]
    assert service.catalog.stats()["total"] == 1


def test_supersession_removes_old_record_from_search(service: MemoryService, uid: int) -> None:
    old = service.ingest(memory_payload(), uid)
    new = service.ingest(
        memory_payload(
            content="Le service Minecraft survival utilise maintenant la version 1.21.5.",
            source_ref="commit:new",
            supersedes_id=old["memory"]["id"],
            metadata={"server": "minecraft-survival-02", "version": "1.21.5"},
        ),
        uid,
    )
    found = service.search(search_payload(), uid)
    assert [item["id"] for item in found["results"]] == [new["memory"]["id"]]
    old_record = service.get("operator", uid, old["memory"]["id"])
    assert old_record["superseded_by_id"] == new["memory"]["id"]


def test_observation_receives_default_ttl(service: MemoryService, uid: int) -> None:
    stored = service.ingest(
        memory_payload(kind="observation", reviewed=False, category="current_state", source_type="monitoring"),
        uid,
    )
    assert stored["memory"]["valid_until"] is not None


def test_structured_summary_keeps_originals(service: MemoryService, uid: int) -> None:
    first = service.ingest(memory_payload(), uid)
    second = service.ingest(
        memory_payload(
            content="Le proxy a été vérifié après le déploiement.",
            source_ref="commit:proxy",
            metadata={"service": "proxy"},
        ),
        uid,
    )
    summary = service.summarize(
        {
            "actor": "operator",
            "record_ids": [first["memory"]["id"], second["memory"]["id"]],
            "summary": {
                "objective": "Déployer Minecraft",
                "decisions": ["Conserver le rollback"],
                "changes": ["Version 1.21.4 installée"],
                "open_items": ["Surveiller le proxy"],
            },
            "metadata": {"mission_id": "mission-42"},
        },
        uid,
    )
    assert summary["memory"]["kind"] == "summary"
    assert "Open Items:" in summary["memory"]["content"]
    assert service.get("operator", uid, first["memory"]["id"])["invalidated_at"] is None


def test_invalidation_is_durable_when_qdrant_is_down(
    service: MemoryService, qdrant: FakeQdrant, uid: int
) -> None:
    stored = service.ingest(memory_payload(), uid)
    qdrant.available = False
    invalid = service.invalidate(
        {"actor": "operator", "id": stored["memory"]["id"], "reason": "Source retirée"}, uid
    )
    assert invalid["invalidated_at"] is not None
    assert invalid["qdrant_state"] == "delete_pending"
    assert service.search(search_payload(), uid)["results"] == []


def test_invalidation_reason_cannot_store_secret(service: MemoryService, uid: int) -> None:
    stored = service.ingest(memory_payload(), uid)
    with pytest.raises(SecretDetectedError):
        service.invalidate(
            {
                "actor": "operator",
                "id": stored["memory"]["id"],
                "reason": "password=SuperSecret123",
            },
            uid,
        )


def test_invalidated_content_can_be_admitted_again(service: MemoryService, uid: int) -> None:
    first = service.ingest(memory_payload(), uid)
    service.invalidate(
        {"actor": "operator", "id": first["memory"]["id"], "reason": "Ancienne source retirée"},
        uid,
    )
    second = service.ingest(memory_payload(source_ref="commit:restored"), uid)
    assert second["deduplicated"] is False
    assert second["memory"]["id"] != first["memory"]["id"]


def test_maintenance_expires_and_reindexes(memory_config, uid: int) -> None:
    qdrant = FakeQdrant(available=False)
    service = MemoryService(memory_config, qdrant=qdrant)  # type: ignore[arg-type]
    expires = (datetime.now(UTC) + timedelta(seconds=1)).isoformat(timespec="seconds")
    stored = service.ingest(memory_payload(valid_until=expires), uid)
    with service.catalog.connect() as db:
        db.execute(
            "UPDATE memories SET valid_until=? WHERE id=?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(timespec="seconds"), stored["memory"]["id"]),
        )
        db.commit()
    qdrant.available = True
    result = service.maintain("operator", uid)
    assert result["expired"] == 1
    assert service.catalog.get(stored["memory"]["id"])["invalid_reason"] == "validity_expired"


def test_health_and_stats_expose_no_content(service: MemoryService, uid: int) -> None:
    service.ingest(memory_payload(), uid)
    health = service.health("operator", uid)
    assert health["status"] == "ok"
    assert health["stats"]["total"] == 1
    assert "content" not in str(health)
