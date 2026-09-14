from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import socket
import stat
import tempfile

from .config import AppConfig
from .database import Database


_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    if any(not _LABEL_NAME.fullmatch(key) for key in labels):
        raise ValueError("invalid internal metric label name")
    return "{" + ",".join(f'{key}="{_escape(str(value))}"' for key, value in sorted(labels.items())) + "}"


def _line(name: str, value: int | float, labels: dict[str, str] | None = None) -> str:
    if not _METRIC_NAME.fullmatch(name):
        raise ValueError("invalid internal metric name")
    return f"{name}{_labels(labels or {})} {value}"


def _service_is_listening(path: Path) -> int:
    try:
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode):
            return 0
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(0.5)
        try:
            client.connect(str(path))
        finally:
            client.close()
        return 1
    except OSError:
        return 0


def render_metrics(
    config: AppConfig,
    database: Database,
    *,
    service_up: int | None = None,
) -> str:
    if service_up not in {None, 0, 1}:
        raise ValueError("service_up must be zero, one or unspecified")
    lines = [
        "# HELP ops_orchestrator_up Whether the orchestrator Unix API accepts connections.",
        "# TYPE ops_orchestrator_up gauge",
        _line(
            "ops_orchestrator_up",
            _service_is_listening(config.socket_path) if service_up is None else service_up,
        ),
        "# HELP ops_orchestrator_database_readable Whether the durable metrics database is readable.",
        "# TYPE ops_orchestrator_database_readable gauge",
        _line("ops_orchestrator_database_readable", 1),
    ]
    with database.connect_readonly() as connection:
        counter_rows = connection.execute(
            "SELECT metric_key, labels_json, value FROM metric_counters ORDER BY metric_key, labels_json"
        ).fetchall()
        run_rows = connection.execute(
            "SELECT status, COUNT(*) AS total, COALESCE(SUM(duration_ms), 0) AS duration_ms FROM mission_runs GROUP BY status"
        ).fetchall()
        usage_rows = connection.execute(
            """
            SELECT provider_id, role, model, location, status, COUNT(*) AS calls,
                   COALESCE(SUM(CASE WHEN status = 'completed' THEN actual_input_tokens ELSE reserved_input_tokens END), 0) AS input_tokens,
                   COALESCE(SUM(CASE WHEN status = 'completed' THEN actual_output_tokens ELSE reserved_output_tokens END), 0) AS output_tokens,
                   COALESCE(SUM(CASE WHEN status = 'completed' THEN actual_cost_microusd ELSE reserved_cost_microusd END), 0) AS cost_microusd
            FROM usage_reservations
            GROUP BY provider_id, role, model, location, status
            ORDER BY provider_id, model, status
            """
        ).fetchall()
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d%")
        month = datetime.now(timezone.utc).strftime("%Y-%m%")
        period_rows = {}
        account_period_rows = {}
        for period, pattern in (("daily", day), ("monthly", month)):
            period_rows[period] = {
                row["provider_id"]: row
                for row in connection.execute(
                    """
                    SELECT provider_id, COUNT(*) AS calls,
                           COALESCE(SUM(CASE WHEN status = 'completed'
                               THEN actual_input_tokens + actual_output_tokens
                               ELSE reserved_input_tokens + reserved_output_tokens END), 0) AS tokens,
                           COALESCE(SUM(CASE WHEN status = 'completed'
                               THEN actual_cost_microusd ELSE reserved_cost_microusd END), 0) AS cost_microusd
                    FROM usage_reservations WHERE created_at LIKE ? GROUP BY provider_id
                    """,
                    (pattern,),
                ).fetchall()
            }
            account_period_rows[period] = {
                row["provider_account_id"]: row
                for row in connection.execute(
                    """
                    SELECT provider_account_id, COUNT(*) AS calls,
                           COALESCE(SUM(CASE WHEN status = 'completed'
                               THEN actual_input_tokens + actual_output_tokens
                               ELSE reserved_input_tokens + reserved_output_tokens END), 0) AS tokens,
                           COALESCE(SUM(CASE WHEN status = 'completed'
                               THEN actual_cost_microusd ELSE reserved_cost_microusd END), 0) AS cost_microusd
                    FROM usage_reservations
                    WHERE provider_account_id IS NOT NULL AND created_at LIKE ?
                    GROUP BY provider_account_id
                    """,
                    (pattern,),
                ).fetchall()
            }
    mapping = {
        "missions_received": "ops_orchestrator_missions_received_total",
        "routing_outcomes": "ops_orchestrator_routing_outcomes_total",
        "model_calls": "ops_orchestrator_model_calls_total",
        "escalations": "ops_orchestrator_escalations_total",
        "human_escalations": "ops_orchestrator_human_escalations_total",
        "budget_blocked": "ops_orchestrator_budget_blocked_total",
        "mission_outcomes": "ops_orchestrator_mission_outcomes_total",
        "tool_errors": "ops_orchestrator_tool_errors_total",
        "memory_searches": "ops_orchestrator_memory_searches_total",
        "validated_model_outcomes": "ops_orchestrator_validated_model_outcomes_total",
        "routing_duration_milliseconds": "ops_orchestrator_routing_duration_milliseconds_total",
    }
    for row in counter_rows:
        metric_name = mapping.get(row["metric_key"])
        if metric_name is None:
            continue
        labels = json.loads(row["labels_json"])
        lines.append(_line(metric_name, int(row["value"]), labels))
    for row in run_rows:
        labels = {"status": row["status"]}
        lines.append(_line("ops_orchestrator_routes_total", int(row["total"]), labels))
        lines.append(_line("ops_orchestrator_route_duration_seconds_sum", int(row["duration_ms"]) / 1000.0, labels))
        lines.append(_line("ops_orchestrator_route_duration_seconds_count", int(row["total"]), labels))
    for row in usage_rows:
        labels = {
            "provider": row["provider_id"], "role": row["role"], "model": row["model"],
            "location": row["location"], "status": row["status"],
        }
        lines.append(_line("ops_orchestrator_provider_calls_total", int(row["calls"]), labels))
        lines.append(_line("ops_orchestrator_provider_tokens_total", int(row["input_tokens"]), {**labels, "direction": "input"}))
        lines.append(_line("ops_orchestrator_provider_tokens_total", int(row["output_tokens"]), {**labels, "direction": "output"}))
        lines.append(_line("ops_orchestrator_provider_cost_usd_total", int(row["cost_microusd"]) / 1_000_000.0, labels))
    for provider in config.providers:
        limits = provider.budget
        for period, call_limit, token_limit, cost_limit in (
            ("daily", limits.daily_calls, limits.daily_tokens, limits.daily_cost_microusd),
            ("monthly", limits.monthly_calls, limits.monthly_tokens, limits.monthly_cost_microusd),
        ):
            row = period_rows[period].get(provider.provider_id)
            labels = {"provider": provider.provider_id, "period": period}
            lines.append(_line("ops_orchestrator_budget_calls", int(row["calls"]) if row else 0, labels))
            lines.append(_line("ops_orchestrator_budget_calls_limit", call_limit, labels))
            lines.append(_line("ops_orchestrator_budget_tokens", int(row["tokens"]) if row else 0, labels))
            lines.append(_line("ops_orchestrator_budget_tokens_limit", token_limit, labels))
            lines.append(_line("ops_orchestrator_budget_cost_usd", (int(row["cost_microusd"]) if row else 0) / 1_000_000.0, labels))
            lines.append(_line("ops_orchestrator_budget_cost_usd_limit", cost_limit / 1_000_000.0, labels))
    for account_id, limits in sorted(config.provider_account_budgets.items()):
        for period, call_limit, token_limit, cost_limit in (
            (
                "daily",
                limits.daily_calls,
                limits.daily_tokens,
                limits.daily_cost_microusd,
            ),
            (
                "monthly",
                limits.monthly_calls,
                limits.monthly_tokens,
                limits.monthly_cost_microusd,
            ),
        ):
            row = account_period_rows[period].get(account_id)
            labels = {"provider_account": account_id, "period": period}
            lines.append(
                _line(
                    "ops_orchestrator_account_budget_calls",
                    int(row["calls"]) if row else 0,
                    labels,
                )
            )
            lines.append(
                _line("ops_orchestrator_account_budget_calls_limit", call_limit, labels)
            )
            lines.append(
                _line(
                    "ops_orchestrator_account_budget_tokens",
                    int(row["tokens"]) if row else 0,
                    labels,
                )
            )
            lines.append(
                _line("ops_orchestrator_account_budget_tokens_limit", token_limit, labels)
            )
            lines.append(
                _line(
                    "ops_orchestrator_account_budget_cost_usd",
                    (int(row["cost_microusd"]) if row else 0) / 1_000_000.0,
                    labels,
                )
            )
            lines.append(
                _line(
                    "ops_orchestrator_account_budget_cost_usd_limit",
                    cost_limit / 1_000_000.0,
                    labels,
                )
            )
        task_labels = {"provider_account": account_id}
        lines.append(
            _line(
                "ops_orchestrator_account_task_calls_limit",
                limits.task_calls,
                task_labels,
            )
        )
        lines.append(
            _line(
                "ops_orchestrator_account_task_tokens_limit",
                limits.task_tokens,
                task_labels,
            )
        )
        lines.append(
            _line(
                "ops_orchestrator_account_task_cost_usd_limit",
                limits.task_cost_microusd / 1_000_000.0,
                task_labels,
            )
        )
    lines.append(_line("ops_orchestrator_metrics_generated_timestamp_seconds", int(datetime.now(timezone.utc).timestamp())))
    return "\n".join(lines) + "\n"


def write_metrics(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    parent_info = path.parent.lstat()
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise RuntimeError("metrics parent is not a real directory")
    if path.exists():
        target_info = path.lstat()
        if stat.S_ISLNK(target_info.st_mode) or not stat.S_ISREG(target_info.st_mode):
            raise RuntimeError("metrics target is not a regular file")
    descriptor, temporary = tempfile.mkstemp(prefix=".ops-orchestrator.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
