"""Public model catalogue, exact cost simulation and capability-first preview.

The catalogue contains no credential value and never makes provider calls.  It
is intentionally separate from runtime deployments: a visible card is not an
authorization to route traffic to that model.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
from pathlib import Path
import re
import stat
from typing import Any, Iterable, Mapping

from .errors import ConfigurationError, ValidationError


MAX_CATALOG_BYTES = 1_048_576
MAX_CARDS = 10_000
MAX_PAGE_SIZE = 200
_IDENTIFIER = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,127}\Z")
_DECIMAL = re.compile(r"\A(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?\Z")
_FINANCIAL_CAPABILITIES = {
    "cash_balance",
    "plan_quota",
    "usage",
    "cost",
    "rate_limits",
    "spending_limit",
}
_FINANCIAL_ACCESS_MODES = {
    "direct_api",
    "admin_api",
    "cloud_billing_api",
    "console_only",
    "not_applicable",
    "unknown",
}
_TECHNICAL_CONFIDENCE = {
    "unknown", "source_declared_unverified", "official_verified", "observed",
}


def _strict_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise ConfigurationError(f"unknown {label} fields: {', '.join(unexpected)}")


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ConfigurationError(f"{label} is not a bounded identifier")
    return value


def _decimal(value: Any, label: str) -> Decimal:
    if not isinstance(value, str) or not _DECIMAL.fullmatch(value):
        raise ConfigurationError(f"{label} is not a non-negative decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ConfigurationError(f"{label} is invalid") from exc
    if not result.is_finite() or result < 0:
        raise ConfigurationError(f"{label} is outside the accepted range")
    return result


def _cost_microusd(input_tokens: int, output_tokens: int, price: Mapping[str, Any]) -> int:
    input_rate = price.get("input_usd_per_million")
    output_rate = price.get("output_usd_per_million")
    if input_rate is None and output_rate is None:
        declared = _decimal(price.get("declared_simulation_usd"), "declared simulation")
        return int((declared * Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))
    if (input_rate is None) != (output_rate is None):
        raise ConfigurationError("catalogue price must define both token directions")
    # rate is USD / 1M tokens. Multiplying by tokens directly yields micro-USD.
    cost = Decimal(input_tokens) * _decimal(input_rate, "catalogue input price")
    cost += Decimal(output_tokens) * _decimal(output_rate, "catalogue output price")
    return int(cost.to_integral_value(rounding=ROUND_CEILING))


@dataclass(frozen=True)
class ModelCatalogue:
    revision: str
    source: dict[str, Any]
    comparison_profile: dict[str, Any]
    dimensions: tuple[str, ...]
    profiles: dict[str, dict[str, int]]
    capability_provenance: dict[str, Any]
    accounts: tuple[dict[str, Any], ...]
    cards: tuple[dict[str, Any], ...]
    runtime_reasons: dict[str, str]

    def profile_scores(self, profile_id: str) -> dict[str, int]:
        """Return an isolated copy of one verified 0..10 capability profile."""

        try:
            return dict(self.profiles[profile_id])
        except KeyError as exc:
            raise ConfigurationError(
                f"runtime references unknown capability profile: {profile_id}"
            ) from exc

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        configured_deployment_ids: Iterable[str] = (),
        auxiliary_deployment_ids: Iterable[str] = (),
        runtime_bindings: Mapping[str, str] | None = None,
        runtime_reasons: Mapping[str, str] | None = None,
    ) -> "ModelCatalogue":
        try:
            info = path.lstat()
        except OSError as exc:
            raise ConfigurationError("model catalogue is unavailable") from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size < 2
            or info.st_size > MAX_CATALOG_BYTES
        ):
            raise ConfigurationError("model catalogue must be a bounded regular file")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError("model catalogue is not valid UTF-8 JSON") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError("model catalogue root must be an object")
        _strict_keys(
            raw,
            {
                "schema_version", "revision", "generated_at", "source",
                "normalization", "comparison_profile", "capability_dimensions",
                "capability_profiles", "capability_provenance",
                "model_id_provenance",
                "provider_accounts", "cards",
            },
            "model catalogue",
        )
        if raw.get("schema_version") != 2:
            raise ConfigurationError("unsupported model catalogue schema")
        revision = raw.get("revision")
        if (
            not isinstance(revision, str)
            or len(revision) > 128
            or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", revision)
        ):
            raise ConfigurationError("model catalogue revision is invalid")

        dimensions_raw = raw.get("capability_dimensions")
        profiles_raw = raw.get("capability_profiles")
        if (
            not isinstance(dimensions_raw, list)
            or not dimensions_raw
            or len(dimensions_raw) > 64
            or any(not isinstance(item, str) or not _IDENTIFIER.fullmatch(item) for item in dimensions_raw)
            or len(set(dimensions_raw)) != len(dimensions_raw)
            or not isinstance(profiles_raw, dict)
            or not profiles_raw
            or len(profiles_raw) > 256
        ):
            raise ConfigurationError("model catalogue capability profiles are invalid")
        dimensions = tuple(dimensions_raw)
        profiles: dict[str, dict[str, int]] = {}
        for raw_profile_id, raw_scores in profiles_raw.items():
            profile_id = _identifier(raw_profile_id, "capability profile id")
            if not isinstance(raw_scores, dict) or set(raw_scores) != set(dimensions):
                raise ConfigurationError(f"capability profile {profile_id} is incomplete")
            scores: dict[str, int] = {}
            for dimension, score in raw_scores.items():
                if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 10:
                    raise ConfigurationError(
                        f"capability profile {profile_id}.{dimension} is invalid"
                    )
                scores[dimension] = score
            profiles[profile_id] = scores

        comparison = raw.get("comparison_profile")
        if not isinstance(comparison, dict):
            raise ConfigurationError("model catalogue comparison profile is invalid")
        for field in (
            "calls", "input_tokens_per_call", "output_tokens_per_call",
            "input_tokens_total", "output_tokens_total",
        ):
            value = comparison.get(field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ConfigurationError(f"comparison profile {field} is invalid")
        if comparison["calls"] < 1 or (
            comparison["calls"] * comparison["input_tokens_per_call"]
            != comparison["input_tokens_total"]
        ) or (
            comparison["calls"] * comparison["output_tokens_per_call"]
            != comparison["output_tokens_total"]
        ):
            raise ConfigurationError("comparison profile totals are inconsistent")

        accounts_raw = raw.get("provider_accounts")
        if not isinstance(accounts_raw, list) or not 1 <= len(accounts_raw) <= 256:
            raise ConfigurationError("model catalogue provider accounts are invalid")
        account_ids: set[str] = set()
        credential_refs: set[str] = set()
        accounts: list[dict[str, Any]] = []
        for raw_account in accounts_raw:
            if not isinstance(raw_account, dict):
                raise ConfigurationError("model catalogue provider account must be an object")
            _strict_keys(
                raw_account,
                {
                    "id",
                    "name",
                    "country",
                    "credential_ref",
                    "balance_mode",
                    "financial_capabilities",
                },
                "provider account",
            )
            account_id = _identifier(raw_account.get("id"), "provider account id")
            credential_ref = raw_account.get("credential_ref")
            if account_id in account_ids:
                raise ConfigurationError(f"duplicate provider account {account_id}")
            if (
                not isinstance(credential_ref, str)
                or len(credential_ref) > 256
                or not credential_ref.startswith("kv-infra-shared/data/llm/")
                or ".." in credential_ref
                or credential_ref in credential_refs
            ):
                raise ConfigurationError(f"provider account {account_id} credential reference is invalid")
            if raw_account.get("balance_mode") not in {
                "unsupported", "unknown", "quota_only", "provider_dependent",
                "verified_balance",
            }:
                raise ConfigurationError(f"provider account {account_id} balance mode is invalid")
            financial = raw_account.get("financial_capabilities")
            if (
                not isinstance(financial, dict)
                or set(financial) != _FINANCIAL_CAPABILITIES
                or any(mode not in _FINANCIAL_ACCESS_MODES for mode in financial.values())
            ):
                raise ConfigurationError(
                    f"provider account {account_id} financial capabilities are invalid"
                )
            account_ids.add(account_id)
            credential_refs.add(credential_ref)
            accounts.append(deepcopy(raw_account))

        configured = {
            _identifier(value, "configured deployment id")
            for value in configured_deployment_ids
        }
        auxiliary = {
            _identifier(value, "auxiliary deployment id")
            for value in auxiliary_deployment_ids
        }
        if not auxiliary <= configured:
            raise ConfigurationError(
                "auxiliary deployments must reference configured runtime providers"
            )
        bindings = dict(runtime_bindings or {})
        reasons = dict(runtime_reasons or {})
        if any(
            not isinstance(card_id, str)
            or not _IDENTIFIER.fullmatch(card_id)
            or not isinstance(deployment_id, str)
            or not _IDENTIFIER.fullmatch(deployment_id)
            for card_id, deployment_id in bindings.items()
        ):
            raise ConfigurationError("catalogue runtime bindings are invalid")
        cards_raw = raw.get("cards")
        if not isinstance(cards_raw, list) or not 1 <= len(cards_raw) <= MAX_CARDS:
            raise ConfigurationError("model catalogue cards are invalid")
        cards: list[dict[str, Any]] = []
        card_ids: set[str] = set()
        claimed_deployments: set[str] = set()
        card_fields = {
            "card_id", "display_name", "entity_kind", "provider_account_id",
            "developer", "country", "description", "profile", "specialties",
            "integration_stage", "deployment_ids", "exact_model_id",
            "context_window_tokens", "languages", "limitations",
            "latency_p50_ms", "latency_p95_ms",
            "technical_metadata_confidence", "technical_metadata_source",
            "source_lines", "pricing",
        }
        price_fields = {
            "mode", "region_scope", "input_usd_per_million",
            "output_usd_per_million", "cached_input_usd_per_million",
            "declared_simulation_usd", "confidence", "source",
        }
        for source_card in cards_raw:
            raw_card = deepcopy(source_card) if isinstance(source_card, dict) else source_card
            if not isinstance(raw_card, dict):
                raise ConfigurationError("model catalogue card must be an object")
            _strict_keys(raw_card, card_fields, "model card")
            if set(raw_card) != card_fields:
                raise ConfigurationError("model card fields are incomplete")
            card_id = _identifier(raw_card.get("card_id"), "model card id")
            if card_id in card_ids:
                raise ConfigurationError(f"duplicate model card {card_id}")
            card_ids.add(card_id)
            if raw_card.get("provider_account_id") not in account_ids:
                raise ConfigurationError(f"model card {card_id} references an unknown account")
            profile_id = raw_card.get("profile")
            if profile_id not in profiles:
                raise ConfigurationError(f"model card {card_id} references an unknown profile")
            stage = raw_card.get("integration_stage")
            if stage not in {"catalogued", "configured", "quarantined"}:
                raise ConfigurationError(f"model card {card_id} stage is invalid")
            deployments = raw_card.get("deployment_ids")
            if (
                not isinstance(deployments, list)
                or len(deployments) > 32
                or len(set(deployments)) != len(deployments)
            ):
                raise ConfigurationError(f"model card {card_id} deployments are invalid")
            generated_deployment = bindings.get(card_id)
            if generated_deployment is not None:
                if (
                    stage != "catalogued"
                    or deployments
                    or not isinstance(raw_card.get("exact_model_id"), str)
                    or generated_deployment not in configured
                    or generated_deployment in auxiliary
                ):
                    raise ConfigurationError(
                        f"model card {card_id} generated runtime binding is unsafe"
                    )
                raw_card["catalogue_integration_stage"] = stage
                raw_card["integration_stage"] = "configured"
                raw_card["deployment_ids"] = [generated_deployment]
                stage = "configured"
                deployments = raw_card["deployment_ids"]
            for deployment_id in deployments:
                deployment_id = _identifier(deployment_id, "deployment id")
                if deployment_id in claimed_deployments:
                    raise ConfigurationError(f"deployment {deployment_id} is assigned twice")
                if deployment_id not in configured:
                    raise ConfigurationError(
                        f"catalogue deployment {deployment_id} has no runtime configuration"
                    )
                claimed_deployments.add(deployment_id)
            if stage == "configured" and (
                not deployments or not isinstance(raw_card.get("exact_model_id"), str)
            ):
                raise ConfigurationError(f"configured card {card_id} is incomplete")
            if stage != "configured" and deployments:
                raise ConfigurationError(f"non-configured card {card_id} claims a deployment")
            context_window = raw_card.get("context_window_tokens")
            if (
                context_window is not None
                and (
                    isinstance(context_window, bool)
                    or not isinstance(context_window, int)
                    or not 1 <= context_window <= 1_000_000_000
                )
            ):
                raise ConfigurationError(f"model card {card_id} context window is invalid")
            for field, maximum_items, maximum_length in (
                ("languages", 64, 128),
                ("limitations", 32, 512),
            ):
                values = raw_card.get(field)
                if (
                    not isinstance(values, list)
                    or len(values) > maximum_items
                    or len(values) != len(set(values))
                    or any(
                        not isinstance(value, str)
                        or not value
                        or len(value) > maximum_length
                        or any(character in value for character in "\x00\r\n")
                        for value in values
                    )
                ):
                    raise ConfigurationError(f"model card {card_id} {field} are invalid")
            latency_p50 = raw_card.get("latency_p50_ms")
            latency_p95 = raw_card.get("latency_p95_ms")
            for label, latency in (("p50", latency_p50), ("p95", latency_p95)):
                if latency is not None and (
                    isinstance(latency, bool)
                    or not isinstance(latency, int)
                    or not 0 <= latency <= 3_600_000
                ):
                    raise ConfigurationError(f"model card {card_id} latency {label} is invalid")
            if latency_p50 is not None and latency_p95 is not None and latency_p95 < latency_p50:
                raise ConfigurationError(f"model card {card_id} latency percentiles are invalid")
            if raw_card.get("technical_metadata_confidence") not in _TECHNICAL_CONFIDENCE:
                raise ConfigurationError(f"model card {card_id} technical confidence is invalid")
            technical_source = raw_card.get("technical_metadata_source")
            if (
                not isinstance(technical_source, str)
                or not technical_source
                or len(technical_source) > 256
            ):
                raise ConfigurationError(f"model card {card_id} technical source is invalid")
            prices = raw_card.get("pricing")
            if not isinstance(prices, list) or len(prices) > 32:
                raise ConfigurationError(f"model card {card_id} prices are invalid")
            modes: set[str] = set()
            for price in prices:
                if not isinstance(price, dict):
                    raise ConfigurationError(f"model card {card_id} price is invalid")
                _strict_keys(price, price_fields, f"model card {card_id} price")
                if set(price) != price_fields:
                    raise ConfigurationError(f"model card {card_id} price fields are incomplete")
                mode = price.get("mode")
                if not isinstance(mode, str) or not mode or len(mode) > 64 or mode in modes:
                    raise ConfigurationError(f"model card {card_id} price mode is invalid")
                modes.add(mode)
                cached_rate = price.get("cached_input_usd_per_million")
                if cached_rate is not None:
                    _decimal(cached_rate, "catalogue cached input price")
                price_source = price.get("source")
                if (
                    not isinstance(price_source, str)
                    or not price_source
                    or len(price_source) > 256
                ):
                    raise ConfigurationError(f"model card {card_id} price source is invalid")
                _cost_microusd(
                    int(comparison["input_tokens_total"]),
                    int(comparison["output_tokens_total"]),
                    price,
                )
            if stage == "quarantined" and deployments:
                raise ConfigurationError(f"quarantined card {card_id} is not inert")
            cards.append(deepcopy(raw_card))

        unclaimed_deployments = configured - claimed_deployments
        if unclaimed_deployments != auxiliary:
            missing_declarations = sorted(unclaimed_deployments - auxiliary)
            incorrectly_auxiliary = sorted(auxiliary - unclaimed_deployments)
            raise ConfigurationError(
                "runtime deployments outside the catalogue must be declared auxiliary; "
                f"missing={missing_declarations}, invalid={incorrectly_auxiliary}"
            )

        source = raw.get("source")
        provenance = raw.get("capability_provenance")
        if not isinstance(source, dict) or not isinstance(provenance, dict):
            raise ConfigurationError("model catalogue provenance is invalid")
        unknown_binding_cards = sorted(set(bindings) - card_ids)
        if unknown_binding_cards:
            raise ConfigurationError(
                "catalogue runtime bindings reference unknown cards: "
                + ", ".join(unknown_binding_cards)
            )
        if reasons and set(reasons) != card_ids:
            raise ConfigurationError("catalogue runtime reasons must cover every card exactly")
        model_id_provenance = raw.get("model_id_provenance")
        if not isinstance(model_id_provenance, dict):
            raise ConfigurationError("model id provenance must be an object")
        cards_by_id = {str(card["card_id"]): card for card in cards}
        for provenance_card_id, record in model_id_provenance.items():
            if provenance_card_id not in cards_by_id or not isinstance(record, dict):
                raise ConfigurationError("model id provenance references an unknown card")
            if set(record) != {"exact_model_id", "confidence", "source", "verified_on"}:
                raise ConfigurationError("model id provenance record is incomplete")
            if (
                record.get("exact_model_id") != cards_by_id[provenance_card_id].get("exact_model_id")
                or record.get("confidence") != "official_verified"
                or not isinstance(record.get("source"), str)
                or not record["source"].startswith("https://")
                or not isinstance(record.get("verified_on"), str)
                or not re.fullmatch(r"20[0-9]{2}-[01][0-9]-[0-3][0-9]", record["verified_on"])
            ):
                raise ConfigurationError("model id provenance record is inconsistent")
        return cls(
            revision=revision,
            source=deepcopy(source),
            comparison_profile=deepcopy(comparison),
            dimensions=dimensions,
            profiles=profiles,
            capability_provenance=deepcopy(provenance),
            accounts=tuple(accounts),
            cards=tuple(cards),
            runtime_reasons=reasons,
        )

    def _public_card(
        self,
        card: Mapping[str, Any],
        runtime_deployments: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        result = deepcopy(dict(card))
        generated = "catalogue_integration_stage" in result
        reason = self.runtime_reasons.get(str(card["card_id"]))
        result["runtime_eligibility"] = {
            "eligible": bool(card["deployment_ids"]),
            "binding": "generated" if generated else (
                "static" if card["deployment_ids"] else "none"
            ),
            "reason": reason or (
                "configured_static_priority" if card["deployment_ids"] else "not_configured"
            ),
        }
        result["capabilities"] = deepcopy(self.profiles[str(card["profile"])])
        input_tokens = int(self.comparison_profile["input_tokens_total"])
        output_tokens = int(self.comparison_profile["output_tokens_total"])
        simulations = []
        for price in card["pricing"]:
            simulations.append(
                {
                    "mode": price["mode"],
                    "cost_microusd": _cost_microusd(input_tokens, output_tokens, price),
                    "basis": "computed_from_rates"
                    if price["input_usd_per_million"] is not None
                    else "source_simulation_only",
                }
            )
        result["simulations"] = simulations
        deployment_states = [
            deepcopy(dict(runtime_deployments[deployment_id]))
            for deployment_id in card["deployment_ids"]
            if deployment_id in runtime_deployments
        ]
        result["runtime"] = {
            "deployments": deployment_states,
            "availability_scope": "configuration-and-secret-only",
            "available": any(item.get("available") is True for item in deployment_states),
        }
        return result

    def page(
        self,
        *,
        offset: int,
        limit: int,
        runtime_deployments: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValidationError("catalogue offset is invalid")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_PAGE_SIZE
        ):
            raise ValidationError(f"catalogue limit must be between 1 and {MAX_PAGE_SIZE}")
        runtime = runtime_deployments or {}
        selected = self.cards[offset : offset + limit]
        public_accounts = []
        for account in self.accounts:
            public_account = deepcopy(account)
            account_deployments = [
                deepcopy(dict(state))
                for state in runtime.values()
                if state.get("provider_account_id") == account["id"]
            ]
            if any(state.get("available") is True for state in account_deployments):
                credential_state = "resolved_by_runtime"
            elif account_deployments:
                credential_state = "not_resolved_by_runtime"
            else:
                credential_state = "not_observable"
            public_account["runtime"] = {
                "credential_state": credential_state,
                "deployment_count": len(account_deployments),
                "availability_scope": "configuration-and-secret-only",
            }
            public_accounts.append(public_account)
        return {
            "schema_version": 2,
            "revision": self.revision,
            "source": deepcopy(self.source),
            "comparison_profile": deepcopy(self.comparison_profile),
            "capability_dimensions": list(self.dimensions),
            "capability_provenance": deepcopy(self.capability_provenance),
            "provider_accounts": public_accounts,
            "pagination": {
                "offset": offset,
                "limit": limit,
                "returned": len(selected),
                "total": len(self.cards),
                "next_offset": offset + len(selected)
                if offset + len(selected) < len(self.cards)
                else None,
            },
            "cards": [self._public_card(card, runtime) for card in selected],
        }

    def preview(
        self,
        payload: Any,
        *,
        runtime_deployments: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValidationError("catalogue preview must be an object")
        allowed = {
            "project_id", "requirements", "required_specialties",
            "maximum_monthly_cost_usd", "runtime_only", "limit",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValidationError(f"unknown catalogue preview fields: {', '.join(unknown)}")
        project_id = payload.get("project_id")
        if not isinstance(project_id, str) or not project_id or len(project_id) > 128:
            raise ValidationError("project_id is invalid")
        requirements = payload.get("requirements", {})
        if not isinstance(requirements, dict) or len(requirements) > len(self.dimensions):
            raise ValidationError("requirements must be a bounded object")
        normalized_requirements: dict[str, int] = {}
        for dimension, score in requirements.items():
            if dimension not in self.dimensions:
                raise ValidationError(f"unknown capability requirement: {dimension}")
            if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 10:
                raise ValidationError(f"capability requirement {dimension} must be 0..10")
            normalized_requirements[dimension] = score
        required_specialties = payload.get("required_specialties", [])
        if (
            not isinstance(required_specialties, list)
            or len(required_specialties) > 16
            or any(
                not isinstance(item, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", item)
                for item in required_specialties
            )
            or len(set(required_specialties)) != len(required_specialties)
        ):
            raise ValidationError("required_specialties is invalid")
        runtime_only = payload.get("runtime_only", True)
        if not isinstance(runtime_only, bool):
            raise ValidationError("runtime_only must be boolean")
        limit = payload.get("limit", 10)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValidationError("preview limit must be between 1 and 50")
        maximum_raw = payload.get("maximum_monthly_cost_usd")
        maximum_microusd: int | None = None
        if maximum_raw is not None:
            try:
                maximum_microusd = int(
                    (_decimal(maximum_raw, "maximum monthly cost") * Decimal(1_000_000))
                    .to_integral_value(rounding=ROUND_CEILING)
                )
            except ConfigurationError as exc:
                raise ValidationError(str(exc)) from exc

        runtime = runtime_deployments or {}
        candidates: list[tuple[int, int, str, dict[str, Any]]] = []
        exclusions: dict[str, int] = {}
        for card in self.cards:
            reason: str | None = None
            stage = str(card["integration_stage"])
            if stage == "quarantined":
                reason = "quarantined"
            profile = self.profiles[str(card["profile"])]
            if reason is None and any(
                profile[dimension] < minimum
                for dimension, minimum in normalized_requirements.items()
            ):
                reason = "capability_below_minimum"
            if reason is None and not set(required_specialties).issubset(card["specialties"]):
                reason = "specialty_missing"
            deployment_states = [
                runtime[item] for item in card["deployment_ids"] if item in runtime
            ]
            available_deployments = [
                item for item in deployment_states if item.get("available") is True
            ]
            runtime_available = bool(available_deployments)
            if reason is None and runtime_only and not runtime_available:
                reason = "no_available_reviewed_deployment"
            simulations = [
                _cost_microusd(
                    int(self.comparison_profile["input_tokens_total"]),
                    int(self.comparison_profile["output_tokens_total"]),
                    price,
                )
                for price in card["pricing"]
            ]
            catalogue_cost = max(simulations) if simulations else None
            selected_deployment: Mapping[str, Any] | None = None
            if reason is None and runtime_only:
                priced_deployments = [
                    item
                    for item in available_deployments
                    if isinstance(item.get("comparison_cost_microusd"), int)
                    and not isinstance(item.get("comparison_cost_microusd"), bool)
                    and int(item["comparison_cost_microusd"]) >= 0
                    and isinstance(item.get("provider_account_id"), str)
                ]
                if priced_deployments:
                    selected_deployment = min(
                        priced_deployments,
                        key=lambda item: (
                            int(item["comparison_cost_microusd"]),
                            str(item.get("deployment_id", "")),
                        ),
                    )
                    conservative_cost = int(
                        selected_deployment["comparison_cost_microusd"]
                    )
                else:
                    conservative_cost = None
                    reason = "runtime_price_unavailable"
            else:
                conservative_cost = catalogue_cost
            if reason is None and conservative_cost is None:
                reason = "price_unavailable"
            if (
                reason is None
                and maximum_microusd is not None
                and conservative_cost is not None
                and conservative_cost > maximum_microusd
            ):
                reason = "cost_above_maximum"
            if reason is not None:
                exclusions[reason] = exclusions.get(reason, 0) + 1
                continue
            assert conservative_cost is not None
            capability_margin = sum(
                profile[dimension] - minimum
                for dimension, minimum in normalized_requirements.items()
            )
            public = self._public_card(card, runtime)
            public["routing_preview"] = {
                "conservative_monthly_cost_microusd": conservative_cost,
                "capability_margin": capability_margin,
                "cost_basis": "runtime_deployment"
                if selected_deployment is not None
                else "catalogue_card",
                "selected_deployment_id": selected_deployment.get("deployment_id")
                if selected_deployment is not None
                else None,
                "selected_provider_account_id": selected_deployment.get(
                    "provider_account_id"
                )
                if selected_deployment is not None
                else card["provider_account_id"],
                "not_an_activation_authorization": True,
            }
            # Cost is primary once all hard capability thresholds are met.
            candidates.append((conservative_cost, -capability_margin, str(card["card_id"]), public))
        candidates.sort(key=lambda item: item[:3])
        return {
            "catalogue_revision": self.revision,
            "project_id": project_id,
            "policy": "hard-capabilities-then-lowest-conservative-cost",
            "runtime_only": runtime_only,
            "requirements": normalized_requirements,
            "required_specialties": list(required_specialties),
            "candidate_count": len(candidates),
            "excluded": exclusions,
            "candidates": [item[3] for item in candidates[:limit]],
            "advisory_only": True,
        }
