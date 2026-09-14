from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import threading

import pytest

import ops_orchestrator.database as database_module
import ops_orchestrator.provider_finance as provider_finance_module
from ops_orchestrator.database import Database
from ops_orchestrator.api import APIHandler, ThreadingUnixHTTPServer
from ops_orchestrator.cli import _request
from ops_orchestrator.errors import ConfigurationError, ProviderUnavailable, ValidationError
from ops_orchestrator.provider_finance import (
    DIRECT_CASH_BALANCE_ENDPOINTS,
    FinanceNetworkError,
    ProviderFinanceManager,
)
from ops_orchestrator.provider_finance_api import (
    FinanceWorkerUnixHTTPServer,
    ProviderFinanceClient,
    validate_refresh_response,
)
from ops_orchestrator.provider_integrations import load_provider_integrations
from ops_orchestrator.service import OrchestratorService

from conftest import make_config


ROOT = Path(__file__).parents[2]
REGISTRY_PATH = ROOT / "catalog" / "provider-integrations.v1.json"


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "state" / "orchestrator.sqlite3")
    database.initialize()
    return database


def _key(tmp_path: Path, name: str = "key") -> Path:
    path = tmp_path / name
    path.write_text("test-secret-never-returned\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _manager(tmp_path: Path, environment: dict[str, str], transport):
    return ProviderFinanceManager(
        load_provider_integrations(REGISTRY_PATH),
        _database(tmp_path),
        environment,
        timeout_seconds=45,
        maximum_response_bytes=1_048_576,
        transport=transport,
    )


def test_runtime_registry_is_strict_and_removes_internal_credential_paths() -> None:
    registry = load_provider_integrations(REGISTRY_PATH)
    assert len(registry.accounts) == 21
    assert registry.revision == "provider-integrations-2026-09-05.1"
    public = registry.account("deepseek").public_record
    assert public["credentials"] == {
        "inference": {"auth_scheme": "bearer"},
        "admin_finance": {
            "auth_scheme": "none",
            "separate_credential_required": False,
        },
    }
    assert "credential_ref" not in json.dumps(public)


def test_runtime_registry_rejects_symlinks_and_unreviewed_endpoints(tmp_path: Path) -> None:
    symlink = tmp_path / "registry.json"
    symlink.symlink_to(REGISTRY_PATH)
    with pytest.raises(ConfigurationError, match="unavailable"):
        load_provider_integrations(symlink)

    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        REGISTRY_PATH.read_text(encoding="utf-8").replace(
            '"schema_version": 1,',
            '"schema_version": 1, "schema_version": 1,',
            1,
        ),
        encoding="utf-8",
    )
    duplicate.chmod(0o600)
    with pytest.raises(ConfigurationError, match="invalid UTF-8 JSON"):
        load_provider_integrations(duplicate)

    document = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    deepseek = next(item for item in document["provider_accounts"] if item["id"] == "deepseek")
    deepseek["finance_endpoints"][0]["endpoint"] = "https://example.invalid/balance"
    changed = tmp_path / "changed.json"
    changed.write_text(json.dumps(document), encoding="utf-8")
    changed.chmod(0o600)
    registry = load_provider_integrations(changed)
    with pytest.raises(ConfigurationError, match="connector contract changed"):
        ProviderFinanceManager(
            registry,
            _database(tmp_path / "changed-db"),
            {},
            timeout_seconds=10,
            maximum_response_bytes=65_536,
        )


def test_finance_transport_is_bounded_and_sends_bearer_only_to_literal_url(
    monkeypatch,
) -> None:
    class Response:
        status = 200
        headers = {"Content-Length": "2"}
        payload = b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, maximum):
            assert maximum == 1025
            return self.payload

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == DIRECT_CASH_BALANCE_ENDPOINTS["deepseek"]
            assert request.get_header("Authorization") == "Bearer opaque-test-value"
            assert timeout == 2
            return Response()

    monkeypatch.setattr(provider_finance_module, "_FINANCE_OPENER", Opener())
    assert provider_finance_module._get_json(
        DIRECT_CASH_BALANCE_ENDPOINTS["deepseek"],
        "opaque-test-value",
        2,
        1024,
    ) == {}

    Response.headers = {"Content-Length": "1025"}
    with pytest.raises(FinanceNetworkError) as error:
        provider_finance_module._get_json(
            DIRECT_CASH_BALANCE_ENDPOINTS["deepseek"],
            "opaque-test-value",
            2,
            1024,
        )
    assert error.value.code == "response_too_large"

    Response.headers = {}
    Response.payload = b'{"value":1,"value":2}'
    with pytest.raises(FinanceNetworkError) as error:
        provider_finance_module._get_json(
            DIRECT_CASH_BALANCE_ENDPOINTS["deepseek"],
            "opaque-test-value",
            2,
            1024,
        )
    assert error.value.code == "invalid_json"


@pytest.mark.parametrize(
    "account_id,environment_name,response,expected",
    [
        (
            "deepseek",
            "DEEPSEEK_API_KEY_FILE",
            {
                "is_available": True,
                "balance_infos": [
                    {
                        "currency": "USD",
                        "total_balance": "5.1200",
                        "granted_balance": "1.1200",
                        "topped_up_balance": "4.0000",
                    },
                    {
                        "currency": "CNY",
                        "total_balance": "10.00",
                        "granted_balance": "0.00",
                        "topped_up_balance": "10.00",
                    },
                ],
            },
            [
                {"currency": "USD", "available": "5.12", "granted": "1.12", "topped_up": "4"},
                {"currency": "CNY", "available": "10", "granted": "0", "topped_up": "10"},
            ],
        ),
        (
            "moonshot",
            "MOONSHOT_API_KEY_FILE",
            {
                "code": 0,
                "data": {
                    "available_balance": 49.58894,
                    "voucher_balance": 46.58893,
                    "cash_balance": 3.00001,
                },
                "scode": "0x0",
                "status": True,
            },
            [
                {
                    "currency": None,
                    "available": "49.58894",
                    "cash": "3.00001",
                    "voucher": "46.58893",
                }
            ],
        ),
        (
            "stepfun",
            "STEPFUN_API_KEY_FILE",
            {
                "object": "account",
                "type": "prepaid",
                "balance": 0.0,
                "total_cash_balance": 0.0,
                "total_voucher_balance": 26.0,
            },
            [
                {
                    "currency": None,
                    "available": "0",
                    "total_cash": "0",
                    "total_voucher": "26",
                    "billing_type": "prepaid",
                }
            ],
        ),
    ],
)
def test_verified_direct_connectors_normalize_without_currency_conversion(
    tmp_path: Path,
    account_id: str,
    environment_name: str,
    response: dict,
    expected: list[dict],
) -> None:
    key_path = _key(tmp_path, account_id)
    calls = []

    def transport(url, credential, timeout, maximum_bytes):
        calls.append((url, credential, timeout, maximum_bytes))
        return response

    manager = _manager(tmp_path, {environment_name: str(key_path)}, transport)
    result = manager.refresh(account_id)["results"][0]
    assert result["status"] == "ok"
    assert result["balances"] == expected
    if account_id in {"moonshot", "stepfun"}:
        assert result["is_available"] is None
    assert calls == [
        (
            DIRECT_CASH_BALANCE_ENDPOINTS[account_id],
            "test-secret-never-returned",
            10.0,
            65_536,
        )
    ]
    assert "test-secret" not in json.dumps(result)


def test_schema_mismatch_and_missing_credentials_are_safe_snapshots(tmp_path: Path) -> None:
    key_path = _key(tmp_path)
    manager = _manager(
        tmp_path,
        {"DEEPSEEK_API_KEY_FILE": str(key_path)},
        lambda *_args: {"is_available": True, "balance_infos": [], "extra": "x"},
    )
    mismatch = manager.refresh("deepseek")["results"][0]
    missing = manager.refresh("moonshot")["results"][0]
    assert mismatch["status"] == "unsupported_schema"
    assert mismatch["error_code"] == "response_schema_mismatch"
    assert mismatch["balances"] == []
    assert missing["status"] == "credential_unavailable"
    assert missing["balances"] == []

    optional_fallback = tmp_path / "empty-systemd-fallback"
    optional_fallback.write_bytes(b"\n")
    optional_fallback.chmod(0o600)
    fallback_manager = _manager(
        tmp_path / "fallback-db",
        {"MOONSHOT_API_KEY_FILE": str(optional_fallback)},
        lambda *_args: pytest.fail("an empty fallback must not reach the provider"),
    )
    fallback = fallback_manager.refresh("moonshot")["results"][0]
    assert fallback["status"] == "credential_unavailable"


def test_non_direct_modes_are_truthful_and_never_call_transport(tmp_path: Path) -> None:
    def unexpected(*_args):
        raise AssertionError("network transport must not be called")

    manager = _manager(tmp_path, {}, unexpected)
    assert manager.refresh("xai")["results"][0]["status"] == "requires_admin_credential"
    assert manager.refresh("openai")["results"][0]["status"] == "console_only"
    assert manager.refresh("google")["results"][0]["status"] == "unsupported"
    public = manager.public_registry()
    assert len(public["provider_accounts"]) == 21
    assert public["finance_cache"]["currency_conversion"] is False
    state = next(
        account["finance_state"]
        for account in public["provider_accounts"]
        if account["id"] == "openai"
    )
    assert set(state) == {
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
    }
    assert state["snapshot_id"] is None
    assert state["duration_ms"] is None


def test_finance_snapshots_are_append_only_and_queries_are_bounded(tmp_path: Path) -> None:
    database = _database(tmp_path)
    snapshot = database.record_provider_finance_snapshot(
        provider_account_id="deepseek",
        registry_revision="provider-integrations-2026-09-05.1",
        cash_balance_mode="direct_api",
        status="ok",
        is_available=True,
        balances=[{"currency": "USD", "available": "1"}],
        error_code=None,
        duration_ms=1,
    )
    assert database.provider_finance_history("deepseek", 1) == [snapshot]
    with database.connect() as connection, pytest.raises(
        sqlite3.IntegrityError, match="append-only"
    ):
        connection.execute(
            "UPDATE provider_finance_snapshots SET status = 'provider_unavailable'"
        )
    with pytest.raises(ValidationError, match="outside bounds"):
        database.provider_finance_history("deepseek", 201)
    with pytest.raises(ValidationError, match="amount is invalid"):
        database.record_provider_finance_snapshot(
            provider_account_id="deepseek",
            registry_revision="provider-integrations-2026-09-05.1",
            cash_balance_mode="direct_api",
            status="ok",
            is_available=True,
            balances=[{"currency": "USD", "available": "secret-shaped-value"}],
            error_code=None,
            duration_ms=1,
        )


def test_append_only_finance_storage_has_a_hard_per_account_cap(
    tmp_path: Path, monkeypatch
) -> None:
    database = _database(tmp_path)
    monkeypatch.setattr(database_module, "MAX_FINANCE_SNAPSHOTS_PER_ACCOUNT", 1)
    arguments = {
        "provider_account_id": "deepseek",
        "registry_revision": "provider-integrations-2026-09-05.1",
        "cash_balance_mode": "direct_api",
        "status": "ok",
        "is_available": True,
        "balances": [{"currency": "USD", "available": "1"}],
        "error_code": None,
        "duration_ms": 1,
    }
    database.record_provider_finance_snapshot(**arguments)
    with pytest.raises(ValidationError, match="capacity is exhausted"):
        database.record_provider_finance_snapshot(**arguments)


def test_refresh_rejects_unknown_account_and_concurrent_reentry(tmp_path: Path) -> None:
    manager = _manager(tmp_path, {}, lambda *_args: {})
    with pytest.raises(ValidationError, match="unknown"):
        manager.refresh("invented")
    assert manager._refresh_lock.acquire(blocking=False)
    try:
        with pytest.raises(ValidationError, match="already running"):
            manager.refresh("all")
    finally:
        manager._refresh_lock.release()


def test_unix_api_exposes_public_registry_and_scoped_refresh(tmp_path: Path) -> None:
    key_path = _key(tmp_path)
    config = make_config(tmp_path)
    config.socket_path.parent.mkdir(parents=True)
    service = OrchestratorService(
        config,
        environment={},
    )
    finance_manager = ProviderFinanceManager(
        service.provider_integrations_registry,
        None,
        {"DEEPSEEK_API_KEY_FILE": str(key_path)},
        timeout_seconds=10,
        maximum_response_bytes=65_536,
    )
    finance_manager.transport = lambda *_args: {
        "is_available": True,
        "balance_infos": [
            {
                "currency": "USD",
                "total_balance": "7.50",
                "granted_balance": "0.50",
                "topped_up_balance": "7.00",
            }
        ],
    }
    worker_snapshots: list[dict] = []
    worker_snapshot = finance_manager._snapshot

    def capture_worker_snapshot(*args, **kwargs):
        result = worker_snapshot(*args, **kwargs)
        worker_snapshots.append(result)
        return result

    finance_manager._snapshot = capture_worker_snapshot
    config.finance_socket_path.parent.mkdir(mode=0o750, parents=True)
    finance_server = FinanceWorkerUnixHTTPServer(
        str(config.finance_socket_path), finance_manager
    )
    config.finance_socket_path.chmod(0o660)
    finance_thread = threading.Thread(
        target=finance_server.serve_forever, daemon=True
    )
    finance_thread.start()
    server = ThreadingUnixHTTPServer(str(config.socket_path), APIHandler, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        public = _request(
            str(config.socket_path), "GET", "/v1/provider-integrations", timeout=5
        )
        assert len(public["provider_accounts"]) == 21
        assert "credential_ref" not in json.dumps(public)

        refreshed = _request(
            str(config.socket_path),
            "POST",
            "/v1/provider-finance/refresh",
            {"project_id": "infra-shared", "provider_account_id": "deepseek"},
            timeout=5,
        )
        assert refreshed["results"][0]["balances"][0] == {
            "currency": "USD",
            "available": "7.5",
            "granted": "0.5",
            "topped_up": "7",
        }
        assert len(worker_snapshots) == 1
        assert refreshed["results"][0]["snapshot_id"] != worker_snapshots[0][
            "snapshot_id"
        ]
        assert refreshed["results"][0]["captured_at"] != worker_snapshots[0][
            "captured_at"
        ]
        assert len(service.database.provider_finance_history("deepseek", 10)) == 1
        cached = _request(
            str(config.socket_path), "GET", "/v1/provider-integrations", timeout=5
        )
        deepseek = next(
            account
            for account in cached["provider_accounts"]
            if account["id"] == "deepseek"
        )
        assert deepseek["finance_state"]["status"] == "ok"
        assert deepseek["finance_state"]["balances"][0]["currency"] == "USD"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        finance_server.shutdown()
        finance_server.server_close()
        finance_thread.join(timeout=3)


def test_finance_socket_contract_rejects_secret_shaped_or_wrong_account_data(
    tmp_path: Path,
) -> None:
    registry = load_provider_integrations(REGISTRY_PATH)
    manager = _manager(tmp_path, {}, lambda *_args: {})
    valid = manager.refresh("openai")
    assert validate_refresh_response(valid, registry, "openai") == valid

    injected = json.loads(json.dumps(valid))
    injected["results"][0]["provider_account_id"] = "deepseek"
    with pytest.raises(ProviderUnavailable, match="contract drifted|wrong accounts"):
        validate_refresh_response(injected, registry, "openai")

    successful = _manager(
        tmp_path / "success",
        {"DEEPSEEK_API_KEY_FILE": str(_key(tmp_path, "deepseek-contract"))},
        lambda *_args: {
            "is_available": True,
            "balance_infos": [
                {
                    "currency": "USD",
                    "total_balance": "1",
                    "granted_balance": "0",
                    "topped_up_balance": "1",
                }
            ],
        },
    ).refresh("deepseek")
    successful["results"][0]["balances"][0]["api_key"] = "must-not-cross"
    with pytest.raises(ProviderUnavailable, match="invalid balances"):
        validate_refresh_response(successful, registry, "deepseek")


def test_finance_worker_socket_rejects_wrong_peer_and_unsafe_mode(
    tmp_path: Path,
) -> None:
    registry = load_provider_integrations(REGISTRY_PATH)
    manager = _manager(tmp_path, {}, lambda *_args: {})
    socket_parent = tmp_path / "finance-run"
    socket_parent.mkdir(mode=0o750)
    socket_path = socket_parent / "finance.sock"
    server = FinanceWorkerUnixHTTPServer(
        str(socket_path), manager, allowed_uid=os.geteuid() + 1
    )
    socket_path.chmod(0o660)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = ProviderFinanceClient(
        registry,
        socket_path,
        expected_socket_uid=os.geteuid(),
        expected_socket_gid=os.getegid(),
    )
    try:
        with pytest.raises(ProviderUnavailable, match="refused"):
            client.health()
        socket_path.chmod(0o600)
        with pytest.raises(ProviderUnavailable, match="unsafe"):
            client.health()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_finance_timer_client_has_no_network_or_credential_access() -> None:
    unit = (
        ROOT / "systemd" / "ops-orchestrator-provider-finance.service"
    ).read_text(encoding="utf-8")
    timer = (
        ROOT / "systemd" / "ops-orchestrator-provider-finance.timer"
    ).read_text(encoding="utf-8")
    assert "User=ops-monitor\n" in unit
    assert "Group=opsorchestrator-api\n" in unit
    assert "refresh-finance --account all --project-id infra-shared" in unit
    assert "RestrictAddressFamilies=AF_UNIX\n" in unit
    assert "PrivateNetwork=yes\n" in unit
    assert "MemoryDenyWriteExecute=yes\n" in unit
    assert "LoadCredential=" not in unit
    assert "Environment=" not in unit
    assert "StandardOutput=null\n" in unit
    assert "OnUnitActiveSec=30min\n" in timer
    assert "Persistent=true\n" in timer


def test_finance_daemon_and_consumers_have_exact_credential_sets() -> None:
    main = (ROOT / "systemd" / "ops-orchestrator.service").read_text(
        encoding="utf-8"
    )
    finance = (
        ROOT / "systemd" / "ops-orchestrator-provider-finance-daemon.service"
    ).read_text(encoding="utf-8")
    facade = (
        ROOT / "systemd" / "ops-orchestrator-hermes-facade.service"
    ).read_text(encoding="utf-8")
    timer_client = (
        ROOT / "systemd" / "ops-orchestrator-provider-finance.service"
    ).read_text(encoding="utf-8")

    assert [line for line in main.splitlines() if line.startswith("LoadCredential=")] == [
        "LoadCredential=qwen-api-key:/run/ops-orchestrator-secrets/current/qwen-api-key",
        "LoadCredential=deepseek-api-key:/run/ops-orchestrator-secrets/current/deepseek-api-key",
        "LoadCredential=z-ai-api-key:/run/ops-orchestrator-secrets/current/z-ai-api-key",
        "LoadCredential=mistral-ai-api-key:/run/ops-orchestrator-secrets/current/mistral-ai-api-key",
        "LoadCredential=minimax-api-key:/run/ops-orchestrator-secrets/current/minimax-api-key",
        "LoadCredential=google-api-key:/run/ops-orchestrator-secrets/current/google-api-key",
        "LoadCredential=cohere-api-key:/run/ops-orchestrator-secrets/current/cohere-api-key",
        "LoadCredential=moonshot-api-key:/run/ops-orchestrator-secrets/current/moonshot-api-key",
        "LoadCredential=tencent-api-key:/run/ops-orchestrator-secrets/current/tencent-api-key",
        "LoadCredential=xai-api-key:/run/ops-orchestrator-secrets/current/xai-api-key",
        "LoadCredential=openai-api-key:/run/ops-orchestrator-secrets/current/openai-api-key",
        "LoadCredential=anthropic-api-key:/run/ops-orchestrator-secrets/current/anthropic-api-key",
    ]
    assert [line for line in finance.splitlines() if line.startswith("LoadCredential=")] == [
        "LoadCredential=deepseek-api-key:/run/ops-orchestrator-secrets/current/deepseek-api-key",
        "LoadCredential=moonshot-api-key:/run/ops-orchestrator-secrets/current/moonshot-api-key",
        "LoadCredential=stepfun-api-key:/run/ops-orchestrator-secrets/current/stepfun-api-key",
    ]
    assert [line for line in facade.splitlines() if line.startswith("LoadCredential=")] == [
        "LoadCredential=qwen-api-key:/run/ops-orchestrator-secrets/current/qwen-api-key",
        "LoadCredential=deepseek-api-key:/run/ops-orchestrator-secrets/current/deepseek-api-key",
        "LoadCredential=hermes-facade-token:/run/ops-orchestrator-secrets/current/hermes-facade-token",
    ]
    assert "SetCredential=moonshot-api-key:\\n\n" in finance
    assert "SetCredential=stepfun-api-key:\\n\n" in finance
    assert "SetCredential=deepseek-api-key:\\n\n" in finance
    assert "SetCredential=qwen-api-key:\\n\n" in facade
    assert "SetCredential=deepseek-api-key:\\n\n" in facade
    for name in (
        "qwen", "deepseek", "z-ai", "mistral-ai", "minimax", "google",
        "cohere", "moonshot", "tencent", "xai", "openai", "anthropic",
    ):
        assert f"SetCredential={name}-api-key:\\n\n" in main
    assert "User=opsfinance\n" in finance
    assert "Group=opsfinance\n" in finance
    assert "SupplementaryGroups=opsorchestrator\n" in finance
    assert "RuntimeDirectoryMode=0750\n" in finance
    assert "StateDirectory=" not in finance
    assert "/var/lib/ops-orchestrator" not in finance
    assert "ReadWritePaths=/run/ops-orchestrator-finance\n" in finance
    assert "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6\n" in finance
    assert "SupplementaryGroups=opsorchestrator opsfinance\n" in main
    assert "InaccessiblePaths=/run/ops-orchestrator-finance\n" in facade
    assert "LoadCredential=" not in timer_client
    assert "providers_" not in main + finance + facade

    sysusers = (
        ROOT / "orchestrator" / "deploy" / "ops-orchestrator.sysusers"
    ).read_text(encoding="utf-8")
    assert "g opsfinance -\n" in sysusers
    assert (
        'u opsfinance - "Ops provider finance worker" /nonexistent '
        "/usr/sbin/nologin\n"
    ) in sysusers
    assert "m opsorchestrator opsfinance\n" in sysusers
    assert "m hermesd opsfinance" not in sysusers
