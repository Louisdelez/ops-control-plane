from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "config" / "hermes"


def test_offline_validator_covers_all_profiles_skills_and_mcp_surfaces() -> None:
    environment = dict(os.environ)
    environment["TMPDIR"] = "/var/tmp"
    completed = subprocess.run(
        [str(ROOT / "scripts" / "validate-hermes-config.py")],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "8 profiles" in completed.stdout
    assert "5 skills" in completed.stdout
    assert "31+6+7" in completed.stdout
    assert "9 orchestrator available" in completed.stdout


@pytest.mark.parametrize(
    "wrapper",
    [
        "ops-broker-mcp-profile",
        "ops-orchestrator-mcp-profile",
        "ops-memory-mcp-profile",
    ],
)
def test_profile_wrappers_reject_unlisted_identity_before_execution(wrapper: str) -> None:
    completed = subprocess.run(
        [str(ROOT / "scripts" / wrapper), "../../invented"],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 77
    assert "not allowlisted" in completed.stderr


def test_gateway_and_installer_remain_explicitly_dormant() -> None:
    unit = (ROOT / "systemd" / "hermes-gateway.service").read_text(encoding="utf-8")
    installer = (ROOT / "scripts" / "install-hermes.sh").read_text(encoding="utf-8")
    assert "ConditionPathExists=/etc/hermes/hermes-gateway.enabled" in unit
    assert (
        "Requires=ops-orchestrator.service ops-orchestrator-hermes-facade.service "
        "ops-memory.service"
    ) in unit
    assert "Conflicts=ollama.service" in unit
    assert "IPAddressDeny=any" in unit
    assert "IPAddressAllow=localhost" in unit
    assert "systemctl enable --now" not in installer
    assert "touch $activation_marker" not in installer
    assert "production_model" not in installer


def test_installer_forces_the_committed_hash_locked_hermes_environment() -> None:
    installer = (ROOT / "scripts" / "install-hermes.sh").read_text(
        encoding="utf-8"
    )
    assert "readonly uv_version=0.12.0" in installer
    assert 'scripts/vendor/uv-0.12.0-installer.sh' in installer
    assert 'UV_UNMANAGED_INSTALL="$hermes_home/bin"' in installer
    assert 'reported=$(runuser -u hermesd -- "$managed_uv" --version)' in installer
    import hashlib
    vendor = (ROOT / "scripts/vendor/uv-0.12.0-installer.sh").read_bytes()
    assert hashlib.sha256(vendor).hexdigest() == "b67e385074fddc9b99cd152b838fd91046d9fbc261b2c45f448a983ad23b8764"
    assert "Hermes source has no safe committed uv.lock." in installer
    assert (
        '"$managed_uv" sync --project "$install_dir" --extra all --locked'
        in installer
    )
    assert "uv_sync_mode=(--check)" in installer
    assert "Hermes hash-locked dependency synchronization failed." in installer


def test_installer_uses_only_the_authenticated_vendored_hermes_source() -> None:
    installer = (ROOT / "scripts" / "install-hermes.sh").read_text(
        encoding="utf-8"
    )
    extractor = (ROOT / "scripts" / "extract-hermes-source.py").read_text(
        encoding="utf-8"
    )
    assert "--archive" in installer
    assert "--source" not in installer
    assert "git fetch" not in installer
    assert "git clone" not in installer
    assert "--stage venv" in installer
    assert "--stage path" not in installer
    assert "--stage python-deps" not in installer
    assert "UV_NO_MODIFY_PATH=1" in installer
    assert (
        "readonly hermes_execution_path=$hermes_home/bin:"
        "$hermes_home/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"
        in installer
    )
    assert installer.count('PATH="$hermes_execution_path"') >= 5
    assert '"$hermes_home/.bashrc"' not in installer
    assert '"$hermes_home/.bash_profile"' not in installer
    assert '"$hermes_home/.profile"' not in installer
    assert 'TMPDIR="$upstream_tmp"' in installer
    assert ".XXXXXXXX" not in installer
    assert "/var/lib/.hermes-agent.extract" not in installer
    assert installer.count('--verify-installed "$install_dir"') == 2
    assert "PYTHONDONTWRITEBYTECODE=1" in installer
    assert "tarfile.open" in extractor
    assert "extractall" not in extractor
    assert "os.O_NOFOLLOW" in extractor
    assert "os.O_EXCL" in extractor
    assert "os.fsync(output_fd)" in extractor
    assert 'sync -f "$extraction_root"' in installer
    assert 'sync -f "$hermes_home"' in installer
    assert 'sync -f "$install_dir"' in installer
    assert "--verify-installed" in extractor
    assert "CANONICAL_TREE_SHA256" in extractor
    assert "DIRECTORY_TREE_SHA256" in extractor


def test_preflight_readiness_retry_accepts_delay_and_stays_bounded() -> None:
    preflight = ROOT / "scripts" / "hermes-gateway-preflight"
    script = f"""
source {shlex.quote(str(preflight))}
calls=0
ready_after_three() {{
  calls=$((calls + 1))
  (( calls >= 3 ))
}}
wait_for_ready 5 0.01 ready_after_three
[[ $calls == 3 ]]

calls=0
never_ready() {{
  calls=$((calls + 1))
  return 1
}}
if wait_for_ready 4 0 never_ready; then
  exit 90
fi
[[ $calls == 4 ]]
"""
    completed = subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    source = preflight.read_text(encoding="utf-8")
    assert "readonly readiness_attempts=10" in source
    assert "readonly readiness_delay_seconds=1" in source
    assert "--max-time 1" in source


def test_installer_deploys_exact_root_owned_hermes_openbao_policies() -> None:
    installer = (ROOT / "scripts" / "install-hermes.sh").read_text(encoding="utf-8")
    policy_dir = "/usr/local/share/ops-control-plane/openbao/policies"
    policy_names = (
        "hermes-runtime.hcl",
        "hermes-coordinator.hcl",
        "deepseek-client.hcl",
    )

    assert f"readonly openbao_policy_dir={policy_dir}" in installer
    assert "required_commands=(" in installer and " cmp " in installer
    assert 'install -o root -g root -m 0644 "$policy_source" "$policy_target"' in installer
    assert "stat -c '%U:%G:%a'" in installer
    assert "root:root:644" in installer
    assert 'cmp -s -- "$policy_source" "$policy_target"' in installer
    assert 'reject_symlink "$policy_install_dir"' in installer
    assert 'reject_symlink "$policy_target"' in installer
    for name in policy_names:
        source = f'$repository_dir/config/openbao/policies/{name}'
        target = f'$openbao_policy_dir/{name}'
        assert source in installer
        assert target in installer

    policy_install = installer.index(
        'install -o root -g root -m 0644 "$policy_source" "$policy_target"'
    )
    provisioner_install = installer.index(
        '"$repository_dir/scripts/provision-hermes-openbao" '
        "/usr/local/sbin/provision-hermes-openbao"
    )
    assert policy_install < provisioner_install


def test_all_profile_configs_use_only_local_untrusted_stdio() -> None:
    paths = [HERMES / "default" / "config.yaml", *sorted((HERMES / "profiles").glob("*/config.yaml"))]
    for path in paths:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert set(config["mcp_servers"]) == {"ops-broker", "ops-orchestrator", "ops-memory"}
        for server in config["mcp_servers"].values():
            assert server["command"].startswith("/usr/local/libexec/")
            assert "url" not in server and "headers" not in server
            assert server["trust"] == "untrusted"
            assert server["sampling"] == {"enabled": False}
            assert server["elicitation"] == {"enabled": False}
            assert server["tools"]["resources"] is False
            assert server["tools"]["prompts"] is False
            assert not any(
                tool.startswith("approve") or "secret" in tool
                for tool in server["tools"]["include"]
            )


def test_all_profiles_use_the_pinned_custom_provider_non_thinking_wire_contract() -> None:
    paths = [
        HERMES / "default" / "config.yaml",
        HERMES / "profile-config.yaml.in",
        *sorted((HERMES / "profiles").glob("*/config.yaml")),
    ]
    for path in paths:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert config["model"]["provider"] == "custom:ops-orchestrator-hermes"
        # Hermes 0.21's CustomProfile serializes this as the top-level
        # `reasoning_effort` field.  The facade accepts that exact bounded
        # field, removes it, and owns the Qwen `enable_thinking=false` policy.
        assert config["agent"]["reasoning_effort"] == "none"


def test_hermes_sudoers_policies_parse() -> None:
    visudo = shutil.which("visudo")
    if visudo is None:
        pytest.skip("visudo is unavailable")
    for name in ("ops-broker-mcp-profile.sudoers", "ops-local-mcp-profile.sudoers"):
        completed = subprocess.run(
            [visudo, "-cf", str(HERMES / name)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
