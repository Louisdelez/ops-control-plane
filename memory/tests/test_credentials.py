from __future__ import annotations

from pathlib import Path

import pytest

from ops_memory.config import APIProviderConfig
from ops_memory.credentials import provider_credential
from ops_memory.errors import BackendUnavailableError


def test_provider_credential_uses_private_systemd_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "embedding-api-token"
    path.write_text("test-token\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    config = APIProviderConfig(credential_credential="embedding-api-token")
    assert provider_credential(config, "embedding API") == "test-token"


def test_provider_credential_rejects_group_readable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "embedding-api-token"
    path.write_text("test-token", encoding="utf-8")
    path.chmod(0o660)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    config = APIProviderConfig(credential_credential="embedding-api-token")
    with pytest.raises(BackendUnavailableError, match="permissions"):
        provider_credential(config, "embedding API")


def test_provider_credential_accepts_systemd_non_root_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "embedding-api-token"
    path.write_text("test-token", encoding="utf-8")
    path.chmod(0o440)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    config = APIProviderConfig(credential_credential="embedding-api-token")
    assert provider_credential(config, "embedding API") == "test-token"
