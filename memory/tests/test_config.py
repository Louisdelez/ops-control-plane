from __future__ import annotations

from pathlib import Path

import pytest

from ops_memory.config import MemoryConfig
from ops_memory.errors import ConfigurationError


def test_production_example_parses() -> None:
    config = MemoryConfig.from_toml(Path(__file__).parents[1] / "config" / "memory.toml")
    assert config.qdrant.url == "http://127.0.0.1:6333"
    assert config.qdrant.api_key_credential == "qdrant-api-key"
    assert config.qdrant.api_key_file is None
    assert config.result_limit == 5
    assert config.embedding.api.enabled is False
    assert config.reranker.api.daily_call_budget == 0
    assert config.actors["codex-supervised"].unix_users == ("ops-user",)
    assert config.actors["claude-supervised"].unix_users == ("ops-user",)
    assert config.actors["claude-supervised"].capabilities == config.actors["codex-supervised"].capabilities
    assert config.actors["network-shared"].unix_users == ("infra-network",)
    assert config.actors["network-shared"].roles == ("network-shared",)
    assert config.actors["network-shared"].projects == ("network-shared", "infra-shared")
    assert "minecraft" not in config.actors["network-shared"].projects


def test_non_loopback_qdrant_is_rejected(tmp_path: Path) -> None:
    source = (Path(__file__).parents[1] / "config" / "memory.toml").read_text(encoding="utf-8")
    path = tmp_path / "bad.toml"
    path.write_text(source.replace("http://127.0.0.1:6333", "https://qdrant.example.test"), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="loopback"):
        MemoryConfig.from_toml(path)


def test_symlinked_config_is_rejected(tmp_path: Path) -> None:
    source = Path(__file__).parents[1] / "config" / "memory.toml"
    link = tmp_path / "memory.toml"
    link.symlink_to(source)
    with pytest.raises(ConfigurationError, match="symlink"):
        MemoryConfig.from_toml(link)


def test_string_boolean_is_rejected(tmp_path: Path) -> None:
    source = (Path(__file__).parents[1] / "config" / "memory.toml").read_text(encoding="utf-8")
    path = tmp_path / "bad.toml"
    path.write_text(
        source.replace("allow_degraded_reads = true", 'allow_degraded_reads = "false"'),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="must be a boolean"):
        MemoryConfig.from_toml(path)


def test_unsafe_timeouts_are_rejected(tmp_path: Path) -> None:
    source = (Path(__file__).parents[1] / "config" / "memory.toml").read_text(encoding="utf-8")
    path = tmp_path / "bad.toml"
    path.write_text(source.replace("timeout_seconds = 3.0", "timeout_seconds = 0.0", 1), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="qdrant.timeout_seconds"):
        MemoryConfig.from_toml(path)


def test_group_writable_config_is_rejected(tmp_path: Path) -> None:
    source = (Path(__file__).parents[1] / "config" / "memory.toml").read_text(encoding="utf-8")
    path = tmp_path / "bad.toml"
    path.write_text(source, encoding="utf-8")
    path.chmod(0o664)
    with pytest.raises(ConfigurationError, match="permissions are unsafe"):
        MemoryConfig.from_toml(path)
