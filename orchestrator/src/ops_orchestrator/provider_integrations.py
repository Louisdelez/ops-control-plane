"""Strict, non-secret runtime view of the reviewed provider registry.

The catalogue validator used during packaging is intentionally not imported at
runtime.  This module applies the same fail-closed v1 contract from the installed
package, without adding a JSON Schema dependency or resolving credentials.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
from urllib.parse import urlsplit

from .errors import ConfigurationError


MAX_REGISTRY_BYTES = 1_048_576
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
REVIEWED_PROVIDER_IDS = (
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
_PROTOCOLS = {
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
_AUTH_SCHEMES = {
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
_OFFICIAL_SOURCE_HOSTS = {
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
_IDENTIFIER = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,127}\Z")
_REVISION = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,127}\Z")
_TEMPLATE_FIELD = re.compile(r"\{[a-z][a-z0-9_]{0,63}\}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object field")
        result[key] = value
    return result


@dataclass(frozen=True)
class CredentialIntegration:
    reference: str | None
    auth_scheme: str
    header_name: str | None
    value_prefix: str | None


@dataclass(frozen=True)
class FinanceEndpoint:
    capabilities: tuple[str, ...]
    access: str
    method: str
    endpoint: str
    operation: str | None
    credential_scope: str


@dataclass(frozen=True)
class ProviderIntegration:
    account_id: str
    display_name: str
    cash_balance_mode: str
    financial_capabilities: dict[str, str]
    inference_credential: CredentialIntegration
    admin_credential: CredentialIntegration
    finance_endpoints: tuple[FinanceEndpoint, ...]
    public_record: dict[str, Any]

    def cash_balance_endpoint(self) -> FinanceEndpoint | None:
        matches = [
            endpoint
            for endpoint in self.finance_endpoints
            if "cash_balance" in endpoint.capabilities
        ]
        return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True)
class ProviderIntegrationRegistry:
    revision: str
    verified_on: str
    accounts: tuple[ProviderIntegration, ...]
    deployment_mappings: tuple[dict[str, Any], ...]

    @property
    def accounts_by_id(self) -> dict[str, ProviderIntegration]:
        return {account.account_id: account for account in self.accounts}

    def account(self, account_id: str) -> ProviderIntegration:
        try:
            return self.accounts_by_id[account_id]
        except KeyError as exc:
            raise ConfigurationError(f"unknown provider integration: {account_id}") from exc


def _fail(message: str) -> None:
    raise ConfigurationError(f"provider integration registry: {message}")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be an object")
    return value


def _array(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        _fail(f"{label} must be an array")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        _fail(f"{label} fields differ from the v1 contract")


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        _fail(f"{label} is not a bounded identifier")
    return value


def _canonical_date(value: Any, label: str) -> str:
    if not isinstance(value, str):
        _fail(f"{label} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        _fail(f"{label} must be an ISO date")
    if parsed.isoformat() != value:
        _fail(f"{label} must be a canonical ISO date")
    return value


def _https_url(value: Any, label: str, *, allow_template: bool) -> str:
    if not isinstance(value, str) or not value.startswith("https://") or len(value) > 1024:
        _fail(f"{label} must be a bounded HTTPS URL")
    if "\x00" in value or "\\" in value or ".." in value:
        _fail(f"{label} contains an unsafe URL component")
    plain = _TEMPLATE_FIELD.sub("template", value) if allow_template else value
    if "{" in plain or "}" in plain:
        _fail(f"{label} contains an invalid template field")
    parsed = urlsplit(plain)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        _fail(f"{label} is not a safe HTTPS URL")
    return value


def _bounded_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or "\x00" in value:
        _fail(f"{label} is invalid")
    return value


def _credential(value: Any, label: str, *, inference: bool) -> CredentialIntegration:
    raw = _object(value, label)
    _exact_keys(raw, {"credential_ref", "auth_scheme", "header_name", "value_prefix"}, label)
    reference = raw["credential_ref"]
    scheme = raw["auth_scheme"]
    header = raw["header_name"]
    prefix = raw["value_prefix"]
    if scheme not in _AUTH_SCHEMES:
        _fail(f"{label}.auth_scheme is invalid")
    if reference is None:
        if inference or scheme != "none" or header is not None or prefix is not None:
            _fail(f"{label} is not an inert optional credential")
    else:
        expected = "kv-infra-shared/data/llm/" if inference else "kv-infra-shared/data/llm-admin/"
        if (
            not isinstance(reference, str)
            or not reference.startswith(expected)
            or len(reference) > 256
            or ".." in reference
            or not re.fullmatch(r"[a-z0-9/_-]+", reference)
        ):
            _fail(f"{label}.credential_ref is unsafe")
        if scheme == "none":
            _fail(f"{label} has no authentication scheme")
    if header is not None and (not isinstance(header, str) or not 1 <= len(header) <= 64):
        _fail(f"{label}.header_name is invalid")
    if prefix is not None and (not isinstance(prefix, str) or len(prefix) > 32):
        _fail(f"{label}.value_prefix is invalid")
    return CredentialIntegration(reference, scheme, header, prefix)


def _read_document(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ConfigurationError("provider integration registry is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size < 2
            or info.st_size > MAX_REGISTRY_BYTES
            or info.st_uid not in {0, os.geteuid()}
            or info.st_mode & 0o022
        ):
            _fail("file must be bounded, regular, non-symlink and not writable by group/others")
        chunks: list[bytes] = []
        remaining = MAX_REGISTRY_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != info.st_size:
            _fail("file changed or could not be read completely")
    finally:
        os.close(descriptor)
    try:
        document = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ConfigurationError("provider integration registry is invalid UTF-8 JSON") from exc
    return _object(document, "root")


def load_provider_integrations(path: Path) -> ProviderIntegrationRegistry:
    root = _read_document(path)
    _exact_keys(
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
        "root",
    )
    if root["schema_version"] != 1:
        _fail("schema_version must be 1")
    revision = root["revision"]
    if not isinstance(revision, str) or not _REVISION.fullmatch(revision):
        _fail("revision is invalid")
    verified_on = _canonical_date(root["verified_on"], "verified_on")
    if root["finance_capability_names"] != list(FINANCE_CAPABILITIES):
        _fail("finance capabilities differ from the v1 contract")
    if root["finance_access_modes"] != list(FINANCE_ACCESS_MODES):
        _fail("finance modes differ from the v1 contract")

    raw_accounts = _array(root["provider_accounts"], "provider_accounts")
    if len(raw_accounts) != len(REVIEWED_PROVIDER_IDS):
        _fail("provider account count differs from the reviewed v1 set")
    integrations: list[ProviderIntegration] = []
    inference_references: set[str] = set()
    admin_references: set[str] = set()
    account_ids: list[str] = []
    for raw_value in raw_accounts:
        account = _object(raw_value, "provider account")
        _exact_keys(
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
        if account_id in account_ids:
            _fail(f"duplicate provider account {account_id}")
        account_ids.append(account_id)
        display_name = _bounded_text(account["display_name"], f"{account_id}.display_name", 160)

        inference = _object(account["inference"], f"{account_id}.inference")
        _exact_keys(inference, {"protocols", "sites", "model_discovery"}, f"{account_id}.inference")
        protocols = _array(inference["protocols"], f"{account_id}.protocols")
        if (
            not protocols
            or len(protocols) > 8
            or len(protocols) != len(set(protocols))
            or any(item not in _PROTOCOLS for item in protocols)
        ):
            _fail(f"{account_id}.protocols are invalid")
        sites = _array(inference["sites"], f"{account_id}.sites")
        if not 1 <= len(sites) <= 16:
            _fail(f"{account_id}.sites are invalid")
        site_ids: set[str] = set()
        for raw_site in sites:
            site = _object(raw_site, f"{account_id}.site")
            _exact_keys(site, {"id", "region", "base_url", "status"}, f"{account_id}.site")
            site_id = _identifier(site["id"], f"{account_id}.site.id")
            if site_id in site_ids:
                _fail(f"duplicate site {account_id}/{site_id}")
            site_ids.add(site_id)
            _bounded_text(site["region"], f"{account_id}/{site_id}.region", 64)
            if site["status"] not in {"documented", "template", "unverified"}:
                _fail(f"{account_id}/{site_id}.status is invalid")
            if site["base_url"] is None:
                if site["status"] != "unverified":
                    _fail(f"{account_id}/{site_id} lacks a documented URL")
            else:
                _https_url(site["base_url"], f"{account_id}/{site_id}.base_url", allow_template=True)

        discovery = _object(inference["model_discovery"], f"{account_id}.model_discovery")
        _exact_keys(
            discovery,
            {"mode", "method", "endpoint", "operation", "credential_scope"},
            f"{account_id}.model_discovery",
        )
        mode = discovery["mode"]
        if mode not in {"api", "control_plane_api", "static_documentation", "unverified"}:
            _fail(f"{account_id}.model_discovery.mode is invalid")
        if mode in {"api", "control_plane_api"}:
            if discovery["method"] not in {"GET", "POST"} or discovery["endpoint"] is None:
                _fail(f"{account_id}.model_discovery is incomplete")
            _https_url(discovery["endpoint"], f"{account_id}.model_discovery.endpoint", allow_template=True)
            if discovery["credential_scope"] not in {"inference", "admin_finance"}:
                _fail(f"{account_id}.model_discovery credential scope is invalid")
        elif (
            any(
                discovery[field] is not None
                for field in ("method", "endpoint", "operation")
            )
            or discovery["credential_scope"] != "none"
        ):
            _fail(f"{account_id}.model_discovery must be inert")
        if discovery["operation"] is not None:
            _bounded_text(discovery["operation"], f"{account_id}.model_discovery.operation", 128)

        credentials = _object(account["credentials"], f"{account_id}.credentials")
        _exact_keys(credentials, {"inference", "admin_finance"}, f"{account_id}.credentials")
        inference_credential = _credential(
            credentials["inference"],
            f"{account_id}.credentials.inference",
            inference=True,
        )
        admin_credential = _credential(
            credentials["admin_finance"],
            f"{account_id}.credentials.admin_finance",
            inference=False,
        )
        assert inference_credential.reference is not None
        if inference_credential.reference in inference_references:
            _fail(f"duplicate inference credential reference for {account_id}")
        inference_references.add(inference_credential.reference)
        if admin_credential.reference is not None:
            if (
                admin_credential.reference in admin_references
                or admin_credential.reference == inference_credential.reference
            ):
                _fail(f"duplicate or shared admin credential reference for {account_id}")
            admin_references.add(admin_credential.reference)
        if discovery["credential_scope"] == "admin_finance" and admin_credential.reference is None:
            _fail(f"{account_id}.model_discovery lacks an admin credential slot")

        financial = _object(account["financial_capabilities"], f"{account_id}.financial_capabilities")
        if set(financial) != set(FINANCE_CAPABILITIES) or any(
            value not in FINANCE_ACCESS_MODES for value in financial.values()
        ):
            _fail(f"{account_id}.financial_capabilities are invalid")
        if (
            any(
                value in {"admin_api", "cloud_billing_api"}
                for value in financial.values()
            )
            and admin_credential.reference is None
        ):
            _fail(f"{account_id} lacks its separate admin credential slot")

        raw_endpoints = _array(account["finance_endpoints"], f"{account_id}.finance_endpoints")
        if len(raw_endpoints) > 32:
            _fail(f"{account_id}.finance_endpoints exceeds its bound")
        endpoints: list[FinanceEndpoint] = []
        direct_coverage: set[str] = set()
        endpoint_keys: set[tuple[str, str, str | None]] = set()
        for raw_endpoint in raw_endpoints:
            endpoint = _object(raw_endpoint, f"{account_id}.finance_endpoint")
            _exact_keys(
                endpoint,
                {
                    "capabilities",
                    "access",
                    "method",
                    "endpoint",
                    "operation",
                    "credential_scope",
                },
                f"{account_id}.finance_endpoint",
            )
            capabilities = _array(endpoint["capabilities"], f"{account_id}.finance_endpoint.capabilities")
            if (
                not capabilities
                or len(capabilities) > 6
                or len(capabilities) != len(set(capabilities))
                or any(item not in FINANCE_CAPABILITIES for item in capabilities)
            ):
                _fail(f"{account_id}.finance_endpoint capabilities are invalid")
            access = endpoint["access"]
            if access not in {"direct_api", "admin_api", "cloud_billing_api"}:
                _fail(f"{account_id}.finance_endpoint access is invalid")
            for capability in capabilities:
                if financial[capability] != access:
                    _fail(f"{account_id}.{capability} endpoint contradicts its mode")
                if access == "direct_api":
                    direct_coverage.add(capability)
            credential_scope = endpoint["credential_scope"]
            expected_scope = "inference" if access == "direct_api" else "admin_finance"
            if credential_scope != expected_scope:
                _fail(f"{account_id}.finance_endpoint has the wrong credential scope")
            method = endpoint["method"]
            if method not in {"GET", "POST"}:
                _fail(f"{account_id}.finance_endpoint method is invalid")
            endpoint_url = _https_url(
                endpoint["endpoint"],
                f"{account_id}.finance_endpoint.endpoint",
                allow_template=True,
            )
            operation = endpoint["operation"]
            key = (method, endpoint_url, operation)
            if key in endpoint_keys:
                _fail(f"{account_id} has a duplicate finance endpoint")
            endpoint_keys.add(key)
            if operation is not None:
                _bounded_text(operation, f"{account_id}.finance_endpoint.operation", 160)
            endpoints.append(
                FinanceEndpoint(
                    tuple(capabilities),
                    access,
                    method,
                    endpoint_url,
                    operation,
                    credential_scope,
                )
            )
        expected_direct = {name for name, access in financial.items() if access == "direct_api"}
        if direct_coverage != expected_direct:
            _fail(f"{account_id} direct finance endpoints do not cover the declared capabilities")

        sources = _array(account["official_sources"], f"{account_id}.official_sources")
        if not 1 <= len(sources) <= 16:
            _fail(f"{account_id}.official_sources are invalid")
        source_urls: set[str] = set()
        for raw_source in sources:
            source = _object(raw_source, f"{account_id}.official_source")
            _exact_keys(source, {"title", "url", "verified_on"}, f"{account_id}.official_source")
            _bounded_text(source["title"], f"{account_id}.official_source.title", 160)
            url = _https_url(source["url"], f"{account_id}.official_source.url", allow_template=False)
            if urlsplit(url).hostname not in _OFFICIAL_SOURCE_HOSTS or url in source_urls:
                _fail(f"{account_id}.official_source is not uniquely on the reviewed allowlist")
            source_urls.add(url)
            if _canonical_date(source["verified_on"], f"{account_id}.official_source.verified_on") != verified_on:
                _fail(f"{account_id}.official_source verification date differs")
        limitations = _array(account["limitations"], f"{account_id}.limitations")
        if not limitations or len(limitations) > 16 or len(limitations) != len(set(limitations)):
            _fail(f"{account_id}.limitations are invalid")
        for limitation in limitations:
            _bounded_text(limitation, f"{account_id}.limitation", 512)

        # Preserve public facts while deliberately removing internal credential
        # references.  Runtime callers receive capability metadata, never paths.
        public_record = json.loads(json.dumps(account))
        public_record["credentials"] = {
            "inference": {"auth_scheme": inference_credential.auth_scheme},
            "admin_finance": {
                "auth_scheme": admin_credential.auth_scheme,
                "separate_credential_required": admin_credential.reference is not None,
            },
        }
        integrations.append(
            ProviderIntegration(
                account_id=account_id,
                display_name=display_name,
                cash_balance_mode=financial["cash_balance"],
                financial_capabilities=dict(financial),
                inference_credential=inference_credential,
                admin_credential=admin_credential,
                finance_endpoints=tuple(endpoints),
                public_record=public_record,
            )
        )

    if tuple(account_ids) != REVIEWED_PROVIDER_IDS:
        _fail("provider accounts differ from the reviewed v1 order")

    raw_mappings = _array(root["deployment_mappings"], "deployment_mappings")
    if len(raw_mappings) > 1024:
        _fail("deployment_mappings exceeds its bound")
    mappings: list[dict[str, Any]] = []
    deployment_ids: set[str] = set()
    for raw_mapping in raw_mappings:
        mapping = _object(raw_mapping, "deployment mapping")
        _exact_keys(
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
        if deployment_id in deployment_ids:
            _fail(f"duplicate deployment mapping {deployment_id}")
        deployment_ids.add(deployment_id)
        _identifier(mapping["card_id"], f"{deployment_id}.card_id")
        _identifier(mapping["developer_id"], f"{deployment_id}.developer_id")
        provider_id = _identifier(mapping["inference_provider_account_id"], f"{deployment_id}.provider")
        if provider_id not in account_ids:
            _fail(f"{deployment_id} references an unknown provider")
        _bounded_text(mapping["exact_model_id"], f"{deployment_id}.exact_model_id", 200)
        if mapping["activation_state"] not in {"configured_not_network_verified", "canary_verified", "disabled"}:
            _fail(f"{deployment_id}.activation_state is invalid")
        if mapping["source"] != "local_orchestrator_configuration":
            _fail(f"{deployment_id}.source is invalid")
        mappings.append(json.loads(json.dumps(mapping)))

    return ProviderIntegrationRegistry(revision, verified_on, tuple(integrations), tuple(mappings))
