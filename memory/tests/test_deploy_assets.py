from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[2]
def test_shell_assets_parse() -> None:
    for path in (
        ROOT / "scripts" / "install-memory-stack.sh",
        ROOT / "memory" / "deploy" / "ops-memory",
        ROOT / "memory" / "deploy" / "ops-memory-mcp",
        ROOT / "memory" / "deploy" / "ops-memory-qdrant-launcher",
    ):
        subprocess.run(["bash", "-n", str(path)], check=True)
        assert os.access(path, os.X_OK)
    preflight = ROOT / "memory" / "deploy" / "ops-memory-openbao-preflight"
    resolver = ROOT / "memory" / "deploy" / "ops-memory-openbao-resolve"
    renderer = ROOT / "memory" / "deploy" / "ops-memory-qdrant-render-config"
    provisioner = ROOT / "scripts" / "provision-memory-openbao"
    for path in (preflight, resolver, renderer, provisioner):
        subprocess.run(["python3", "-m", "py_compile", str(path)], check=True)
        assert os.access(path, os.X_OK)


def test_systemd_assets_verify() -> None:
    units = [
        ROOT / "systemd" / "ops-memory-secrets.service",
        ROOT / "systemd" / "ops-memory.service",
        ROOT / "systemd" / "ops-memory-qdrant.service",
        ROOT / "systemd" / "ops-memory-maintenance.service",
        ROOT / "systemd" / "ops-memory-maintenance.timer",
        ROOT / "systemd" / "ops-memory-health.service",
        ROOT / "systemd" / "ops-memory-health.timer",
    ]
    completed = subprocess.run(
        ["systemd-analyze", "verify", *(str(path) for path in units)],
        text=True,
        capture_output=True,
    )
    # The production executable is intentionally installed under /opt, so an
    # offline source-tree verification may emit that one benign warning.
    unexpected = "\n".join(
        line for line in completed.stderr.splitlines() if "is not executable: No such file" not in line
    )
    assert completed.returncode in {0, 1}, completed.stderr
    assert not unexpected, unexpected


def test_installer_publishes_validated_openbao_ca_copy() -> None:
    installer = (ROOT / "scripts" / "install-memory-stack.sh").read_text(encoding="utf-8")
    preflight = (ROOT / "memory" / "deploy" / "ops-memory-openbao-preflight").read_text(
        encoding="utf-8"
    )
    assert "openbao_ca_source=/etc/openbao.d/tls/ca.crt" in installer
    assert "! -f $openbao_ca_source || -L $openbao_ca_source" in installer
    assert "stat -Lc '%u'" in installer
    assert "-perm /022" in installer
    assert '"$config_dir/openbao-ca.crt"' in installer
    assert 'CA_FILE = Path("/etc/ops-memory/openbao-ca.crt")' in preflight


def test_qdrant_key_has_no_persistent_runtime_fallback() -> None:
    installer = (ROOT / "scripts" / "install-memory-stack.sh").read_text(encoding="utf-8")
    resolver = (ROOT / "memory" / "deploy" / "ops-memory-openbao-resolve").read_text(
        encoding="utf-8"
    )
    units = "\n".join(
        (ROOT / "systemd" / name).read_text(encoding="utf-8")
        for name in (
            "ops-memory.service",
            "ops-memory-qdrant.service",
            "ops-memory-maintenance.service",
        )
    )
    assert "LoadCredential=qdrant-api-key:/run/ops-memory-secrets/qdrant-api-key" in units
    assert "LoadCredentialEncrypted=qdrant-api-key" not in units
    assert "Requires=ops-memory-secrets.service" in units
    assert "/run/ops-memory-secrets/qdrant-api-key" in resolver
    assert "systemd-creds encrypt --with-key=host+tpm2 --name=qdrant-api-key" not in installer


def test_encrypted_approle_names_match_the_resolver_credentials() -> None:
    resolver = (ROOT / "memory" / "deploy" / "ops-memory-openbao-resolve").read_text(
        encoding="utf-8"
    )
    unit = (ROOT / "systemd" / "ops-memory-secrets.service").read_text(
        encoding="utf-8"
    )
    for name in ("ops-memory-openbao-role-id", "ops-memory-openbao-secret-id"):
        assert f'{name}: /etc/credstore.encrypted/{name}' not in unit
        assert f"LoadCredentialEncrypted={name}:/etc/credstore.encrypted/{name}" in unit
        assert f'"{name}"' in resolver


def test_openbao_policy_is_exact_read_and_revoke() -> None:
    policy = (
        ROOT / "config" / "openbao" / "policies" / "ops-memory-runtime.hcl"
    ).read_text(encoding="utf-8")
    assert 'path "kv-infra-shared/data/memory/qdrant"' in policy
    assert 'path "auth/token/revoke-self"' in policy
    assert policy.count('capabilities = ["read"]') == 1
    assert policy.count('capabilities = ["update"]') == 1
    assert "list" not in policy


def test_openbao_bootstrap_and_installer_publish_memory_approle_assets() -> None:
    bootstrap = (ROOT / "scripts" / "bootstrap-openbao.sh").read_text(encoding="utf-8")
    configure = (ROOT / "scripts" / "configure-openbao.sh").read_text(encoding="utf-8")
    installer = (ROOT / "scripts" / "install-memory-stack.sh").read_text(encoding="utf-8")
    assert 'auth/approle/role/ops-memory-runtime' in bootstrap
    assert 'token_policies="ops-memory-runtime"' in bootstrap
    assert "token_ttl=60s" in bootstrap
    assert "token_num_uses=2" in bootstrap
    assert "token_no_default_policy=true" in bootstrap
    assert "provision-memory-openbao" in configure
    assert "provision-memory-openbao" in installer


def test_provisioner_forces_private_umask_and_can_clean_unwritable_staging() -> None:
    provisioner = (ROOT / "scripts" / "provision-memory-openbao").read_text(
        encoding="utf-8"
    )
    assert "os.umask(0o077)" in provisioner
    assert '_validate_root_regular(output, "new encrypted AppRole credential", private=False)' in provisioner
    assert '_validate_root_regular(child, "transaction credential", private=False)' in provisioner
