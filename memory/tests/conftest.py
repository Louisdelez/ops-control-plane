from __future__ import annotations

import math
import os
import pwd
import grp
from pathlib import Path
from typing import Any

import pytest

from ops_memory.config import (
    APIProviderConfig,
    ActorPolicy,
    EmbeddingConfig,
    MemoryConfig,
    QdrantConfig,
    RerankerConfig,
)
from ops_memory.errors import BackendUnavailableError
from ops_memory.qdrant import VectorHit
from ops_memory.service import MemoryService


class FakeQdrant:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.points: dict[str, tuple[dict[str, Any], tuple[float, ...]]] = {}
        self.ensure_calls = 0
        self.search_calls: list[dict[str, Any]] = []

    def _check(self) -> None:
        if not self.available:
            raise BackendUnavailableError("simulated Qdrant outage")

    def ensure_collection(self) -> None:
        self._check()
        self.ensure_calls += 1

    def health(self) -> bool:
        self._check()
        return True

    def upsert(self, record: dict[str, Any], vector: tuple[float, ...]) -> None:
        self._check()
        self.points[record["id"]] = (dict(record), vector)

    def delete(self, record_id: str) -> None:
        self._check()
        self.points.pop(record_id, None)

    def search(self, vector: tuple[float, ...], **filters: Any) -> list[VectorHit]:
        self._check()
        self.search_calls.append(filters)
        roles = set(filters["roles"])
        categories = set(filters["categories"])
        kinds = set(filters["kinds"])
        hits: list[VectorHit] = []
        for record_id, (record, stored) in self.points.items():
            if record["project"] != filters["project"] or record["environment"] != filters["environment"]:
                continue
            if not roles.intersection(record["allowed_roles"]):
                continue
            if categories and record["category"] not in categories:
                continue
            if kinds and record["kind"] not in kinds:
                continue
            score = sum(left * right for left, right in zip(vector, stored))
            hits.append(VectorHit(record_id, score))
        hits.sort(key=lambda item: -item.score)
        return hits[: filters["limit"]]


@pytest.fixture
def uid() -> int:
    return os.getuid()


@pytest.fixture
def memory_config(tmp_path: Path) -> MemoryConfig:
    username = pwd.getpwuid(os.getuid()).pw_name
    operator = ActorPolicy(
        name="operator",
        unix_users=(username,),
        roles=("minecraft-ops",),
        grantable_roles=("minecraft-ops", "monitoring-shared"),
        projects=("minecraft",),
        environments=("production", "staging"),
        max_classification="restricted",
        capabilities=frozenset({"read", "write", "summarize", "invalidate", "health", "stats", "maintenance", "review", "api-escalate"}),
        source_types=("git-docs", "monitoring", "zulip", "memory-consolidation"),
        authoritative_source_types=("git-docs", "monitoring"),
    )
    outsider = ActorPolicy(
        name="outsider",
        unix_users=(username,),
        roles=("project-b",),
        grantable_roles=("project-b",),
        projects=("project-b",),
        environments=("production",),
        max_classification="internal",
        capabilities=frozenset({"read", "write", "health"}),
        source_types=("git-docs",),
        authoritative_source_types=(),
    )
    return MemoryConfig(
        database_path=tmp_path / "state" / "memory.sqlite3",
        socket_path=tmp_path / "run" / "memory.sock",
        socket_group=grp.getgrgid(os.getgid()).gr_name,
        socket_mode=0o600,
        qdrant=QdrantConfig(),
        embedding=EmbeddingConfig(dimensions=64, api=APIProviderConfig()),
        reranker=RerankerConfig(api=APIProviderConfig()),
        actors={"operator": operator, "outsider": outsider},
        source_truth={"current_state": ("monitoring",), "documentation": ("git-docs",)},
        candidate_limit=20,
        result_limit=5,
        fallback_scan_limit=100,
        stale_after_days=365,
    )


@pytest.fixture
def qdrant() -> FakeQdrant:
    return FakeQdrant()


@pytest.fixture
def service(memory_config: MemoryConfig, qdrant: FakeQdrant) -> MemoryService:
    return MemoryService(memory_config, qdrant=qdrant)  # type: ignore[arg-type]


def memory_payload(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "actor": "operator",
        "content": "Le service Minecraft survival utilise la version 1.21.4.",
        "project": "minecraft",
        "environment": "production",
        "classification": "internal",
        "category": "documentation",
        "kind": "information",
        "tier": "cold",
        "allowed_roles": ["minecraft-ops"],
        "source_type": "git-docs",
        "source_uri": "git://ops/docs/minecraft.md",
        "source_ref": "commit:abc123",
        "source_authority": "ops-repository",
        "metadata": {"server": "minecraft-survival-02", "version": "1.21.4"},
        "importance": 0.8,
        "reviewed": True,
        "promote": True,
    }
    value.update(overrides)
    return value
