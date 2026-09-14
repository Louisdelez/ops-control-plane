"""Read-only, allowlisted provider finance refreshes.

Only three provider cash-balance schemas are implemented because their exact
GET endpoints and response shapes are documented by the providers.  Admin and
cloud billing APIs deliberately remain descriptive until a distinct read-only
administrative credential and signed connector have been reviewed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping
import urllib.error
import urllib.request
import uuid

from .database import Database, MAX_FINANCE_SNAPSHOTS_PER_ACCOUNT
from .errors import ConfigurationError, ProviderUnavailable, ValidationError
from .provider_integrations import ProviderIntegration, ProviderIntegrationRegistry
from .providers import _read_secret


MAX_FINANCE_RESPONSE_BYTES = 65_536
MAX_FINANCE_TIMEOUT_SECONDS = 10.0
MAX_HISTORY_ROWS = 200

# Credential values never live in configuration.  systemd/OpenBao may expose a
# root-resolved credential file path through these fixed environment names.
FINANCE_CREDENTIAL_FILE_ENVS = {
    "deepseek": "DEEPSEEK_API_KEY_FILE",
    "moonshot": "MOONSHOT_API_KEY_FILE",
    "stepfun": "STEPFUN_API_KEY_FILE",
}

# This is both an egress allowlist and a schema dispatch table.  A registry edit
# cannot redirect credentials to another origin without a matching code review.
DIRECT_CASH_BALANCE_ENDPOINTS = {
    "deepseek": "https://api.deepseek.com/user/balance",
    "moonshot": "https://api.moonshot.ai/v1/users/me/balance",
    "stepfun": "https://api.stepfun.com/v1/accounts",
}


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_FINANCE_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _RejectRedirects(),
)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object field")
        result[key] = value
    return result


class FinanceNetworkError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _network_error_code(status: int) -> str:
    if status in {401, 403}:
        return "authentication_rejected"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "provider_server_error"
    return "provider_http_error"


def _get_json(url: str, credential: str, timeout: float, maximum_bytes: int) -> dict[str, Any]:
    # `url` has already matched a literal reviewed endpoint.  No proxy and no
    # redirect handler are used, so Authorization cannot leave that origin.
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {credential}",
        },
        method="GET",
    )
    try:
        with _FINANCE_OPENER.open(request, timeout=timeout) as response:
            if response.status < 200 or response.status >= 300:
                raise FinanceNetworkError(_network_error_code(response.status))
            declared = response.headers.get("Content-Length")
            if declared is not None:
                if not declared.isascii() or not declared.isdigit():
                    raise FinanceNetworkError("invalid_content_length")
                if int(declared) > maximum_bytes:
                    raise FinanceNetworkError("response_too_large")
            raw = response.read(maximum_bytes + 1)
    except urllib.error.HTTPError as exc:
        # Never read or expose provider error bodies; they can contain account
        # data and occasionally echo request metadata.
        raise FinanceNetworkError(_network_error_code(exc.code)) from exc
    except FinanceNetworkError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError):
        raise FinanceNetworkError("connection_failed") from None
    if len(raw) > maximum_bytes:
        raise FinanceNetworkError("response_too_large")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            parse_float=Decimal,
            parse_int=int,
            object_pairs_hook=_unique_object,
        )
    except (UnicodeError, json.JSONDecodeError, InvalidOperation, ValueError):
        raise FinanceNetworkError("invalid_json") from None
    if not isinstance(value, dict):
        raise FinanceNetworkError("invalid_json_root")
    return value


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{label}_fields")
    return value


def _amount(value: Any, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(label)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(label)
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(label) from exc
    if not amount.is_finite() or amount < 0 or amount > Decimal("1000000000000000"):
        raise ValueError(label)
    if amount.as_tuple().exponent < -12:
        raise ValueError(label)
    rendered = format(amount, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _parse_deepseek(value: dict[str, Any]) -> tuple[bool | None, list[dict[str, Any]]]:
    root = _exact_keys(value, {"is_available", "balance_infos"}, "deepseek_root")
    is_available = root["is_available"]
    infos = root["balance_infos"]
    if not isinstance(is_available, bool) or not isinstance(infos, list) or not 1 <= len(infos) <= 8:
        raise ValueError("deepseek_envelope")
    balances: list[dict[str, Any]] = []
    currencies: set[str] = set()
    for raw in infos:
        info = _exact_keys(
            raw,
            {"currency", "total_balance", "granted_balance", "topped_up_balance"},
            "deepseek_balance",
        )
        currency = info["currency"]
        if currency not in {"CNY", "USD"} or currency in currencies:
            raise ValueError("deepseek_currency")
        currencies.add(currency)
        balances.append(
            {
                "currency": currency,
                "available": _amount(info["total_balance"], "deepseek_total"),
                "granted": _amount(info["granted_balance"], "deepseek_granted"),
                "topped_up": _amount(info["topped_up_balance"], "deepseek_topped_up"),
            }
        )
    return is_available, balances


def _parse_moonshot(value: dict[str, Any]) -> tuple[bool | None, list[dict[str, Any]]]:
    root = _exact_keys(value, {"code", "data", "scode", "status"}, "moonshot_root")
    if (
        isinstance(root["code"], bool)
        or not isinstance(root["code"], int)
        or root["code"] != 0
        or root["scode"] != "0x0"
        or root["status"] is not True
    ):
        raise ValueError("moonshot_status")
    data = _exact_keys(
        root["data"],
        {"available_balance", "voucher_balance", "cash_balance"},
        "moonshot_data",
    )
    available = _amount(data["available_balance"], "moonshot_available")
    return None, [
        {
            # The documented response does not identify a currency.  Keeping it
            # null is more truthful than inferring USD from the global hostname.
            "currency": None,
            "available": available,
            "cash": _amount(data["cash_balance"], "moonshot_cash"),
            "voucher": _amount(data["voucher_balance"], "moonshot_voucher"),
        }
    ]


def _parse_stepfun(value: dict[str, Any]) -> tuple[bool | None, list[dict[str, Any]]]:
    root = _exact_keys(
        value,
        {"object", "type", "balance", "total_cash_balance", "total_voucher_balance"},
        "stepfun_root",
    )
    if root["object"] != "account" or root["type"] not in {"prepaid", "postpaid"}:
        raise ValueError("stepfun_envelope")
    available = _amount(root["balance"], "stepfun_balance")
    return None, [
        {
            # StepFun's endpoint likewise omits an ISO currency code.
            "currency": None,
            "available": available,
            "total_cash": _amount(root["total_cash_balance"], "stepfun_cash"),
            "total_voucher": _amount(root["total_voucher_balance"], "stepfun_voucher"),
            "billing_type": root["type"],
        }
    ]


_PARSERS: dict[
    str, Callable[[dict[str, Any]], tuple[bool | None, list[dict[str, Any]]]]
] = {
    "deepseek": _parse_deepseek,
    "moonshot": _parse_moonshot,
    "stepfun": _parse_stepfun,
}


class ProviderFinanceManager:
    def __init__(
        self,
        registry: ProviderIntegrationRegistry,
        database: Database | None,
        environment: Mapping[str, str],
        *,
        timeout_seconds: float,
        maximum_response_bytes: int,
        transport: Callable[[str, str, float, int], dict[str, Any]] | None = None,
    ):
        self.registry = registry
        self.database = database
        self.environment = dict(environment)
        self.timeout_seconds = min(
            max(float(timeout_seconds), 1.0), MAX_FINANCE_TIMEOUT_SECONDS
        )
        self.maximum_response_bytes = min(
            max(int(maximum_response_bytes), 1024), MAX_FINANCE_RESPONSE_BYTES
        )
        self.transport = transport or _get_json
        self._refresh_lock = threading.Lock()
        self._validate_direct_connectors()

    def _validate_direct_connectors(self) -> None:
        for account_id, reviewed_endpoint in DIRECT_CASH_BALANCE_ENDPOINTS.items():
            account = self.registry.account(account_id)
            endpoint = account.cash_balance_endpoint()
            credential = account.inference_credential
            if (
                account.cash_balance_mode != "direct_api"
                or endpoint is None
                or endpoint.access != "direct_api"
                or endpoint.method != "GET"
                or endpoint.endpoint != reviewed_endpoint
                or endpoint.credential_scope != "inference"
                or credential.auth_scheme != "bearer"
                or credential.header_name != "Authorization"
                or credential.value_prefix != "Bearer "
            ):
                raise ConfigurationError(
                    f"provider finance connector contract changed for {account_id}"
                )

    @staticmethod
    def _static_status(account: ProviderIntegration) -> str:
        if account.cash_balance_mode in {"admin_api", "cloud_billing_api"}:
            return "requires_admin_credential"
        if account.cash_balance_mode == "console_only":
            return "console_only"
        if account.cash_balance_mode in {"not_applicable", "unknown"}:
            return "unsupported"
        if account.cash_balance_mode == "direct_api" and account.account_id not in _PARSERS:
            return "unsupported_schema"
        return "never_refreshed"

    def _empty_state(
        self, account: ProviderIntegration, status: str
    ) -> dict[str, Any]:
        return {
            "snapshot_id": None,
            "provider_account_id": account.account_id,
            "registry_revision": self.registry.revision,
            "captured_at": None,
            "cash_balance_mode": account.cash_balance_mode,
            "status": status,
            "is_available": None,
            "balances": [],
            "error_code": None,
            "duration_ms": None,
        }

    def _credential(self, account_id: str) -> str:
        environment_name = FINANCE_CREDENTIAL_FILE_ENVS[account_id]
        raw_path = self.environment.get(environment_name, "")
        if not raw_path or not os.path.isabs(raw_path):
            raise ProviderUnavailable("credential file is not configured")
        return _read_secret(Path(raw_path))

    def _snapshot(
        self,
        account: ProviderIntegration,
        *,
        status: str,
        is_available: bool | None,
        balances: list[dict[str, Any]],
        error_code: str | None,
        duration_ms: int,
    ) -> dict[str, Any]:
        if self.database is None:
            return {
                "snapshot_id": str(uuid.uuid4()),
                "provider_account_id": account.account_id,
                "registry_revision": self.registry.revision,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "cash_balance_mode": account.cash_balance_mode,
                "status": status,
                "is_available": is_available,
                "balances": json.loads(json.dumps(balances)),
                "error_code": error_code,
                "duration_ms": duration_ms,
            }
        return self.database.record_provider_finance_snapshot(
            provider_account_id=account.account_id,
            registry_revision=self.registry.revision,
            cash_balance_mode=account.cash_balance_mode,
            status=status,
            is_available=is_available,
            balances=balances,
            error_code=error_code,
            duration_ms=duration_ms,
        )

    def _refresh_one_locked(self, account: ProviderIntegration) -> dict[str, Any]:
        static = self._static_status(account)
        if static != "never_refreshed":
            return self._empty_state(account, static)
        started = time.monotonic()
        try:
            credential = self._credential(account.account_id)
        except ProviderUnavailable:
            return self._snapshot(
                account,
                status="credential_unavailable",
                is_available=None,
                balances=[],
                error_code="credential_unavailable",
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        endpoint = DIRECT_CASH_BALANCE_ENDPOINTS[account.account_id]
        try:
            raw = self.transport(
                endpoint,
                credential,
                self.timeout_seconds,
                self.maximum_response_bytes,
            )
            is_available, balances = _PARSERS[account.account_id](raw)
        except FinanceNetworkError as exc:
            return self._snapshot(
                account,
                status="provider_unavailable",
                is_available=None,
                balances=[],
                error_code=exc.code,
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        except (ValueError, KeyError, TypeError, InvalidOperation):
            return self._snapshot(
                account,
                status="unsupported_schema",
                is_available=None,
                balances=[],
                error_code="response_schema_mismatch",
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        return self._snapshot(
            account,
            status="ok",
            is_available=is_available,
            balances=balances,
            error_code=None,
            duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        )

    def refresh(self, account_id: str) -> dict[str, Any]:
        if account_id != "all" and account_id not in self.registry.accounts_by_id:
            raise ValidationError("provider_account_id is unknown")
        if not self._refresh_lock.acquire(blocking=False):
            raise ValidationError("a provider finance refresh is already running")
        try:
            accounts = (
                self.registry.accounts
                if account_id == "all"
                else (self.registry.account(account_id),)
            )
            results = [self._refresh_one_locked(account) for account in accounts]
        finally:
            self._refresh_lock.release()
        return {
            "status": "completed",
            "registry_revision": self.registry.revision,
            "requested_account": account_id,
            "results": results,
        }

    def public_registry(self) -> dict[str, Any]:
        if self.database is None:
            raise ConfigurationError(
                "stateless provider finance worker has no public registry cache"
            )
        latest = {
            item["provider_account_id"]: item
            for item in self.database.latest_provider_finance_snapshots(
                tuple(account.account_id for account in self.registry.accounts)
            )
        }
        accounts: list[dict[str, Any]] = []
        for account in self.registry.accounts:
            record = json.loads(json.dumps(account.public_record))
            record["finance_state"] = latest.get(
                account.account_id
            ) or self._empty_state(account, self._static_status(account))
            accounts.append(record)
        return {
            "schema_version": 1,
            "revision": self.registry.revision,
            "verified_on": self.registry.verified_on,
            "provider_accounts": accounts,
            "deployment_mappings": list(self.registry.deployment_mappings),
            "finance_cache": {
                "persistence": "sqlite_append_only",
                "maximum_history_rows_per_query": MAX_HISTORY_ROWS,
                "maximum_snapshots_per_account": MAX_FINANCE_SNAPSHOTS_PER_ACCOUNT,
                "currency_conversion": False,
            },
        }

    def history(self, account_id: str, limit: int = 50) -> dict[str, Any]:
        if self.database is None:
            raise ConfigurationError(
                "stateless provider finance worker has no history"
            )
        if account_id not in self.registry.accounts_by_id:
            raise ValidationError("provider_account_id is unknown")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_HISTORY_ROWS:
            raise ValidationError("provider finance history limit is outside bounds")
        return {
            "provider_account_id": account_id,
            "snapshots": self.database.provider_finance_history(account_id, limit),
        }
