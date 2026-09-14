from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "orchestrator" / "deploy" / "ops-model-credentials-reload"
SUDOERS = ROOT / "orchestrator" / "deploy" / "ops-model-credentials-reload.sudoers"


def _load():
    loader = importlib.machinery.SourceFileLoader(
        "ops_model_credentials_reload", str(SCRIPT)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_reload_helper_and_sudoers_are_fixed_and_non_secret() -> None:
    module = _load()
    source = SCRIPT.read_text(encoding="utf-8")
    sudoers = SUDOERS.read_text(encoding="utf-8")

    assert source.startswith("#!/usr/bin/python3 -I\n")
    assert "sys.argv[1:]" in source
    assert "OpenBao" in source
    assert "api_key" not in source
    assert "provider" in source
    assert "https://" not in source
    assert "curl" in source
    assert module.START_ORDER[0] == "ops-orchestrator-secrets.service"
    assert module.START_ORDER[-1] == "ops-orchestrator-provider-finance.timer"
    assert sudoers.splitlines()[-1] == (
        "ops-user ALL=(root) NOPASSWD: "
        "/usr/local/libexec/ops-model-credentials-reload"
    )
    assert "*" not in sudoers


def test_reload_restores_only_previously_active_units(monkeypatch) -> None:
    module = _load()
    active_units = {
        "ops-orchestrator-secrets.service",
        "ops-orchestrator-provider-finance-daemon.service",
        "ops-orchestrator.service",
        "ops-orchestrator-hermes-facade.service",
    }
    state = {unit: unit in active_units for unit in module.STOP_ORDER}
    commands: list[tuple[str, ...]] = []
    descriptor = os.open("/dev/null", os.O_RDONLY)

    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.os, "umask", lambda _mode: 0o022)
    monkeypatch.setattr(module.os, "environ", {})
    monkeypatch.setattr(module.sys, "argv", [str(SCRIPT)])
    monkeypatch.setattr(module, "_acquire_locks", lambda: [descriptor])
    monkeypatch.setattr(module, "_active", lambda unit: state.get(unit, False))
    monkeypatch.setattr(module, "_enabled", lambda _unit: False)
    monkeypatch.setattr(
        module,
        "_credential_published",
        lambda path: path.name == "qwen-api-key",
    )

    def run(argv, **_kwargs):
        commands.append(tuple(argv))
        if argv[0] == module.SYSTEMCTL and argv[1] in {"start", "stop"}:
            state[argv[2]] = argv[1] == "start"
        elif argv[0] == module.SYSTEMCTL and argv[1:3] == ["enable", "--now"]:
            state[argv[3]] = True
        elif argv[0] == module.SYSTEMCTL and argv[1:3] == ["disable", "--now"]:
            for unit in argv[3:]:
                state[unit] = False

    monkeypatch.setattr(module, "_run", run)
    module.reload_credentials()

    stopped = [command[2] for command in commands if command[1] == "stop"]
    started = [command[2] for command in commands if command[1] == "start"]
    assert stopped == [unit for unit in module.STOP_ORDER if unit in active_units]
    assert started == [unit for unit in module.START_ORDER if unit in active_units]
    assert "hermes-gateway.service" not in started
    assert any("/v1/health" in part for command in commands for part in command)
    assert any("/healthz" in part for command in commands for part in command)
    assert not any("https://" in part for command in commands for part in command)


def test_optional_consumers_follow_only_published_credential_metadata(
    monkeypatch,
) -> None:
    module = _load()
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        module,
        "_credential_published",
        lambda path: path.name in {"deepseek-api-key"},
    )
    monkeypatch.setattr(
        module, "_run", lambda argv, **_kwargs: commands.append(tuple(argv))
    )

    expected = module._configure_optional_consumers()

    assert expected == {
        "ops-orchestrator-hermes-facade.service": True,
        "hermes-gateway.service": True,
        "ops-orchestrator-provider-finance.timer": True,
    }
    assert (
        module.SYSTEMCTL,
        "enable",
        "--now",
        "ops-orchestrator-hermes-facade.service",
    ) in commands
    assert (module.SYSTEMCTL, "enable", "--now", "hermes-gateway.service") in commands
    assert (
        module.SYSTEMCTL,
        "enable",
        "--now",
        "ops-orchestrator-provider-finance.timer",
    ) in commands
    assert not any("https://" in part for command in commands for part in command)


def test_optional_consumers_remain_dormant_without_published_keys(monkeypatch) -> None:
    module = _load()
    commands: list[tuple[str, ...]] = []
    monkeypatch.setattr(module, "_credential_published", lambda _path: False)
    monkeypatch.setattr(
        module, "_run", lambda argv, **_kwargs: commands.append(tuple(argv))
    )

    expected = module._configure_optional_consumers()

    assert not any(expected.values())
    assert (
        module.SYSTEMCTL,
        "disable",
        "--now",
        "hermes-gateway.service",
        "ops-orchestrator-hermes-facade.service",
    ) in commands
    assert (
        module.SYSTEMCTL,
        "disable",
        "--now",
        "ops-orchestrator-provider-finance.timer",
    ) in commands


def test_reload_rejects_arguments_before_any_systemd_action(monkeypatch) -> None:
    module = _load()
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.sys, "argv", [str(SCRIPT), "unexpected"])
    monkeypatch.setattr(
        module,
        "_acquire_locks",
        lambda: pytest.fail("locks must not be acquired for invalid arguments"),
    )

    with pytest.raises(module.ReloadError, match="no arguments"):
        module.reload_credentials()


def test_reload_lock_order_matches_deployment_then_install_then_reload() -> None:
    module = _load()
    assert module.LOCK_PATHS == (
        Path("/run/lock/ops-control-plane-deployment.lock"),
        Path("/run/lock/ops-orchestrator-install.lock"),
        Path("/run/lock/ops-model-credentials-reload.lock"),
    )


def test_sudoers_source_parses_when_visudo_is_available() -> None:
    visudo = Path("/usr/sbin/visudo")
    if not visudo.exists():
        pytest.skip("visudo is unavailable")
    completed = subprocess.run(
        [str(visudo), "-cf", str(SUDOERS)],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
