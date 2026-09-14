from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "deploy" / "ops-memory-openbao-resolve"


def _load():
    loader = importlib.machinery.SourceFileLoader("ops_memory_openbao_resolve", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_resolver_revokes_two_use_token_before_publishing(monkeypatch) -> None:
    module = _load()
    events: list[str] = []

    def request(method, path, **kwargs):
        if path == "/v1/auth/approle/login":
            events.append("login")
            return {"auth": {"client_token": "token-memory-runtime-12345"}}
        if path == module.KV_PATH:
            events.append("read")
            return {"data": {"data": {"api_key": "A" * 48}}}
        if path == "/v1/auth/token/revoke-self":
            events.append("revoke")
            return None
        raise AssertionError(path)

    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/test")
    monkeypatch.setattr(module, "_read_private", lambda path, maximum: "identifier-memory-12345")
    monkeypatch.setattr(module, "_request", request)
    monkeypatch.setattr(module, "_remove_output", lambda output: events.append("remove"))
    monkeypatch.setattr(
        module, "_write_key", lambda key, output: events.append("publish:" + key[:1])
    )

    module.resolve()

    assert events == ["remove", "login", "read", "revoke", "publish:A"]


def test_resolver_never_publishes_when_revoke_fails(monkeypatch) -> None:
    module = _load()
    events: list[str] = []

    def request(method, path, **kwargs):
        if path == "/v1/auth/approle/login":
            return {"auth": {"client_token": "token-memory-runtime-12345"}}
        if path == module.KV_PATH:
            return {"data": {"data": {"api_key": "B" * 48}}}
        if path == "/v1/auth/token/revoke-self":
            raise module.ResolutionError("revocation failed")
        raise AssertionError(path)

    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", "/run/credentials/test")
    monkeypatch.setattr(module, "_read_private", lambda path, maximum: "identifier-memory-12345")
    monkeypatch.setattr(module, "_request", request)
    monkeypatch.setattr(module, "_remove_output", lambda output: events.append("remove"))
    monkeypatch.setattr(module, "_write_key", lambda key, output: events.append("publish"))

    with pytest.raises(module.ResolutionError, match="revocation"):
        module.resolve()

    assert events == ["remove", "remove"]


def test_tmpfs_output_is_private_and_atomic(tmp_path: Path, monkeypatch) -> None:
    module = _load()
    runtime = tmp_path / "ops-memory-secrets"
    runtime.mkdir(mode=0o700)
    output = runtime / "qdrant-api-key"
    monkeypatch.setattr(module, "EXPECTED_OUTPUT", output)
    monkeypatch.setattr(module, "EXPECTED_OUTPUT_OWNER", os.getuid())

    module._write_key("C" * 48, output)

    assert output.read_text(encoding="ascii") == "C" * 48 + "\n"
    assert output.stat().st_mode & 0o777 == 0o400
    assert not list(runtime.glob(".qdrant-api-key.*"))


def test_qdrant_document_schema_is_exactly_bounded() -> None:
    module = _load()
    assert module._extract_key({"data": {"data": {"api_key": "D" * 48}}}) == "D" * 48
    with pytest.raises(module.ResolutionError):
        module._extract_key({"data": {"data": {"api_key": "short"}}})
