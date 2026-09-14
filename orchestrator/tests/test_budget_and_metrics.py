from __future__ import annotations

import argparse
from dataclasses import replace
import sqlite3

import pytest

import ops_orchestrator.cli as cli
from ops_orchestrator.budget import estimated_cost_microusd
from ops_orchestrator.cli import run as run_cli
from ops_orchestrator.config import PriceTier
from ops_orchestrator.database import Database
from ops_orchestrator.errors import ValidationError
from ops_orchestrator.metrics import render_metrics, write_metrics
from ops_orchestrator.models import parse_route_request
from ops_orchestrator.providers import conservative_input_tokens
from ops_orchestrator.service import OrchestratorService

from conftest import FakeProvider, make_config, result


def test_durable_daily_call_budget_blocks_second_call(tmp_path, payload):
    config = make_config(
        tmp_path,
        ["qwen-utility-api"],
        budgets={"qwen-utility-api": {"daily_calls": 1, "monthly_calls": 1}},
    )
    tiny = FakeProvider(config.provider("qwen-utility-api"), [result(), result()])
    first = OrchestratorService(config, provider_overrides={"qwen-utility-api": tiny})
    assert first.route(payload)["selected_provider"] == "qwen-utility-api"
    payload["mission_id"] = "mission-124"
    # A fresh service proves the limit is persistent rather than in-memory.
    second = OrchestratorService(config, provider_overrides={"qwen-utility-api": tiny})
    decision = second.route(payload)
    assert decision["disposition"] == "human_escalation_required"
    assert tiny.calls == 1
    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM usage_reservations").fetchone()[0] == 1


def test_request_cost_cap_blocks_remote_before_network(tmp_path, payload):
    payload.update({"risk": "HIGH", "requested_max_cost_usd": "0.000001"})
    key = tmp_path / "key"
    key.write_text("test-only-key", encoding="utf-8")
    key.chmod(0o600)
    config = make_config(tmp_path, ["deepseek-reasoning"])
    provider = FakeProvider(config.provider("deepseek-reasoning"), [result()])
    service = OrchestratorService(
        config,
        environment={"DEEPSEEK_API_KEY_FILE": str(key)},
        provider_overrides={"deepseek-reasoning": provider},
    )
    decision = service.route(payload)
    assert decision["disposition"] == "human_escalation_required"
    assert provider.calls == 0


def test_observed_usage_above_reservation_is_retained_as_uncertain_cost(
    tmp_path, payload
):
    config = make_config(tmp_path, ["qwen-utility-api"])
    request = parse_route_request(payload, config.limits)
    source_provider = config.provider("qwen-utility-api")
    estimated_input = conservative_input_tokens(request, source_provider.kind)
    observed_input = estimated_input + 17
    observed_output = config.limits.max_output_tokens + 3
    initial_prices = (1_000_000, 1_000_000)
    observed_prices = (10_000_000, 10_000_000)
    provider_config = replace(
        source_provider,
        price_tiers=(
            PriceTier(estimated_input, *initial_prices),
            PriceTier(observed_input, *observed_prices),
        ),
    )
    initial_cost = estimated_cost_microusd(
        estimated_input, config.limits.max_output_tokens, *initial_prices
    )
    provider = FakeProvider(
        provider_config,
        [result(input_tokens=observed_input, output_tokens=observed_output)],
    )
    service = OrchestratorService(
        config,
        provider_overrides={"qwen-utility-api": provider},
    )

    decision = service.route(payload)
    assert decision["disposition"] == "human_escalation_required"
    assert provider.calls == 1
    with sqlite3.connect(config.database_path) as connection:
        row = connection.execute(
            "SELECT status, error_code, reserved_input_tokens, "
            "reserved_output_tokens, reserved_cost_microusd "
            "FROM usage_reservations"
        ).fetchone()

    assert row is not None
    status, error_code, retained_input, retained_output, retained_cost = row
    assert status == "uncertain"
    assert error_code == "ProviderProtocolError"
    assert retained_input == observed_input
    assert retained_output == observed_output
    assert retained_cost == estimated_cost_microusd(
        observed_input, observed_output, *observed_prices
    )
    assert retained_cost > initial_cost


def test_completed_usage_is_repriced_from_observed_input_tier(tmp_path, payload):
    config = make_config(tmp_path, ["qwen-utility-api"])
    request = parse_route_request(payload, config.limits)
    source_provider = config.provider("qwen-utility-api")
    estimated_input = conservative_input_tokens(request, source_provider.kind)
    lower_prices = (1_000_000, 2_000_000)
    estimated_prices = (10_000_000, 20_000_000)
    provider_config = replace(
        source_provider,
        price_tiers=(
            PriceTier(100, *lower_prices),
            PriceTier(1_000_000, *estimated_prices),
        ),
    )
    provider = FakeProvider(
        provider_config,
        [result(input_tokens=50, output_tokens=20)],
    )
    service = OrchestratorService(
        config,
        provider_overrides={"qwen-utility-api": provider},
    )

    decision = service.route(payload)
    assert decision["selected_provider"] == "qwen-utility-api"
    with sqlite3.connect(config.database_path) as connection:
        row = connection.execute(
            "SELECT status, reserved_cost_microusd, actual_cost_microusd "
            "FROM usage_reservations"
        ).fetchone()

    assert row is not None
    status, reserved_cost, actual_cost = row
    assert status == "completed"
    assert reserved_cost == estimated_cost_microusd(
        estimated_input, config.limits.max_output_tokens, *estimated_prices
    )
    assert actual_cost == estimated_cost_microusd(50, 20, *lower_prices)
    assert actual_cost < reserved_cost


def test_budget_dashboard_exposes_enforced_shared_provider_account_cap(
    tmp_path, payload
):
    config = make_config(tmp_path, ["qwen-utility-api"])
    tiny = FakeProvider(config.provider("qwen-utility-api"), [result()])
    service = OrchestratorService(
        config,
        provider_overrides={"qwen-utility-api": tiny},
    )
    service.route(payload)

    summary = service.budget_summary()
    alibaba = next(
        account
        for account in summary["accounts"]
        if account["provider_account_id"] == "alibaba"
    )
    qwen = next(
        provider
        for provider in summary["providers"]
        if provider["provider"] == "qwen-utility-api"
    )
    assert alibaba["usage"]["monthly"]["cost_microusd"] == qwen["usage"]["monthly"]["cost"]
    assert set(alibaba["deployments"]) == {
        "qwen-utility-api",
        "alibaba-deepseek-ops-api",
        "qwen-ops-api",
        "qwen-reasoning-api",
        "qwen-coder-api",
    }
    assert alibaba["hard_shared_cap"] is True
    assert alibaba["enforcement_scope"] == "provider_account_atomic"
    assert alibaba["summed_deployment_limits"]["monthly"] == {
        "calls": 110000,
        "tokens": 200000000,
        "cost_microusd": 50_000_000,
    }
    assert qwen["remaining"]["monthly"]["cost_microusd"] >= 0


def test_prometheus_export_contains_ai_observability(tmp_path, payload):
    config = make_config(tmp_path, ["qwen-utility-api"])
    tiny = FakeProvider(config.provider("qwen-utility-api"), [result()])
    service = OrchestratorService(config, provider_overrides={"qwen-utility-api": tiny})
    service.route(payload)
    for signal in ("mission_succeeded", "tool_error", "memory_search"):
        service.record_signal(signal, payload["project_id"])
    trace = service.mission_trace(payload["mission_id"])
    assert trace["chain_valid"] is True
    content = render_metrics(config, service.database)
    assert "ops_orchestrator_missions_received_total 1" in content
    assert "ops_orchestrator_model_calls_total" in content
    assert "ops_orchestrator_provider_tokens_total" in content
    assert "ops_orchestrator_provider_cost_usd_total" in content
    assert 'ops_orchestrator_mission_outcomes_total{outcome="succeeded",project="minecraft"} 1' in content
    assert 'ops_orchestrator_tool_errors_total{project="minecraft"} 1' in content
    assert 'ops_orchestrator_memory_searches_total{project="minecraft"} 1' in content
    write_metrics(config.metrics_path, content)
    assert config.metrics_path.read_text(encoding="utf-8") == content


def test_signal_project_metric_label_is_a_bounded_safe_identifier(tmp_path):
    config = make_config(tmp_path)
    service = OrchestratorService(config)

    for project_id in ("", "x" * 129, "unsafe project", "line\nbreak"):
        with pytest.raises(ValidationError, match="safe bounded identifier"):
            service.record_signal("tool_error", project_id)

    with sqlite3.connect(config.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM metric_counters WHERE metric_key = 'tool_errors'"
        ).fetchone()[0] == 0


def test_cli_signal_submits_exact_project_scoped_payload(monkeypatch):
    calls = []

    def fake_request(socket_path, method, path, payload=None, *, timeout=240):
        calls.append((socket_path, method, path, payload, timeout))
        return {"status": "recorded"}

    monkeypatch.setattr(cli, "_request", fake_request)
    arguments = cli.build_parser().parse_args(
        [
            "--socket",
            "/run/test-orchestrator.sock",
            "signal",
            "mission_succeeded",
            "--project-id",
            "minecraft",
        ]
    )

    assert run_cli(arguments) == {"status": "recorded"}
    assert calls == [
        (
            "/run/test-orchestrator.sock",
            "POST",
            "/v1/signals",
            {"signal": "mission_succeeded", "project_id": "minecraft"},
            240,
        )
    ]


def test_metrics_database_connection_is_read_only(tmp_path):
    config = make_config(tmp_path, ["qwen-utility-api"])
    service = OrchestratorService(config)
    service.database.initialize()
    with service.database.connect_readonly() as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute(
                "INSERT INTO metric_counters(metric_key, labels_json, value) "
                "VALUES ('forbidden', '{}', 1)"
            )


def test_export_metrics_never_initializes_or_mutates_database(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    OrchestratorService(config)

    def forbidden_initialize(self):
        raise AssertionError("metrics export must not initialize a live database")

    monkeypatch.setattr(Database, "initialize", forbidden_initialize)
    calls = []

    def snapshot_request(socket_path, method, path, payload=None, *, timeout=240):
        calls.append((socket_path, method, path, payload, timeout))
        return {"metrics": "ops_orchestrator_up 1\n"}

    monkeypatch.setattr(cli, "_request", snapshot_request)
    target = tmp_path / "export" / "orchestrator.prom"
    result = run_cli(
        argparse.Namespace(
            command="export-metrics",
            socket=str(config.socket_path),
            config=str(tmp_path / "config.json"),
            target=str(target),
        )
    )
    assert result["status"] == "written"
    assert target.read_text(encoding="utf-8") == "ops_orchestrator_up 1\n"
    assert calls == [(str(config.socket_path), "GET", "/v1/metrics", None, 10)]
