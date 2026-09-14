from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from ops_orchestrator.catalogue import ModelCatalogue
from ops_orchestrator import cli
from ops_orchestrator.errors import ConfigurationError, ValidationError


ROOT = Path(__file__).parents[2]
CATALOGUE = ROOT / "catalog" / "model-catalog.v2.json"
SOURCE = Path("/home/ops-user/Téléchargements/catalogue_modeles_IA_API_2026.txt")
CONFIGURED = {
    "qwen-utility-api",
    "alibaba-deepseek-ops-api",
    "qwen-ops-api",
    "deepseek-ops-api",
    "deepseek-reasoning",
    "qwen-reasoning-api",
    "deepseek-coder-api",
}


def _load() -> ModelCatalogue:
    return ModelCatalogue.load(CATALOGUE, configured_deployment_ids=CONFIGURED)


def _write_catalogue(tmp_path: Path, mutate) -> Path:
    document = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    mutate(document)
    target = tmp_path / "catalogue.json"
    target.write_text(json.dumps(document), encoding="utf-8")
    return target


def test_supplied_catalogue_is_imported_exhaustively_and_normalized() -> None:
    catalogue = _load()
    assert len(catalogue.cards) == 58
    assert len(catalogue.accounts) == 21
    assert sum(bool(card["pricing"]) for card in catalogue.cards) == 48
    assert sum(card["integration_stage"] == "quarantined" for card in catalogue.cards) == 11
    assert sum(card["integration_stage"] == "configured" for card in catalogue.cards) == 5
    assert sum(card["integration_stage"] == "catalogued" for card in catalogue.cards) == 42
    assert sum(len(card["deployment_ids"]) for card in catalogue.cards) == 7

    by_id = {card["card_id"]: card for card in catalogue.cards}
    assert set(by_id["deepseek-v4-flash"]["source_lines"]) == {59, 119}
    assert {price["mode"] for price in by_id["deepseek-v4-flash"]["pricing"]} == {
        "off_peak",
        "peak",
    }
    assert set(by_id["deepseek-v4-pro"]["source_lines"]) == {144, 205}
    assert "cohere-aya-expanse-8b" in by_id
    assert "cohere-aya-expanse-32b" in by_id
    assert by_id["cohere-aya-expanse-8b"]["source_lines"] == [124]
    assert by_id["cohere-aya-expanse-32b"]["source_lines"] == [124]

    assert by_id["deepseek-v4-flash"]["context_window_tokens"] == 1_000_000
    assert by_id["gemini-3-1-flash-lite"]["context_window_tokens"] == 1_000_000
    assert by_id["grok-4-20"]["context_window_tokens"] == 1_000_000
    assert by_id["gpt-5-4-mini"]["context_window_tokens"] == 400_000
    assert by_id["gpt-5-3-codex"]["context_window_tokens"] == 400_000
    assert by_id["gpt-5-4"]["context_window_tokens"] == 1_050_000
    assert by_id["gpt-5-4-pro"]["context_window_tokens"] == 1_050_000
    assert by_id["kimi-k3"]["context_window_tokens"] == 1_000_000
    assert sum(card["context_window_tokens"] is not None for card in catalogue.cards) == 8
    expected_official_cache_prices = {
        "glm-4-7-flashx": "0.01",
        "minimax-m3": "0.06",
        "glm-4-7": "0.11",
        "glm-5": "0.20",
        "gpt-5-4-mini": "0.075",
        "glm-5-1": "0.26",
        "gpt-5-3-codex": "0.175",
        "gpt-5-4": "0.25",
        "kimi-k3": "0.30",
        "gpt-5-4-pro": None,
    }
    for card_id, expected_cache_price in expected_official_cache_prices.items():
        assert len(by_id[card_id]["pricing"]) == 1
        official_price = by_id[card_id]["pricing"][0]
        assert official_price["mode"] == "standard"
        assert official_price["confidence"] == "official_verified"
        assert official_price["cached_input_usd_per_million"] == expected_cache_price
    assert by_id["cohere-command-a"]["languages"] == [
        "Multilingue (langues non détaillées)"
    ]
    assert by_id["baidu-ernie-family"]["languages"] == ["Chinois"]
    assert all(card["latency_p50_ms"] is None for card in catalogue.cards)
    assert all(card["latency_p95_ms"] is None for card in catalogue.cards)
    assert all(
        card["technical_metadata_source"] == "catalogue_modeles_IA_API_2026.txt"
        or card["technical_metadata_source"].startswith("https://")
        for card in catalogue.cards
    )
    assert all(
        price["source"] == "catalogue_modeles_IA_API_2026.txt"
        or price["source"].startswith("https://")
        for card in catalogue.cards
        for price in card["pricing"]
    )


def test_catalogue_source_digest_matches_supplied_file_when_present() -> None:
    if not SOURCE.exists():
        pytest.skip("original user catalogue is not present on this machine")
    document = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == document["source"]["sha256"]
    assert len(SOURCE.read_bytes().splitlines()) == 324


def test_public_page_resolves_capabilities_costs_and_runtime_without_secrets() -> None:
    catalogue = _load()
    page = catalogue.page(
        offset=0,
        limit=2,
        runtime_deployments={
            "qwen-utility-api": {
                "deployment_id": "qwen-utility-api",
                "provider_account_id": "alibaba",
                "role": "ROLE_TINY",
                "model": "qwen3.7-flash-2026-07-15",
                "available": True,
                "reason": "configured secret is readable",
                "comparison_cost_microusd": 500_000,
            }
        },
    )
    assert page["pagination"] == {
        "offset": 0,
        "limit": 2,
        "returned": 2,
        "total": 58,
        "next_offset": 2,
    }
    first = page["cards"][0]
    assert first["card_id"] == "qwen-3-7-flash"
    assert first["capabilities"]["speed"] == 10
    assert first["simulations"] == [
        {
            "mode": "standard",
            "cost_microusd": 500_000,
            "basis": "computed_from_rates",
        }
    ]
    assert first["runtime"]["available"] is True
    alibaba = next(
        account
        for account in page["provider_accounts"]
        if account["id"] == "alibaba"
    )
    assert alibaba["runtime"]["credential_state"] == "resolved_by_runtime"
    serialized = json.dumps(page, ensure_ascii=False).lower()
    assert "api_key" not in serialized
    assert "secret_value" not in serialized


def test_capability_preview_filters_hard_then_orders_by_conservative_cost() -> None:
    catalogue = _load()
    result = catalogue.preview(
        {
            "project_id": "minecraft",
            "requirements": {"code": 8, "agentic": 8},
            "required_specialties": ["code"],
            "runtime_only": False,
            "limit": 5,
        }
    )
    assert result["policy"] == "hard-capabilities-then-lowest-conservative-cost"
    assert result["advisory_only"] is True
    costs = [
        card["routing_preview"]["conservative_monthly_cost_microusd"]
        for card in result["candidates"]
    ]
    assert costs == sorted(costs)
    assert result["candidates"][0]["card_id"] == "qwen3-coder-next"
    assert all(card["capabilities"]["code"] >= 8 for card in result["candidates"])


def test_runtime_preview_never_treats_catalogued_card_as_active() -> None:
    catalogue = _load()
    runtime = {
        "qwen-utility-api": {
            "deployment_id": "qwen-utility-api",
            "provider_account_id": "alibaba",
            "role": "ROLE_TINY",
            "model": "qwen3.7-flash-2026-07-15",
            "available": True,
            "reason": "configured secret is readable",
            "comparison_cost_microusd": 500_000,
        }
    }
    result = catalogue.preview(
        {
            "project_id": "minecraft",
            "requirements": {"general": 4},
            "runtime_only": True,
            "limit": 10,
        },
        runtime_deployments=runtime,
    )
    assert [item["card_id"] for item in result["candidates"]] == ["qwen-3-7-flash"]
    assert result["excluded"]["no_available_reviewed_deployment"] > 0


def test_runtime_preview_selects_the_cheapest_available_host_account() -> None:
    catalogue = _load()
    runtime = {
        "alibaba-deepseek-ops-api": {
            "deployment_id": "alibaba-deepseek-ops-api",
            "provider_account_id": "alibaba",
            "role": "ROLE_LOCAL_OPS",
            "model": "deepseek-v4-flash",
            "available": True,
            "reason": "configured secret is readable",
            "comparison_cost_microusd": 1_930_000,
        },
        "deepseek-ops-api": {
            "deployment_id": "deepseek-ops-api",
            "provider_account_id": "deepseek",
            "role": "ROLE_LOCAL_OPS",
            "model": "deepseek-v4-flash",
            "available": True,
            "reason": "configured secret is readable",
            "comparison_cost_microusd": 7_040_000,
        },
    }
    result = catalogue.preview(
        {
            "project_id": "minecraft",
            "requirements": {"reasoning": 7},
            "runtime_only": True,
            "limit": 10,
        },
        runtime_deployments=runtime,
    )
    candidate = next(
        item for item in result["candidates"] if item["card_id"] == "deepseek-v4-flash"
    )
    preview = candidate["routing_preview"]
    assert preview["cost_basis"] == "runtime_deployment"
    assert preview["selected_deployment_id"] == "alibaba-deepseek-ops-api"
    assert preview["selected_provider_account_id"] == "alibaba"
    assert preview["conservative_monthly_cost_microusd"] == 1_930_000


@pytest.mark.parametrize(
    "payload",
    [
        {"project_id": "minecraft", "requirements": {"invented": 4}},
        {"project_id": "minecraft", "requirements": {"code": 11}},
        {"project_id": "minecraft", "runtime_only": "yes"},
        {"project_id": "minecraft", "limit": 51},
        {"project_id": "minecraft", "unknown": True},
    ],
)
def test_preview_rejects_unbounded_or_unknown_inputs(payload) -> None:
    with pytest.raises(ValidationError):
        _load().preview(payload)


def test_catalogue_rejects_unknown_runtime_deployment(tmp_path: Path) -> None:
    target = _write_catalogue(
        tmp_path,
        lambda document: document["cards"][0]["deployment_ids"].append("invented"),
    )
    with pytest.raises(ConfigurationError, match="no runtime configuration"):
        ModelCatalogue.load(target, configured_deployment_ids=CONFIGURED)


def test_runtime_deployments_outside_catalogue_require_an_exact_auxiliary_declaration() -> None:
    with pytest.raises(ConfigurationError, match="must be declared auxiliary"):
        ModelCatalogue.load(
            CATALOGUE,
            configured_deployment_ids=CONFIGURED | {"qwen-coder-api"},
        )

    catalogue = ModelCatalogue.load(
        CATALOGUE,
        configured_deployment_ids=CONFIGURED | {"qwen-coder-api"},
        auxiliary_deployment_ids={"qwen-coder-api"},
    )
    assert not any(
        "qwen-coder-api" in card["deployment_ids"] for card in catalogue.cards
    )


def test_catalogue_rejects_quarantine_that_can_route(tmp_path: Path) -> None:
    def mutate(document):
        card = next(item for item in document["cards"] if item["integration_stage"] == "quarantined")
        card["deployment_ids"] = ["reranker-api-backup"]

    target = _write_catalogue(tmp_path, mutate)
    with pytest.raises(ConfigurationError, match="non-configured card"):
        ModelCatalogue.load(
            target,
            configured_deployment_ids=CONFIGURED | {"reranker-api-backup"},
        )


def test_catalogue_page_has_strict_bounds() -> None:
    catalogue = _load()
    with pytest.raises(ValidationError):
        catalogue.page(offset=-1, limit=10)
    with pytest.raises(ValidationError):
        catalogue.page(offset=0, limit=201)


def test_check_config_can_validate_the_source_catalogue_before_install() -> None:
    arguments = cli.build_parser().parse_args(
        [
            "check-config",
            "--config",
            str(ROOT / "orchestrator" / "config" / "orchestrator.json"),
            "--catalogue",
            str(CATALOGUE),
        ]
    )
    result = cli.run(arguments)
    assert result["status"] == "valid"
    assert result["catalogue_cards"] == 58
    assert result["catalogue_linked_deployments"] == 16
    assert result["catalogue_auxiliary_deployments"] == [
        "embedding-api-backup",
        "qwen-coder-api",
        "reranker-api-backup",
    ]
    assert result["catalogue_revision"] == "catalogue-api-ia-2026-09-05.3"
