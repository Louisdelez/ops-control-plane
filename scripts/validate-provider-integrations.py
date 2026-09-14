#!/usr/bin/python3
"""Validate the public provider integration registry without network access.

The registry contains only public integration facts and opaque OpenBao paths.
This validator never resolves a credential and never calls a provider API.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import urlsplit


MAX_REGISTRY_BYTES = 1_048_576
MAX_CATALOGUE_BYTES = 1_048_576
IDENTIFIER = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,127}\Z")
REVISION = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,127}\Z")
TEMPLATE_FIELD = re.compile(r"\{[a-z][a-z0-9_]{0,63}\}")
FINANCE_CAPABILITIES = (
    "cash_balance",
    "plan_quota",
    "usage",
    "cost",
    "rate_limits",
    "spending_limit",
)
FINANCE_ACCESS_MODES = (
    "direct_api",
    "admin_api",
    "cloud_billing_api",
    "console_only",
    "not_applicable",
    "unknown",
)
API_ACCESS_MODES = {"direct_api", "admin_api", "cloud_billing_api"}
PROVIDER_IDS = (
    "alibaba",
    "z-ai",
    "mistral-ai",
    "deepseek",
    "minimax",
    "google",
    "cohere",
    "moonshot",
    "tencent",
    "xai",
    "openai",
    "bytedance",
    "anthropic",
    "nvidia",
    "aws",
    "meta",
    "microsoft",
    "xiaomi",
    "baidu",
    "baichuan",
    "stepfun",
)
PROTOCOLS = {
    "openai_native",
    "openai_compatible",
    "openai_chat_partial",
    "dashscope_native",
    "anthropic_compatible",
    "anthropic_messages",
    "gemini_native",
    "cohere_v2",
    "bedrock_native",
    "preview_openai_compatible",
}
AUTH_SCHEMES = {
    "none",
    "unknown",
    "bearer",
    "x_api_key",
    "x_api_key_and_version",
    "google_api_key",
    "google_oauth2",
    "api_key_or_bearer",
    "aws_sigv4",
    "aws_sigv4_or_bearer",
    "azure_api_key_or_entra",
    "azure_entra",
    "alibaba_rpc_signature",
    "tencent_tc3_hmac",
    "volcengine_hmac",
}
OFFICIAL_SOURCE_HOSTS = {
    "www.alibabacloud.com",
    "docs.z.ai",
    "docs.mistral.ai",
    "api-docs.deepseek.com",
    "platform.minimax.io",
    "ai.google.dev",
    "docs.cohere.com",
    "platform.kimi.ai",
    "cloud.tencent.com",
    "docs.x.ai",
    "developers.openai.com",
    "www.volcengine.com",
    "api.volcengine.com",
    "platform.claude.com",
    "docs.nvidia.com",
    "docs.aws.amazon.com",
    "ai.meta.com",
    "learn.microsoft.com",
    "mimo.mi.com",
    "cloud.baidu.com",
    "platform.baichuan-ai.com",
    "platform.stepfun.ai",
    "platform.stepfun.com",
}


class RegistryError(ValueError):
    pass


def _fail(message: str) -> None:
    raise RegistryError(message)


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{label} must be an array")
    return value


def _strict_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    observed = set(value)
    if observed != expected:
        _fail(
            f"{label} fields differ "
            f"(missing={sorted(expected - observed)}, extra={sorted(observed - expected)})"
        )


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        _fail(f"{label} is not a bounded identifier")
    return value


def _date(value: Any, label: str) -> str:
    if not isinstance(value, str):
        _fail(f"{label} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise RegistryError(f"{label} must be an ISO date") from exc
    if parsed.isoformat() != value:
        _fail(f"{label} must be a canonical ISO date")
    return value


def _https_url(value: Any, label: str, *, allow_template: bool) -> str:
    if not isinstance(value, str) or not value.startswith("https://") or len(value) > 1024:
        _fail(f"{label} must be a bounded HTTPS URL")
    if "\x00" in value or "\\" in value or ".." in value:
        _fail(f"{label} contains an unsafe URL component")
    plain = TEMPLATE_FIELD.sub("template", value) if allow_template else value
    if "{" in plain or "}" in plain:
        _fail(f"{label} contains an invalid template field")
    parsed = urlsplit(plain)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        _fail(f"{label} must not contain user information")
    return value


def _load_json(path: Path, maximum: int, label: str) -> dict[str, Any]:
    try:
        info = path.lstat()
        raw = path.read_bytes()
    except OSError as exc:
        raise RegistryError(f"{label} is unavailable") from exc
    if path.is_symlink() or not path.is_file() or info.st_size < 2 or info.st_size > maximum:
        _fail(f"{label} must be a bounded regular non-symlink file")
    try:
        document = json.loads(raw.decode("utf-8", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RegistryError(f"{label} is not valid UTF-8 JSON") from exc
    return _object(document, label)


def _credential(value: Any, label: str, *, inference: bool) -> str | None:
    credential = _object(value, label)
    _strict_keys(
        credential,
        {"credential_ref", "auth_scheme", "header_name", "value_prefix"},
        label,
    )
    reference = credential["credential_ref"]
    scheme = credential["auth_scheme"]
    header = credential["header_name"]
    prefix = credential["value_prefix"]
    if scheme not in AUTH_SCHEMES:
        _fail(f"{label}.auth_scheme is invalid")
    if reference is None:
        if inference:
            _fail(f"{label}.credential_ref is required")
        if scheme != "none" or header is not None or prefix is not None:
            _fail(f"{label} without a reference must be inert")
        return None
    expected_prefix = (
        "kv-infra-shared/data/llm/"
        if inference
        else "kv-infra-shared/data/llm-admin/"
    )
    if (
        not isinstance(reference, str)
        or len(reference) > 256
        or not reference.startswith(expected_prefix)
        or ".." in reference
    ):
        _fail(f"{label}.credential_ref is unsafe")
    if scheme == "none":
        _fail(f"{label} with a reference needs an authentication scheme")
    if header is not None and (
        not isinstance(header, str) or not 1 <= len(header) <= 64
    ):
        _fail(f"{label}.header_name is invalid")
    if prefix is not None and (
        not isinstance(prefix, str) or len(prefix) > 32
    ):
        _fail(f"{label}.value_prefix is invalid")
    return reference


def load_and_validate(path: Path) -> dict[str, Any]:
    root = _load_json(path, MAX_REGISTRY_BYTES, "provider integration registry")
    _strict_keys(
        root,
        {
            "schema_version",
            "revision",
            "verified_on",
            "finance_capability_names",
            "finance_access_modes",
            "provider_accounts",
            "deployment_mappings",
        },
        "provider integration registry",
    )
    if root["schema_version"] != 1:
        _fail("schema_version must be 1")
    if not isinstance(root["revision"], str) or not REVISION.fullmatch(root["revision"]):
        _fail("revision is not a bounded identifier")
    verified_on = _date(root["verified_on"], "verified_on")
    if root["finance_capability_names"] != list(FINANCE_CAPABILITIES):
        _fail("finance_capability_names differ from the v1 contract")
    if root["finance_access_modes"] != list(FINANCE_ACCESS_MODES):
        _fail("finance_access_modes differ from the v1 contract")

    raw_accounts = _array(root["provider_accounts"], "provider_accounts")
    if len(raw_accounts) != 21:
        _fail("exactly 21 provider accounts are required")
    account_ids: list[str] = []
    inference_refs: set[str] = set()
    admin_refs: set[str] = set()
    accounts: dict[str, dict[str, Any]] = {}
    for raw_account in raw_accounts:
        account = _object(raw_account, "provider account")
        _strict_keys(
            account,
            {
                "id",
                "display_name",
                "inference",
                "credentials",
                "financial_capabilities",
                "finance_endpoints",
                "official_sources",
                "limitations",
            },
            "provider account",
        )
        account_id = _identifier(account["id"], "provider account id")
        if account_id in accounts:
            _fail(f"duplicate provider account {account_id}")
        if not isinstance(account["display_name"], str) or not 1 <= len(account["display_name"]) <= 160:
            _fail(f"provider account {account_id} display_name is invalid")
        account_ids.append(account_id)
        accounts[account_id] = account

        inference = _object(account["inference"], f"{account_id}.inference")
        _strict_keys(inference, {"protocols", "sites", "model_discovery"}, f"{account_id}.inference")
        protocols = _array(inference["protocols"], f"{account_id}.protocols")
        if not protocols or len(protocols) != len(set(protocols)) or any(item not in PROTOCOLS for item in protocols):
            _fail(f"{account_id}.protocols are invalid")
        sites = _array(inference["sites"], f"{account_id}.sites")
        if not sites or len(sites) > 16:
            _fail(f"{account_id}.sites are invalid")
        site_ids: set[str] = set()
        for raw_site in sites:
            site = _object(raw_site, f"{account_id}.site")
            _strict_keys(site, {"id", "region", "base_url", "status"}, f"{account_id}.site")
            site_id = _identifier(site["id"], f"{account_id}.site.id")
            if site_id in site_ids:
                _fail(f"duplicate site {account_id}/{site_id}")
            site_ids.add(site_id)
            if not isinstance(site["region"], str) or not 1 <= len(site["region"]) <= 64:
                _fail(f"{account_id}/{site_id}.region is invalid")
            if site["status"] not in {"documented", "template", "unverified"}:
                _fail(f"{account_id}/{site_id}.status is invalid")
            if site["base_url"] is None:
                if site["status"] != "unverified":
                    _fail(f"{account_id}/{site_id} lacks a documented base URL")
            else:
                _https_url(site["base_url"], f"{account_id}/{site_id}.base_url", allow_template=True)

        discovery = _object(inference["model_discovery"], f"{account_id}.model_discovery")
        _strict_keys(
            discovery,
            {"mode", "method", "endpoint", "operation", "credential_scope"},
            f"{account_id}.model_discovery",
        )
        mode = discovery["mode"]
        if mode not in {"api", "control_plane_api", "static_documentation", "unverified"}:
            _fail(f"{account_id}.model_discovery.mode is invalid")
        if mode in {"api", "control_plane_api"}:
            if discovery["method"] not in {"GET", "POST"} or discovery["endpoint"] is None:
                _fail(f"{account_id}.model_discovery API is incomplete")
            _https_url(discovery["endpoint"], f"{account_id}.model_discovery.endpoint", allow_template=True)
            if discovery["credential_scope"] not in {"inference", "admin_finance"}:
                _fail(f"{account_id}.model_discovery credential scope is invalid")
        elif any(
            discovery[field] is not None
            for field in ("method", "endpoint", "operation")
        ) or discovery["credential_scope"] != "none":
            _fail(f"{account_id}.non-API model discovery must be inert")
        operation = discovery["operation"]
        if operation is not None and (
            not isinstance(operation, str) or not 1 <= len(operation) <= 128
        ):
            _fail(f"{account_id}.model_discovery.operation is invalid")

        credentials = _object(account["credentials"], f"{account_id}.credentials")
        _strict_keys(credentials, {"inference", "admin_finance"}, f"{account_id}.credentials")
        inference_ref = _credential(credentials["inference"], f"{account_id}.credentials.inference", inference=True)
        admin_ref = _credential(credentials["admin_finance"], f"{account_id}.credentials.admin_finance", inference=False)
        if inference_ref in inference_refs:
            _fail(f"duplicate inference credential reference for {account_id}")
        inference_refs.add(str(inference_ref))
        if admin_ref is not None:
            if admin_ref in admin_refs or admin_ref == inference_ref:
                _fail(f"unsafe or duplicate admin credential reference for {account_id}")
            admin_refs.add(admin_ref)
        if discovery["credential_scope"] == "admin_finance" and admin_ref is None:
            _fail(f"{account_id} model discovery needs an admin credential slot")

        financial = _object(account["financial_capabilities"], f"{account_id}.financial_capabilities")
        if set(financial) != set(FINANCE_CAPABILITIES):
            _fail(f"{account_id}.financial_capabilities differ from the v1 contract")
        if any(value not in FINANCE_ACCESS_MODES for value in financial.values()):
            _fail(f"{account_id}.financial_capabilities contain an invalid access mode")
        if any(value in {"admin_api", "cloud_billing_api"} for value in financial.values()) and admin_ref is None:
            _fail(f"{account_id} needs a separate admin_finance credential slot")

        direct_coverage: set[str] = set()
        for raw_endpoint in _array(account["finance_endpoints"], f"{account_id}.finance_endpoints"):
            endpoint = _object(raw_endpoint, f"{account_id}.finance_endpoint")
            _strict_keys(
                endpoint,
                {"capabilities", "access", "method", "endpoint", "operation", "credential_scope"},
                f"{account_id}.finance_endpoint",
            )
            capabilities = _array(endpoint["capabilities"], f"{account_id}.finance_endpoint.capabilities")
            if (
                not capabilities
                or len(capabilities) != len(set(capabilities))
                or any(capability not in FINANCE_CAPABILITIES for capability in capabilities)
            ):
                _fail(f"{account_id}.finance_endpoint capabilities are invalid")
            access = endpoint["access"]
            if access not in API_ACCESS_MODES:
                _fail(f"{account_id}.finance_endpoint access is invalid")
            for capability in capabilities:
                if financial[capability] != access:
                    _fail(f"{account_id}.{capability} endpoint disagrees with its capability mode")
                if access == "direct_api":
                    direct_coverage.add(capability)
            expected_scope = "inference" if access == "direct_api" else "admin_finance"
            if endpoint["credential_scope"] != expected_scope:
                _fail(f"{account_id}.finance_endpoint uses the wrong credential scope")
            if endpoint["method"] not in {"GET", "POST"}:
                _fail(f"{account_id}.finance_endpoint method is invalid")
            _https_url(endpoint["endpoint"], f"{account_id}.finance_endpoint.endpoint", allow_template=True)
            endpoint_operation = endpoint["operation"]
            if endpoint_operation is not None and (
                not isinstance(endpoint_operation, str) or not 1 <= len(endpoint_operation) <= 160
            ):
                _fail(f"{account_id}.finance_endpoint operation is invalid")
        required_direct = {
            capability
            for capability, access in financial.items()
            if access == "direct_api"
        }
        if direct_coverage != required_direct:
            _fail(f"{account_id} direct financial APIs need explicit endpoints")

        sources = _array(account["official_sources"], f"{account_id}.official_sources")
        if not sources or len(sources) > 16:
            _fail(f"{account_id}.official_sources are invalid")
        source_urls: set[str] = set()
        for raw_source in sources:
            source = _object(raw_source, f"{account_id}.official_source")
            _strict_keys(source, {"title", "url", "verified_on"}, f"{account_id}.official_source")
            if not isinstance(source["title"], str) or not 1 <= len(source["title"]) <= 160:
                _fail(f"{account_id}.official_source.title is invalid")
            source_url = _https_url(source["url"], f"{account_id}.official_source.url", allow_template=False)
            hostname = urlsplit(source_url).hostname
            if hostname not in OFFICIAL_SOURCE_HOSTS:
                _fail(f"{account_id} source is not on the reviewed official-domain allowlist")
            if source_url in source_urls:
                _fail(f"{account_id} has a duplicate official source")
            source_urls.add(source_url)
            if _date(source["verified_on"], f"{account_id}.official_source.verified_on") != verified_on:
                _fail(f"{account_id} source verification date differs from the registry")

        limitations = _array(account["limitations"], f"{account_id}.limitations")
        if (
            not limitations
            or len(limitations) != len(set(limitations))
            or any(not isinstance(item, str) or not 1 <= len(item) <= 512 for item in limitations)
        ):
            _fail(f"{account_id}.limitations are invalid")

    if tuple(account_ids) != PROVIDER_IDS:
        _fail("provider accounts differ from the reviewed 21-account order")

    mappings = _array(root["deployment_mappings"], "deployment_mappings")
    deployment_ids: set[str] = set()
    for raw_mapping in mappings:
        mapping = _object(raw_mapping, "deployment mapping")
        _strict_keys(
            mapping,
            {
                "deployment_id",
                "card_id",
                "developer_id",
                "inference_provider_account_id",
                "exact_model_id",
                "activation_state",
                "source",
            },
            "deployment mapping",
        )
        deployment_id = _identifier(mapping["deployment_id"], "deployment id")
        _identifier(mapping["card_id"], f"{deployment_id}.card_id")
        _identifier(mapping["developer_id"], f"{deployment_id}.developer_id")
        provider_id = _identifier(
            mapping["inference_provider_account_id"],
            f"{deployment_id}.inference_provider_account_id",
        )
        if deployment_id in deployment_ids:
            _fail(f"duplicate deployment mapping {deployment_id}")
        if provider_id not in accounts:
            _fail(f"deployment {deployment_id} references an unknown inference provider")
        if not isinstance(mapping["exact_model_id"], str) or not 1 <= len(mapping["exact_model_id"]) <= 200:
            _fail(f"deployment {deployment_id} exact_model_id is invalid")
        if mapping["activation_state"] not in {
            "configured_not_network_verified",
            "canary_verified",
            "disabled",
        }:
            _fail(f"deployment {deployment_id} activation_state is invalid")
        if mapping["source"] != "local_orchestrator_configuration":
            _fail(f"deployment {deployment_id} source is invalid")
        deployment_ids.add(deployment_id)
    return root


def verify_catalogue(registry: dict[str, Any], catalogue_path: Path) -> list[str]:
    catalogue = _load_json(catalogue_path, MAX_CATALOGUE_BYTES, "model catalogue")
    raw_accounts = _array(catalogue.get("provider_accounts"), "model catalogue provider_accounts")
    catalogue_accounts = {
        _identifier(account.get("id"), "model catalogue provider account id"): account
        for account in raw_accounts
        if isinstance(account, dict)
    }
    registry_accounts = {
        account["id"]: account
        for account in registry["provider_accounts"]
    }
    if set(catalogue_accounts) != set(registry_accounts):
        _fail("registry and model catalogue provider accounts differ")
    for account_id, registry_account in registry_accounts.items():
        catalogue_account = catalogue_accounts[account_id]
        if catalogue_account.get("credential_ref") != registry_account["credentials"]["inference"]["credential_ref"]:
            _fail(f"{account_id} inference credential reference differs from the model catalogue")
        if catalogue_account.get("financial_capabilities") != registry_account["financial_capabilities"]:
            _fail(f"{account_id} financial capabilities differ from the model catalogue")

    cards = {
        _identifier(card.get("card_id"), "model card id"): card
        for card in _array(catalogue.get("cards"), "model catalogue cards")
        if isinstance(card, dict)
    }
    catalogue_deployments: set[str] = set()
    mappings = {mapping["deployment_id"]: mapping for mapping in registry["deployment_mappings"]}
    for card_id, card in cards.items():
        for deployment_id in _array(card.get("deployment_ids"), f"{card_id}.deployment_ids"):
            deployment_id = _identifier(deployment_id, f"{card_id}.deployment_id")
            catalogue_deployments.add(deployment_id)
            mapping = mappings.get(deployment_id)
            if mapping is None:
                _fail(f"catalogue deployment {deployment_id} lacks a provider mapping")
            if mapping["card_id"] != card_id:
                _fail(f"deployment {deployment_id} maps to the wrong card")
            if mapping["exact_model_id"] != card.get("exact_model_id"):
                _fail(f"deployment {deployment_id} exact model differs from its card")
            if card.get("integration_stage") != "configured":
                _fail(f"deployment {deployment_id} belongs to a non-configured card")
    if set(mappings) != catalogue_deployments:
        _fail("provider deployment mappings differ from catalogue deployment IDs")

    aya = cards.get("cohere-aya-expanse-8b")
    if aya is None or aya.get("integration_stage") != "quarantined" or aya.get("deployment_ids") != []:
        _fail("retired Cohere Aya Expanse 8B must remain non-routable and quarantined")
    if aya.get("exact_model_id") != "c4ai-aya-expanse-8b":
        _fail("retired Cohere Aya Expanse 8B exact ID is not preserved")
    aya_prices = aya.get("pricing")
    if not isinstance(aya_prices, list) or len(aya_prices) != 1:
        _fail("Cohere Aya Expanse 8B must preserve the historical shared TXT price")
    aya_price = aya_prices[0]
    if not isinstance(aya_price, dict) or (
        aya_price.get("input_usd_per_million") != "0.50"
        or aya_price.get("output_usd_per_million") != "1.50"
        or aya_price.get("declared_simulation_usd") != "8.00"
        or aya_price.get("source") != "catalogue_modeles_IA_API_2026.txt"
        or aya_price.get("confidence") != "shared_source_price"
    ):
        _fail("Cohere Aya Expanse 8B historical TXT price differs from the source")
    if catalogue_accounts["tencent"].get("name") != "Tencent Cloud TokenHub":
        _fail("Tencent account must target TokenHub rather than legacy Hunyuan")

    return sorted(
        card_id
        for card_id, card in cards.items()
        if card.get("exact_model_id") is None
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the public provider integration registry"
    )
    parser.add_argument("registry", type=Path)
    parser.add_argument("--catalogue", type=Path)
    arguments = parser.parse_args()
    try:
        registry = load_and_validate(arguments.registry)
        unresolved: list[str] | None = None
        if arguments.catalogue is not None:
            unresolved = verify_catalogue(registry, arguments.catalogue)
    except RegistryError as exc:
        print(f"validate-provider-integrations: {exc}", file=sys.stderr)
        return 1
    suffix = ""
    if unresolved is not None:
        suffix = f", {len(unresolved)} unresolved exact model IDs"
    print(
        "provider integration registry valid: "
        f"{len(registry['provider_accounts'])} accounts, "
        f"{len(registry['deployment_mappings'])} deployment mappings{suffix}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
