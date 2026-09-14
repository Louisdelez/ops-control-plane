from __future__ import annotations

from dataclasses import replace

import pytest

from ops_orchestrator.capability_routing import capability_check, requirements_for
from ops_orchestrator.errors import ConfigurationError
from ops_orchestrator.models import Role, parse_route_request
from ops_orchestrator.service import OrchestratorService

from conftest import FakeProvider, make_config, result


def test_structured_task_facts_produce_bounded_brand_neutral_requirements(
    tmp_path, payload
):
    config = make_config(tmp_path)
    request = parse_route_request(payload, config.limits)

    requirements = requirements_for(request)

    assert requirements == {
        "factuality": 5,
        "general": 4,
        "instructions": 5,
        "reasoning": 4,
        "reliability": 5,
    }
    assert all(0 <= value <= 10 for value in requirements.values())


def test_capability_check_reports_only_hard_minimum_deficits():
    passed, deficits, margin = capability_check(
        {"code": 7, "tools": 9, "reliability": 8},
        {"code": 8, "tools": 7, "reliability": 8},
    )

    assert passed is False
    assert deficits == {"code": 1}
    assert margin == 2


def test_incapable_cheaper_provider_is_rejected_before_egress(tmp_path, payload):
    payload.update({"complexity": 0.5, "confidence": 0.7})
    source = make_config(
        tmp_path,
        ["qwen-ops-api", "deepseek-ops-api"],
        prices={
            "qwen-ops-api": ("0.000001", "0.000001"),
            "deepseek-ops-api": ("1.000000", "2.000000"),
        },
    )
    weakened = replace(source.provider("qwen-ops-api"), capability_profile="utility_flash")
    providers = dict(source.providers_by_role)
    providers[Role.LOCAL_OPS] = tuple(
        weakened if item.provider_id == weakened.provider_id else item
        for item in providers[Role.LOCAL_OPS]
    )
    config = replace(source, providers_by_role=providers)
    cheap = FakeProvider(weakened, [result()])
    capable_config = config.provider("deepseek-ops-api")
    capable = FakeProvider(capable_config, [result()])
    service = OrchestratorService(
        config,
        provider_overrides={
            "qwen-ops-api": cheap,
            "deepseek-ops-api": capable,
        },
    )

    decision = service.route(payload)

    assert decision["selected_provider"] == "deepseek-ops-api"
    assert cheap.calls == 0
    assert capable.calls == 1
    rejected = [
        item
        for item in service.mission_trace(payload["mission_id"])["events"]
        if item["event_type"] == "CAPABILITY_REJECT"
    ]
    assert len(rejected) == 1
    assert rejected[0]["provider_id"] == "qwen-ops-api"
    assert "profile=utility_flash" in rejected[0]["reason"]


def test_critical_task_fails_closed_when_no_profile_meets_reliability(tmp_path, payload):
    payload.update({"risk": "CRITICAL", "complexity": 0.1})
    config = make_config(
        tmp_path,
        ["deepseek-reasoning", "qwen-reasoning-api"],
    )
    deepseek = FakeProvider(config.provider("deepseek-reasoning"), [result()])
    qwen = FakeProvider(config.provider("qwen-reasoning-api"), [result()])
    service = OrchestratorService(
        config,
        provider_overrides={
            "deepseek-reasoning": deepseek,
            "qwen-reasoning-api": qwen,
        },
    )

    decision = service.route(payload)

    assert decision["disposition"] == "human_escalation_required"
    assert deepseek.calls == 0 and qwen.calls == 0
    assert [
        item["provider_id"]
        for item in service.mission_trace(payload["mission_id"])["events"]
        if item["event_type"] == "CAPABILITY_REJECT"
    ] == ["qwen-reasoning-api"]


def test_runtime_rejects_unknown_capability_profile(tmp_path):
    source = make_config(tmp_path)
    providers = dict(source.providers_by_role)
    providers[Role.TINY] = (
        replace(providers[Role.TINY][0], capability_profile="not_catalogued"),
    )

    with pytest.raises(ConfigurationError, match="unknown capability profile"):
        OrchestratorService(replace(source, providers_by_role=providers))
