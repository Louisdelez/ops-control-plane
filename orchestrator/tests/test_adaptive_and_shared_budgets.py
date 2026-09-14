from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import sqlite3
import threading

import pytest

from ops_orchestrator.adaptive import ALGORITHM_VERSION, candidate_score
from ops_orchestrator.budget import BudgetLedger
from ops_orchestrator.database import Database
from ops_orchestrator.errors import BudgetExceeded, ConfigurationError, ValidationError
from ops_orchestrator.metrics import render_metrics
from ops_orchestrator.service import OrchestratorService

from conftest import FakeProvider, make_config, result


def _ledger(tmp_path, *, account_updates=None):
    config = make_config(tmp_path)
    account = config.provider_account_budget("alibaba")
    if account_updates:
        account = replace(account, **account_updates)
    database = Database(config.database_path)
    database.initialize()
    database.bind_provider_accounts(
        {
            provider.provider_id: provider.account_id
            for provider in config.providers
            if provider.account_id is not None
        }
    )
    return config, database, BudgetLedger(database, {"alibaba": account})


def _reserve(ledger, provider, *, route_id, mission_id, tokens=10, prices=(1, 1)):
    return ledger.reserve(
        route_id=route_id,
        mission_id=mission_id,
        provider=provider,
        model="test-model",
        reason="bounded test",
        input_tokens=tokens,
        max_output_tokens=tokens,
        requested_max_cost_microusd=None,
        prices=prices,
    )


def test_shared_account_daily_cap_is_atomic_across_deployments_and_restarts(tmp_path):
    config, database, ledger = _ledger(
        tmp_path,
        account_updates={"daily_calls": 1, "task_calls": 1},
    )
    first = config.provider("qwen-utility-api")
    second = config.provider("qwen-ops-api")
    barrier = threading.Barrier(2)
    accepted = []
    blocked = []

    def reserve(provider, suffix):
        barrier.wait()
        try:
            accepted.append(
                _reserve(
                    ledger,
                    provider,
                    route_id=f"route-{suffix}",
                    mission_id=f"mission-{suffix}",
                )
            )
        except BudgetExceeded as exc:
            blocked.append(exc)

    threads = [
        threading.Thread(target=reserve, args=(first, "a")),
        threading.Thread(target=reserve, args=(second, "b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(accepted) == 1
    assert len(blocked) == 1
    assert blocked[0].provider == "account:alibaba"
    assert blocked[0].period == "account_daily"
    with database.connect_readonly() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM usage_reservations"
        ).fetchone()[0] == 1

    restarted = BudgetLedger(
        database,
        {
            "alibaba": replace(
                config.provider_account_budget("alibaba"),
                daily_calls=1,
                task_calls=1,
            )
        },
    )
    with pytest.raises(BudgetExceeded, match="account_daily calls"):
        _reserve(
            restarted,
            second,
            route_id="route-after-restart",
            mission_id="mission-after-restart",
        )


def test_shared_account_monthly_cap_includes_another_deployment(tmp_path):
    config, database, ledger = _ledger(
        tmp_path,
        account_updates={"daily_calls": 1, "monthly_calls": 1, "task_calls": 1},
    )
    _reserve(
        ledger,
        config.provider("qwen-utility-api"),
        route_id="route-old-day",
        mission_id="mission-old-day",
    )
    now = datetime.now(timezone.utc)
    other_day = 2 if now.day == 1 else 1
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE usage_reservations SET created_at = ?",
            (f"{now.year:04d}-{now.month:02d}-{other_day:02d}T00:00:00+00:00",),
        )
        connection.commit()

    with pytest.raises(BudgetExceeded) as blocked:
        _reserve(
            ledger,
            config.provider("qwen-ops-api"),
            route_id="route-current-day",
            mission_id="mission-current-day",
        )
    assert blocked.value.provider == "account:alibaba"
    assert blocked.value.period == "account_monthly"
    assert blocked.value.limit == "calls"


@pytest.mark.parametrize(
    ("account_updates", "expected_dimension"),
    [
        ({"task_calls": 1}, "calls"),
        ({"task_calls": 5, "task_tokens": 30}, "tokens"),
        ({"task_calls": 5, "task_cost_microusd": 30}, "cost"),
    ],
)
def test_shared_account_task_caps_cover_calls_tokens_and_cost(
    tmp_path, account_updates, expected_dimension
):
    config, _database, ledger = _ledger(tmp_path, account_updates=account_updates)
    first = config.provider("qwen-utility-api")
    second = config.provider("qwen-ops-api")
    _reserve(
        ledger,
        first,
        route_id="shared-task",
        mission_id="mission-one",
        tokens=10,
        prices=(1_000_000, 1_000_000),
    )
    with pytest.raises(BudgetExceeded) as blocked:
        _reserve(
            ledger,
            second,
            route_id="shared-task",
            mission_id="mission-two",
            tokens=10,
            prices=(1_000_000, 1_000_000),
        )
    assert blocked.value.provider == "account:alibaba"
    assert blocked.value.period == "account_task"
    assert blocked.value.limit == expected_dimension


def test_accounted_provider_fails_closed_without_shared_budget(tmp_path):
    config = make_config(tmp_path)
    database = Database(config.database_path)
    database.initialize()
    ledger = BudgetLedger(database)
    with pytest.raises(ConfigurationError, match="no shared account budget"):
        _reserve(
            ledger,
            config.provider("qwen-utility-api"),
            route_id="route-unconfigured-account",
            mission_id="mission-unconfigured-account",
        )


def test_v1_database_migrates_and_old_usage_is_backfilled_to_account(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES ('schema_version', '1');
            CREATE TABLE usage_reservations (
                call_id TEXT PRIMARY KEY, route_id TEXT NOT NULL,
                mission_id TEXT NOT NULL, provider_id TEXT NOT NULL,
                role TEXT NOT NULL, model TEXT NOT NULL, location TEXT NOT NULL,
                reason TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT,
                status TEXT NOT NULL, reserved_input_tokens INTEGER NOT NULL,
                reserved_output_tokens INTEGER NOT NULL, actual_input_tokens INTEGER,
                actual_output_tokens INTEGER, reserved_cost_microusd INTEGER NOT NULL,
                actual_cost_microusd INTEGER, error_code TEXT
            );
            INSERT INTO usage_reservations VALUES (
                'old-call', 'old-route', 'old-mission', 'qwen-utility-api',
                'ROLE_TINY', 'old-model', 'remote', 'legacy',
                '2026-09-05T00:00:00+00:00', NULL, 'reserved', 1, 1,
                NULL, NULL, 1, NULL, NULL
            );
            """
        )
    database = Database(path)
    database.initialize()
    database.bind_provider_accounts({"qwen-utility-api": "alibaba"})
    with database.connect_readonly() as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "4"
        assert connection.execute(
            "SELECT provider_account_id FROM usage_reservations WHERE call_id = 'old-call'"
        ).fetchone()[0] == "alibaba"
    with pytest.raises(ConfigurationError, match="binding changed"):
        database.bind_provider_accounts({"qwen-utility-api": "deepseek"})
    with pytest.raises(ConfigurationError, match="binding removed"):
        database.bind_provider_accounts({"qwen-utility-api": None})


def test_v3_adaptive_audit_migrates_latency_columns_idempotently(tmp_path):
    path = tmp_path / "v3.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO schema_meta VALUES ('schema_version', '3');
            CREATE TABLE adaptive_score_audit (
                audit_id TEXT PRIMARY KEY,
                route_id TEXT NOT NULL,
                task_type TEXT NOT NULL,
                role TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                provider_account_id TEXT,
                model TEXT NOT NULL,
                estimated_cost_microusd INTEGER NOT NULL,
                attempt_count INTEGER NOT NULL,
                completed_attempts INTEGER NOT NULL,
                uncertain_attempts INTEGER NOT NULL,
                validation_count INTEGER NOT NULL,
                successful_validations INTEGER NOT NULL,
                posterior_quality_milli INTEGER NOT NULL,
                adjustment_ppm INTEGER NOT NULL,
                adaptive_cost_microusd INTEGER NOT NULL,
                rank INTEGER NOT NULL,
                algorithm_version TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(route_id, role, provider_id)
            );
            """
        )

    database = Database(path)
    database.initialize()
    database.initialize()

    with database.connect_readonly() as connection:
        assert connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "4"
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(adaptive_score_audit)"
            ).fetchall()
        }
    assert {
        "quality_adjustment_ppm",
        "observed_latency_ms",
        "latency_sample_count",
        "latency_target_ms",
        "latency_adjustment_ppm",
    } <= columns


def _outcome_payload(decision, mission_id, *, outcome="failed", quality=0.0):
    return {
        "route_id": decision["route_id"],
        "mission_id": mission_id,
        "project_id": "minecraft",
        "outcome": outcome,
        "quality": quality,
        "corrections_required": 1,
        "evidence_kind": "deterministic_test",
    }


def test_validated_outcome_is_bound_to_route_category_and_append_only(tmp_path, payload):
    config = make_config(tmp_path, ["qwen-utility-api"])
    provider = FakeProvider(config.provider("qwen-utility-api"), [result()])
    service = OrchestratorService(
        config, provider_overrides={"qwen-utility-api": provider}
    )
    decision = service.route(payload)
    recorded = service.record_model_outcome(
        _outcome_payload(decision, payload["mission_id"], outcome="succeeded", quality=0.91)
    )
    assert recorded["task_type"] == "ANALYZE"
    assert recorded["provider_id"] == "qwen-utility-api"
    assert recorded["provider_account_id"] == "alibaba"
    assert recorded["quality_milli"] == 910

    analyze = candidate_score(
        service.database,
        task_type="ANALYZE",
        provider_id="qwen-utility-api",
        provider_account_id="alibaba",
        model="test-model",
        estimated_cost_microusd=100,
    )
    plan = candidate_score(
        service.database,
        task_type="PLAN",
        provider_id="qwen-utility-api",
        provider_account_id="alibaba",
        model="test-model",
        estimated_cost_microusd=100,
    )
    assert analyze["validation_count"] == 1
    assert plan["validation_count"] == 0
    metrics = render_metrics(config, service.database, service_up=1)
    assert (
        'ops_orchestrator_validated_model_outcomes_total{outcome="succeeded",'
        'project="minecraft",task_type="ANALYZE"} 1'
    ) in metrics
    assert (
        'ops_orchestrator_account_budget_cost_usd_limit{period="monthly",'
        'provider_account="alibaba"} 50.0'
    ) in metrics
    assert (
        'ops_orchestrator_account_task_calls_limit{provider_account="alibaba"} 5'
    ) in metrics

    with pytest.raises(ValidationError, match="already recorded"):
        service.record_model_outcome(_outcome_payload(decision, payload["mission_id"]))
    spoofed = _outcome_payload(decision, "different-mission")
    with pytest.raises(ValidationError, match="does not match"):
        service.record_model_outcome(spoofed)
    with sqlite3.connect(config.database_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE model_result_validations SET quality_milli = 0"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM model_result_validations")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("outcome", {"not": "a string"}, "outcome"),
        ("quality", float("nan"), "quality"),
        ("corrections_required", True, "corrections_required"),
        ("evidence_kind", ["human_review"], "evidence_kind"),
    ],
)
def test_model_outcome_rejects_malformed_feedback_without_mutation(
    tmp_path, payload, field, value, message
):
    config = make_config(tmp_path, ["qwen-utility-api"])
    provider = FakeProvider(config.provider("qwen-utility-api"), [result()])
    service = OrchestratorService(
        config, provider_overrides={"qwen-utility-api": provider}
    )
    decision = service.route(payload)
    outcome = _outcome_payload(decision, payload["mission_id"])
    outcome[field] = value
    with pytest.raises(ValidationError, match=message):
        service.record_model_outcome(outcome)
    with service.database.connect_readonly() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM model_result_validations"
        ).fetchone()[0] == 0


def test_adaptive_scoring_is_bounded_audited_and_can_prefer_better_value(
    tmp_path, payload
):
    config = make_config(
        tmp_path,
        ["alibaba-deepseek-ops-api", "qwen-ops-api"],
        prices={
            "alibaba-deepseek-ops-api": ("1.000000", "1.000000"),
            "qwen-ops-api": ("1.050000", "1.050000"),
        },
    )
    catalogue_path = config.catalogue_path
    original_digest = hashlib.sha256(catalogue_path.read_bytes()).hexdigest()
    cheap = FakeProvider(
        config.provider("alibaba-deepseek-ops-api"),
        [result() for _ in range(5)],
    )
    better = FakeProvider(config.provider("qwen-ops-api"), [result()])
    service = OrchestratorService(
        config,
        provider_overrides={
            "alibaba-deepseek-ops-api": cheap,
            "qwen-ops-api": better,
        },
    )
    payload.update({"complexity": 0.5, "confidence": 0.7})
    for index in range(5):
        mission_id = f"adaptive-training-{index}"
        payload["mission_id"] = mission_id
        decision = service.route(payload)
        assert decision["selected_provider"] == "alibaba-deepseek-ops-api"
        service.record_model_outcome(_outcome_payload(decision, mission_id))

    payload["mission_id"] = "adaptive-selection"
    selected = service.route(payload)
    assert selected["selected_provider"] == "qwen-ops-api"
    assert cheap.calls == 5
    assert better.calls == 1
    trace = service.mission_trace(payload["mission_id"])
    local_scores = [
        item for item in trace["adaptive_scores"] if item["role"] == "ROLE_LOCAL_OPS"
    ]
    assert local_scores[0]["provider_id"] == "qwen-ops-api"
    penalized = next(
        item
        for item in local_scores
        if item["provider_id"] == "alibaba-deepseek-ops-api"
    )
    assert penalized["validation_count"] == 5
    assert 0 < penalized["adjustment_ppm"] <= 150_000
    assert penalized["algorithm_version"] == ALGORITHM_VERSION
    assert hashlib.sha256(catalogue_path.read_bytes()).hexdigest() == original_digest


def test_adaptive_adjustment_never_exceeds_fifteen_percent(tmp_path):
    config, database, _ledger_instance = _ledger(tmp_path)
    provider = config.provider("qwen-utility-api")
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for index in range(500):
            route_id = f"route-history-{index}"
            connection.execute(
                """
                INSERT INTO mission_runs(
                    route_id, mission_id, project_id, task_type, risk,
                    complexity, impact, requested_role, final_role,
                    final_provider, status, reason, started_at, completed_at,
                    duration_ms
                ) VALUES (?, ?, 'minecraft', 'ANALYZE', 'LOW', 0, 0,
                          'ROLE_TINY', 'ROLE_TINY', ?, 'routed', 'test', ?, ?, 1)
                """,
                (
                    route_id,
                    f"mission-history-{index}",
                    provider.provider_id,
                    "2026-09-05T00:00:00+00:00",
                    "2026-09-05T00:00:01+00:00",
                ),
            )
            connection.execute(
                """
                INSERT INTO usage_reservations(
                    call_id, route_id, mission_id, provider_id,
                    provider_account_id, role, model, location, reason,
                    created_at, completed_at, status, reserved_input_tokens,
                    reserved_output_tokens, actual_input_tokens,
                    actual_output_tokens, reserved_cost_microusd,
                    actual_cost_microusd
                ) VALUES (?, ?, ?, ?, 'alibaba', 'ROLE_TINY', 'test-model',
                          'remote', 'test', ?, ?, 'completed', 1, 1, 1, 1, 1, 1)
                """,
                (
                    f"call-history-{index}",
                    route_id,
                    f"mission-history-{index}",
                    provider.provider_id,
                    "2026-09-05T00:00:00+00:00",
                    "2026-09-05T00:00:01+00:00",
                ),
            )
            connection.execute(
                """
                INSERT INTO model_result_validations VALUES (
                    ?, ?, 'minecraft', 'ANALYZE', 'ROLE_TINY', ?, 'alibaba',
                    'test-model', 'failed', 0, 1000, 'deterministic_test', ?
                )
                """,
                (
                    route_id,
                    f"mission-history-{index}",
                    provider.provider_id,
                    "2026-09-05T00:00:02+00:00",
                ),
            )
        connection.commit()

    score = candidate_score(
        database,
        task_type="ANALYZE",
        provider_id=provider.provider_id,
        provider_account_id="alibaba",
        model="test-model",
        estimated_cost_microusd=1_000_000,
    )
    assert score["validation_count"] == 500
    assert score["adjustment_ppm"] == 150_000
    assert score["adaptive_cost_microusd"] == 1_150_000


def _insert_completed_latency_history(
    database,
    *,
    provider_id,
    provider_account_id,
    duration_ms,
    count=5,
    usage_role="ROLE_LOCAL_OPS",
    fixture_suffix="execution",
):
    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for index in range(count):
            route_id = f"latency-{provider_id}-{fixture_suffix}-{index}"
            mission_id = f"mission-{route_id}"
            completed_second = duration_ms // 1000
            completed_microsecond = (duration_ms % 1000) * 1000
            started_at = "2026-09-05T00:00:00.000000+00:00"
            completed_at = (
                f"2026-09-05T00:00:{completed_second:02d}."
                f"{completed_microsecond:06d}+00:00"
            )
            connection.execute(
                """
                INSERT INTO mission_runs(
                    route_id, mission_id, project_id, task_type, risk,
                    complexity, impact, requested_role, final_role,
                    final_provider, status, reason, started_at, completed_at,
                    duration_ms
                ) VALUES (?, ?, 'minecraft', 'ANALYZE', 'LOW', 0.5, 0.1,
                          ?, ?, ?, 'routed',
                          'latency fixture', ?, ?, ?)
                """,
                (
                    route_id,
                    mission_id,
                    usage_role,
                    usage_role,
                    provider_id,
                    started_at,
                    completed_at,
                    duration_ms,
                ),
            )
            connection.execute(
                """
                INSERT INTO usage_reservations(
                    call_id, route_id, mission_id, provider_id,
                    provider_account_id, role, model, location, reason,
                    created_at, completed_at, status, reserved_input_tokens,
                    reserved_output_tokens, actual_input_tokens,
                    actual_output_tokens, reserved_cost_microusd,
                    actual_cost_microusd
                ) VALUES (?, ?, ?, ?, ?, ?, 'test-model',
                          'remote', 'latency fixture', ?, ?, 'completed',
                          10, 10, 10, 10, 1, 1)
                """,
                (
                    f"call-{route_id}",
                    route_id,
                    mission_id,
                    provider_id,
                    provider_account_id,
                    usage_role,
                    started_at,
                    completed_at,
                ),
            )
        connection.commit()


def test_adaptive_history_keeps_executor_and_verifier_latency_separate(tmp_path):
    config, database, _ledger_instance = _ledger(tmp_path)
    provider = config.provider("qwen-utility-api")
    common = {
        "provider_id": provider.provider_id,
        "provider_account_id": "alibaba",
        "count": 5,
    }
    _insert_completed_latency_history(
        database,
        **common,
        duration_ms=1_000,
        usage_role="ROLE_LOCAL_OPS",
        fixture_suffix="executor",
    )
    _insert_completed_latency_history(
        database,
        **common,
        duration_ms=20_000,
        usage_role="ROLE_VERIFIER",
        fixture_suffix="verifier",
    )

    executor_score = candidate_score(
        database,
        task_type="ANALYZE",
        provider_id=provider.provider_id,
        provider_account_id="alibaba",
        model="test-model",
        estimated_cost_microusd=1_000,
        urgency="URGENT",
        usage_role="ROLE_LOCAL_OPS",
    )
    verifier_score = candidate_score(
        database,
        task_type="ANALYZE",
        provider_id=provider.provider_id,
        provider_account_id="alibaba",
        model="test-model",
        estimated_cost_microusd=1_000,
        urgency="URGENT",
        usage_role="ROLE_VERIFIER",
    )

    assert executor_score["history_usage_role"] == "ROLE_LOCAL_OPS"
    assert executor_score["latency_sample_count"] == 5
    assert executor_score["observed_latency_ms"] == 1_000
    assert verifier_score["history_usage_role"] == "ROLE_VERIFIER"
    assert verifier_score["latency_sample_count"] == 5
    assert verifier_score["observed_latency_ms"] == 20_000


def test_latency_history_below_minimum_is_observed_but_does_not_reorder(tmp_path):
    config, database, _ledger_instance = _ledger(tmp_path)
    provider = config.provider("qwen-utility-api")
    _insert_completed_latency_history(
        database,
        provider_id=provider.provider_id,
        provider_account_id="alibaba",
        duration_ms=30_000,
        count=4,
    )

    score = candidate_score(
        database,
        task_type="ANALYZE",
        provider_id=provider.provider_id,
        provider_account_id="alibaba",
        model="test-model",
        estimated_cost_microusd=1_000,
        urgency="EMERGENCY",
    )

    assert score["observed_latency_ms"] == 30_000
    assert score["latency_sample_count"] == 4
    assert score["latency_evidence_sufficient"] is False
    assert score["latency_adjustment_ppm"] == 0
    assert score["adaptive_cost_microusd"] == 1_000


def test_observed_latency_changes_urgent_order_only_after_bounded_evidence(
    tmp_path, payload
):
    config = make_config(
        tmp_path,
        ["alibaba-deepseek-ops-api", "deepseek-ops-api"],
        prices={
            "alibaba-deepseek-ops-api": ("1.000000", "1.000000"),
            "deepseek-ops-api": ("1.000000", "1.000000"),
        },
    )
    slow = FakeProvider(config.provider("alibaba-deepseek-ops-api"), [result()])
    fast = FakeProvider(config.provider("deepseek-ops-api"), [result()])
    service = OrchestratorService(
        config,
        provider_overrides={
            "alibaba-deepseek-ops-api": slow,
            "deepseek-ops-api": fast,
        },
    )
    _insert_completed_latency_history(
        service.database,
        provider_id="alibaba-deepseek-ops-api",
        provider_account_id="alibaba",
        duration_ms=12_000,
    )
    _insert_completed_latency_history(
        service.database,
        provider_id="deepseek-ops-api",
        provider_account_id="deepseek",
        duration_ms=1_000,
    )
    payload.update(
        {
            "mission_id": "urgent-latency-selection",
            "complexity": 0.5,
            "confidence": 0.7,
            "urgency": "URGENT",
        }
    )

    decision = service.route(payload)

    assert decision["selected_provider"] == "deepseek-ops-api"
    assert slow.calls == 0 and fast.calls == 1
    scores = [
        item
        for item in service.mission_trace(payload["mission_id"])["adaptive_scores"]
        if item["role"] == "ROLE_LOCAL_OPS"
        and item["provider_id"]
        in {"alibaba-deepseek-ops-api", "deepseek-ops-api"}
    ]
    assert [item["provider_id"] for item in scores] == [
        "deepseek-ops-api",
        "alibaba-deepseek-ops-api",
    ]
    by_provider = {item["provider_id"]: item for item in scores}
    assert by_provider["deepseek-ops-api"]["observed_latency_ms"] == 1_000
    assert by_provider["deepseek-ops-api"]["latency_sample_count"] == 5
    assert by_provider["deepseek-ops-api"]["latency_target_ms"] == 4_000
    assert by_provider["deepseek-ops-api"]["latency_adjustment_ppm"] == -75_000
    assert by_provider["alibaba-deepseek-ops-api"]["observed_latency_ms"] == 12_000
    assert by_provider["alibaba-deepseek-ops-api"]["latency_adjustment_ppm"] == 100_000
    assert all(item["algorithm_version"] == ALGORITHM_VERSION for item in scores)
