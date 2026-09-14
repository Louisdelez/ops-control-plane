from __future__ import annotations

import importlib.machinery
import importlib.util
import copy
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import subprocess
import traceback
import urllib.error

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ops-model-key-manager"
SENTINEL_PASSWORD = "NEVER-EXPOSE-OPENBAO-PASSWORD-846231"
SENTINEL_KEY = "NEVER-EXPOSE-PROVIDER-KEY-975314"
SENTINEL_TOKEN = "hvs.NEVER-EXPOSE-SESSION-TOKEN-417593"

EXPECTED_TARGETS = {
    "jina": ("/v1/kv-infra-shared/data/llm/providers/jina", "/v1/kv-infra-shared/metadata/llm/providers/jina"),
    "voyage": ("/v1/kv-infra-shared/data/llm/providers/voyage", "/v1/kv-infra-shared/metadata/llm/providers/voyage"),
    "siliconflow": ("/v1/kv-infra-shared/data/llm/siliconflow", "/v1/kv-infra-shared/metadata/llm/siliconflow"),
    "alibaba": (
        "/v1/kv-infra-shared/data/llm/qwen",
        "/v1/kv-infra-shared/metadata/llm/qwen",
    ),
    "z-ai": (
        "/v1/kv-infra-shared/data/llm/providers/z-ai",
        "/v1/kv-infra-shared/metadata/llm/providers/z-ai",
    ),
    "mistral-ai": (
        "/v1/kv-infra-shared/data/llm/providers/mistral-ai",
        "/v1/kv-infra-shared/metadata/llm/providers/mistral-ai",
    ),
    "deepseek": (
        "/v1/kv-infra-shared/data/llm/deepseek",
        "/v1/kv-infra-shared/metadata/llm/deepseek",
    ),
    "minimax": (
        "/v1/kv-infra-shared/data/llm/providers/minimax",
        "/v1/kv-infra-shared/metadata/llm/providers/minimax",
    ),
    "google": (
        "/v1/kv-infra-shared/data/llm/providers/google",
        "/v1/kv-infra-shared/metadata/llm/providers/google",
    ),
    "cohere": (
        "/v1/kv-infra-shared/data/llm/providers/cohere",
        "/v1/kv-infra-shared/metadata/llm/providers/cohere",
    ),
    "moonshot": (
        "/v1/kv-infra-shared/data/llm/providers/moonshot",
        "/v1/kv-infra-shared/metadata/llm/providers/moonshot",
    ),
    "tencent": (
        "/v1/kv-infra-shared/data/llm/providers/tencent",
        "/v1/kv-infra-shared/metadata/llm/providers/tencent",
    ),
    "xai": (
        "/v1/kv-infra-shared/data/llm/providers/xai",
        "/v1/kv-infra-shared/metadata/llm/providers/xai",
    ),
    "openai": (
        "/v1/kv-infra-shared/data/llm/providers/openai",
        "/v1/kv-infra-shared/metadata/llm/providers/openai",
    ),
    "bytedance": (
        "/v1/kv-infra-shared/data/llm/providers/bytedance",
        "/v1/kv-infra-shared/metadata/llm/providers/bytedance",
    ),
    "anthropic": (
        "/v1/kv-infra-shared/data/llm/providers/anthropic",
        "/v1/kv-infra-shared/metadata/llm/providers/anthropic",
    ),
    "nvidia": (
        "/v1/kv-infra-shared/data/llm/providers/nvidia",
        "/v1/kv-infra-shared/metadata/llm/providers/nvidia",
    ),
    "aws": (
        "/v1/kv-infra-shared/data/llm/providers/aws",
        "/v1/kv-infra-shared/metadata/llm/providers/aws",
    ),
    "meta": (
        "/v1/kv-infra-shared/data/llm/providers/meta",
        "/v1/kv-infra-shared/metadata/llm/providers/meta",
    ),
    "microsoft": (
        "/v1/kv-infra-shared/data/llm/providers/microsoft",
        "/v1/kv-infra-shared/metadata/llm/providers/microsoft",
    ),
    "xiaomi": (
        "/v1/kv-infra-shared/data/llm/providers/xiaomi",
        "/v1/kv-infra-shared/metadata/llm/providers/xiaomi",
    ),
    "baidu": (
        "/v1/kv-infra-shared/data/llm/providers/baidu",
        "/v1/kv-infra-shared/metadata/llm/providers/baidu",
    ),
    "baichuan": (
        "/v1/kv-infra-shared/data/llm/providers/baichuan",
        "/v1/kv-infra-shared/metadata/llm/providers/baichuan",
    ),
    "stepfun": (
        "/v1/kv-infra-shared/data/llm/providers/stepfun",
        "/v1/kv-infra-shared/metadata/llm/providers/stepfun",
    ),
}


def load_script():
    loader = importlib.machinery.SourceFileLoader(
        "ops_model_key_manager", str(SCRIPT)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def manager():
    return load_script()


def _assert_exception_is_clean(module, error: BaseException) -> None:
    combined = "".join(traceback.format_exception(error))
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        combined += str(current) + repr(current)
        trace = current.__traceback__
        while trace is not None:
            if Path(trace.tb_frame.f_code.co_filename) == SCRIPT:
                for value in trace.tb_frame.f_locals.values():
                    if isinstance(value, str):
                        combined += value
                    elif isinstance(value, (bytes, bytearray)):
                        combined += bytes(value).decode("utf-8", errors="ignore")
                    elif isinstance(value, dict):
                        combined += repr(value)
            trace = trace.tb_next
        current = current.__cause__ or current.__context__
    for secret in (SENTINEL_PASSWORD, SENTINEL_KEY, SENTINEL_TOKEN):
        assert secret not in combined


def test_allowlist_is_closed_and_maps_exactly_twenty_one_accounts(manager) -> None:
    assert dict(manager.PROVIDER_TARGETS) == EXPECTED_TARGETS
    assert len(manager.PROVIDER_TARGETS) == 24
    assert manager.INSTALLED_PROGRAM == Path(
        "/usr/local/libexec/ops-model-key-manager"
    )
    assert manager.RELOAD_PROGRAM == Path(
        "/usr/local/libexec/ops-model-credentials-reload"
    )


def test_allowlist_matches_every_catalogue_account_and_credential_reference(
    manager,
) -> None:
    catalogue = json.loads(
        (ROOT / "catalog" / "model-catalog.v2.json").read_text(encoding="utf-8")
    )
    accounts = {account["id"]: account for account in catalogue["provider_accounts"]}
    assert set(accounts) | {"siliconflow", "voyage", "jina"} == set(manager.PROVIDER_TARGETS)
    for account_id, (data_path, metadata_path) in manager.PROVIDER_TARGETS.items():
        if account_id in {"siliconflow", "voyage", "jina"}:
            continue
        assert data_path == f"/v1/{accounts[account_id]['credential_ref']}"
        assert metadata_path == data_path.replace("/data/", "/metadata/", 1)


@pytest.mark.parametrize(
    "argv",
    [
        ["ops-model-key-manager"],
        ["ops-model-key-manager", "openai"],
        ["ops-model-key-manager", "get", "openai"],
        ["ops-model-key-manager", "set"],
        ["ops-model-key-manager", "set", "openai", "extra"],
        ["ops-model-key-manager", "set", "OpenAI"],
        ["ops-model-key-manager", "set", "openai/../deepseek"],
        ["ops-model-key-manager", "set", "unknown"],
        ["ops-model-key-manager", "set", SENTINEL_KEY],
    ],
)
def test_main_rejects_every_non_exact_argument_without_prompting(
    manager, monkeypatch, capsys, argv
) -> None:
    prompted = False

    def unexpected_prompt(*args, **kwargs):
        nonlocal prompted
        prompted = True
        raise AssertionError("prompt must not run")

    monkeypatch.setattr(manager, "_ask_secret", unexpected_prompt)
    assert manager.main(argv) == 1
    document = json.loads(capsys.readouterr().out)
    assert document == {"provider_account_id": None, "status": "failed"}
    assert prompted is False


def test_parser_accepts_only_the_fixed_set_contract(manager) -> None:
    assert manager._parse_provider(
        ["ops-model-key-manager", "set", "openai"]
    ) == "openai"


def test_native_prompt_never_places_secret_in_argv_environment_or_stderr(
    manager, monkeypatch
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command, **kwargs):
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        observed["stderr"] = kwargs["stderr"]
        return SimpleNamespace(returncode=0, stdout=SENTINEL_KEY.encode("ascii"))

    monkeypatch.setenv("API_KEY", SENTINEL_KEY)
    monkeypatch.setenv("OPENBAO_PASSWORD", SENTINEL_PASSWORD)
    monkeypatch.setattr(manager.subprocess, "run", fake_run)
    value = manager._ask_secret("fixed-prompt", "Fixed message", 128)
    try:
        assert bytes(value).decode("ascii") == SENTINEL_KEY
        assert SENTINEL_KEY not in repr(observed["command"])
        assert SENTINEL_PASSWORD not in repr(observed["command"])
        assert SENTINEL_KEY not in repr(observed["environment"])
        assert SENTINEL_PASSWORD not in repr(observed["environment"])
        assert observed["stderr"] is subprocess.DEVNULL
    finally:
        manager._wipe(value)


def test_prompt_timeout_does_not_retain_partial_secret_in_exception(
    manager, monkeypatch
) -> None:
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(
            command, timeout=1, output=SENTINEL_KEY.encode("ascii")
        )

    monkeypatch.setattr(manager.subprocess, "run", timeout)
    with pytest.raises(manager.PromptCancelled) as caught:
        manager._ask_secret("fixed-prompt", "Fixed message", 128)
    _assert_exception_is_clean(manager, caught.value)


def test_request_is_tls_loopback_only_without_proxy_or_redirects(manager) -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'BAO_ORIGIN = "https://127.0.0.1:8200"' in source
    assert 'BAO_CA = Path("/etc/openbao.d/tls/ca.crt")' in source
    assert "urllib.request.ProxyHandler({})" in source
    assert "_RejectRedirects()" in source
    assert 'LOGIN_PATH = "/v1/auth/userpass/login/ops-user"' in source
    assert source.startswith("#!/usr/bin/python3 -I\n")
    assert "os.environ.clear()" in source


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/kv-infra-shared/data/llm/qwen"),
        ("POST", "/v1/kv-infra-shared/metadata/llm/qwen"),
        ("POST", "/v1/kv-infra-shared/data/llm/providers/unknown"),
        ("GET", "/v1/sys/health"),
    ],
)
def test_request_allowlist_rejects_wrong_method_or_path(
    manager, monkeypatch, method, path
) -> None:
    monkeypatch.setattr(
        manager, "_opener", lambda: pytest.fail("network must not be reached")
    )
    with pytest.raises(manager.KeyManagerError):
        manager._request(method, path, payload={}, expect_json=False)


def test_transport_exception_and_request_payload_do_not_leak(
    manager, monkeypatch
) -> None:
    class FailedOpener:
        def open(self, request, timeout):
            raise urllib.error.URLError(SENTINEL_PASSWORD)

    monkeypatch.setattr(manager, "_opener", FailedOpener)
    with pytest.raises(manager.KeyManagerError) as caught:
        manager._request(
            "POST",
            manager.LOGIN_PATH,
            payload={"password": SENTINEL_PASSWORD},
            expect_json=True,
        )
    _assert_exception_is_clean(manager, caught.value)


def test_invalid_session_header_does_not_retain_token_or_payload(manager) -> None:
    with pytest.raises(manager.KeyManagerError) as caught:
        manager._request(
            "POST",
            "/v1/kv-infra-shared/data/llm/providers/openai",
            payload={"data": {"api_key": SENTINEL_KEY}},
            token=SENTINEL_TOKEN + "\n",
            expect_json=False,
        )
    _assert_exception_is_clean(manager, caught.value)


def test_invalid_login_document_does_not_retain_password_or_token(
    manager, monkeypatch
) -> None:
    monkeypatch.setattr(
        manager,
        "_request",
        lambda *args, **kwargs: (
            200,
            {"auth": {"client_token": SENTINEL_TOKEN + "\n"}},
        ),
    )
    with pytest.raises(manager.KeyManagerError) as caught:
        manager._login(SENTINEL_PASSWORD)
    _assert_exception_is_clean(manager, caught.value)


def _successful_login_response():
    return 200, {"auth": {"client_token": SENTINEL_TOKEN}}


@pytest.mark.parametrize(
    ("metadata_response", "expected_options"),
    [
        ((200, {"data": {"current_version": 7}}), {"cas": 7}),
        ((404, None), {"cas": 0}),
    ],
)
def test_rotation_uses_cas_when_metadata_is_available(
    manager, monkeypatch, metadata_response, expected_options
) -> None:
    calls: list[tuple[str, str, dict]] = []

    def request(method, path, **kwargs):
        calls.append((method, path, copy.deepcopy(kwargs)))
        if path == manager.LOGIN_PATH:
            return _successful_login_response()
        if "/metadata/" in path:
            return metadata_response
        return 204, None

    monkeypatch.setattr(manager, "_request", request)
    manager._rotate("openai", SENTINEL_PASSWORD, SENTINEL_KEY)

    write = next(
        call
        for call in calls
        if call[1] == "/v1/kv-infra-shared/data/llm/providers/openai"
    )
    assert write[2]["payload"]["data"]["api_key"] == SENTINEL_KEY
    assert write[2]["payload"]["options"] == expected_options
    assert calls[-1][0:2] == ("POST", manager.REVOKE_PATH)


def test_metadata_access_denial_fails_closed_and_still_revokes(
    manager, monkeypatch
) -> None:
    calls: list[tuple[str, str]] = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        if path == manager.LOGIN_PATH:
            return _successful_login_response()
        if "/metadata/" in path:
            raise manager.KeyManagerError("metadata denied")
        if path == manager.REVOKE_PATH:
            return 204, None
        pytest.fail("write must not run when CAS metadata is unavailable")

    monkeypatch.setattr(manager, "_request", request)
    with pytest.raises(manager.KeyManagerError) as caught:
        manager._rotate("openai", SENTINEL_PASSWORD, SENTINEL_KEY)
    assert calls[-1] == ("POST", manager.REVOKE_PATH)
    assert not any("/data/" in path for _, path in calls)
    _assert_exception_is_clean(manager, caught.value)


def test_write_failure_still_revokes_token_and_raises_clean_error(
    manager, monkeypatch
) -> None:
    calls: list[tuple[str, str]] = []

    def request(method, path, **kwargs):
        calls.append((method, path))
        if path == manager.LOGIN_PATH:
            return _successful_login_response()
        if "/metadata/" in path:
            return 200, {"data": {"current_version": 3}}
        if path == manager.REVOKE_PATH:
            return 204, None
        raise manager.KeyManagerError("write rejected")

    monkeypatch.setattr(manager, "_request", request)
    with pytest.raises(manager.KeyManagerError) as caught:
        manager._rotate("openai", SENTINEL_PASSWORD, SENTINEL_KEY)
    assert calls[-1] == ("POST", manager.REVOKE_PATH)
    _assert_exception_is_clean(manager, caught.value)


def test_revoke_failure_never_reports_rotation_success(manager, monkeypatch) -> None:
    def request(method, path, **kwargs):
        if path == manager.LOGIN_PATH:
            return _successful_login_response()
        if "/metadata/" in path:
            return 404, None
        if path == manager.REVOKE_PATH:
            raise manager.KeyManagerError("revoke rejected")
        return 204, None

    monkeypatch.setattr(manager, "_request", request)
    with pytest.raises(manager.KeyManagerError) as caught:
        manager._rotate("openai", SENTINEL_PASSWORD, SENTINEL_KEY)
    _assert_exception_is_clean(manager, caught.value)


def test_main_emits_only_bounded_non_sensitive_json(
    manager, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(manager, "_harden_process", lambda: None)
    monkeypatch.setattr(manager, "_validate_runtime", lambda: None)
    monkeypatch.setattr(
        manager,
        "_rotate_from_native_prompts",
        lambda provider: (_ for _ in ()).throw(
            manager.KeyManagerError(SENTINEL_KEY)
        ),
    )
    assert manager.main(["ops-model-key-manager", "set", "openai"]) == 1
    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.encode("utf-8")) <= 128
    assert json.loads(captured.out) == {
        "provider_account_id": "openai",
        "status": "failed",
    }
    assert SENTINEL_KEY not in captured.out


def test_cancelled_native_prompt_emits_only_cancelled_status(
    manager, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(manager, "_harden_process", lambda: None)
    monkeypatch.setattr(manager, "_validate_runtime", lambda: None)

    def cancelled(command, **kwargs):
        return SimpleNamespace(
            returncode=1, stdout=SENTINEL_PASSWORD.encode("ascii")
        )

    monkeypatch.setattr(manager.subprocess, "run", cancelled)
    assert manager.main(["ops-model-key-manager", "set", "deepseek"]) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "provider_account_id": "deepseek",
        "status": "cancelled",
    }
    assert SENTINEL_PASSWORD not in captured.out


def test_success_status_contains_no_credential_metadata(
    manager, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(manager, "_harden_process", lambda: None)
    monkeypatch.setattr(manager, "_validate_runtime", lambda: None)
    monkeypatch.setattr(manager, "_rotate_from_native_prompts", lambda provider: None)
    monkeypatch.setattr(manager, "_reload_consumers", lambda: None)
    assert manager.main(["ops-model-key-manager", "set", "alibaba"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "provider_account_id": "alibaba",
        "status": "rotated",
    }
    assert "fingerprint" not in captured.out
    assert "prefix" not in captured.out
    assert "secret" not in captured.out


def test_successful_rotation_requests_only_the_fixed_root_reload(
    manager, monkeypatch
) -> None:
    observed: dict[str, object] = {}

    def run(command, **kwargs):
        observed["command"] = command
        observed["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(manager.subprocess, "run", run)
    manager._reload_consumers()

    assert observed["command"] == [
        "/usr/bin/sudo",
        "-n",
        "--",
        "/usr/local/libexec/ops-model-credentials-reload",
    ]
    kwargs = observed["kwargs"]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
    assert SENTINEL_KEY not in repr(observed)
    assert SENTINEL_PASSWORD not in repr(observed)


def test_reload_failure_never_reports_rotation_success(
    manager, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(manager, "_harden_process", lambda: None)
    monkeypatch.setattr(manager, "_validate_runtime", lambda: None)
    monkeypatch.setattr(manager, "_rotate_from_native_prompts", lambda provider: None)
    monkeypatch.setattr(
        manager,
        "_reload_consumers",
        lambda: (_ for _ in ()).throw(manager.KeyManagerError("reload failed")),
    )

    assert manager.main(["ops-model-key-manager", "set", "deepseek"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "provider_account_id": "deepseek",
        "status": "failed",
    }


def test_native_prompt_buffers_are_wiped_after_failure(manager, monkeypatch) -> None:
    password = bytearray(SENTINEL_PASSWORD, "ascii")
    api_key = bytearray(SENTINEL_KEY, "ascii")
    answers = [password, api_key]
    monkeypatch.setattr(manager, "_ask_secret", lambda *args: answers.pop(0))
    monkeypatch.setattr(
        manager,
        "_rotate",
        lambda *args: (_ for _ in ()).throw(manager.KeyManagerError("failed")),
    )
    with pytest.raises(manager.KeyManagerError) as caught:
        manager._rotate_from_native_prompts("openai")
    assert set(password) == {0}
    assert set(api_key) == {0}
    _assert_exception_is_clean(manager, caught.value)


def test_helper_has_no_service_or_secret_persistence_actions() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for forbidden in (
        "systemctl",
        "restart",
        "start.service",
        "NamedTemporaryFile",
        "mkstemp",
        "getpass",
    ):
        assert forbidden not in source
    assert source.startswith("#!/usr/bin/python3 -I\n")


def _framed_credentials(password: bytes, api_key: bytes, trailing: bytes = b"") -> bytes:
    return (
        b"ATLASKEY1\n"
        + len(password).to_bytes(4, "big")
        + password
        + len(api_key).to_bytes(4, "big")
        + api_key
        + trailing
    )


def _mock_private_stdin(manager, monkeypatch, payload: bytes) -> bytearray:
    queued = bytearray(payload)

    def read(_descriptor, size):
        chunk = bytes(queued[:size])
        del queued[:size]
        return chunk

    monkeypatch.setattr(
        manager.os,
        "fstat",
        lambda _fd: SimpleNamespace(
            st_mode=stat.S_IFIFO | 0o600,
            st_uid=os.getuid(),
            st_nlink=1,
        ),
    )
    monkeypatch.setattr(manager.os, "isatty", lambda _fd: False)
    monkeypatch.setattr(manager.fcntl, "fcntl", lambda *_args: os.O_RDONLY)
    monkeypatch.setattr(manager.select, "select", lambda *_args: ([0], [], []))
    monkeypatch.setattr(manager.os, "read", read)
    return queued


def test_set_stdin_reads_exact_binary_frame_and_returns_wipeable_buffers(
    manager, monkeypatch
) -> None:
    password = SENTINEL_PASSWORD.encode("utf-8")
    api_key = SENTINEL_KEY.encode("ascii")
    queued = _mock_private_stdin(
        manager, monkeypatch, _framed_credentials(password, api_key)
    )
    supplied_password, supplied_key = manager._read_stdin_credentials()
    try:
        assert bytes(supplied_password) == password
        assert bytes(supplied_key) == api_key
        assert queued == bytearray()
    finally:
        manager._wipe(supplied_password)
        manager._wipe(supplied_key)
    assert set(supplied_password) == {0}
    assert set(supplied_key) == {0}


@pytest.mark.parametrize(
    "payload",
    [
        b"WRONGMAGIC\n",
        b"ATLASKEY1\n" + (0).to_bytes(4, "big"),
        b"ATLASKEY1\n" + (4097).to_bytes(4, "big"),
        _framed_credentials(
            SENTINEL_PASSWORD.encode(), SENTINEL_KEY.encode(), b"unexpected"
        ),
    ],
)
def test_set_stdin_rejects_malformed_oversized_and_trailing_frames_cleanly(
    manager, monkeypatch, payload
) -> None:
    _mock_private_stdin(manager, monkeypatch, payload)
    with pytest.raises(manager.KeyManagerError) as caught:
        manager._read_stdin_credentials()
    _assert_exception_is_clean(manager, caught.value)


def test_set_stdin_requires_a_private_anonymous_pipe(manager, monkeypatch) -> None:
    monkeypatch.setattr(
        manager.os,
        "fstat",
        lambda _fd: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_uid=os.getuid(),
            st_nlink=1,
        ),
    )
    monkeypatch.setattr(manager.fcntl, "fcntl", lambda *_args: os.O_RDONLY)
    monkeypatch.setattr(manager.os, "isatty", lambda _fd: False)
    with pytest.raises(manager.KeyManagerError, match="private pipe"):
        manager._validate_private_stdin_pipe()


def test_set_stdin_wipes_both_buffers_after_rotation_failure(
    manager, monkeypatch
) -> None:
    password = bytearray(SENTINEL_PASSWORD, "ascii")
    api_key = bytearray(SENTINEL_KEY, "ascii")
    monkeypatch.setattr(
        manager, "_read_stdin_credentials", lambda: (password, api_key)
    )
    monkeypatch.setattr(
        manager,
        "_rotate",
        lambda *_args: (_ for _ in ()).throw(manager.KeyManagerError("failed")),
    )
    with pytest.raises(manager.KeyManagerError) as caught:
        manager._rotate_from_stdin("openai")
    assert set(password) == {0}
    assert set(api_key) == {0}
    _assert_exception_is_clean(manager, caught.value)


def test_main_set_stdin_uses_no_prompt_and_emits_only_safe_result(
    manager, monkeypatch, capsys
) -> None:
    observed: list[str] = []
    monkeypatch.setattr(manager, "_harden_process", lambda: None)
    monkeypatch.setattr(manager, "_validate_runtime", lambda: None)
    monkeypatch.setattr(
        manager, "_rotate_from_stdin", lambda provider: observed.append(provider)
    )
    monkeypatch.setattr(
        manager,
        "_rotate_from_native_prompts",
        lambda _provider: pytest.fail("native prompt must not run"),
    )
    monkeypatch.setattr(manager, "_reload_consumers", lambda: None)
    assert manager.main(["ops-model-key-manager", "set-stdin", "openai"]) == 0
    assert observed == ["openai"]
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out) == {
        "provider_account_id": "openai",
        "status": "rotated",
    }


def test_stdin_protocol_contains_no_secret_in_argv_or_environment() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "ATLASKEY1\\n" in source
    assert 'argv[1] not in {"set", "set-stdin", "store-stdin"}' in source
    assert "os.environ.clear()" in source
    assert "NamedTemporaryFile" not in source
    assert "mkstemp" not in source


@pytest.mark.parametrize(
    "password",
    [
        "x" * 13,
        "x" * 101,
        " " + "x" * 14,
        "x" * 14 + "\n",
        "x" * 14 + "\x7f",
    ],
)
def test_openbao_password_policy_rejects_wrong_length_trim_and_controls(
    manager, password
) -> None:
    value = bytearray(password.encode("utf-8"))
    with pytest.raises(manager.KeyManagerError):
        manager._decode_password(value)
    manager._wipe(value)


def test_openbao_password_policy_accepts_fourteen_to_one_hundred_unicode_chars(
    manager,
) -> None:
    minimum = bytearray("é" * 14, "utf-8")
    maximum = bytearray("界" * 100, "utf-8")
    try:
        assert manager._decode_password(minimum) == "é" * 14
        assert manager._decode_password(maximum) == "界" * 100
    finally:
        manager._wipe(minimum)
        manager._wipe(maximum)


def test_store_stdin_does_not_reload_or_prompt(manager, monkeypatch, capsys):
    calls=[]
    monkeypatch.setattr(manager,"_harden_process",lambda:None)
    monkeypatch.setattr(manager,"_validate_runtime",lambda **kw:calls.append(kw))
    monkeypatch.setattr(manager,"_rotate_from_stdin",lambda provider:calls.append(provider))
    monkeypatch.setattr(manager,"_reload_consumers",lambda:pytest.fail("No activation in deferred mode"))
    monkeypatch.setattr(manager,"_rotate_from_native_prompts",lambda _:pytest.fail("No desktop popup"))
    assert manager.main(["ops-model-key-manager","store-stdin","deepseek"])==0
    assert calls==[{"require_reload":False},"deepseek"]
    assert json.loads(capsys.readouterr().out)=={"status":"stored","provider_account_id":"deepseek"}


def test_store_stdin_failure_never_reports_saved(manager,monkeypatch,capsys):
    monkeypatch.setattr(manager,"_harden_process",lambda:None)
    monkeypatch.setattr(manager,"_validate_runtime",lambda **_:None)
    def fail(_):raise manager.KeyManagerError("write failed")
    monkeypatch.setattr(manager,"_rotate_from_stdin",fail)
    assert manager.main(["ops-model-key-manager","store-stdin","deepseek"])==1
    assert json.loads(capsys.readouterr().out)["status"]=="failed"
