"""Isolated Unix API for reviewed provider-finance refreshes.

The main orchestrator never receives Moonshot or StepFun credentials. It sends
an already-authorized account identifier to a dedicated-UID, stateless worker.
Only the main process persists the worker's strictly revalidated, normalized
result; neither side transports a credential or a raw provider response.
"""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import grp
from http.server import BaseHTTPRequestHandler
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import signal
import socket
import socketserver
import stat
import struct
from typing import Any
from urllib.parse import urlsplit
import uuid

from .bounded_server import BoundedThreadingMixIn
from .config import load_config
from .errors import ConfigurationError, ProviderUnavailable, ValidationError
from .provider_finance import ProviderFinanceManager
from .provider_integrations import ProviderIntegrationRegistry, load_provider_integrations


MAX_WORKER_REQUEST_BYTES = 1_024
MAX_WORKER_RESPONSE_BYTES = 262_144
WORKER_CLIENT_TIMEOUT_SECONDS = 35.0
WORKER_MAX_REQUEST_THREADS = 4
_ACCOUNT_ID = re.compile(r"\A[a-z0-9][a-z0-9-]{0,63}\Z")
_ERROR_CODE = re.compile(r"\A[a-z][a-z0-9_]{0,63}\Z")
_AMOUNT = re.compile(r"\A(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?\Z")
_CAPTURED_AT = re.compile(
    r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?\+00:00\Z"
)
_SNAPSHOT_STATUSES = {
    "ok",
    "credential_unavailable",
    "provider_unavailable",
    "unsupported_schema",
}


class FinanceWorkerAccessDenied(Exception):
    """The Unix peer is not the dedicated orchestrator service identity."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object field")
        result[key] = value
    return result


def _validate_account_id(value: Any, registry: ProviderIntegrationRegistry) -> str:
    if value == "all":
        return value
    if not isinstance(value, str) or not _ACCOUNT_ID.fullmatch(value):
        raise ValidationError("provider finance account is invalid")
    if value not in registry.accounts_by_id:
        raise ValidationError("provider finance account is unknown")
    return value


def _amount(value: Any) -> None:
    if not isinstance(value, str) or not _AMOUNT.fullmatch(value):
        raise ProviderUnavailable("provider finance worker returned an invalid amount")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ProviderUnavailable(
            "provider finance worker returned an invalid amount"
        ) from exc
    if not parsed.is_finite() or parsed < 0 or parsed > Decimal("1000000000000000"):
        raise ProviderUnavailable("provider finance worker returned an invalid amount")


def _validate_balances(account_id: str, balances: Any) -> None:
    if not isinstance(balances, list) or not 1 <= len(balances) <= 8:
        raise ProviderUnavailable("provider finance worker returned invalid balances")
    if account_id == "deepseek":
        expected = {"currency", "available", "granted", "topped_up"}
        currencies: set[str] = set()
        for balance in balances:
            if not isinstance(balance, dict) or set(balance) != expected:
                raise ProviderUnavailable("provider finance worker returned invalid balances")
            currency = balance["currency"]
            if currency not in {"CNY", "USD"} or currency in currencies:
                raise ProviderUnavailable("provider finance worker returned invalid balances")
            currencies.add(currency)
            for field in ("available", "granted", "topped_up"):
                _amount(balance[field])
        return
    if account_id == "moonshot":
        expected = {"currency", "available", "cash", "voucher"}
        if len(balances) != 1 or not isinstance(balances[0], dict) or set(balances[0]) != expected:
            raise ProviderUnavailable("provider finance worker returned invalid balances")
        if balances[0]["currency"] is not None:
            raise ProviderUnavailable("provider finance worker invented a currency")
        for field in ("available", "cash", "voucher"):
            _amount(balances[0][field])
        return
    if account_id == "stepfun":
        expected = {
            "currency",
            "available",
            "total_cash",
            "total_voucher",
            "billing_type",
        }
        if len(balances) != 1 or not isinstance(balances[0], dict) or set(balances[0]) != expected:
            raise ProviderUnavailable("provider finance worker returned invalid balances")
        if balances[0]["currency"] is not None or balances[0]["billing_type"] not in {
            "prepaid",
            "postpaid",
        }:
            raise ProviderUnavailable("provider finance worker returned invalid balances")
        for field in ("available", "total_cash", "total_voucher"):
            _amount(balances[0][field])
        return
    raise ProviderUnavailable("provider finance worker returned an unexpected balance account")


def _static_status(cash_balance_mode: str) -> str:
    if cash_balance_mode in {"admin_api", "cloud_billing_api"}:
        return "requires_admin_credential"
    if cash_balance_mode == "console_only":
        return "console_only"
    if cash_balance_mode in {"not_applicable", "unknown"}:
        return "unsupported"
    return "never_refreshed"


def validate_refresh_response(
    value: Any,
    registry: ProviderIntegrationRegistry,
    requested_account: str,
) -> dict[str, Any]:
    """Validate every normalized field crossing the finance socket."""

    if not isinstance(value, dict) or set(value) != {
        "status",
        "registry_revision",
        "requested_account",
        "results",
    }:
        raise ProviderUnavailable("provider finance worker response is malformed")
    if (
        value["status"] != "completed"
        or value["registry_revision"] != registry.revision
        or value["requested_account"] != requested_account
        or not isinstance(value["results"], list)
    ):
        raise ProviderUnavailable("provider finance worker response is inconsistent")
    expected_ids = (
        [account.account_id for account in registry.accounts]
        if requested_account == "all"
        else [requested_account]
    )
    observed_ids: list[str] = []
    for result in value["results"]:
        if not isinstance(result, dict) or set(result) != {
            "snapshot_id",
            "provider_account_id",
            "registry_revision",
            "captured_at",
            "cash_balance_mode",
            "status",
            "is_available",
            "balances",
            "error_code",
            "duration_ms",
        }:
            raise ProviderUnavailable("provider finance worker result is malformed")
        account_id = result["provider_account_id"]
        if not isinstance(account_id, str) or account_id not in registry.accounts_by_id:
            raise ProviderUnavailable("provider finance worker returned an unknown account")
        observed_ids.append(account_id)
        account = registry.account(account_id)
        if (
            result["registry_revision"] != registry.revision
            or result["cash_balance_mode"] != account.cash_balance_mode
        ):
            raise ProviderUnavailable("provider finance worker result contract drifted")
        snapshot_id = result["snapshot_id"]
        if snapshot_id is None:
            if (
                result["captured_at"] is not None
                or result["is_available"] is not None
                or result["balances"] != []
                or result["error_code"] is not None
                or result["duration_ms"] is not None
                or result["status"] != _static_status(account.cash_balance_mode)
            ):
                raise ProviderUnavailable("provider finance worker static result is invalid")
            continue
        try:
            parsed_snapshot_id = uuid.UUID(snapshot_id)
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProviderUnavailable("provider finance worker snapshot id is invalid") from exc
        if not isinstance(snapshot_id, str) or str(parsed_snapshot_id) != snapshot_id:
            raise ProviderUnavailable("provider finance worker snapshot id is invalid")
        if (
            not isinstance(result["captured_at"], str)
            or not _CAPTURED_AT.fullmatch(result["captured_at"])
            or isinstance(result["duration_ms"], bool)
            or not isinstance(result["duration_ms"], int)
            or not 0 <= result["duration_ms"] <= 60_000
            or result["status"] not in _SNAPSHOT_STATUSES
            or (result["is_available"] is not None and not isinstance(result["is_available"], bool))
        ):
            raise ProviderUnavailable("provider finance worker snapshot metadata is invalid")
        if result["status"] == "ok":
            if result["error_code"] is not None:
                raise ProviderUnavailable("provider finance worker success contains an error")
            if account_id == "deepseek":
                if not isinstance(result["is_available"], bool):
                    raise ProviderUnavailable("DeepSeek availability is missing")
            elif result["is_available"] is not None:
                raise ProviderUnavailable("provider finance worker inferred availability")
            _validate_balances(account_id, result["balances"])
        elif (
            result["is_available"] is not None
            or result["balances"] != []
            or not isinstance(result["error_code"], str)
            or not _ERROR_CODE.fullmatch(result["error_code"])
        ):
            raise ProviderUnavailable("provider finance worker failure result is invalid")
    if observed_ids != expected_ids:
        raise ProviderUnavailable("provider finance worker returned the wrong accounts")
    # Return an isolated JSON-compatible value rather than the parser's object.
    return json.loads(json.dumps(value))


class _FinanceUnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(str(self.socket_path))
        self.sock = connection


class ProviderFinanceClient:
    """Strict client used only by the main orchestrator process."""

    def __init__(
        self,
        registry: ProviderIntegrationRegistry,
        socket_path: Path,
        *,
        timeout_seconds: float = WORKER_CLIENT_TIMEOUT_SECONDS,
        expected_socket_uid: int,
        expected_socket_gid: int,
    ):
        if not socket_path.is_absolute():
            raise ConfigurationError("provider finance socket path must be absolute")
        if not 1 <= float(timeout_seconds) <= 60:
            raise ConfigurationError("provider finance worker timeout is invalid")
        if (
            isinstance(expected_socket_uid, bool)
            or not isinstance(expected_socket_uid, int)
            or expected_socket_uid < 0
            or isinstance(expected_socket_gid, bool)
            or not isinstance(expected_socket_gid, int)
            or expected_socket_gid < 0
        ):
            raise ConfigurationError("provider finance socket identity is invalid")
        self.registry = registry
        self.socket_path = socket_path
        self.timeout_seconds = float(timeout_seconds)
        self.expected_socket_uid = expected_socket_uid
        self.expected_socket_gid = expected_socket_gid

    def _request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        try:
            info = self.socket_path.lstat()
            parent = self.socket_path.parent.lstat()
        except OSError as exc:
            raise ProviderUnavailable("provider finance worker is unavailable") from exc
        if (
            not stat.S_ISDIR(parent.st_mode)
            or stat.S_ISLNK(parent.st_mode)
            or parent.st_uid != self.expected_socket_uid
            or parent.st_gid != self.expected_socket_gid
            or stat.S_IMODE(parent.st_mode) != 0o750
            or not stat.S_ISSOCK(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or info.st_uid != self.expected_socket_uid
            or info.st_gid != self.expected_socket_gid
            or stat.S_IMODE(info.st_mode) != 0o660
        ):
            raise ProviderUnavailable("provider finance worker socket is unsafe")
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if len(body) > MAX_WORKER_REQUEST_BYTES:
                raise ValidationError("provider finance request exceeds its bound")
            headers.update(
                {
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                }
            )
        connection = _FinanceUnixConnection(
            self.socket_path,
            self.timeout_seconds if timeout_seconds is None else timeout_seconds,
        )
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            content_length = response.getheader("Content-Length")
            content_type = response.getheader("Content-Type", "").split(";", 1)[0]
            if (
                response.getheader("Transfer-Encoding") is not None
                or content_type != "application/json"
                or content_length is None
                or not content_length.isascii()
                or not content_length.isdigit()
                or len(content_length) > 9
                or int(content_length) > MAX_WORKER_RESPONSE_BYTES
            ):
                raise ProviderUnavailable("provider finance worker response framing is invalid")
            raw = response.read(MAX_WORKER_RESPONSE_BYTES + 1)
            status = response.status
        except ProviderUnavailable:
            raise
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise ProviderUnavailable("provider finance worker request failed") from exc
        finally:
            connection.close()
        if len(raw) != int(content_length) or len(raw) > MAX_WORKER_RESPONSE_BYTES:
            raise ProviderUnavailable("provider finance worker response is truncated")
        try:
            value = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ProviderUnavailable("provider finance worker returned invalid JSON") from exc
        if status < 200 or status >= 300:
            raise ProviderUnavailable("provider finance worker refused the request")
        if not isinstance(value, dict):
            raise ProviderUnavailable("provider finance worker returned a non-object")
        return value

    def health(self) -> dict[str, Any]:
        value = self._request("GET", "/v1/health", timeout_seconds=2.0)
        if value != {
            "status": "ok",
            "registry_revision": self.registry.revision,
            "direct_connectors": ["deepseek", "moonshot", "stepfun"],
            "credential_scope": "finance_direct_only",
        }:
            raise ProviderUnavailable("provider finance worker health is inconsistent")
        return value

    def refresh(self, account_id: str) -> dict[str, Any]:
        account_id = _validate_account_id(account_id, self.registry)
        value = self._request(
            "POST",
            "/v1/refresh",
            {"provider_account_id": account_id},
        )
        return validate_refresh_response(value, self.registry, account_id)


class FinanceWorkerUnixHTTPServer(
    BoundedThreadingMixIn, socketserver.UnixStreamServer
):
    allow_reuse_address = False

    def __init__(
        self,
        address: str,
        manager: ProviderFinanceManager,
        *,
        allowed_uid: int | None = None,
    ):
        if (
            isinstance(allowed_uid, bool)
            or (allowed_uid is not None and (not isinstance(allowed_uid, int) or allowed_uid < 0))
        ):
            raise ConfigurationError("provider finance peer identity is invalid")
        self.manager = manager
        self.allowed_uid = os.geteuid() if allowed_uid is None else allowed_uid
        super().__init__(
            address,
            FinanceWorkerHandler,
            client_timeout_seconds=WORKER_CLIENT_TIMEOUT_SECONDS,
            max_request_workers=WORKER_MAX_REQUEST_THREADS,
        )


class FinanceWorkerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ops-orchestrator-finance"
    sys_version = ""

    @property
    def manager(self) -> ProviderFinanceManager:
        return self.server.manager  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _authorize(self) -> None:
        try:
            credentials = self.connection.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("3i"),
            )
            _pid, uid, _gid = struct.unpack("3i", credentials)
        except (AttributeError, OSError, struct.error) as exc:
            raise FinanceWorkerAccessDenied from exc
        if uid != self.server.allowed_uid:  # type: ignore[attr-defined]
            raise FinanceWorkerAccessDenied

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        if len(data) > MAX_WORKER_RESPONSE_BYTES:
            status = 500
            data = b'{"error":"internal_error"}'
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.close_connection = True

    def _body(self) -> dict[str, Any]:
        if self.headers.get("Transfer-Encoding"):
            raise ValidationError("transfer encoding is not accepted")
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValidationError("Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.isascii() or not raw_length.isdigit():
            raise ValidationError("valid Content-Length is required")
        length = int(raw_length)
        if not 2 <= length <= MAX_WORKER_REQUEST_BYTES:
            raise ValidationError("request body size is outside bounds")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ValidationError("request body is incomplete")
        try:
            value = json.loads(
                raw.decode("utf-8", errors="strict"),
                object_pairs_hook=_unique_object,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValidationError("request body is invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("request body must be an object")
        return value

    def do_GET(self) -> None:
        try:
            self._authorize()
            parsed = urlsplit(self.path)
            if parsed.path != "/v1/health" or parsed.query:
                self._json(404, {"error": "not_found"})
                return
            self._json(
                200,
                {
                    "status": "ok",
                    "registry_revision": self.manager.registry.revision,
                    "direct_connectors": ["deepseek", "moonshot", "stepfun"],
                    "credential_scope": "finance_direct_only",
                },
            )
        except FinanceWorkerAccessDenied:
            self._json(403, {"error": "access_denied"})
        except TimeoutError:
            self.close_connection = True
        except Exception:
            self._json(500, {"error": "internal_error"})

    def do_POST(self) -> None:
        try:
            self._authorize()
            parsed = urlsplit(self.path)
            if parsed.path != "/v1/refresh" or parsed.query:
                self._json(404, {"error": "not_found"})
                return
            payload = self._body()
            if set(payload) != {"provider_account_id"}:
                raise ValidationError("provider finance worker fields are invalid")
            account_id = _validate_account_id(
                payload["provider_account_id"], self.manager.registry
            )
            self._json(200, self.manager.refresh(account_id))
        except FinanceWorkerAccessDenied:
            self._json(403, {"error": "access_denied"})
        except ValidationError:
            self._json(400, {"error": "validation_error"})
        except TimeoutError:
            self.close_connection = True
        except Exception:
            self._json(500, {"error": "internal_error"})


def serve(config_path: str) -> None:
    config = load_config(config_path)
    registry = load_provider_integrations(config.provider_integrations_path)
    try:
        worker_account = pwd.getpwnam(config.finance_socket_user)
        worker_group = grp.getgrnam(config.finance_socket_group)
        client_account = pwd.getpwnam(config.finance_client_user)
    except KeyError as exc:
        raise ConfigurationError("provider finance system identity is unavailable") from exc
    if (
        worker_account.pw_gid != worker_group.gr_gid
        or worker_account.pw_uid == client_account.pw_uid
        or set(worker_group.gr_mem) != {client_account.pw_name}
        or any(
            account.pw_gid == worker_group.gr_gid
            and account.pw_name != worker_account.pw_name
            for account in pwd.getpwall()
        )
        or os.geteuid() != worker_account.pw_uid
        or os.getegid() != worker_group.gr_gid
    ):
        raise ConfigurationError("provider finance worker identity is invalid")
    manager = ProviderFinanceManager(
        registry,
        None,
        os.environ,
        timeout_seconds=config.limits.provider_timeout_seconds,
        maximum_response_bytes=config.limits.max_response_bytes,
    )
    socket_path = config.finance_socket_path
    socket_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    parent = socket_path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != os.geteuid()
        or parent.st_gid != worker_group.gr_gid
        or stat.S_IMODE(parent.st_mode) != 0o750
    ):
        raise ConfigurationError("provider finance socket parent is unsafe")
    if socket_path.exists() or socket_path.is_symlink():
        info = socket_path.lstat()
        if (
            not stat.S_ISSOCK(info.st_mode)
            or info.st_uid != worker_account.pw_uid
            or info.st_gid != worker_group.gr_gid
        ):
            raise ConfigurationError("refusing to replace unsafe provider finance socket")
        socket_path.unlink()
    previous_mask = os.umask(0o077)
    try:
        server = FinanceWorkerUnixHTTPServer(
            str(socket_path), manager, allowed_uid=client_account.pw_uid
        )
    finally:
        os.umask(previous_mask)
    try:
        os.chmod(socket_path, 0o660)
        socket_info = socket_path.lstat()
        if (
            not stat.S_ISSOCK(socket_info.st_mode)
            or socket_info.st_uid != worker_account.pw_uid
            or socket_info.st_gid != worker_group.gr_gid
            or stat.S_IMODE(socket_info.st_mode) != 0o660
        ):
            raise ConfigurationError("provider finance socket ownership is invalid")
        server.timeout = 0.5
        stopping = False

        def request_stop(signum: int, frame: Any) -> None:
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        while not stopping:
            server.handle_request()
    finally:
        server.server_close()
        try:
            info = socket_path.lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid():
                socket_path.unlink()
        except FileNotFoundError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the isolated provider-finance Unix service"
    )
    parser.add_argument("--config", default="/etc/ops-orchestrator/config.json")
    arguments = parser.parse_args()
    serve(arguments.config)


if __name__ == "__main__":
    main()
