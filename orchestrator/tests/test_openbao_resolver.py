from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "orchestrator" / "deploy" / "ops-orchestrator-openbao-resolve"


def _load():
    loader = importlib.machinery.SourceFileLoader(
        "ops_orchestrator_openbao_resolve", str(SCRIPT)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _prepare(monkeypatch, module, events: list[str]) -> None:
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module, "_ensure_runtime_directories", lambda: None)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/test")
    monkeypatch.setattr(
        module, "_read_private", lambda path, maximum: "identifier-orchestrator-12345"
    )
    monkeypatch.setattr(
        module, "_clear_secret_directory", lambda path: events.append("clear:" + path.name)
    )


def test_resolver_reads_all_accounts_and_revokes_before_bundle_publication(monkeypatch) -> None:
    module = _load()
    events: list[str] = []

    def request(method, path, **kwargs):
        if path == "/v1/auth/approle/login":
            events.append("login")
            return {"auth": {"client_token": "token-orchestrator-runtime-12345"}}
        provider = next(
            (
                provider_id
                for provider_id, value in module.PROVIDER_KV_PATHS.items()
                if value[0] == path
            ),
            None,
        )
        if provider is not None:
            events.append(f"read:{provider}")
            if provider == "alibaba":
                return {"data": {"data": {"api_key": "Q" * 48}}}
            if provider == "deepseek":
                return {"data": {"data": {"api_key": "D" * 48}}}
            return None
        if path == module.HERMES_GATEWAY_KV_PATH:
            events.append("read:hermes")
            return {"data": {"data": {"orchestrator_api_key": "H" * 48}}}
        if path == "/v1/auth/token/revoke-self":
            events.append("revoke")
            return None
        raise AssertionError(path)

    _prepare(monkeypatch, module, events)
    monkeypatch.setattr(module, "_request", request)
    monkeypatch.setattr(
        module,
        "_publish_bundle",
        lambda keys, hermes: events.append(
            f"publish:{keys['qwen-api-key'][0]}{keys['deepseek-api-key'][0]}{hermes[0]}"
        ),
    )

    module.resolve()

    assert events == [
        "clear:current",
        "clear:stage",
        "login",
        *[f"read:{provider_id}" for provider_id in module.PROVIDER_KV_PATHS],
        "read:hermes",
        "revoke",
        "publish:QDH",
    ]


def test_resolver_never_publishes_when_revoke_fails(monkeypatch) -> None:
    module = _load()
    events: list[str] = []

    def request(method, path, **kwargs):
        if path == "/v1/auth/approle/login":
            return {"auth": {"client_token": "token-orchestrator-runtime-12345"}}
        if path in {value[0] for value in module.PROVIDER_KV_PATHS.values()}:
            return {"data": {"data": {"api_key": "K" * 48}}}
        if path == module.HERMES_GATEWAY_KV_PATH:
            return {"data": {"data": {"orchestrator_api_key": "H" * 48}}}
        if path == "/v1/auth/token/revoke-self":
            raise module.ResolutionError("revocation failed")
        raise AssertionError(path)

    _prepare(monkeypatch, module, events)
    monkeypatch.setattr(module, "_request", request)
    monkeypatch.setattr(module, "_publish_bundle", lambda *args: events.append("publish"))

    with pytest.raises(module.ResolutionError, match="revocation"):
        module.resolve()

    assert "publish" not in events
    assert events.count("clear:current") == 2
    assert events.count("clear:stage") == 2


def test_bundle_publication_exposes_all_root_only_files_together(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load()
    live = tmp_path / "ops-orchestrator-secrets"
    stage = tmp_path / "ops-orchestrator-secrets-stage"
    live.mkdir(mode=0o700)
    stage.mkdir(mode=0o700)
    monkeypatch.setattr(module, "OUTPUT_DIRECTORY", live)
    monkeypatch.setattr(module, "STAGING_DIRECTORY", stage)
    monkeypatch.setattr(module, "EXPECTED_OWNER", os.getuid())
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)

    module._publish_bundle(
        {"qwen-api-key": "Q" * 48, "deepseek-api-key": "D" * 48},
        "H" * 48,
    )

    assert stage.is_dir()
    assert list(stage.iterdir()) == []
    assert sorted(path.name for path in live.iterdir()) == [
        "deepseek-api-key",
        "hermes-facade-token",
        "qwen-api-key",
    ]
    assert (live / "qwen-api-key").read_text(encoding="ascii") == "Q" * 48 + "\n"
    assert (live / "deepseek-api-key").read_text(encoding="ascii") == "D" * 48 + "\n"
    assert (live / "hermes-facade-token").read_text(encoding="ascii") == "H" * 48 + "\n"
    assert (live / "qwen-api-key").stat().st_mode & 0o777 == 0o400
    assert (live / "deepseek-api-key").stat().st_mode & 0o777 == 0o400
    assert (live / "hermes-facade-token").stat().st_mode & 0o777 == 0o400

    module.clear_runtime_credentials()
    assert list(live.iterdir()) == []
    assert list(stage.iterdir()) == []


def test_kv_schema_and_internal_requests_are_exactly_bounded() -> None:
    module = _load()
    document = {"data": {"data": {"api_key": "K" * 48}}}
    assert module._extract_api_key(document, "provider") == "K" * 48
    assert module._extract_api_key(
        {"data": {"data": {"api_key": "K" * 48, "finance_api_key": "F" * 48}}},
        "provider",
    ) == "K" * 48
    with pytest.raises(module.ResolutionError):
        module._extract_api_key(
            {"data": {"data": {"api_key": "K" * 48, "extra": "denied"}}},
            "provider",
        )
    assert module._extract_hermes_facade_token(
        {
            "data": {
                "data": {
                    "api_server_key": "S" * 48,
                    "orchestrator_api_key": "H" * 48,
                }
            }
        }
    ) == "H" * 48
    with pytest.raises(module.ResolutionError, match="internal"):
        module._request("GET", "/v1/kv-infra-shared/data/llm/other")


def test_unit_and_policy_enforce_23_use_22_read_boundary() -> None:
    unit = (ROOT / "systemd" / "ops-orchestrator-secrets.service").read_text(
        encoding="utf-8"
    )
    policy = (
        ROOT
        / "config"
        / "openbao"
        / "policies"
        / "ops-orchestrator-runtime.hcl"
    ).read_text(encoding="utf-8")
    resolver = SCRIPT.read_text(encoding="utf-8")

    assert "RuntimeDirectoryMode=0700" in unit
    assert "IPAddressDeny=any" in unit
    assert "IPAddressAllow=localhost" in unit
    assert "ProxyHandler({})" in resolver
    assert "_RejectRedirects()" in resolver
    assert policy.count('capabilities = ["read"]') == 22
    assert policy.count('capabilities = ["update"]') == 1
    assert 'path "kv-infra-shared/data/llm/qwen"' in policy
    assert 'path "kv-infra-shared/data/llm/deepseek"' in policy
    assert 'path "kv-infra-shared/data/hermes/gateway"' in policy
    assert 'path "auth/token/revoke-self"' in policy
    assert "list" not in policy
    main_unit = (ROOT / "systemd" / "ops-orchestrator.service").read_text(
        encoding="utf-8"
    )
    assert "LoadCredential=providers:/run/ops-orchestrator-secrets" not in main_unit
    assert main_unit.count("LoadCredential=") == 12
    assert main_unit.count("SetCredential=") == 12
    assert "SetCredential=qwen-api-key:\\n\n" in main_unit
    assert "SetCredential=deepseek-api-key:\\n\n" in main_unit
