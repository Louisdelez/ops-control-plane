from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
from typing import Mapping
import uuid

from .config import AppConfig, ProviderAccountBudget, ProviderConfig
from .database import Database, utc_now
from .errors import BudgetExceeded, ConfigurationError


@dataclass(frozen=True)
class Reservation:
    call_id: str
    reserved_input_tokens: int
    reserved_output_tokens: int
    reserved_cost_microusd: int


def estimated_cost_microusd(
    input_tokens: int,
    output_tokens: int,
    input_price_microusd_per_million: int,
    output_price_microusd_per_million: int,
) -> int:
    amount = (
        Decimal(input_tokens) * Decimal(input_price_microusd_per_million)
        + Decimal(output_tokens) * Decimal(output_price_microusd_per_million)
    ) / Decimal(1_000_000)
    return int(amount.quantize(Decimal("1"), rounding=ROUND_CEILING))


class BudgetLedger:
    def __init__(
        self,
        database: Database,
        account_budgets: Mapping[str, ProviderAccountBudget] | None = None,
    ):
        self.database = database
        self.account_budgets = dict(account_budgets or {})

    @classmethod
    def from_config(cls, database: Database, config: AppConfig) -> BudgetLedger:
        return cls(database, config.provider_account_budgets)

    def reserve(
        self,
        *,
        route_id: str,
        mission_id: str,
        provider: ProviderConfig,
        model: str,
        reason: str,
        input_tokens: int,
        max_output_tokens: int,
        requested_max_cost_microusd: int | None,
        prices: tuple[int, int],
        usage_role: str | None = None,
    ) -> Reservation:
        if usage_role is not None and usage_role != "ROLE_VERIFIER":
            raise ValueError("usage role override is invalid")
        stored_role = usage_role or provider.role.value
        reserved_cost = estimated_cost_microusd(input_tokens, max_output_tokens, *prices)
        now = datetime.now(timezone.utc)
        day_prefix = now.strftime("%Y-%m-%d")
        month_prefix = now.strftime("%Y-%m")
        call_id = str(uuid.uuid4())
        limits = provider.budget
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            day = self._aggregate(connection, provider.provider_id, f"{day_prefix}%")
            month = self._aggregate(connection, provider.provider_id, f"{month_prefix}%")
            mission = self._aggregate(connection, provider.provider_id, "%", mission_id=mission_id)
            route_cost_row = connection.execute(
                """
                SELECT COALESCE(SUM(CASE WHEN status = 'completed'
                           THEN actual_cost_microusd ELSE reserved_cost_microusd END), 0) AS cost
                FROM usage_reservations WHERE route_id = ?
                """,
                (route_id,),
            ).fetchone()
            route_cost = int(route_cost_row["cost"])
            if requested_max_cost_microusd is not None and route_cost + reserved_cost > requested_max_cost_microusd:
                connection.rollback()
                raise BudgetExceeded(provider.provider_id, "request", "max_cost")
            checks = (
                (day["calls"] + 1, limits.daily_calls, "daily", "calls"),
                (month["calls"] + 1, limits.monthly_calls, "monthly", "calls"),
                (day["tokens"] + input_tokens + max_output_tokens, limits.daily_tokens, "daily", "tokens"),
                (month["tokens"] + input_tokens + max_output_tokens, limits.monthly_tokens, "monthly", "tokens"),
                (day["cost"] + reserved_cost, limits.daily_cost_microusd, "daily", "cost"),
                (month["cost"] + reserved_cost, limits.monthly_cost_microusd, "monthly", "cost"),
                (mission["cost"] + reserved_cost, limits.mission_cost_microusd, "mission", "cost"),
            )
            for prospective, limit, period, dimension in checks:
                if prospective > limit:
                    connection.rollback()
                    raise BudgetExceeded(provider.provider_id, period, dimension)
            if provider.account_id is not None:
                try:
                    account_limits = self.account_budgets[provider.account_id]
                except KeyError as exc:
                    connection.rollback()
                    raise ConfigurationError(
                        f"provider {provider.provider_id} has no shared account budget"
                    ) from exc
                account_day = self._aggregate_account(
                    connection, provider.account_id, f"{day_prefix}%"
                )
                account_month = self._aggregate_account(
                    connection, provider.account_id, f"{month_prefix}%"
                )
                account_task = self._aggregate_account(
                    connection, provider.account_id, "%", route_id=route_id
                )
                account_checks = (
                    (
                        account_day["calls"] + 1,
                        account_limits.daily_calls,
                        "account_daily",
                        "calls",
                    ),
                    (
                        account_month["calls"] + 1,
                        account_limits.monthly_calls,
                        "account_monthly",
                        "calls",
                    ),
                    (
                        account_day["tokens"] + input_tokens + max_output_tokens,
                        account_limits.daily_tokens,
                        "account_daily",
                        "tokens",
                    ),
                    (
                        account_month["tokens"] + input_tokens + max_output_tokens,
                        account_limits.monthly_tokens,
                        "account_monthly",
                        "tokens",
                    ),
                    (
                        account_day["cost"] + reserved_cost,
                        account_limits.daily_cost_microusd,
                        "account_daily",
                        "cost",
                    ),
                    (
                        account_month["cost"] + reserved_cost,
                        account_limits.monthly_cost_microusd,
                        "account_monthly",
                        "cost",
                    ),
                    (
                        account_task["calls"] + 1,
                        account_limits.task_calls,
                        "account_task",
                        "calls",
                    ),
                    (
                        account_task["tokens"] + input_tokens + max_output_tokens,
                        account_limits.task_tokens,
                        "account_task",
                        "tokens",
                    ),
                    (
                        account_task["cost"] + reserved_cost,
                        account_limits.task_cost_microusd,
                        "account_task",
                        "cost",
                    ),
                )
                for prospective, limit, period, dimension in account_checks:
                    if prospective > limit:
                        connection.rollback()
                        raise BudgetExceeded(
                            f"account:{provider.account_id}", period, dimension
                        )
            connection.execute(
                """
                INSERT INTO usage_reservations(
                    call_id, route_id, mission_id, provider_id, provider_account_id,
                    role, model, location, reason, created_at, status, reserved_input_tokens,
                    reserved_output_tokens, reserved_cost_microusd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?)
                """,
                (
                    call_id,
                    route_id,
                    mission_id,
                    provider.provider_id,
                    provider.account_id,
                    stored_role,
                    model,
                    provider.location,
                    reason[:500],
                    utc_now(),
                    input_tokens,
                    max_output_tokens,
                    reserved_cost,
                ),
            )
            connection.commit()
        return Reservation(call_id, input_tokens, max_output_tokens, reserved_cost)

    @staticmethod
    def _aggregate(connection, provider_id: str, time_pattern: str, mission_id: str | None = None):
        sql = """
            SELECT
                COUNT(*) AS calls,
                COALESCE(SUM(
                    CASE WHEN status = 'completed'
                         THEN actual_input_tokens + actual_output_tokens
                         ELSE reserved_input_tokens + reserved_output_tokens END
                ), 0) AS tokens,
                COALESCE(SUM(
                    CASE WHEN status = 'completed'
                         THEN actual_cost_microusd
                         ELSE reserved_cost_microusd END
                ), 0) AS cost
            FROM usage_reservations
            WHERE provider_id = ? AND created_at LIKE ?
        """
        parameters: list[object] = [provider_id, time_pattern]
        if mission_id is not None:
            sql += " AND mission_id = ?"
            parameters.append(mission_id)
        row = connection.execute(sql, parameters).fetchone()
        return {"calls": int(row["calls"]), "tokens": int(row["tokens"]), "cost": int(row["cost"])}

    @staticmethod
    def _aggregate_account(
        connection,
        account_id: str,
        time_pattern: str,
        route_id: str | None = None,
    ) -> dict[str, int]:
        sql = """
            SELECT
                COUNT(*) AS calls,
                COALESCE(SUM(
                    CASE WHEN status = 'completed'
                         THEN actual_input_tokens + actual_output_tokens
                         ELSE reserved_input_tokens + reserved_output_tokens END
                ), 0) AS tokens,
                COALESCE(SUM(
                    CASE WHEN status = 'completed'
                         THEN actual_cost_microusd
                         ELSE reserved_cost_microusd END
                ), 0) AS cost
            FROM usage_reservations
            WHERE provider_account_id = ? AND created_at LIKE ?
        """
        parameters: list[object] = [account_id, time_pattern]
        if route_id is not None:
            sql += " AND route_id = ?"
            parameters.append(route_id)
        row = connection.execute(sql, parameters).fetchone()
        return {
            "calls": int(row["calls"]),
            "tokens": int(row["tokens"]),
            "cost": int(row["cost"]),
        }

    def complete(
        self,
        reservation: Reservation,
        *,
        input_tokens: int,
        output_tokens: int,
        prices: tuple[int, int],
    ) -> int:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token usage must be non-negative")
        actual_cost = estimated_cost_microusd(input_tokens, output_tokens, *prices)
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE usage_reservations
                SET status = 'completed', completed_at = ?, actual_input_tokens = ?,
                    actual_output_tokens = ?, actual_cost_microusd = ?
                WHERE call_id = ? AND status = 'reserved'
                """,
                (utc_now(), input_tokens, output_tokens, actual_cost, reservation.call_id),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise RuntimeError("budget reservation state transition failed")
            connection.commit()
        return actual_cost

    def mark_uncertain(
        self,
        reservation: Reservation,
        error_code: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        prices: tuple[int, int] | None = None,
    ) -> None:
        retained_input = max(reservation.reserved_input_tokens, input_tokens or 0)
        retained_output = max(reservation.reserved_output_tokens, output_tokens or 0)
        retained_cost = reservation.reserved_cost_microusd
        if prices is not None:
            retained_cost = max(
                retained_cost,
                estimated_cost_microusd(retained_input, retained_output, *prices),
            )
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                """
                UPDATE usage_reservations
                SET status = 'uncertain', completed_at = ?, error_code = ?,
                    reserved_input_tokens = ?, reserved_output_tokens = ?,
                    reserved_cost_microusd = ?
                WHERE call_id = ? AND status = 'reserved'
                """,
                (
                    utc_now(), error_code[:100], retained_input, retained_output,
                    retained_cost, reservation.call_id,
                ),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise RuntimeError("budget reservation state transition failed")
            connection.commit()

    def summary(self) -> list[dict[str, object]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT provider_id, status, COUNT(*) AS calls,
                       COALESCE(SUM(CASE WHEN status = 'completed'
                           THEN actual_input_tokens + actual_output_tokens
                           ELSE reserved_input_tokens + reserved_output_tokens END), 0) AS tokens,
                       COALESCE(SUM(CASE WHEN status = 'completed'
                           THEN actual_cost_microusd ELSE reserved_cost_microusd END), 0) AS cost_microusd
                FROM usage_reservations GROUP BY provider_id, status
                ORDER BY provider_id, status
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def current_periods(self, provider: ProviderConfig) -> dict[str, dict[str, int]]:
        now = datetime.now(timezone.utc)
        with self.database.connect() as connection:
            daily = self._aggregate(connection, provider.provider_id, now.strftime("%Y-%m-%d%"))
            monthly = self._aggregate(connection, provider.provider_id, now.strftime("%Y-%m%"))
        return {"daily": daily, "monthly": monthly}

    def current_account_periods(self, account_id: str) -> dict[str, dict[str, int]]:
        now = datetime.now(timezone.utc)
        with self.database.connect() as connection:
            daily = self._aggregate_account(
                connection, account_id, now.strftime("%Y-%m-%d%")
            )
            monthly = self._aggregate_account(
                connection, account_id, now.strftime("%Y-%m%")
            )
        return {"daily": daily, "monthly": monthly}
