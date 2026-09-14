from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import grp
import pwd
from typing import Iterable

import pytest

from ops_orchestrator.config import load_config
from ops_orchestrator.models import ProviderResult


SOURCE_CONFIG = Path(__file__).parents[1] / "config" / "orchestrator.json"


def make_config(
    tmp_path: Path,
    active: Iterable[str] = (),
    budgets: dict[str, dict] | None = None,
    prices: dict[str, tuple[str, str]] | None = None,
    account_budgets: dict[str, dict] | None = None,
):
    raw = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    # Unit-service fixtures exercise the explicitly selected providers.  The
    # generated production catalogue runtime has its own exhaustive tests and
    # would otherwise add irrelevant unavailable candidates to every trace.
    raw["catalogue_runtime"]["enabled"] = False
    current_user = pwd.getpwuid(os.getuid()).pw_name
    current_group = grp.getgrgid(os.getgid()).gr_name
    raw["paths"] = {
        "database": str(tmp_path / "state" / "orchestrator.sqlite3"),
        "socket": str(tmp_path / "run" / "api.sock"),
        "finance_socket": str(tmp_path / "finance-run" / "api.sock"),
        "finance_socket_user": current_user,
        "finance_socket_group": current_group,
        "finance_client_user": current_user,
        "socket_group": "root",
        "metrics": str(tmp_path / "metrics" / "ops-orchestrator.prom"),
        "model_catalogue": str(
            Path(__file__).parents[2] / "catalog" / "model-catalog.v2.json"
        ),
        "provider_integrations": str(
            Path(__file__).parents[2] / "catalog" / "provider-integrations.v1.json"
        ),
    }
    raw["access"] = {
        "route_grants": [
            {
                "user": current_user,
                "projects": [
                    "minecraft",
                    "infra-shared",
                    "network-shared",
                    "monitoring-shared",
                    "backup-shared",
                ],
                "remote_allowed": True,
            }
        ]
    }
    active = set(active)
    for account in raw["provider_accounts"]:
        if account_budgets and account["id"] in account_budgets:
            account["budget"].update(account_budgets[account["id"]])
    for entries in raw["roles"].values():
        for provider in entries:
            provider["enabled"] = provider["id"] in active
            provider.pop("activation_env", None)
            provider["model"] = "test-model"
            provider.pop("model_env", None)
            if provider["location"] == "remote":
                price_override = (prices or {}).get(provider["id"])
                if "price_tiers" in provider:
                    if price_override is not None:
                        input_price, output_price = price_override
                        for tier in provider["price_tiers"]:
                            tier["input_price_per_million_usd"] = input_price
                            tier["output_price_per_million_usd"] = output_price
                else:
                    input_price, output_price = price_override or (
                        "1.000000",
                        "2.000000",
                    )
                    provider["input_price_per_million_usd"] = input_price
                    provider["output_price_per_million_usd"] = output_price
                    provider.pop("input_price_env", None)
                    provider.pop("output_price_env", None)
            if budgets and provider["id"] in budgets:
                provider["budget"].update(budgets[provider["id"]])
    destination = tmp_path / "config.json"
    destination.write_text(json.dumps(raw), encoding="utf-8")
    return load_config(destination)


@dataclass
class FakeProvider:
    config: object
    results: list[object]
    calls: int = 0
    available_value: bool = True
    received_requests: list[object] = field(default_factory=list)

    def available(self):
        return (self.available_value, "test provider" if self.available_value else "test unavailable")

    def invoke(self, request, maximum_output_tokens):
        self.calls += 1
        self.received_requests.append(request)
        if not self.results:
            raise RuntimeError("unexpected fake provider call")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def result(
    *,
    status="ok",
    confidence=0.9,
    summary="bounded result",
    input_tokens=50,
    output_tokens=20,
    verification=None,
):
    return ProviderResult(
        status=status,
        confidence=confidence,
        summary=summary,
        plan=("inspect using an authorized read-only tool",),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        raw_usage_known=True,
        verification=verification,
    )


@pytest.fixture
def payload():
    return {
        "mission_id": "mission-123",
        "project_id": "minecraft",
        "task_type": "ANALYZE",
        "risk": "LOW",
        "complexity": 0.1,
        "impact": 0.1,
        "confidence": 0.95,
        "urgency": "NORMAL",
        "deterministic_available": False,
        "remote_allowed": True,
        "context": {
            "instructions": "Use facts only and stop when evidence is absent.",
            "mission": "Classify the bounded service alert.",
            "current_state": "One alert is firing.",
            "relevant_memories": ["Reviewed runbook reference RB-1."],
            "tool_results": ["service_state=failed"],
        },
    }
