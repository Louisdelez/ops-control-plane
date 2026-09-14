from __future__ import annotations

from collections import Counter
from dataclasses import replace
import json
from pathlib import Path

import pytest

from ops_orchestrator.catalogue import ModelCatalogue
import ops_orchestrator.catalogue_runtime as catalogue_runtime_module
from ops_orchestrator.catalogue_runtime import build_catalogue_runtime_plan
from ops_orchestrator.config import expand_catalogue_runtime, load_config
from ops_orchestrator.errors import ProviderProtocolError
from ops_orchestrator.models import parse_route_request
from ops_orchestrator.providers import HTTPProvider
import ops_orchestrator.providers as provider_module

from conftest import SOURCE_CONFIG


ROOT = Path(__file__).parents[2]
CATALOGUE = ROOT / "catalog" / "model-catalog.v2.json"
REGISTRY = ROOT / "catalog" / "provider-integrations.v1.json"


def _expanded():
    return expand_catalogue_runtime(
        load_config(SOURCE_CONFIG),
        catalogue_path=CATALOGUE,
        provider_integrations_path=REGISTRY,
    )


def _mutated_catalogue(tmp_path: Path, mutate) -> Path:
    document = json.loads(CATALOGUE.read_text(encoding="utf-8"))
    mutate(document)
    target = tmp_path / "catalogue.json"
    target.write_text(json.dumps(document), encoding="utf-8")
    return target


def _payload():
    return {
        "mission_id": "catalog-runtime-test",
        "project_id": "infra-shared",
        "task_type": "ANALYZE",
        "risk": "LOW",
        "complexity": 0.5,
        "impact": 0.2,
        "confidence": 0.8,
        "urgency": "NORMAL",
        "deterministic_available": False,
        "remote_allowed": True,
        "context": {"instructions": "Use facts.", "mission": "Analyze state."},
    }


def _envelope():
    return json.dumps(
        {"status": "ok", "confidence": 0.9, "summary": "bounded", "plan": []}
    )


def test_runtime_generation_is_exact_fail_closed_and_account_budgeted() -> None:
    config = _expanded()
    assert len(config.catalogue_runtime_bindings) == 9
    assert len(config.providers) == 19
    assert set(config.catalogue_runtime_bindings) == {
        "glm-4-7-flashx", "glm-4-7", "glm-5", "glm-5-1",
        "gpt-5-4-mini", "gpt-5-3-codex", "gpt-5-4", "gpt-5-4-pro",
        "kimi-k3",
    }
    counts = Counter(config.catalogue_runtime_reasons.values())
    assert counts == {
        "token_prices_not_officially_verified": 28,
        "quarantined": 11,
        "generated_reviewed_api_deployment": 9,
        "configured_static_priority": 5,
        "exact_model_id_unverified": 4,
        "minimax_m3_chat_api_contract_unverified": 1,
    }
    cards = {
        card["card_id"]: card
        for card in json.loads(CATALOGUE.read_text(encoding="utf-8"))["cards"]
    }
    for card_id, deployment_id in config.catalogue_runtime_bindings.items():
        card = cards[card_id]
        provider = config.provider(deployment_id)
        assert card["integration_stage"] != "quarantined"
        assert provider.model == card["exact_model_id"]
        assert provider.location == "remote"
        assert provider.account_id in config.provider_account_budgets
        assert provider.api_key_file_env and provider.api_key_file_env.endswith("_API_KEY_FILE")
        assert provider.price_microusd_per_million({})[0] > 0
        assert provider.price_microusd_per_million({})[1] > 0
    assert not any(provider.kind.startswith("ollama_") for provider in config.providers)


@pytest.mark.parametrize(
    ("mutation", "card_id", "reason"),
    [
        (
            lambda document: document["model_id_provenance"]["gpt-5-4-mini"].update(
                source="https://developers.openai.com.evil.invalid/api/docs/models/gpt-5.4-mini"
            ),
            "gpt-5-4-mini",
            "exact_model_id_provenance_unverified",
        ),
        (
            lambda document: next(
                card for card in document["cards"] if card["card_id"] == "gpt-5-4-mini"
            )["pricing"][0].update(
                source="https://developers.openai.com@evil.invalid/api/docs/models/gpt-5.4-mini"
            ),
            "gpt-5-4-mini",
            "token_prices_not_officially_verified",
        ),
        (
            lambda document: next(
                card for card in document["cards"] if card["card_id"] == "gpt-5-4-mini"
            )["pricing"][0].update(confidence="declared_unverified"),
            "gpt-5-4-mini",
            "token_prices_not_officially_verified",
        ),
        (
            lambda document: next(
                card for card in document["cards"] if card["card_id"] == "gpt-5-4-mini"
            ).update(integration_stage="quarantined", deployment_ids=[]),
            "gpt-5-4-mini",
            "quarantined",
        ),
    ],
)
def test_generation_rejects_deceptive_sources_and_unsafe_cards(
    tmp_path: Path, mutation, card_id: str, reason: str
) -> None:
    plan = build_catalogue_runtime_plan(
        _mutated_catalogue(tmp_path, mutation), REGISTRY
    )
    assert card_id not in plan.bindings
    assert plan.ineligibility[card_id] == reason


def test_alibaba_runtime_allowlist_uses_static_shared_endpoint() -> None:
    integration = catalogue_runtime_module._INTEGRATIONS["alibaba"]
    assert integration.base_url == (
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    )
    assert integration.required_path == "/compatible-mode/v1"
    assert integration.base_url_env is None
    assert integration.required_host_suffix is None


def test_runtime_catalogue_exposes_generated_binding_and_exact_reason() -> None:
    config = _expanded()
    catalogue = ModelCatalogue.load(
        CATALOGUE,
        configured_deployment_ids=(provider.provider_id for provider in config.providers),
        auxiliary_deployment_ids=config.catalogue_auxiliary_deployment_ids,
        runtime_bindings=config.catalogue_runtime_bindings,
        runtime_reasons=config.catalogue_runtime_reasons,
    )
    assert sum(card["integration_stage"] == "configured" for card in catalogue.cards) == 14
    page = catalogue.page(offset=0, limit=200)
    cards = {card["card_id"]: card for card in page["cards"]}
    assert cards["gpt-5-4-pro"]["runtime_eligibility"] == {
        "eligible": True,
        "binding": "generated",
        "reason": "generated_reviewed_api_deployment",
    }
    assert cards["minimax-m3"]["runtime_eligibility"]["reason"] == (
        "minimax_m3_chat_api_contract_unverified"
    )
    assert cards["cohere-aya-expanse-8b"]["runtime_eligibility"]["eligible"] is False


def test_openai_responses_adapter_uses_minimal_non_persistent_request(
    tmp_path: Path, monkeypatch
) -> None:
    config = _expanded()
    provider_config = config.provider("catalog-gpt-5-4-pro")
    key = tmp_path / "key"
    key.write_text("test-only-secret", encoding="utf-8")
    key.chmod(0o600)
    captured = {}

    def transport(**kwargs):
        captured.update(kwargs)
        return {
            "status": "completed",
            "error": None,
            "incomplete_details": None,
            "output": [
                {"type": "reasoning"},
                {"type": "message", "status": "completed", "content": [
                    {"type": "output_text", "text": _envelope()}
                ]},
            ],
            "usage": {"input_tokens": 12, "output_tokens": 5},
        }

    monkeypatch.setattr(provider_module, "_post_json", transport)
    provider = HTTPProvider(
        provider_config,
        config.limits,
        {
            "OPS_ORCHESTRATOR_ENABLE_CATALOGUE_API": "1",
            "OPENAI_API_KEY_FILE": str(key),
        },
    )
    result = provider.invoke(parse_route_request(_payload(), config.limits), 64)
    assert result.summary == "bounded"
    assert captured["url"] == "https://api.openai.com/v1/responses"
    assert captured["payload"]["store"] is False
    assert captured["payload"]["max_output_tokens"] == 64
    assert "temperature" not in captured["payload"]
    assert "response_format" not in captured["payload"]


@pytest.mark.parametrize(
    "response",
    [
        {"status": "incomplete", "output": []},
        {"status": "completed", "incomplete_details": {"reason": "max_output_tokens"}, "output": []},
        {"status": "completed", "error": {"code": "failed"}, "output": []},
        {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": "{}"},
            {"type": "output_text", "text": "{}"},
        ]}]},
        {"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": _envelope()},
        ]}], "usage": {"input_tokens": "12", "output_tokens": 5}},
    ],
)
def test_openai_responses_adapter_rejects_incomplete_or_ambiguous_output(
    tmp_path: Path, monkeypatch, response
) -> None:
    config = _expanded()
    key = tmp_path / "key"
    key.write_text("test-only-secret", encoding="utf-8")
    key.chmod(0o600)
    monkeypatch.setattr(provider_module, "_post_json", lambda **_kwargs: response)
    provider = HTTPProvider(
        config.provider("catalog-gpt-5-4-pro"), config.limits,
        {"OPS_ORCHESTRATOR_ENABLE_CATALOGUE_API": "1", "OPENAI_API_KEY_FILE": str(key)},
    )
    with pytest.raises(ProviderProtocolError):
        provider.invoke(parse_route_request(_payload(), config.limits), 64)


def test_kimi_k3_dialect_omits_forbidden_sampling_parameters(
    tmp_path: Path, monkeypatch
) -> None:
    config = _expanded()
    key = tmp_path / "key"
    key.write_text("test-only-secret", encoding="utf-8")
    key.chmod(0o600)
    captured = {}

    def transport(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"finish_reason": "stop", "message": {"content": _envelope()}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5}}

    monkeypatch.setattr(provider_module, "_post_json", transport)
    provider = HTTPProvider(
        config.provider("catalog-kimi-k3"), config.limits,
        {"OPS_ORCHESTRATOR_ENABLE_CATALOGUE_API": "1", "MOONSHOT_API_KEY_FILE": str(key)},
    )
    provider.invoke(parse_route_request(_payload(), config.limits), 64)
    assert captured["payload"]["max_completion_tokens"] == 64
    assert captured["payload"]["response_format"]["type"] == "json_schema"
    assert not {"temperature", "top_p", "top_k", "max_tokens"} & set(captured["payload"])


@pytest.mark.parametrize("finish_reason", [None, "length", "content_filter", "tool_calls"])
def test_generic_chat_uses_minimal_payload_and_rejects_nonterminal_finish(
    tmp_path: Path, monkeypatch, finish_reason
) -> None:
    config = _expanded()
    key = tmp_path / "key"
    key.write_text("test-only-secret", encoding="utf-8")
    key.chmod(0o600)
    captured = {}

    def transport(**kwargs):
        captured.update(kwargs)
        return {"choices": [{"finish_reason": finish_reason, "message": {"content": _envelope()}}]}

    monkeypatch.setattr(provider_module, "_post_json", transport)
    provider = HTTPProvider(
        config.provider("catalog-glm-4-7-flashx"), config.limits,
        {"OPS_ORCHESTRATOR_ENABLE_CATALOGUE_API": "1", "Z_AI_API_KEY_FILE": str(key)},
    )
    with pytest.raises(ProviderProtocolError):
        provider.invoke(parse_route_request(_payload(), config.limits), 64)
    assert set(captured["payload"]) == {"model", "messages", "max_tokens"}
    assert "response_format" not in captured["payload"]


def test_anthropic_native_adapter_allows_thinking_but_one_text_only(
    tmp_path: Path, monkeypatch
) -> None:
    config = load_config(SOURCE_CONFIG)
    base = config.provider("qwen-reasoning-api")
    provider_config = replace(
        base,
        provider_id="anthropic-test",
        account_id="anthropic",
        kind="anthropic_chat",
        activation_env=None,
        base_url="https://api.anthropic.com",
        base_url_env=None,
        required_host_suffix=None,
        required_path=None,
        model="claude-sonnet-5",
        api_key_file_env="ANTHROPIC_API_KEY_FILE",
        request_path="/v1/messages",
        auth_scheme="anthropic",
        chat_family="anthropic",
        chat_dialect="generic",
        thinking_mode=None,
    )
    key = tmp_path / "key"
    key.write_text("test-only-secret", encoding="utf-8")
    key.chmod(0o600)
    captured = {}

    def transport(**kwargs):
        captured.update(kwargs)
        return {
            "stop_reason": "end_turn",
            "content": [{"type": "thinking"}, {"type": "text", "text": _envelope()}],
            "usage": {"input_tokens": 12, "output_tokens": 5},
        }

    monkeypatch.setattr(provider_module, "_post_json", transport)
    provider = HTTPProvider(
        provider_config, config.limits, {"ANTHROPIC_API_KEY_FILE": str(key)}
    )
    assert provider.invoke(parse_route_request(_payload(), config.limits), 64).summary == "bounded"
    assert "temperature" not in captured["payload"]
    assert captured["auth_scheme"] == "anthropic"


def test_anthropic_native_adapter_rejects_refusal_and_multiple_texts(
    tmp_path: Path, monkeypatch
) -> None:
    config = load_config(SOURCE_CONFIG)
    base = config.provider("qwen-reasoning-api")
    provider_config = replace(
        base, provider_id="anthropic-test", account_id="anthropic",
        kind="anthropic_chat", activation_env=None, base_url="https://api.anthropic.com",
        base_url_env=None, required_host_suffix=None, required_path=None,
        model="claude-sonnet-5", api_key_file_env="ANTHROPIC_API_KEY_FILE",
        request_path="/v1/messages", auth_scheme="anthropic", chat_family="anthropic",
        chat_dialect="generic", thinking_mode=None,
    )
    key = tmp_path / "key"
    key.write_text("test-only-secret", encoding="utf-8")
    key.chmod(0o600)
    provider = HTTPProvider(provider_config, config.limits, {"ANTHROPIC_API_KEY_FILE": str(key)})
    request = parse_route_request(_payload(), config.limits)
    for response in (
        {"stop_reason": "refusal", "content": [{"type": "text", "text": _envelope()}]},
        {"stop_reason": "end_turn", "content": [
            {"type": "text", "text": _envelope()}, {"type": "text", "text": _envelope()}
        ]},
        {"stop_reason": "max_tokens", "content": [{"type": "text", "text": _envelope()}]},
    ):
        monkeypatch.setattr(provider_module, "_post_json", lambda **_kwargs: response)
        with pytest.raises(ProviderProtocolError):
            provider.invoke(request, 64)
