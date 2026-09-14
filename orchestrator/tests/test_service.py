from __future__ import annotations

from dataclasses import replace
import sqlite3

import pytest

from ops_orchestrator.config import PriceTier
from ops_orchestrator.errors import ConfigurationError, ProviderProtocolError, ValidationError
from ops_orchestrator.models import ProviderResult, Role, parse_route_request
from ops_orchestrator.providers import conservative_input_tokens
from ops_orchestrator.service import OrchestratorService

from conftest import FakeProvider, make_config, result


def _provider(config, provider_id, results, *, available=True):
    return FakeProvider(config.provider(provider_id), list(results), available_value=available)


def test_simple_request_uses_tiny_and_records_valid_trace(tmp_path, payload):
    config = make_config(tmp_path, ["qwen-utility-api"])
    tiny = _provider(config, "qwen-utility-api", [result()])
    service = OrchestratorService(config, provider_overrides={"qwen-utility-api": tiny})
    decision = service.route(payload)
    assert decision["selected_role"] == "ROLE_TINY"
    assert decision["disposition"] == "advisory_ready"
    assert tiny.calls == 1
    trace = service.mission_trace(payload["mission_id"])
    assert trace["chain_valid"] is True
    assert [event["event_type"] for event in trace["events"]] == ["ROUTE", "MODEL_START", "MODEL_RESULT"]


def test_context_content_is_not_persisted(tmp_path, payload):
    secret_marker = "CONTEXT-MUST-NOT-BE-IN-DB-221"
    payload["context"]["mission"] = secret_marker
    config = make_config(tmp_path, ["qwen-utility-api"])
    tiny = _provider(config, "qwen-utility-api", [result(summary="safe")])
    service = OrchestratorService(config, provider_overrides={"qwen-utility-api": tiny})
    service.route(payload)
    assert secret_marker.encode() not in config.database_path.read_bytes()


def test_mission_id_cannot_cross_project_boundary(tmp_path, payload):
    config = make_config(tmp_path)
    service = OrchestratorService(config)
    payload["deterministic_available"] = True
    service.route(payload)

    payload["project_id"] = "infra-shared"
    with pytest.raises(ValidationError, match="already bound to another project"):
        service.route(payload)

    trace = service.mission_trace(payload["mission_id"])
    assert trace["project_id"] == "minecraft"
    assert len(trace["events"]) == 2


def test_startup_rejects_historical_cross_project_mission_collision(tmp_path, payload):
    config = make_config(tmp_path)
    service = OrchestratorService(config)
    payload["deterministic_available"] = True
    service.route(payload)
    with sqlite3.connect(config.database_path) as connection:
        connection.execute(
            """
            INSERT INTO mission_runs(
                route_id, mission_id, project_id, task_type, risk, complexity,
                impact, requested_role, status, reason, started_at
            )
            SELECT 'conflicting-route', mission_id, 'infra-shared', task_type,
                   risk, complexity, impact, requested_role, status, reason,
                   started_at
            FROM mission_runs LIMIT 1
            """
        )
        connection.commit()

    with pytest.raises(ConfigurationError, match="multiple projects"):
        OrchestratorService(config)


def test_insufficient_utility_confidence_escalates_to_ops_api(tmp_path, payload):
    config = make_config(tmp_path, ["qwen-utility-api", "qwen-ops-api"])
    tiny = _provider(config, "qwen-utility-api", [result(status="escalate", confidence=0.3)])
    ops = _provider(config, "qwen-ops-api", [result(confidence=0.91)])
    service = OrchestratorService(
        config,
        provider_overrides={"qwen-utility-api": tiny, "qwen-ops-api": ops},
    )
    decision = service.route(payload)
    assert decision["selected_role"] == "ROLE_LOCAL_OPS"
    assert tiny.calls == 1 and ops.calls == 1
    assert "UNTRUSTED_PRIOR_MODEL_HANDOFFS" in ops.received_requests[0].context.current_state
    assert "ESCALATION" in [item["event_type"] for item in service.mission_trace(payload["mission_id"])["events"]]


def test_ops_providers_escalate_sequentially_at_equal_cost(tmp_path, payload):
    payload.update({"complexity": 0.5, "confidence": 0.7})
    config = make_config(tmp_path, ["qwen-ops-api", "deepseek-ops-api"])
    qwen = _provider(config, "qwen-ops-api", [result(confidence=0.4)])
    deepseek = _provider(config, "deepseek-ops-api", [result(confidence=0.9)])
    service = OrchestratorService(
        config,
        provider_overrides={"qwen-ops-api": qwen, "deepseek-ops-api": deepseek},
    )
    decision = service.route(payload)
    assert decision["selected_provider"] == "deepseek-ops-api"
    assert qwen.calls == 1 and deepseek.calls == 1


def test_provider_order_is_cheapest_first_within_capability_tier(tmp_path, payload):
    payload.update({"complexity": 0.5, "confidence": 0.7})
    config = make_config(
        tmp_path,
        ["alibaba-deepseek-ops-api", "qwen-ops-api", "deepseek-ops-api"],
        prices={
            "alibaba-deepseek-ops-api": ("0.138000", "0.275000"),
            "qwen-ops-api": ("0.276000", "1.101000"),
            "deepseek-ops-api": ("0.440000", "1.320000"),
        },
    )
    alibaba_deepseek = _provider(config, "alibaba-deepseek-ops-api", [result()])
    qwen = _provider(config, "qwen-ops-api", [result()])
    deepseek = _provider(config, "deepseek-ops-api", [result()])
    service = OrchestratorService(
        config,
        provider_overrides={
            "alibaba-deepseek-ops-api": alibaba_deepseek,
            "qwen-ops-api": qwen,
            "deepseek-ops-api": deepseek,
        },
    )
    decision = service.route(payload)
    assert decision["selected_provider"] == "alibaba-deepseek-ops-api"
    assert alibaba_deepseek.calls == 1
    assert qwen.calls == 0 and deepseek.calls == 0


def test_ops_provider_fallback_preserves_capability_and_diversity_order(
    tmp_path, payload
):
    payload.update({"complexity": 0.5, "confidence": 0.7})
    config = make_config(
        tmp_path,
        ["alibaba-deepseek-ops-api", "qwen-ops-api", "deepseek-ops-api"],
        prices={
            "alibaba-deepseek-ops-api": ("0.138000", "0.275000"),
            "qwen-ops-api": ("0.276000", "1.101000"),
            "deepseek-ops-api": ("0.440000", "1.320000"),
        },
    )
    alibaba_deepseek = _provider(
        config, "alibaba-deepseek-ops-api", [RuntimeError("primary failed")]
    )
    qwen = _provider(config, "qwen-ops-api", [RuntimeError("fallback failed")])
    deepseek = _provider(config, "deepseek-ops-api", [result()])
    service = OrchestratorService(
        config,
        provider_overrides={
            "alibaba-deepseek-ops-api": alibaba_deepseek,
            "qwen-ops-api": qwen,
            "deepseek-ops-api": deepseek,
        },
    )

    decision = service.route(payload)

    assert decision["selected_provider"] == "deepseek-ops-api"
    assert alibaba_deepseek.calls == 1
    assert qwen.calls == 1
    assert deepseek.calls == 1


def test_provider_with_estimate_above_price_tiers_falls_back_without_invoke(
    tmp_path, payload
):
    payload.update({"complexity": 0.5, "confidence": 0.7})
    config = make_config(tmp_path, ["qwen-ops-api", "deepseek-ops-api"])
    request = parse_route_request(payload, config.limits)
    source_qwen = config.provider("qwen-ops-api")
    estimated_input = conservative_input_tokens(request, source_qwen.kind)
    invalid_qwen = replace(
        source_qwen,
        price_tiers=(PriceTier(estimated_input - 1, 1, 1),),
    )
    qwen = FakeProvider(invalid_qwen, [result()])
    deepseek = _provider(config, "deepseek-ops-api", [result()])
    service = OrchestratorService(
        config,
        provider_overrides={"qwen-ops-api": qwen, "deepseek-ops-api": deepseek},
    )

    decision = service.route(payload)
    assert decision["selected_provider"] == "deepseek-ops-api"
    assert deepseek.calls == 1
    assert qwen.calls == 0


def test_high_risk_goes_directly_to_reasoning_and_returns_to_control_plane(tmp_path, payload):
    payload.update({"risk": "HIGH", "complexity": 0.2})
    config = make_config(
        tmp_path,
        [
            "qwen-utility-api",
            "qwen-ops-api",
            "deepseek-reasoning",
            "qwen-reasoning-api",
        ],
    )
    utility = _provider(config, "qwen-utility-api", [result()])
    ops = _provider(config, "qwen-ops-api", [result()])
    reasoning = _provider(config, "deepseek-reasoning", [result(confidence=0.88)])
    verifier = _provider(
        config,
        "qwen-reasoning-api",
        [result(confidence=0.91, verification="agree")],
    )
    service = OrchestratorService(
        config,
        provider_overrides={
            "qwen-utility-api": utility,
            "qwen-ops-api": ops,
            "deepseek-reasoning": reasoning,
            "qwen-reasoning-api": verifier,
        },
    )
    decision = service.route(payload)
    assert decision["selected_role"] == "ROLE_REASONING"
    assert decision["next_role"] is None
    assert decision["disposition"] == "plan_ready_for_control_plane_validation"
    assert utility.calls == 0 and ops.calls == 0 and reasoning.calls == 1
    assert verifier.calls == 1
    assert decision["verification"]["status"] == "agreed"
    assert service.mission_trace(payload["mission_id"])["events"][-1]["event_type"] == "RETURN_CONTROL_PLANE"


def test_high_risk_uses_premium_only_after_reasoning_failure(tmp_path, payload):
    payload.update({"risk": "HIGH", "complexity": 0.2})
    config = make_config(
        tmp_path,
        ["deepseek-reasoning", "qwen-reasoning-api"],
    )
    reasoning = _provider(
        config,
        "deepseek-reasoning",
        [
            result(status="escalate", confidence=0.4),
            result(confidence=0.9, verification="agree"),
        ],
    )
    premium = _provider(
        config,
        "qwen-reasoning-api",
        [result(confidence=0.9)],
    )
    service = OrchestratorService(
        config,
        provider_overrides={
            "deepseek-reasoning": reasoning,
            "qwen-reasoning-api": premium,
        },
    )

    decision = service.route(payload)

    assert decision["selected_role"] == "ROLE_PREMIUM"
    assert decision["selected_provider"] == "qwen-reasoning-api"
    assert decision["disposition"] == "plan_ready_for_control_plane_validation"
    assert reasoning.calls == 2 and premium.calls == 1
    assert decision["verification"]["provider_id"] == "deepseek-reasoning"
    assert "UNTRUSTED_PRIOR_MODEL_HANDOFFS" in (
        premium.received_requests[0].context.current_state
    )


def test_high_risk_uses_premium_as_verifier_not_executor_when_reasoning_succeeds(
    tmp_path, payload
):
    payload.update({"risk": "HIGH", "complexity": 0.2})
    config = make_config(
        tmp_path,
        ["deepseek-reasoning", "qwen-reasoning-api"],
    )
    reasoning = _provider(config, "deepseek-reasoning", [result(confidence=0.9)])
    premium = _provider(
        config,
        "qwen-reasoning-api",
        [result(confidence=0.9, verification="agree")],
    )
    service = OrchestratorService(
        config,
        provider_overrides={
            "deepseek-reasoning": reasoning,
            "qwen-reasoning-api": premium,
        },
    )

    decision = service.route(payload)

    assert decision["selected_role"] == "ROLE_REASONING"
    assert reasoning.calls == 1 and premium.calls == 1
    assert decision["executor_result"]["provider_id"] == "deepseek-reasoning"
    assert decision["verification"]["provider_id"] == "qwen-reasoning-api"


def test_critical_reasoning_result_still_requires_human_approval(tmp_path, payload):
    payload.update({"risk": "CRITICAL", "complexity": 0.1})
    config = make_config(tmp_path, ["deepseek-reasoning"])
    reasoning = _provider(config, "deepseek-reasoning", [result(confidence=0.95)])
    service = OrchestratorService(
        config,
        provider_overrides={"deepseek-reasoning": reasoning},
    )
    decision = service.route(payload)
    assert decision["requires_human_approval"] is True
    assert decision["next_role"] is None


def test_code_is_only_a_reviewable_proposal(tmp_path, payload):
    payload["task_type"] = "CODE"
    config = make_config(tmp_path, ["qwen-coder-api"])
    coder_result = ProviderResult(
        status="ok",
        confidence=0.9,
        summary="proposal generated",
        proposal="#!/bin/sh\n# review required\n",
        input_tokens=50,
        output_tokens=20,
        raw_usage_known=True,
    )
    coder = _provider(config, "qwen-coder-api", [coder_result])
    service = OrchestratorService(
        config,
        provider_overrides={"qwen-coder-api": coder},
    )
    decision = service.route(payload)
    assert decision["disposition"] == "code_proposal_review_tests_git_required"
    assert decision["requires_human_approval"] is True


def test_provider_protocol_failure_fails_closed(tmp_path, payload):
    config = make_config(tmp_path, ["qwen-utility-api"])
    tiny = _provider(config, "qwen-utility-api", [ProviderProtocolError("bad envelope")])
    service = OrchestratorService(config, provider_overrides={"qwen-utility-api": tiny})
    decision = service.route(payload)
    assert decision["disposition"] == "human_escalation_required"
    assert decision["requires_human_approval"] is True
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute("SELECT status FROM usage_reservations").fetchone()[0] == "uncertain"


def test_deterministic_route_makes_no_provider_call(tmp_path, payload):
    payload["deterministic_available"] = True
    config = make_config(tmp_path, ["qwen-utility-api"])
    tiny = _provider(config, "qwen-utility-api", [result()])
    service = OrchestratorService(config, provider_overrides={"qwen-utility-api": tiny})
    decision = service.route(payload)
    assert decision["disposition"] == "deterministic_tool_required"
    assert decision["selected_role"] is None
    assert tiny.calls == 0


def test_remote_consent_is_required_before_api_invoke(tmp_path, payload):
    payload["remote_allowed"] = False
    config = make_config(tmp_path, ["qwen-utility-api"])
    utility = _provider(config, "qwen-utility-api", [result()])
    service = OrchestratorService(
        config,
        provider_overrides={"qwen-utility-api": utility},
    )
    decision = service.route(payload)
    assert decision["disposition"] == "human_escalation_required"
    assert utility.calls == 0


def test_no_provider_is_degraded_and_human_only(tmp_path, payload):
    config = make_config(tmp_path)
    service = OrchestratorService(config)
    health = service.health()
    assert health["status"] == "degraded"
    assert health["mode"] == "api-first"
    assert health["provider_availability_scope"] == "configuration-and-secret-only"
    assert health["provider_network_probe"] is False
    decision = service.route(payload)
    assert decision["disposition"] == "human_escalation_required"
    assert decision["degraded"] is True


def test_health_contains_non_finite_dynamic_price_as_provider_unavailable(tmp_path):
    config = make_config(tmp_path, ["embedding-api-backup"])
    source = config.provider("embedding-api-backup")
    dynamic = replace(
        source,
        input_price_per_million=None,
        input_price_env="EMBEDDING_INPUT_PRICE_USD_PER_MILLION",
    )
    config.providers_by_role[Role.EMBEDDING] = (dynamic,)
    key = tmp_path / "embedding-api-key"
    key.write_text("test-only-provider-key", encoding="utf-8")
    key.chmod(0o600)
    service = OrchestratorService(
        config,
        environment={
            "EMBEDDING_API_BASE_URL": "https://embedding.example.invalid",
            "EMBEDDING_API_KEY_FILE": str(key),
            "EMBEDDING_INPUT_PRICE_USD_PER_MILLION": "NaN",
        },
    )

    health = service.health()

    embedding = next(
        provider
        for provider in health["providers"]
        if provider["id"] == "embedding-api-backup"
    )
    assert embedding["available"] is False
    assert "must be finite" in embedding["reason"]
    assert health["status"] == "degraded"


def test_trace_tampering_is_detected(tmp_path, payload):
    config = make_config(tmp_path, ["qwen-utility-api"])
    tiny = _provider(config, "qwen-utility-api", [result()])
    service = OrchestratorService(config, provider_overrides={"qwen-utility-api": tiny})
    service.route(payload)
    with sqlite3.connect(config.database_path) as connection:
        connection.execute("UPDATE model_trace SET reason = 'tampered' WHERE sequence = 2")
        connection.commit()
    assert service.mission_trace(payload["mission_id"])["chain_valid"] is False
