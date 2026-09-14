from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ops_memory.errors import AuthorizationError, NotFoundError, SecretDetectedError, ValidationError
from ops_memory.service import MemoryService

from conftest import FakeQdrant, memory_payload
from test_service import search_payload


def test_qdrant_results_are_post_filtered_by_requested_classification(
    service: MemoryService, uid: int
) -> None:
    service.ingest(memory_payload(classification="restricted"), uid)
    # FakeQdrant intentionally does not implement a classification filter. This
    # proves the independent application-side filter remains effective.
    result = service.search(search_payload(max_classification="internal"), uid)
    assert result["results"] == []


def test_writer_does_not_receive_memory_outside_own_read_roles(
    service: MemoryService, uid: int
) -> None:
    result = service.ingest(memory_payload(allowed_roles=["monitoring-shared"]), uid)
    assert "content" not in result["memory"]
    assert result["memory"]["allowed_roles"] == ["monitoring-shared"]
    assert service.search(search_payload(), uid)["results"] == []


def test_actor_cannot_claim_an_unapproved_source_type(service: MemoryService, uid: int) -> None:
    with pytest.raises(AuthorizationError, match="source_type"):
        service.ingest(memory_payload(source_type="openbao"), uid)


def test_supersession_cannot_widen_audience(service: MemoryService, uid: int) -> None:
    old = service.ingest(memory_payload(), uid)
    with pytest.raises(AuthorizationError, match="widen"):
        service.ingest(
            memory_payload(
                content="Version révisée",
                supersedes_id=old["memory"]["id"],
                allowed_roles=["minecraft-ops", "monitoring-shared"],
                source_ref="commit:new",
            ),
            uid,
        )


def test_outsider_cannot_supersede_hidden_memory(service: MemoryService, uid: int) -> None:
    old = service.ingest(memory_payload(), uid)
    with pytest.raises(NotFoundError):
        service.ingest(
            memory_payload(
                actor="outsider",
                project="project-b",
                classification="internal",
                allowed_roles=["project-b"],
                content="Tentative de remplacement",
                supersedes_id=old["memory"]["id"],
                reviewed=False,
            ),
            uid,
        )


def test_actor_without_api_capability_cannot_spend_external_budget(
    service: MemoryService, uid: int
) -> None:
    with pytest.raises(AuthorizationError, match="external provider"):
        service.search(
            {
                **search_payload(),
                "actor": "outsider",
                "project": "project-b",
                "max_classification": "internal",
                "allow_api": True,
            },
            uid,
        )


@pytest.mark.parametrize(
    "source_uri",
    [
        "https://user:password@example.test/document",
        "https://example.test/document?token=abcdef",
        "relative/path",
    ],
)
def test_unsafe_source_uri_is_rejected(
    service: MemoryService, uid: int, source_uri: str
) -> None:
    with pytest.raises(ValidationError):
        service.ingest(memory_payload(source_uri=source_uri), uid)


def test_restart_preserves_catalog_and_degraded_retrieval(
    memory_config, uid: int
) -> None:
    unavailable = FakeQdrant(available=False)
    first_service = MemoryService(memory_config, qdrant=unavailable)  # type: ignore[arg-type]
    stored = first_service.ingest(memory_payload(), uid)

    second_service = MemoryService(memory_config, qdrant=FakeQdrant(available=False))  # type: ignore[arg-type]
    found = second_service.search(search_payload(), uid)
    assert found["results"][0]["id"] == stored["memory"]["id"]


def test_maintenance_reconciles_pending_qdrant_record(memory_config, uid: int) -> None:
    qdrant = FakeQdrant(available=False)
    service = MemoryService(memory_config, qdrant=qdrant)  # type: ignore[arg-type]
    stored = service.ingest(memory_payload(), uid)
    assert service.catalog.get(stored["memory"]["id"])["qdrant_state"] == "failed"

    qdrant.available = True
    result = service.maintain("operator", uid)
    assert result["reindexed"] == 1
    assert result["failures"] == 0
    assert service.catalog.get(stored["memory"]["id"])["qdrant_state"] == "indexed"


def test_maintenance_marks_old_context_stale(memory_config, uid: int) -> None:
    service = MemoryService(memory_config, qdrant=FakeQdrant())  # type: ignore[arg-type]
    stored = service.ingest(memory_payload(kind="incident"), uid)
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat(timespec="seconds")
    with service.catalog.connect() as db:
        db.execute("UPDATE memories SET updated_at=? WHERE id=?", (old, stored["memory"]["id"]))
        db.commit()
    result = service.maintain("operator", uid)
    assert result["marked_stale"] == 1
    assert service.catalog.get(stored["memory"]["id"])["stale"] is True
