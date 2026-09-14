from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
import pytest


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "catalog" / "provider-integrations.v1.json"
SCHEMA = ROOT / "catalog" / "provider-integrations.v1.schema.json"
CATALOGUE = ROOT / "catalog" / "model-catalog.v2.json"
VALIDATOR = ROOT / "scripts" / "validate-provider-integrations.py"
UNRESOLVED_EXACT_IDS = {
    "kimi-k2-x",
    "gemini-3-flash",
    "doubao-seed-2-1-pro",
    "gemini-3-1-pro",
    "nvidia-nemotron-family",
    "amazon-nova-family",
    "meta-llama-family",
    "microsoft-phi-family",
    "xiaomi-mimo-family",
    "baidu-ernie-family",
    "baichuan-family",
    "stepfun-family",
    "mistral-medium-family",
}


def _load_validator():
    spec = importlib.util.spec_from_file_location(
        "validate_provider_integrations", VALIDATOR
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_registry(tmp_path: Path, mutate) -> Path:
    document = json.loads(REGISTRY.read_text(encoding="utf-8"))
    mutate(document)
    target = tmp_path / "provider-integrations.json"
    target.write_text(json.dumps(document), encoding="utf-8")
    return target


def _write_catalogue(tmp_path: Path, mutate) -> Path:
    document = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    mutate(document)
    target = tmp_path / "model-catalogue.json"
    target.write_text(json.dumps(document), encoding="utf-8")
    return target


def test_provider_registry_matches_its_json_schema() -> None:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(registry), key=lambda error: list(error.path))
    assert errors == []


def test_registry_has_all_accounts_and_strict_financial_capabilities() -> None:
    module = _load_validator()
    registry = module.load_and_validate(REGISTRY)
    assert tuple(account["id"] for account in registry["provider_accounts"]) == module.PROVIDER_IDS
    assert len(registry["provider_accounts"]) == 21
    assert registry["finance_capability_names"] == list(module.FINANCE_CAPABILITIES)
    assert registry["finance_access_modes"] == list(module.FINANCE_ACCESS_MODES)
    for account in registry["provider_accounts"]:
        assert set(account["financial_capabilities"]) == set(module.FINANCE_CAPABILITIES)
        assert set(account["financial_capabilities"].values()) <= set(module.FINANCE_ACCESS_MODES)
        assert account["official_sources"]
        assert {source["verified_on"] for source in account["official_sources"]} == {"2026-09-05"}


def test_inference_and_admin_finance_credentials_are_separate() -> None:
    registry = _load_validator().load_and_validate(REGISTRY)
    inference_refs = {
        account["credentials"]["inference"]["credential_ref"]
        for account in registry["provider_accounts"]
    }
    admin_refs = {
        account["credentials"]["admin_finance"]["credential_ref"]
        for account in registry["provider_accounts"]
        if account["credentials"]["admin_finance"]["credential_ref"] is not None
    }
    assert len(inference_refs) == 21
    assert inference_refs.isdisjoint(admin_refs)
    assert all(reference.startswith("kv-infra-shared/data/llm-admin/") for reference in admin_refs)


def test_direct_financial_apis_have_an_explicit_non_billable_read_endpoint() -> None:
    registry = _load_validator().load_and_validate(REGISTRY)
    by_id = {account["id"]: account for account in registry["provider_accounts"]}
    assert by_id["deepseek"]["financial_capabilities"]["cash_balance"] == "direct_api"
    assert by_id["moonshot"]["financial_capabilities"]["cash_balance"] == "direct_api"
    assert by_id["stepfun"]["financial_capabilities"]["cash_balance"] == "direct_api"
    assert by_id["minimax"]["financial_capabilities"]["plan_quota"] == "direct_api"
    for account in by_id.values():
        declared = {
            capability
            for endpoint in account["finance_endpoints"]
            if endpoint["access"] == "direct_api"
            for capability in endpoint["capabilities"]
        }
        expected = {
            capability
            for capability, access in account["financial_capabilities"].items()
            if access == "direct_api"
        }
        assert declared == expected


def test_registry_and_catalogue_deployment_mapping_are_synchronized() -> None:
    module = _load_validator()
    registry = module.load_and_validate(REGISTRY)
    unresolved = module.verify_catalogue(registry, CATALOGUE)
    assert set(unresolved) == UNRESOLVED_EXACT_IDS
    mappings = {item["deployment_id"]: item for item in registry["deployment_mappings"]}
    assert len(mappings) == 7
    hosted = mappings["alibaba-deepseek-ops-api"]
    assert hosted["developer_id"] == "deepseek"
    assert hosted["inference_provider_account_id"] == "alibaba"
    assert hosted["developer_id"] != hosted["inference_provider_account_id"]


def test_only_officially_confirmed_exact_ids_were_promoted() -> None:
    catalogue = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    cards = {card["card_id"]: card for card in catalogue["cards"]}
    expected = {
        "minimax-m3": "MiniMax-M3",
        "gemini-3-1-flash-lite": "gemini-3.1-flash-lite",
        "gemini-3-7-flash": "gemini-3.7-flash",
        "grok-build-0-1": "grok-build-0.1",
        "grok-4-20": "grok-4.20",
        "grok-4-20-reasoning": "grok-4.20-reasoning",
        "grok-4-20-multi-agent": "grok-4.20-multi-agent",
        "grok-4-6": "grok-4.6",
        "claude-sonnet-5": "claude-sonnet-5",
        "cohere-command-a": "command-a-03-2025",
        "cohere-command-a-plus": "command-a-plus-05-2026",
        "kimi-k3": "kimi-k3",
    }
    assert {card_id: cards[card_id]["exact_model_id"] for card_id in expected} == expected
    assert {card_id for card_id, card in cards.items() if card["exact_model_id"] is None} == UNRESOLVED_EXACT_IDS


def test_retired_aya_8b_is_visible_non_routable_and_keeps_txt_price() -> None:
    catalogue = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    aya = next(card for card in catalogue["cards"] if card["card_id"] == "cohere-aya-expanse-8b")
    assert aya["exact_model_id"] == "c4ai-aya-expanse-8b"
    assert aya["integration_stage"] == "quarantined"
    assert aya["deployment_ids"] == []
    assert aya["pricing"] == [
        {
            "mode": "standard",
            "region_scope": "unspecified",
            "input_usd_per_million": "0.50",
            "output_usd_per_million": "1.50",
            "declared_simulation_usd": "8.00",
            "confidence": "shared_source_price",
            "cached_input_usd_per_million": None,
            "source": "catalogue_modeles_IA_API_2026.txt",
        }
    ]


@pytest.mark.parametrize(
    "mutate,match",
    [
        (
            lambda document: document["provider_accounts"][0]["financial_capabilities"].update(cash_balance="direct_api"),
            "endpoint disagrees",
        ),
        (
            lambda document: document["provider_accounts"][0]["official_sources"][0].update(url="https://example.com/not-official"),
            "official-domain allowlist",
        ),
        (
            lambda document: document["provider_accounts"][0]["credentials"]["admin_finance"].update(credential_ref=document["provider_accounts"][0]["credentials"]["inference"]["credential_ref"]),
            "credential_ref is unsafe",
        ),
    ],
)
def test_validator_fails_closed_on_unreviewed_provider_changes(
    tmp_path: Path, mutate, match: str
) -> None:
    module = _load_validator()
    target = _write_registry(tmp_path, mutate)
    with pytest.raises(module.RegistryError, match=match):
        module.load_and_validate(target)


def test_catalogue_cross_check_rejects_wrong_host_provider(tmp_path: Path) -> None:
    module = _load_validator()
    registry = copy.deepcopy(module.load_and_validate(REGISTRY))
    mapping = next(
        item
        for item in registry["deployment_mappings"]
        if item["deployment_id"] == "alibaba-deepseek-ops-api"
    )
    mapping["card_id"] = "qwen-3-7-flash"
    target = tmp_path / "registry.json"
    target.write_text(json.dumps(registry), encoding="utf-8")
    loaded = module.load_and_validate(target)
    with pytest.raises(module.RegistryError, match="wrong card"):
        module.verify_catalogue(loaded, CATALOGUE)


def test_catalogue_cross_check_rejects_reactivation_of_retired_aya(tmp_path: Path) -> None:
    module = _load_validator()

    def reactivate(document):
        aya = next(card for card in document["cards"] if card["card_id"] == "cohere-aya-expanse-8b")
        aya["integration_stage"] = "catalogued"

    target = _write_catalogue(tmp_path, reactivate)
    registry = module.load_and_validate(REGISTRY)
    with pytest.raises(module.RegistryError, match="must remain non-routable"):
        module.verify_catalogue(registry, target)
