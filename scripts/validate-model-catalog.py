#!/usr/bin/python3
"""Strict, dependency-free validation for the public model catalogue.

This validator deliberately checks semantic invariants that JSON Schema cannot
express conveniently. It never reads provider credentials or makes a network
request.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


EXPECTED_SOURCE_LINES = {
    24, 29, 34, 39, 44, 49, 54, 59, 64, 69, 74, 79, 84, 89, 94, 99,
    104, 109, 114, 119, 124, 129, 134, 139, 144, 149, 155, 160, 165,
    170, 175, 180, 185, 190, 195, 200, 205, 210, 215, 220, 225, 230,
    235, 240, 245, 250, 255, 260, 265, 274, 278, 282, 286, 290, 294,
    298, 302, 306, 310,
}
DIMENSIONS = {
    "general", "reasoning", "code", "math", "research", "tools",
    "agentic", "instructions", "reliability", "factuality", "speed",
    "multilingual",
}
IDENTIFIER = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,127}\Z")
REVISION = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,127}\Z")
DECIMAL = re.compile(r"\A(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?\Z")
MAX_CATALOG_BYTES = 1_048_576
FINANCIAL_CAPABILITIES = {
    "cash_balance", "plan_quota", "usage", "cost", "rate_limits",
    "spending_limit",
}
FINANCIAL_ACCESS_MODES = {
    "direct_api", "admin_api", "cloud_billing_api", "console_only",
    "not_applicable", "unknown",
}
BALANCE_MODES = {
    "unsupported", "unknown", "quota_only", "provider_dependent",
    "verified_balance",
}
PRICE_CONFIDENCE = {
    "declared_unverified", "approximate", "revalidation_required",
    "discussed_reference", "shared_source_price", "public_unverified",
    "promotion_unverified", "official_verified",
}
TECHNICAL_CONFIDENCE = {
    "unknown", "source_declared_unverified", "official_verified", "observed",
}
OFFICIAL_MODEL_ID_SOURCES = {
    "alibaba": {
        "https://www.alibabacloud.com/help/en/model-studio/model-pricing",
        "https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-responses",
    },
    "z-ai": {"https://docs.z.ai/api-reference/llm/chat-completion"},
    "mistral-ai": {"https://docs.mistral.ai/api/"},
    "minimax": {"https://platform.minimax.io/docs/api-reference/models/openai/list-models"},
    "google": {"https://ai.google.dev/gemini-api/docs/models"},
    "cohere": {"https://docs.cohere.com/reference/list-models"},
    "tencent": {"https://cloud.tencent.com/document/product/1823/130051"},
    "xai": {"https://docs.x.ai/developers/models"},
    "openai": {
        "https://developers.openai.com/api/docs/models/gpt-5.4-mini",
        "https://developers.openai.com/api/docs/models/gpt-5.3-codex",
        "https://developers.openai.com/api/docs/models/gpt-5.4",
        "https://developers.openai.com/api/docs/models/gpt-5.4-pro",
    },
    "anthropic": {"https://docs.anthropic.com/en/docs/about-claude/models/overview"},
    "moonshot": {"https://platform.kimi.ai/docs/api/list-models"},
}
OFFICIAL_PRICE_SOURCES = {
    "z-ai": {"https://docs.z.ai/guides/overview/pricing"},
    "minimax": {"https://platform.minimax.io/subscribe/token-plan?tab=api-enterprise"},
    "openai": OFFICIAL_MODEL_ID_SOURCES["openai"],
    "moonshot": {"https://platform.kimi.ai/"},
}
EXPECTED_CONTEXT_WINDOWS = {
    "deepseek-v4-flash": 1_000_000,
    "gemini-3-1-flash-lite": 1_000_000,
    "grok-4-20": 1_000_000,
    "gpt-5-4-mini": 400_000,
    "gpt-5-3-codex": 400_000,
    "gpt-5-4": 1_050_000,
    "gpt-5-4-pro": 1_050_000,
    "kimi-k3": 1_000_000,
}
EXPECTED_LANGUAGES = {
    "cohere-aya-expanse-8b": ["Multilingue (langues non détaillées)"],
    "cohere-aya-expanse-32b": ["Multilingue (langues non détaillées)"],
    "cohere-command-a": ["Multilingue (langues non détaillées)"],
    "cohere-command-a-plus": ["Multilingue (langues non détaillées)"],
    "baidu-ernie-family": ["Chinois"],
}


class CatalogError(ValueError):
    pass


def _fail(message: str) -> None:
    raise CatalogError(message)


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{label} must be an array")
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        _fail(f"{label} is not a bounded identifier")
    return value


def _decimal(value: Any, label: str) -> Decimal:
    if not isinstance(value, str) or not DECIMAL.fullmatch(value):
        _fail(f"{label} is not a non-negative decimal string")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise CatalogError(f"{label} is invalid") from exc
    if not result.is_finite() or result < 0:
        _fail(f"{label} must be finite and non-negative")
    return result


def _strict_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    observed = set(value)
    if observed != expected:
        missing = sorted(expected - observed)
        extra = sorted(observed - expected)
        _fail(f"{label} fields differ (missing={missing}, extra={extra})")


def load_and_validate(path: Path) -> dict[str, Any]:
    try:
        info = path.lstat()
        raw = path.read_bytes()
    except OSError as exc:
        raise CatalogError("catalogue is unavailable") from exc
    if path.is_symlink() or not path.is_file() or info.st_size > MAX_CATALOG_BYTES:
        _fail("catalogue must be a bounded regular non-symlink file")
    try:
        document = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError("catalogue is not valid UTF-8 JSON") from exc
    root = _object(document, "catalogue")
    _strict_keys(
        root,
        {
            "schema_version", "revision", "generated_at", "source",
            "normalization", "comparison_profile", "capability_dimensions",
            "capability_profiles", "capability_provenance",
            "model_id_provenance",
            "provider_accounts", "cards",
        },
        "catalogue",
    )
    if root["schema_version"] != 2:
        _fail("schema_version must be 2")
    if not isinstance(root["revision"], str) or not REVISION.fullmatch(root["revision"]):
        _fail("revision is not a bounded identifier")

    source = _object(root["source"], "source")
    expected_counts = {
        "entry_count": 59,
        "priced_entry_count": 49,
        "variable_entry_count": 10,
        "provider_label_count": 21,
    }
    for field, expected in expected_counts.items():
        if source.get(field) != expected:
            _fail(f"source.{field} must be {expected}")
    digest = source.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        _fail("source.sha256 is invalid")

    dimensions = _list(root["capability_dimensions"], "capability_dimensions")
    if len(dimensions) != len(set(dimensions)) or set(dimensions) != DIMENSIONS:
        _fail("capability dimensions differ from the reviewed v2 dimensions")
    profiles = _object(root["capability_profiles"], "capability_profiles")
    if not profiles:
        _fail("at least one capability profile is required")
    for profile_id, raw_profile in profiles.items():
        _identifier(profile_id, "profile id")
        profile = _object(raw_profile, f"profile {profile_id}")
        if set(profile) != DIMENSIONS:
            _fail(f"profile {profile_id} does not define every dimension exactly once")
        for dimension, score in profile.items():
            if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 10:
                _fail(f"profile {profile_id}.{dimension} must be an integer from 0 to 10")

    accounts = _list(root["provider_accounts"], "provider_accounts")
    if len(accounts) != 21:
        _fail("exactly 21 normalized provider accounts are required for this revision")
    account_ids: set[str] = set()
    credential_refs: set[str] = set()
    for raw_account in accounts:
        account = _object(raw_account, "provider account")
        _strict_keys(
            account,
            {
                "id", "name", "country", "credential_ref", "balance_mode",
                "financial_capabilities",
            },
            "provider account",
        )
        account_id = _identifier(account["id"], "provider account id")
        if account_id in account_ids:
            _fail(f"duplicate provider account {account_id}")
        account_ids.add(account_id)
        credential_ref = account["credential_ref"]
        if (
            not isinstance(credential_ref, str)
            or not credential_ref.startswith("kv-infra-shared/data/llm/")
            or ".." in credential_ref
            or credential_ref in credential_refs
        ):
            _fail(f"unsafe or duplicate credential reference for {account_id}")
        credential_refs.add(credential_ref)
        if account["balance_mode"] not in BALANCE_MODES:
            _fail(f"provider account {account_id} has an invalid legacy balance mode")
        financial = _object(
            account["financial_capabilities"],
            f"provider account {account_id} financial capabilities",
        )
        if set(financial) != FINANCIAL_CAPABILITIES:
            _fail(f"provider account {account_id} financial capabilities differ")
        if any(mode not in FINANCIAL_ACCESS_MODES for mode in financial.values()):
            _fail(f"provider account {account_id} has an invalid financial access mode")

    cards = _list(root["cards"], "cards")
    if len(cards) != 58:
        _fail("this source revision must normalize to exactly 58 cards")
    card_ids: set[str] = set()
    deployment_ids: set[str] = set()
    observed_source_lines: set[int] = set()
    source_line_occurrences: dict[int, int] = {}
    last_sort_key: tuple[int, Decimal] | None = None
    priced = 0
    quarantined = 0
    configured = 0
    expected_card_fields = {
        "card_id", "display_name", "entity_kind", "provider_account_id",
        "developer", "country", "description", "profile", "specialties",
        "integration_stage", "deployment_ids", "exact_model_id",
        "context_window_tokens", "languages", "limitations",
        "latency_p50_ms", "latency_p95_ms",
        "technical_metadata_confidence", "technical_metadata_source",
        "source_lines", "pricing",
    }
    model_id_provenance = _object(
        root["model_id_provenance"], "model_id_provenance"
    )
    for raw_card in cards:
        card = _object(raw_card, "card")
        _strict_keys(card, expected_card_fields, "card")
        card_id = _identifier(card["card_id"], "card id")
        if card_id in card_ids:
            _fail(f"duplicate card id {card_id}")
        card_ids.add(card_id)
        if card["provider_account_id"] not in account_ids:
            _fail(f"card {card_id} references an unknown provider account")
        if card["profile"] not in profiles:
            _fail(f"card {card_id} references an unknown capability profile")
        if card["entity_kind"] not in {"model", "family", "unresolved"}:
            _fail(f"card {card_id} has an invalid entity_kind")
        if card["integration_stage"] not in {"catalogued", "configured", "quarantined"}:
            _fail(f"card {card_id} has an invalid integration stage")
        exact_model_id = card["exact_model_id"]
        if exact_model_id is not None and (
            not isinstance(exact_model_id, str)
            or not exact_model_id
            or len(exact_model_id) > 200
        ):
            _fail(f"card {card_id} exact_model_id is invalid")
        context_window = card["context_window_tokens"]
        if context_window != EXPECTED_CONTEXT_WINDOWS.get(card_id):
            _fail(f"card {card_id} context window differs from the supplied source")
        languages = _list(card["languages"], f"{card_id}.languages")
        if languages != EXPECTED_LANGUAGES.get(card_id, []):
            _fail(f"card {card_id} languages differ from the supplied source")
        limitations = _list(card["limitations"], f"{card_id}.limitations")
        if (
            len(limitations) > 32
            or len(limitations) != len(set(limitations))
            or any(
                not isinstance(item, str)
                or not item
                or len(item) > 512
                or any(character in item for character in "\x00\r\n")
                for item in limitations
            )
        ):
            _fail(f"card {card_id} limitations are invalid")
        if card["latency_p50_ms"] is not None or card["latency_p95_ms"] is not None:
            _fail(f"card {card_id} invents latency not present in the supplied source")
        confidence = card["technical_metadata_confidence"]
        if confidence not in TECHNICAL_CONFIDENCE:
            _fail(f"card {card_id} technical metadata confidence is invalid")
        expected_confidence = (
            "source_declared_unverified"
            if context_window is not None or languages or limitations
            else "unknown"
        )
        provenance = model_id_provenance.get(card_id)
        official_technical = (
            confidence == "official_verified"
            and isinstance(provenance, dict)
            and card["technical_metadata_source"] == provenance.get("source")
        )
        if confidence != expected_confidence and not official_technical:
            _fail(f"card {card_id} technical metadata confidence is inconsistent")
        if (
            card["technical_metadata_source"] != "catalogue_modeles_IA_API_2026.txt"
            and not official_technical
        ):
            _fail(f"card {card_id} technical metadata source is invalid")
        specialties = _list(card["specialties"], f"{card_id}.specialties")
        if not specialties or len(specialties) != len(set(specialties)):
            _fail(f"card {card_id} specialties must be non-empty and unique")
        lines = _list(card["source_lines"], f"{card_id}.source_lines")
        if not lines or any(isinstance(line, bool) or not isinstance(line, int) for line in lines):
            _fail(f"card {card_id} source lines are invalid")
        for line in lines:
            observed_source_lines.add(line)
            source_line_occurrences[line] = source_line_occurrences.get(line, 0) + 1

        deployments = _list(card["deployment_ids"], f"{card_id}.deployment_ids")
        for deployment_id in deployments:
            deployment_id = _identifier(deployment_id, "deployment id")
            if deployment_id in deployment_ids:
                _fail(f"deployment {deployment_id} is assigned to multiple cards")
            deployment_ids.add(deployment_id)
        if card["integration_stage"] == "configured":
            configured += 1
            if not deployments or not isinstance(card["exact_model_id"], str):
                _fail(f"configured card {card_id} needs an exact model and deployment")
        elif deployments:
            _fail(f"non-configured card {card_id} cannot claim a deployment")

        prices = _list(card["pricing"], f"{card_id}.pricing")
        if prices:
            priced += 1
        if card["integration_stage"] == "quarantined":
            quarantined += 1
            if deployments:
                _fail(f"quarantined card {card_id} must have no deployment")
        simulations: list[Decimal] = []
        modes: set[str] = set()
        for raw_price in prices:
            price = _object(raw_price, f"{card_id}.price")
            _strict_keys(
                price,
                {
                    "mode", "region_scope", "input_usd_per_million",
                    "output_usd_per_million", "cached_input_usd_per_million",
                    "declared_simulation_usd", "confidence", "source",
                },
                f"{card_id}.price",
            )
            mode = price["mode"]
            if not isinstance(mode, str) or not mode or mode in modes:
                _fail(f"card {card_id} price modes must be non-empty and unique")
            modes.add(mode)
            if price["confidence"] not in PRICE_CONFIDENCE:
                _fail(f"card {card_id} price confidence is invalid")
            if price["confidence"] == "official_verified":
                if price["source"] not in OFFICIAL_PRICE_SOURCES.get(
                    card["provider_account_id"], set()
                ):
                    _fail(f"card {card_id} official price source is not allowlisted")
            elif price["source"] != "catalogue_modeles_IA_API_2026.txt":
                _fail(f"card {card_id} price source is invalid")
            if (
                price["cached_input_usd_per_million"] is not None
                and price["confidence"] != "official_verified"
            ):
                _fail(f"card {card_id} invents a cache price absent from the supplied source")
            simulation = _decimal(price["declared_simulation_usd"], "declared simulation")
            simulations.append(simulation)
            input_price = price["input_usd_per_million"]
            output_price = price["output_usd_per_million"]
            if (input_price is None) != (output_price is None):
                _fail(f"card {card_id} must define both token prices or neither")
            if input_price is not None:
                expected = _decimal(input_price, "input price") * 10
                expected += _decimal(output_price, "output price") * 2
                if abs(expected - simulation) > Decimal("0.01"):
                    _fail(f"card {card_id} declared simulation does not match its token prices")
            elif mode != "simulation_only":
                _fail(f"card {card_id} without token prices must be simulation_only")

        # Quarantine prevents routing; it must not erase a historical price
        # explicitly present in the supplied source.
        if prices:
            sort_key = (0, min(simulations))
            if last_sort_key is not None and sort_key < last_sort_key:
                _fail(f"cards are not sorted by cheapest comparable simulation at {card_id}")
            last_sort_key = sort_key

    if observed_source_lines != EXPECTED_SOURCE_LINES:
        _fail("card source-line coverage does not match all 59 input entries")
    duplicate_lines = {line for line, count in source_line_occurrences.items() if count > 1}
    if duplicate_lines != {124} or source_line_occurrences.get(124) != 2:
        _fail("only the split Aya source line may occur twice")
    if priced != 48 or quarantined != 11 or configured != 5:
        _fail("catalogue stage/count invariants differ from the reviewed import")

    for provenance_card_id, raw_record in model_id_provenance.items():
        if provenance_card_id not in card_ids:
            _fail("model id provenance references an unknown card")
        record = _object(raw_record, f"model id provenance {provenance_card_id}")
        _strict_keys(
            record,
            {"exact_model_id", "confidence", "source", "verified_on"},
            f"model id provenance {provenance_card_id}",
        )
        card = next(item for item in cards if item["card_id"] == provenance_card_id)
        if (
            record["exact_model_id"] != card["exact_model_id"]
            or record["confidence"] != "official_verified"
            or record["source"] not in OFFICIAL_MODEL_ID_SOURCES.get(
                card["provider_account_id"], set()
            )
            or record["verified_on"] != "2026-09-05"
        ):
            _fail(f"model id provenance {provenance_card_id} is inconsistent")

    normalization = _object(root["normalization"], "normalization")
    if (
        normalization.get("card_count") != 58
        or normalization.get("priced_card_count") != 48
        or normalization.get("quarantined_family_count") != 11
    ):
        _fail("normalization counters differ from the reviewed catalogue")

    comparison = _object(root["comparison_profile"], "comparison_profile")
    if (
        comparison.get("calls") * comparison.get("input_tokens_per_call")
        != comparison.get("input_tokens_total")
        or comparison.get("calls") * comparison.get("output_tokens_per_call")
        != comparison.get("output_tokens_total")
    ):
        _fail("comparison totals do not match per-call values")
    return root


def verify_source(document: dict[str, Any], source_path: Path) -> None:
    try:
        raw = source_path.read_bytes()
    except OSError as exc:
        raise CatalogError("source catalogue is unavailable") from exc
    digest = hashlib.sha256(raw).hexdigest()
    expected = document["source"]["sha256"]
    if digest != expected:
        _fail("source catalogue SHA-256 differs from the reviewed import")
    if len(raw.splitlines()) != 324:
        _fail("source catalogue line count differs from the reviewed import")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the public model catalogue")
    parser.add_argument("catalogue", type=Path)
    parser.add_argument("--source", type=Path)
    arguments = parser.parse_args()
    try:
        document = load_and_validate(arguments.catalogue)
        if arguments.source is not None:
            verify_source(document, arguments.source)
    except CatalogError as exc:
        print(f"validate-model-catalog: {exc}", file=sys.stderr)
        return 1
    print(
        "model catalogue valid: "
        f"{len(document['cards'])} cards, "
        f"{len(document['provider_accounts'])} provider accounts"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
