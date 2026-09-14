"""Deterministic runtime deployments derived from the reviewed model catalogue.

This module deliberately contains a small protocol/endpoint allowlist.  The
catalogue may grow freely, but a card only becomes invocable when all of its
model id, token prices, provider protocol, endpoint and authentication contract
match this reviewed table.  Credential *values* are never read here; generated
providers only name systemd credential-file environment variables.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import stat
from typing import Any
from urllib.parse import urlparse

from .errors import ConfigurationError


_IDENTIFIER = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,127}\Z")
_MODEL_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")
_DECIMAL = re.compile(r"\A(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?\Z")


@dataclass(frozen=True)
class _Integration:
    protocol: str
    base_url: str | None
    base_url_env: str | None
    required_host_suffix: str | None
    required_path: str | None
    request_path: str
    auth_scheme: str
    credential_env: str
    registry_site_url: str


# Exact values only.  Adding a provider to provider-integrations.v1.json cannot
# expand network or authentication authority without a code review here.
_INTEGRATIONS = {
    "alibaba": _Integration(
        "openai_compatible",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        None, None, "/compatible-mode/v1",
        "/chat/completions", "bearer", "QWEN_API_KEY_FILE",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    ),
    "z-ai": _Integration(
        "openai_compatible", "https://api.z.ai/api/paas/v4", None, None,
        "/api/paas/v4", "/chat/completions", "bearer", "Z_AI_API_KEY_FILE",
        "https://api.z.ai/api/paas/v4",
    ),
    "mistral-ai": _Integration(
        "openai_compatible", "https://api.mistral.ai/v1", None, None, "/v1",
        "/chat/completions", "bearer", "MISTRAL_AI_API_KEY_FILE",
        "https://api.mistral.ai/v1",
    ),
    "minimax": _Integration(
        "openai_compatible", "https://api.minimax.io/v1", None, None, "/v1",
        "/chat/completions", "bearer", "MINIMAX_API_KEY_FILE",
        "https://api.minimax.io/v1",
    ),
    "google": _Integration(
        "openai_compatible", "https://generativelanguage.googleapis.com/v1beta/openai",
        None, None, "/v1beta/openai", "/chat/completions", "bearer",
        "GOOGLE_API_KEY_FILE",
        "https://generativelanguage.googleapis.com/v1beta/openai",
    ),
    "cohere": _Integration(
        "openai_compatible", "https://api.cohere.ai/compatibility/v1", None,
        None, "/compatibility/v1", "/chat/completions", "bearer",
        "COHERE_API_KEY_FILE", "https://api.cohere.ai/compatibility/v1",
    ),
    "moonshot": _Integration(
        "openai_compatible", "https://api.moonshot.ai/v1", None, None, "/v1",
        "/chat/completions", "bearer", "MOONSHOT_API_KEY_FILE",
        "https://api.moonshot.ai/v1",
    ),
    "tencent": _Integration(
        "openai_compatible", "https://tokenhub.tencentmaas.com", None, None, None,
        "/v1/chat/completions", "bearer", "TENCENT_API_KEY_FILE",
        "https://tokenhub.tencentmaas.com",
    ),
    "xai": _Integration(
        "openai_compatible", "https://api.x.ai/v1", None, None, "/v1",
        "/chat/completions", "bearer", "XAI_API_KEY_FILE",
        "https://api.x.ai/v1",
    ),
    "openai": _Integration(
        "openai_native", "https://api.openai.com/v1", None, None, "/v1",
        "/chat/completions", "bearer", "OPENAI_API_KEY_FILE",
        "https://api.openai.com/v1",
    ),
    "anthropic": _Integration(
        "anthropic_messages", "https://api.anthropic.com", None, None, None,
        "/v1/messages", "anthropic", "ANTHROPIC_API_KEY_FILE",
        "https://api.anthropic.com",
    ),
}

_PROFILE_ROLES = {
    "utility_flash": "ROLE_TINY",
    "balanced_economy": "ROLE_LOCAL_OPS",
    "agentic_economy": "ROLE_LOCAL_OPS",
    "multilingual": "ROLE_LOCAL_OPS",
    "enterprise_rag": "ROLE_LOCAL_OPS",
    "reasoning_economy": "ROLE_REASONING",
    "advanced_reasoning": "ROLE_REASONING",
    "advanced_general": "ROLE_REASONING",
    "coder_economy": "ROLE_CODER",
    "advanced_coder": "ROLE_CODER",
    "premium_frontier": "ROLE_PREMIUM",
    "exceptional": "ROLE_PREMIUM",
}

_MODEL_ID_SOURCES = {
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

_PRICE_SOURCES = {
    "z-ai": {"https://docs.z.ai/guides/overview/pricing"},
    "minimax": {"https://platform.minimax.io/subscribe/token-plan?tab=api-enterprise"},
    "openai": _MODEL_ID_SOURCES["openai"],
    "moonshot": {"https://platform.kimi.ai/"},
}

_PROVIDER_BUDGET = {
    "daily_calls": 200,
    "monthly_calls": 4000,
    "daily_tokens": 1_000_000,
    "monthly_tokens": 20_000_000,
    "daily_cost_usd": "2.000000",
    "monthly_cost_usd": "20.000000",
    "mission_cost_usd": "1.000000",
}


@dataclass(frozen=True)
class CatalogueRuntimePlan:
    providers_by_role: dict[str, tuple[dict[str, Any], ...]]
    bindings: dict[str, str]
    ineligibility: dict[str, str]


def _document(path: Path, label: str) -> dict[str, Any]:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ConfigurationError(f"{label} must be a regular non-symlink file")
        if not 2 <= info.st_size <= 1_048_576:
            raise ConfigurationError(f"{label} has an invalid size")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot load {label}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} root must be an object")
    return value


def _official_source(account_id: str, source: Any, allowed: dict[str, set[str]]) -> bool:
    if not isinstance(source, str) or source not in allowed.get(account_id, set()):
        return False
    parsed = urlparse(source)
    return (
        parsed.scheme == "https"
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and parsed.fragment == ""
    )


def _rates(card: dict[str, Any], account_id: str) -> tuple[str, str] | None:
    prices = card.get("pricing")
    if not isinstance(prices, list) or not prices:
        return None
    parsed: list[tuple[Decimal, Decimal]] = []
    for price in prices:
        if not isinstance(price, dict):
            return None
        if (
            price.get("confidence") != "official_verified"
            or not _official_source(account_id, price.get("source"), _PRICE_SOURCES)
        ):
            return None
        raw_input = price.get("input_usd_per_million")
        raw_output = price.get("output_usd_per_million")
        if (
            not isinstance(raw_input, str)
            or not isinstance(raw_output, str)
            or not _DECIMAL.fullmatch(raw_input)
            or not _DECIMAL.fullmatch(raw_output)
        ):
            return None
        try:
            input_rate = Decimal(raw_input)
            output_rate = Decimal(raw_output)
        except InvalidOperation:
            return None
        if (
            not input_rate.is_finite()
            or not output_rate.is_finite()
            or input_rate <= 0
            or output_rate <= 0
        ):
            return None
        parsed.append((input_rate, output_rate))
    # Reserve peak / most expensive rates; promotional and off-peak prices do
    # not weaken the budget boundary.
    return (format(max(item[0] for item in parsed), "f"), format(max(item[1] for item in parsed), "f"))


def build_catalogue_runtime_plan(
    catalogue_path: Path, provider_integrations_path: Path
) -> CatalogueRuntimePlan:
    catalogue = _document(catalogue_path, "model catalogue")
    registry = _document(provider_integrations_path, "provider integrations")
    cards = catalogue.get("cards")
    model_id_provenance = catalogue.get("model_id_provenance")
    accounts = registry.get("provider_accounts")
    if (
        not isinstance(cards, list)
        or not isinstance(accounts, list)
        or not isinstance(model_id_provenance, dict)
    ):
        raise ConfigurationError("catalogue runtime sources are incomplete")
    registry_accounts = {
        account.get("id"): account for account in accounts if isinstance(account, dict)
    }
    generated: dict[str, list[dict[str, Any]]] = {
        role: [] for role in _PROFILE_ROLES.values()
    }
    bindings: dict[str, str] = {}
    reasons: dict[str, str] = {}
    for card in cards:
        if not isinstance(card, dict):
            raise ConfigurationError("model catalogue contains a malformed card")
        card_id = card.get("card_id")
        if not isinstance(card_id, str) or not _IDENTIFIER.fullmatch(card_id):
            raise ConfigurationError("model catalogue contains an invalid card id")
        if card.get("integration_stage") == "quarantined":
            reasons[card_id] = "quarantined"
            continue
        deployments = card.get("deployment_ids")
        if isinstance(deployments, list) and deployments:
            reasons[card_id] = "configured_static_priority"
            continue
        model_id = card.get("exact_model_id")
        if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
            reasons[card_id] = "exact_model_id_unverified"
            continue
        provenance = model_id_provenance.get(card_id)
        account_id = card.get("provider_account_id")
        if (
            not isinstance(provenance, dict)
            or provenance.get("exact_model_id") != model_id
            or provenance.get("confidence") != "official_verified"
            or not _official_source(str(account_id), provenance.get("source"), _MODEL_ID_SOURCES)
        ):
            reasons[card_id] = "exact_model_id_provenance_unverified"
            continue
        rates = _rates(card, str(account_id))
        if rates is None:
            reasons[card_id] = "token_prices_not_officially_verified"
            continue
        if card_id == "minimax-m3":
            reasons[card_id] = "minimax_m3_chat_api_contract_unverified"
            continue
        integration = _INTEGRATIONS.get(account_id)
        account = registry_accounts.get(account_id)
        if integration is None or not isinstance(account, dict):
            reasons[card_id] = "provider_adapter_not_reviewed"
            continue
        inference = account.get("inference")
        credentials = account.get("credentials")
        credential = credentials.get("inference") if isinstance(credentials, dict) else None
        sites = inference.get("sites") if isinstance(inference, dict) else None
        protocols = inference.get("protocols") if isinstance(inference, dict) else None
        documented_urls = {
            site.get("base_url")
            for site in sites or []
            if isinstance(site, dict) and site.get("status") == "documented"
        }
        expected_registry_auth = {
            "bearer": ("bearer", "Authorization", "Bearer "),
            "google_api_key": ("google_api_key", "x-goog-api-key", None),
            "anthropic": ("x_api_key_and_version", "x-api-key", None),
        }[integration.auth_scheme]
        actual_registry_auth = (
            credential.get("auth_scheme"),
            credential.get("header_name"),
            credential.get("value_prefix"),
        ) if isinstance(credential, dict) else None
        if (
            not isinstance(protocols, list)
            or integration.protocol not in protocols
            or integration.registry_site_url not in documented_urls
            or actual_registry_auth != expected_registry_auth
        ):
            reasons[card_id] = "provider_contract_mismatch"
            continue
        role = _PROFILE_ROLES.get(card.get("profile"))
        if role is None:
            reasons[card_id] = "capability_profile_not_routable"
            continue
        deployment_id = f"catalog-{card_id}"
        if len(deployment_id) > 64:
            reasons[card_id] = "generated_deployment_id_too_long"
            continue
        provider = {
            "id": deployment_id,
            "account_id": account_id,
            "capability_profile": card["profile"],
            "kind": (
                "anthropic_chat" if integration.auth_scheme == "anthropic" else
                "openai_responses" if account_id == "openai" else
                "openai_chat"
            ),
            "location": "remote",
            "enabled": True,
            "activation_env": "OPS_ORCHESTRATOR_ENABLE_CATALOGUE_API",
            "model": model_id,
            "api_key_file_env": integration.credential_env,
            "input_price_per_million_usd": rates[0],
            "output_price_per_million_usd": rates[1],
            "request_path": "/responses" if account_id == "openai" else integration.request_path,
            "auth_scheme": integration.auth_scheme,
            "chat_family": str(account_id),
            "chat_dialect": (
                "moonshot_k3" if card_id == "kimi-k3" else
                "generic"
            ),
            "budget": dict(_PROVIDER_BUDGET),
            "keep_alive": "0",
        }
        if integration.base_url is not None:
            provider["base_url"] = integration.base_url
        if integration.base_url_env is not None:
            provider["base_url_env"] = integration.base_url_env
        if integration.required_host_suffix is not None:
            provider["required_host_suffix"] = integration.required_host_suffix
        if integration.required_path is not None:
            provider["required_path"] = integration.required_path
        generated[role].append(provider)
        bindings[card_id] = deployment_id
        reasons[card_id] = "generated_reviewed_api_deployment"
    return CatalogueRuntimePlan(
        providers_by_role={
            role: tuple(sorted(values, key=lambda item: str(item["id"])))
            for role, values in generated.items()
        },
        bindings=bindings,
        ineligibility=reasons,
    )
