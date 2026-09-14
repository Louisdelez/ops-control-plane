from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ops_memory.storage import MemoryCatalog


def test_provider_budget_reservations_are_atomic(tmp_path: Path) -> None:
    catalog = MemoryCatalog(tmp_path / "memory.sqlite3")

    def reserve(index: int) -> int | None:
        return catalog.reserve_provider_call(
            "operator", f"search-{index}", "reranker-api", "test-model", 2
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        reservations = list(executor.map(reserve, range(8)))

    active = [call_id for call_id in reservations if call_id is not None]
    assert len(active) == 2
    for call_id in active:
        catalog.complete_provider_call(call_id, "success")

    with catalog.connect() as db:
        outcomes = {
            row["outcome"]: row["count"]
            for row in db.execute(
                "SELECT outcome, COUNT(*) count FROM provider_calls GROUP BY outcome"
            )
        }
    assert outcomes == {"budget_denied": 6, "success": 2}


def test_zero_provider_budget_fails_closed(tmp_path: Path) -> None:
    catalog = MemoryCatalog(tmp_path / "memory.sqlite3")
    assert catalog.reserve_provider_call(
        "operator", "search", "reranker-api", "test-model", 0
    ) is None
