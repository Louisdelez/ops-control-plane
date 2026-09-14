from __future__ import annotations

import copy
import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "provision-orchestrator-openbao"


def _load():
    loader = importlib.machinery.SourceFileLoader(
        "provision_orchestrator_openbao", str(SCRIPT)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _safe_role(module):
    return {
        "data": {
            "token_policies": [module.POLICY_NAME],
            "bind_secret_id": True,
            "token_no_default_policy": True,
            "secret_id_num_uses": 1024,
            "token_num_uses": 23,
            "token_period": 0,
            "token_type": "service",
            "secret_id_bound_cidrs": ["127.0.0.1/32"],
            "token_bound_cidrs": ["127.0.0.1/32"],
            "secret_id_ttl": 2_592_000,
            "token_ttl": 60,
            "token_max_ttl": 120,
            "token_explicit_max_ttl": 120,
        }
    }


def test_runtime_role_requires_exactly_twenty_three_uses_and_one_policy() -> None:
    module = _load()
    role = _safe_role(module)
    module._validate_role(role)

    role["data"]["token_num_uses"] = 22
    with pytest.raises(module.ProvisioningError):
        module._validate_role(role)
    role = _safe_role(module)
    role["data"]["token_policies"].append("default")
    with pytest.raises(module.ProvisioningError):
        module._validate_role(role)


def test_configure_runtime_sets_exact_use_loopback_role(monkeypatch) -> None:
    module = _load()
    calls: list[tuple[str, str, dict]] = []

    def request(method, path, **kwargs):
        calls.append((method, path, copy.deepcopy(kwargs)))
        return None

    monkeypatch.setattr(module, "_request", request)
    module._configure_runtime("human-token-identifier-12345", "reviewed-policy")

    role_call = calls[1]
    assert role_call[1] == f"/v1/auth/approle/role/{module.ROLE_NAME}"
    assert role_call[2]["payload"]["token_num_uses"] == 23
    assert role_call[2]["payload"]["token_no_default_policy"] is True
    assert role_call[2]["payload"]["token_bound_cidrs"] == ["127.0.0.1/32"]


def test_provider_and_local_credentials_must_be_pairwise_distinct() -> None:
    module = _load()
    module._require_distinct_secrets(
        {
            "Qwen API": "Q" * 48,
            "DeepSeek API": "D" * 48,
            "Hermes facade": "F" * 48,
            "Hermes API server": "S" * 48,
        }
    )

    with pytest.raises(module.ProvisioningError, match="must be distinct"):
        module._require_distinct_secrets(
            {
                "Qwen API": "Q" * 48,
                "Hermes facade": "Q" * 48,
            }
        )


def test_runtime_test_reads_all_provider_paths_then_gateway_and_revokes(monkeypatch) -> None:
    module = _load()
    calls: list[tuple[str, str]] = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        if path == "/v1/auth/approle/login":
            return {"auth": {"client_token": "token-orchestrator-runtime-12345"}}
        if path == module.QWEN_KV_PATH:
            return {"data": {"data": {"api_key": "Q" * 48}}}
        if path == module.DEEPSEEK_KV_PATH:
            return {"data": {"data": {"api_key": "D" * 48}}}
        if path == module.HERMES_GATEWAY_KV_PATH:
            return {
                "data": {
                    "data": {
                        "api_server_key": "S" * 48,
                        "orchestrator_api_key": "H" * 48,
                    }
                }
            }
        if path in module.PROVIDER_KV_PATHS.values():
            return None
        if path == "/v1/auth/token/revoke-self":
            return None
        raise AssertionError(path)

    monkeypatch.setattr(module, "_request", request)
    module._test_runtime(
        "role-identifier-12345",
        "secret-identifier-12345",
        {"alibaba": "Q" * 48, "deepseek": "D" * 48},
        "H" * 48,
    )

    assert calls == [
        ("POST", "/v1/auth/approle/login"),
        *[("GET", path) for path in module.PROVIDER_KV_PATHS.values()],
        ("GET", module.HERMES_GATEWAY_KV_PATH),
        ("POST", "/v1/auth/token/revoke-self"),
    ]


def test_runtime_test_accepts_no_provider_objects_but_keeps_local_gateway(
    monkeypatch,
) -> None:
    module = _load()
    calls: list[tuple[str, str]] = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        if path == "/v1/auth/approle/login":
            return {"auth": {"client_token": "token-orchestrator-runtime-12345"}}
        if path in module.PROVIDER_KV_PATHS.values():
            return None
        if path == module.HERMES_GATEWAY_KV_PATH:
            return {
                "data": {
                    "data": {
                        "api_server_key": "S" * 48,
                        "orchestrator_api_key": "H" * 48,
                    }
                }
            }
        if path == "/v1/auth/token/revoke-self":
            return None
        raise AssertionError(path)

    monkeypatch.setattr(module, "_request", request)
    module._test_runtime(
        "role-identifier-12345",
        "secret-identifier-12345",
        {},
        "H" * 48,
    )

    assert calls == [
        ("POST", "/v1/auth/approle/login"),
        *[("GET", path) for path in module.PROVIDER_KV_PATHS.values()],
        ("GET", module.HERMES_GATEWAY_KV_PATH),
        ("POST", "/v1/auth/token/revoke-self"),
    ]


def test_runtime_test_rejects_an_unexpected_provider_object_and_revokes(
    monkeypatch,
) -> None:
    module = _load()
    calls: list[tuple[str, str]] = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        if path == "/v1/auth/approle/login":
            return {"auth": {"client_token": "token-orchestrator-runtime-12345"}}
        if path == module.PROVIDER_KV_PATHS["openai"]:
            return {"data": {"data": {"api_key": "O" * 48}}}
        if path in module.PROVIDER_KV_PATHS.values():
            return None
        if path == module.HERMES_GATEWAY_KV_PATH:
            return {
                "data": {
                    "data": {
                        "api_server_key": "S" * 48,
                        "orchestrator_api_key": "H" * 48,
                    }
                }
            }
        if path == "/v1/auth/token/revoke-self":
            return None
        raise AssertionError(path)

    monkeypatch.setattr(module, "_request", request)
    with pytest.raises(module.ProvisioningError, match="22-read/revoke"):
        module._test_runtime(
            "role-identifier-12345",
            "secret-identifier-12345",
            {},
            "H" * 48,
        )

    assert calls[-1] == ("POST", "/v1/auth/token/revoke-self")


def test_zero_key_namespace_check_rejects_any_existing_provider_object(
    monkeypatch,
) -> None:
    module = _load()
    calls: list[str] = []

    def request(method, path, **kwargs):
        assert method == "GET"
        calls.append(path)
        if path == module.PROVIDER_KV_PATHS["mistral-ai"]:
            return {"data": {"data": {"api_key": "M" * 48}}}
        return None

    monkeypatch.setattr(module, "_request", request)
    with pytest.raises(module.ProvisioningError, match="empty provider"):
        module._assert_provider_namespace_empty("human-token-identifier-12345")

    assert calls[-1] == module.PROVIDER_KV_PATHS["mistral-ai"]


def test_bootstrap_password_is_read_only_from_an_inherited_pipe() -> None:
    module = _load()
    read_descriptor, write_descriptor = os.pipe()
    os.write(write_descriptor, "mot-de-passe-local-très-long\n".encode())
    os.close(write_descriptor)

    assert module._password_from_pipe(read_descriptor) == "mot-de-passe-local-très-long"
    with pytest.raises(OSError):
        os.fstat(read_descriptor)


def test_bootstrap_password_rejects_a_named_fifo(tmp_path: Path) -> None:
    module = _load()
    fifo = tmp_path / "password"
    os.mkfifo(fifo, mode=0o600)
    descriptor = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        with pytest.raises(module.ProvisioningError, match="not a pipe"):
            module._password_from_pipe(descriptor)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def test_bootstrap_arguments_are_an_exact_pair() -> None:
    module = _load()
    arguments = module._parse_args(["--bootstrap-empty", "--password-fd", "9"])
    assert arguments.bootstrap_empty is True
    assert arguments.password_fd == 9

    with pytest.raises(SystemExit):
        module._parse_args(["--bootstrap-empty"])
    with pytest.raises(SystemExit):
        module._parse_args(["--password-fd", "9"])


def test_zero_key_bootstrap_creates_only_local_gateway_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load()
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.os, "umask", lambda _mode: 0o022)
    monkeypatch.setattr(module, "CREDSTORE", tmp_path)
    monkeypatch.setattr(module, "_service_is_active", lambda _service: False)
    monkeypatch.setattr(module, "_validate_root_regular", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_read_policy", lambda: "reviewed-policy")
    monkeypatch.setattr(module, "_prepare_credstore", lambda: None)
    monkeypatch.setattr(module, "_password_from_pipe", lambda _fd: "human-password")
    generated = iter(("F" * 48, "S" * 48))
    monkeypatch.setattr(module.secrets, "token_urlsafe", lambda _size: next(generated))

    calls: list[tuple[str, str, dict]] = []

    def request(method, path, **kwargs):
        calls.append((method, path, copy.deepcopy(kwargs)))
        if path == "/v1/auth/userpass/login/ops-user":
            return {"auth": {"client_token": "human-token-identifier-12345"}}
        if method == "GET" and path == f"/v1/auth/approle/role/{module.ROLE_NAME}":
            return _safe_role(module)
        if method == "GET" and path == module.HERMES_GATEWAY_KV_PATH:
            return None
        if method == "GET" and path.endswith("/role-id"):
            return {"data": {"role_id": "role-identifier-12345"}}
        if method == "POST" and path.endswith("/secret-id"):
            return {
                "data": {
                    "secret_id": "secret-identifier-12345",
                    "secret_id_accessor": "accessor-identifier-12345",
                    "secret_id_ttl": 2_592_000,
                }
            }
        return None

    monkeypatch.setattr(module, "_request", request)
    runtime_test: dict[str, object] = {}

    def test_runtime(role_id, secret_id, expected_provider_keys, facade_token):
        runtime_test.update(
            role_id=role_id,
            secret_id=secret_id,
            provider_keys=dict(expected_provider_keys),
            facade_token=facade_token,
        )

    monkeypatch.setattr(module, "_test_runtime", test_runtime)

    def encrypt(_name, value, output):
        output.write_text(value, encoding="ascii")

    monkeypatch.setattr(module, "_encrypt", encrypt)
    monkeypatch.setattr(
        module, "_decrypt", lambda _name, path: path.read_text(encoding="ascii")
    )

    module.provision(bootstrap_empty=True, password_fd=9)

    provider_writes = [
        path
        for method, path, _kwargs in calls
        if method == "POST" and path in module.PROVIDER_KV_PATHS.values()
    ]
    assert provider_writes == []
    gateway_writes = [
        kwargs["payload"]
        for method, path, kwargs in calls
        if method == "POST" and path == module.HERMES_GATEWAY_KV_PATH
    ]
    assert gateway_writes == [
        {"data": {"api_server_key": "A" + "S" * 48, "orchestrator_api_key": "A" + "F" * 48}}
    ]
    assert runtime_test["provider_keys"] == {}
    assert runtime_test["facade_token"] == "A" + "F" * 48


def test_provisioner_never_places_plaintext_secret_in_systemd_creds_argv(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load()
    output = tmp_path / "encrypted"
    secret = "SENSITIVE-provider-key-123456789"
    observed = {}

    class Completed:
        returncode = 0

    def run(command, **kwargs):
        observed["command"] = command
        observed["input"] = kwargs.get("input")
        output.write_bytes(b"encrypted material")
        return Completed()

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module, "_validate_root_regular", lambda *args, **kwargs: None)
    module._encrypt("credential-name", secret, output)

    assert secret not in " ".join(observed["command"])
    assert observed["input"] == (secret + "\n").encode("ascii")
    assert observed["command"][-2] == "-"


def test_assets_declare_separate_identity_and_no_proxy_redirects() -> None:
    module = _load()
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'POLICY_NAME = "ops-orchestrator-runtime"' in source
    assert 'ROLE_NAME = "ops-orchestrator-runtime"' in source
    assert "ProxyHandler({})" in source
    assert "_RejectRedirects()" in source
    assert "getpass.getpass" in source
    assert '"token_num_uses": 23' in source
    assert 'payload={"data": {"api_key": qwen_key}}' in source
    assert "--with-key=host+tpm2" in source
    assert "ops-orchestrator-provider-finance-daemon.service" in module.SERVICES
