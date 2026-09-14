from __future__ import annotations

from dataclasses import replace
import hashlib
import json

import pytest

from ops_orchestrator.config import ConfigurationError, load_config
from ops_orchestrator.errors import ValidationError
from ops_orchestrator.models import Risk, Role, parse_route_request
from ops_orchestrator.routing import RoutingPolicy

from conftest import SOURCE_CONFIG, make_config


def test_default_configuration_is_valid_and_all_roles_exist():
    config = load_config(SOURCE_CONFIG)
    assert set(config.providers_by_role) == set(Role)
    assert len(config.providers) == 10
    assert {provider.location for provider in config.providers} == {"remote"}
    assert not any(provider.kind.startswith("ollama_") for provider in config.providers)
    assert {provider.provider_id for provider in config.providers} == {
        "qwen-utility-api",
        "alibaba-deepseek-ops-api",
        "qwen-ops-api",
        "deepseek-ops-api",
        "deepseek-reasoning",
        "qwen-reasoning-api",
        "qwen-coder-api",
        "deepseek-coder-api",
        "embedding-api-backup",
        "reranker-api-backup",
    }
    assert config.catalogue_auxiliary_deployment_ids == {
        "qwen-coder-api",
        "embedding-api-backup",
        "reranker-api-backup",
    }
    assert [
        (
            tier.max_input_tokens,
            tier.input_price_microusd_per_million,
            tier.output_price_microusd_per_million,
        )
        for tier in config.provider("qwen-utility-api").price_tiers
    ] == [
        (32000, 28000, 110000),
        (256000, 83000, 330000),
        (1000000, 165000, 660000),
    ]
    assert [
        (
            tier.max_input_tokens,
            tier.input_price_microusd_per_million,
            tier.output_price_microusd_per_million,
        )
        for tier in config.provider("qwen-ops-api").price_tiers
    ] == [
        (256000, 276000, 1101000),
        (1000000, 826000, 3301000),
    ]
    assert [
        (
            tier.max_input_tokens,
            tier.input_price_microusd_per_million,
            tier.output_price_microusd_per_million,
        )
        for tier in config.provider("qwen-coder-api").price_tiers
    ] == [
        (32000, 144000, 574000),
        (128000, 216000, 861000),
        (256000, 359000, 1434000),
        (1000000, 717000, 3584000),
    ]
    alibaba_deepseek = config.provider("alibaba-deepseek-ops-api")
    qwen_ops = config.provider("qwen-ops-api")
    direct_deepseek = config.provider("deepseek-ops-api")
    assert alibaba_deepseek.model == "deepseek-v4-flash"
    assert alibaba_deepseek.account_id == "alibaba"
    assert direct_deepseek.account_id == "deepseek"
    assert alibaba_deepseek.price_microusd_per_million({}) == (138000, 275000)
    assert (
        alibaba_deepseek.base_url,
        alibaba_deepseek.base_url_env,
        alibaba_deepseek.api_key_file_env,
        alibaba_deepseek.required_host_suffix,
        alibaba_deepseek.required_path,
    ) == (
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        qwen_ops.base_url_env,
        qwen_ops.api_key_file_env,
        None,
        "/compatible-mode/v1",
    )
    assert [
        provider.provider_id for provider in config.providers_by_role[Role.LOCAL_OPS]
    ] == [
        "alibaba-deepseek-ops-api",
        "qwen-ops-api",
        "deepseek-ops-api",
    ]
    assert [
        provider.provider_id for provider in config.providers_by_role[Role.REASONING]
    ] == ["deepseek-reasoning"]
    assert [
        provider.provider_id for provider in config.providers_by_role[Role.PREMIUM]
    ] == ["qwen-reasoning-api"]
    assert [
        (provider.chat_family, provider.chat_dialect, provider.thinking_mode)
        for provider in config.providers_by_role[Role.LOCAL_OPS]
    ] == [
        ("deepseek", "alibaba", "disabled"),
        ("qwen", "alibaba", "disabled"),
        ("deepseek", "deepseek", "disabled"),
    ]
    assert alibaba_deepseek.budget.daily_cost_microusd == 1_000_000
    assert alibaba_deepseek.budget.monthly_cost_microusd == 15_000_000
    assert alibaba_deepseek.budget.mission_cost_microusd == 250_000
    assert set(config.provider_account_budgets) == {"alibaba", "deepseek"}
    alibaba_budget = config.provider_account_budget("alibaba")
    assert alibaba_budget.daily_cost_microusd == 4_000_000
    assert alibaba_budget.monthly_cost_microusd == 50_000_000
    assert alibaba_budget.task_calls == 5
    assert alibaba_budget.task_tokens == 300000
    assert alibaba_budget.task_cost_microusd == 1_000_000


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda raw: raw["provider_accounts"].append(raw["provider_accounts"][0]), "duplicate"),
        (lambda raw: raw["provider_accounts"].pop(), "exactly match"),
        (
            lambda raw: raw["provider_accounts"][0]["budget"].update(
                {"task_calls": 6_000}
            ),
            "task_calls",
        ),
        (
            lambda raw: raw["provider_accounts"][0]["budget"].update(
                {"task_cost_usd": "51.000000"}
            ),
            "inconsistent",
        ),
    ],
)
def test_shared_provider_account_budgets_are_strict(tmp_path, mutation, message):
    raw = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    mutation(raw)
    target = tmp_path / "config.json"
    target.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigurationError, match=message):
        load_config(target)


@pytest.mark.parametrize(
    ("input_tokens", "expected"),
    [
        (32000, (28000, 110000)),
        (32001, (83000, 330000)),
        (256000, (83000, 330000)),
        (256001, (165000, 660000)),
        (1000000, (165000, 660000)),
    ],
)
def test_price_tier_selection_includes_upper_boundary(input_tokens, expected):
    provider = load_config(SOURCE_CONFIG).provider("qwen-utility-api")
    assert provider.price_microusd_per_million(
        {}, input_tokens=input_tokens
    ) == expected


def test_price_tiers_fail_closed_above_reviewed_maximum():
    provider = load_config(SOURCE_CONFIG).provider("qwen-utility-api")
    with pytest.raises(ConfigurationError, match="exceed configured price tiers"):
        provider.price_microusd_per_million({}, input_tokens=1000001)


def test_price_tiers_are_mutually_exclusive_with_flat_prices(tmp_path):
    raw = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    raw["roles"]["ROLE_TINY"][0]["input_price_per_million_usd"] = "0.001000"
    target = tmp_path / "config.json"
    target.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="mutually exclusive"):
        load_config(target)


def test_price_tier_bounds_must_be_strictly_increasing(tmp_path):
    raw = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    tiers = raw["roles"]["ROLE_TINY"][0]["price_tiers"]
    tiers[1]["max_input_tokens"] = tiers[0]["max_input_tokens"]
    target = tmp_path / "config.json"
    target.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="strictly increasing"):
        load_config(target)


@pytest.mark.parametrize(
    "price_field",
    ["input_price_per_million_usd", "output_price_per_million_usd"],
)
def test_price_tier_rates_must_be_non_decreasing(tmp_path, price_field):
    raw = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    tiers = raw["roles"]["ROLE_TINY"][0]["price_tiers"]
    tiers[1][price_field] = "0.000001"
    target = tmp_path / "config.json"
    target.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="prices must be non-decreasing"):
        load_config(target)


def test_price_tier_rejects_unknown_fields(tmp_path):
    raw = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    raw["roles"]["ROLE_TINY"][0]["price_tiers"][0]["discount"] = True
    target = tmp_path / "config.json"
    target.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unknown"):
        load_config(target)


@pytest.mark.parametrize("value", ["NaN", "sNaN", "Infinity", "-Infinity"])
def test_static_price_rejects_non_finite_decimals(tmp_path, value):
    raw = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    raw["roles"]["ROLE_REASONING"][0]["input_price_per_million_usd"] = value
    target = tmp_path / "config.json"
    target.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="must be finite"):
        load_config(target)


def _synthetic_local_provider(
    provider,
    *,
    kind,
    promotion_file,
    model="approved-test-model",
    promotion_slot="tiny",
):
    """Exercise retained rollback gates without putting local models in defaults."""

    return replace(
        provider,
        kind=kind,
        location="local",
        activation_env=None,
        base_url="http://127.0.0.1:11434",
        base_url_env=None,
        required_host_suffix=None,
        required_path=None,
        api_key_file_env=None,
        model=model,
        model_env=None,
        input_price_per_million=None,
        input_price_env=None,
        output_price_per_million=None,
        output_price_env=None,
        price_tiers=(),
        chat_family=None,
        chat_dialect=None,
        thinking_mode=None,
        promotion_file=str(promotion_file),
        promotion_slot=promotion_slot,
    )


def test_configuration_rejects_inline_secret(tmp_path):
    raw = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    raw["roles"]["ROLE_REASONING"][0]["api_key"] = "must-never-be-here"
    target = tmp_path / "config.json"
    target.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unknown"):
        load_config(target)


def test_route_grants_are_exact_and_cannot_duplicate_unix_peers(tmp_path):
    raw = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    raw["access"]["route_grants"].append(dict(raw["access"]["route_grants"][0]))
    target = tmp_path / "config.json"
    target.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="duplicate route grant"):
        load_config(target)

    raw["access"]["route_grants"].pop()
    raw["access"]["route_grants"][0]["remote_allowed"] = "yes"
    target.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="remote_allowed"):
        load_config(target)


def test_qwen_endpoint_is_static_and_ignores_hostile_environment_override():
    provider = load_config(SOURCE_CONFIG).provider("qwen-utility-api")
    assert provider.base_url_env is None
    assert provider.required_host_suffix is None
    assert provider.resolved_base_url(
        {"QWEN_API_BASE_URL": "https://attacker.invalid/v1"}
    ) == "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


@pytest.mark.parametrize(
    ("provider_id", "field", "value", "message"),
    [
        (
            "alibaba-deepseek-ops-api",
            "chat_dialect",
            "deepseek",
            "reviewed direct endpoint",
        ),
        (
            "alibaba-deepseek-ops-api",
            "required_host_suffix",
            ".aliyuncs.com",
            "exact reviewed shared endpoint",
        ),
        (
            "alibaba-deepseek-ops-api",
            "base_url_env",
            "QWEN_API_BASE_URL",
            "exact reviewed shared endpoint",
        ),
        (
            "qwen-utility-api",
            "base_url",
            "https://attacker.invalid/compatible-mode/v1",
            "exact reviewed shared endpoint",
        ),
        (
            "alibaba-deepseek-ops-api",
            "thinking_mode",
            None,
            "explicit thinking_mode",
        ),
        (
            "qwen-ops-api",
            "chat_dialect",
            None,
            "explicit provider dialect",
        ),
        (
            "deepseek-ops-api",
            "chat_dialect",
            "alibaba",
            "exact reviewed shared endpoint",
        ),
        (
            "deepseek-ops-api",
            "base_url",
            "https://compatible.example.invalid",
            "reviewed direct endpoint",
        ),
    ],
)
def test_chat_dialect_is_pinned_to_its_reviewed_endpoint(
    tmp_path, provider_id, field, value, message
):
    raw = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    provider = next(
        item
        for entries in raw["roles"].values()
        for item in entries
        if item["id"] == provider_id
    )
    provider[field] = value
    target = tmp_path / "config.json"
    target.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_config(target)


def test_local_provider_requires_human_benchmark_promotion(tmp_path):
    config = load_config(SOURCE_CONFIG)
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": "2026-09-04T12:00:00Z",
                "model": "approved-test-model",
                "promotion_eligible": True,
                "automatic_promotion": False,
                "promotion_performed": False,
            }
        ) + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    registry = tmp_path / "promotions.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "automatic_promotion": False,
                "promotions": {
                    "tiny": {
                        "model": "approved-test-model",
                        "eligible": True,
                        "human_approved": True,
                        "benchmark_report": str(report),
                        "benchmark_sha256": digest,
                        "benchmark_generated_at": "2026-09-04T12:00:00Z",
                        "approved_at": "2026-09-04T13:00:00Z",
                        "approved_by": "ops-user",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    provider = _synthetic_local_provider(
        config.provider("embedding-api-backup"),
        kind="ollama_embedding",
        promotion_file=registry,
        model="approved-test-model",
    )
    provider.verify_promotion({})

    chat_provider = _synthetic_local_provider(
        config.provider("qwen-utility-api"),
        kind="ollama_chat",
        promotion_file=registry,
    )
    with pytest.raises(ConfigurationError, match="eligible"):
        chat_provider.verify_promotion({})

    report.write_text('{"schema_version":1,"tampered":true}\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="digest"):
        provider.verify_promotion({})


def test_multi_model_promotion_requires_a_complete_campaign(tmp_path):
    config = load_config(SOURCE_CONFIG)
    report_path = tmp_path / "report.json"
    registry_path = tmp_path / "promotions.json"
    report = {
        "schema_version": 1,
        "generated_at": "2026-09-04T12:00:00Z",
        "campaign": {
            "status": "completed",
            "complete": True,
            "interruption_reason": None,
        },
        "inputs": {
            "configured_candidate_count": 1,
            "evaluated_candidate_count": 1,
        },
        "models": [
            {
                "model": "approved-test-model",
                "status": "completed",
                "gate": {
                    "promotion_eligible": True,
                    "promotion_performed": False,
                },
            }
        ],
        "selection": {
            "eligible_models": ["approved-test-model"],
            "automatic_promotion": False,
            "promotion_performed": False,
            "requires_human_approval": True,
        },
    }

    def write_evidence() -> None:
        report_path.write_text(json.dumps(report) + "\n", encoding="utf-8")
        registry_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "automatic_promotion": False,
                    "promotions": {
                        "tiny": {
                            "model": "approved-test-model",
                            "eligible": True,
                            "human_approved": True,
                            "benchmark_report": str(report_path),
                            "benchmark_sha256": hashlib.sha256(
                                report_path.read_bytes()
                            ).hexdigest(),
                            "benchmark_generated_at": report["generated_at"],
                            "approved_at": "2026-09-04T13:00:00Z",
                            "approved_by": "ops-user",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    provider = _synthetic_local_provider(
        config.provider("qwen-utility-api"),
        kind="ollama_chat",
        promotion_file=registry_path,
    )
    write_evidence()
    provider.verify_promotion({})

    report["campaign"] = {
        "status": "interrupted",
        "complete": False,
        "interruption_reason": "ollama_unavailable",
    }
    write_evidence()
    with pytest.raises(ConfigurationError, match="eligible"):
        provider.verify_promotion({})

    report["campaign"] = {
        "status": "completed",
        "complete": True,
        "interruption_reason": None,
    }
    report["inputs"]["configured_candidate_count"] = 2
    write_evidence()
    with pytest.raises(ConfigurationError, match="eligible"):
        provider.verify_promotion({})

    report["inputs"]["configured_candidate_count"] = 1
    report["inputs"]["evaluated_candidate_count"] = True
    write_evidence()
    with pytest.raises(ConfigurationError, match="eligible"):
        provider.verify_promotion({})


def test_request_rejects_unknown_fields_and_excess_context(tmp_path, payload):
    config = make_config(tmp_path)
    payload["surprise"] = True
    with pytest.raises(ValidationError, match="unknown"):
        parse_route_request(payload, config.limits)
    payload.pop("surprise")
    payload["context"]["mission"] = "x" * (config.limits.max_section_chars + 1)
    with pytest.raises(ValidationError, match="exceeds"):
        parse_route_request(payload, config.limits)


def test_deterministic_tool_always_precedes_models(tmp_path, payload):
    config = make_config(tmp_path)
    payload["deterministic_available"] = True
    payload["risk"] = "CRITICAL"
    request = parse_route_request(payload, config.limits)
    plan = RoutingPolicy(config.thresholds, config.limits.max_context_tokens).plan(request)
    assert plan.deterministic is True
    assert plan.roles == ()
    assert request.risk is Risk.CRITICAL


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({}, (Role.TINY, Role.LOCAL_OPS, Role.REASONING, Role.PREMIUM)),
        (
            {"complexity": 0.5, "confidence": 0.7},
            (Role.LOCAL_OPS, Role.REASONING, Role.PREMIUM),
        ),
        ({"complexity": 0.9}, (Role.REASONING, Role.PREMIUM)),
        ({"impact": 0.9}, (Role.REASONING, Role.PREMIUM)),
        ({"confidence": 0.2}, (Role.REASONING, Role.PREMIUM)),
        ({"risk": "HIGH"}, (Role.REASONING, Role.PREMIUM)),
        ({"risk": "CRITICAL"}, (Role.PREMIUM,)),
        ({"task_type": "CODE"}, (Role.CODER, Role.PREMIUM)),
        ({"task_type": "EMBED"}, (Role.EMBEDDING,)),
        ({"task_type": "RERANK"}, (Role.RERANKER,)),
    ],
)
def test_routing_policy_uses_complexity_risk_confidence_and_task(tmp_path, payload, updates, expected):
    config = make_config(tmp_path)
    payload.update(updates)
    request = parse_route_request(payload, config.limits)
    assert RoutingPolicy(config.thresholds, config.limits.max_context_tokens).plan(request).roles == expected


def test_large_but_bounded_context_routes_to_reasoning(tmp_path, payload):
    config = make_config(tmp_path)
    payload["context"]["mission"] = "m" * 5000
    payload["context"]["current_state"] = "s" * 5000
    request = parse_route_request(payload, config.limits)
    assert request.context.estimated_tokens < config.limits.max_context_tokens
    assert RoutingPolicy(config.thresholds, config.limits.max_context_tokens).plan(
        request
    ).roles == (Role.REASONING, Role.PREMIUM)
