from pathlib import Path


REPOSITORY = Path(__file__).parents[2]


def test_runtime_socket_directory_uses_api_group_but_state_stays_private():
    unit = (REPOSITORY / "systemd" / "ops-orchestrator.service").read_text(encoding="utf-8")
    assert "Group=opsorchestrator-api\n" in unit
    assert "SupplementaryGroups=opsorchestrator opsfinance\n" in unit
    assert "RuntimeDirectoryMode=0750\n" in unit
    assert "StateDirectoryMode=0700\n" in unit
    assert "SupplementaryGroups=opsorchestrator-api" not in unit
    assert "Description=Fail-closed API-first Ops model orchestrator\n" in unit
    assert "ops-orchestrator-provider-finance-daemon.service" in unit
    assert "Requires=ops-orchestrator-secrets.service ops-orchestrator-provider-finance-daemon.service\n" in unit
    assert "ollama.service" not in unit

    finance = (
        REPOSITORY
        / "systemd"
        / "ops-orchestrator-provider-finance-daemon.service"
    ).read_text(encoding="utf-8")
    assert "User=opsfinance\n" in finance
    assert "Group=opsfinance\n" in finance
    assert "SupplementaryGroups=opsorchestrator\n" in finance
    assert "StateDirectory=" not in finance
    assert "/var/lib/ops-orchestrator" not in finance

    facade = (
        REPOSITORY / "systemd" / "ops-orchestrator-hermes-facade.service"
    ).read_text(encoding="utf-8")
    assert "InaccessiblePaths=/run/ops-orchestrator-finance\n" in facade

    tmpfiles = (
        REPOSITORY / "orchestrator" / "deploy" / "ops-orchestrator.tmpfiles"
    ).read_text(encoding="utf-8")
    assert "d /run/ops-orchestrator 0750 opsorchestrator opsorchestrator-api -" in tmpfiles
    assert "d /var/lib/ops-orchestrator 0700 opsorchestrator opsorchestrator -" in tmpfiles


def test_metrics_publisher_uses_socket_and_cannot_access_private_state():
    unit = (REPOSITORY / "systemd" / "ops-orchestrator-metrics.service").read_text(
        encoding="utf-8"
    )
    assert "Group=opsorchestrator-api\n" in unit
    assert "--socket /run/ops-orchestrator/api.sock export-metrics" in unit
    assert "ConditionPathExists=/run/ops-orchestrator/api.sock\n" in unit
    assert "CapabilityBoundingSet=\n" in unit
    assert "/var/lib/ops-orchestrator" not in unit
    assert "ReadWritePaths=/var/lib/node-exporter/textfile\n" in unit


def test_api_first_config_replacement_is_explicit_backed_up_and_prevalidated():
    installer = (REPOSITORY / "scripts" / "install-orchestrator.sh").read_text(
        encoding="utf-8"
    )
    assert "--replace-api-config" in installer
    assert 'mktemp -d "$config_dir/.pre-api-first.XXXXXXXX"' in installer
    assert '"$backup_dir/config.json"' in installer
    assert '"$backup_dir/orchestrator.env"' in installer
    assert installer.index('PYTHONPATH="$source_dir/src" python3') < installer.index(
        "systemctl stop ops-orchestrator-provider-finance.timer"
    )
    assert "catalog/model-catalog.v2.json" in installer
    assert "catalog/sources/catalogue_modeles_IA_API_2026.txt" in installer
    assert "/usr/local/share/ops-control-plane/catalog" in installer
    assert installer.index(
        '"$model_catalogue_dir/model-catalog.v2.json"'
    ) < installer.index('PYTHONPATH="$source_dir/src" python3')
    assert "scripts/ops-model-key-manager" in installer
    assert "model_key_manager=/usr/local/libexec/ops-model-key-manager" in installer
    assert "model_reload_helper=/usr/local/libexec/ops-model-credentials-reload" in installer
    assert "model_reload_sudoers=/etc/sudoers.d/ops-model-credentials-reload" in installer
    assert (
        'install -o root -g root -m 0755 "$repository_dir/scripts/ops-model-key-manager"'
        in installer
    )
    assert "ops-model-credentials-reload.sudoers" in installer
    assert 'visudo -cf "$model_reload_sudoers"' in installer
    assert "--source" in installer
    compatibility_check = installer.index(
        "Existing configuration is incompatible; rerun with --replace-api-config."
    )
    first_install = installer.index(
        "install -d -o root -g root -m 0755 /run/lock"
    )
    assert compatibility_check < first_install
    assert "Dedicated provider-finance identity was not created." in installer
    assert "Provider-finance and orchestrator UIDs must be distinct." in installer
    assert "Provider-finance socket group must contain only opsorchestrator." in installer
    assert "Provider-finance group is unexpectedly primary for %s." in installer


def test_python_runtime_closures_are_exact_and_installers_verify_them():
    pairs = (
        (
            REPOSITORY / "orchestrator" / "requirements-runtime.lock",
            REPOSITORY / "scripts" / "install-orchestrator.sh",
            29,
        ),
        (
            REPOSITORY / "broker" / "requirements-mcp.txt",
            REPOSITORY / "scripts" / "install-broker.sh",
            32,
        ),
    )
    for lock_path, installer_path, expected_count in pairs:
        requirements = [
            line.strip()
            for line in lock_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        assert len(requirements) == expected_count
        assert len(requirements) == len(set(requirements))
        assert all(
            item.count("==") == 1
            and item.partition("==")[0]
            and item.partition("==")[2]
            for item in requirements
        )
        installer = installer_path.read_text(encoding="utf-8")
        assert str(lock_path.relative_to(REPOSITORY)) in installer or lock_path.name in installer
        assert "--only-binary=:all:" in installer
        assert "--no-build-isolation" in installer
        assert 'from importlib.metadata import PackageNotFoundError, version' in installer
        assert '"$venv_dir/bin/python" -m pip check' in installer


def test_supervised_clients_have_distinct_fixed_mcp_identities():
    deploy = REPOSITORY / "orchestrator" / "deploy"
    codex = (deploy / "ops-orchestrator-mcp-codex").read_text(encoding="utf-8")
    claude = (deploy / "ops-orchestrator-mcp-claude").read_text(encoding="utf-8")
    assert "OPS_ORCHESTRATOR_MCP_ACTOR_ID=codex-supervised" in codex
    assert "OPS_ORCHESTRATOR_MCP_ACTOR_ID=claude-supervised" in claude


def test_openbao_keys_are_resolved_root_only_then_loaded_as_credentials():
    installer = (REPOSITORY / "scripts" / "install-orchestrator.sh").read_text(
        encoding="utf-8"
    )
    unit = (REPOSITORY / "systemd" / "ops-orchestrator.service").read_text(
        encoding="utf-8"
    )
    secret_unit = (
        REPOSITORY / "systemd" / "ops-orchestrator-secrets.service"
    ).read_text(encoding="utf-8")

    assert "LoadCredential=providers:/run/ops-orchestrator-secrets" not in unit
    assert unit.count("LoadCredential=") == 12
    assert "QWEN_API_KEY_FILE=%d/qwen-api-key" in unit
    assert "DEEPSEEK_API_KEY_FILE=%d/deepseek-api-key" in unit
    assert "MOONSHOT_API_KEY_FILE=%d/moonshot-api-key" in unit
    assert unit.count("SetCredential=") == 12
    assert "SetCredential=qwen-api-key:\\n\n" in unit
    assert "SetCredential=deepseek-api-key:\\n\n" in unit
    assert "STEPFUN_API_KEY_FILE=" not in unit
    assert "RuntimeDirectoryMode=0700" in secret_unit
    assert "Before=ops-orchestrator-provider-finance-daemon.service" in secret_unit
    assert "ops-orchestrator-openbao-resolve" in installer
    assert "ops-orchestrator-secrets.service" in installer
    assert "ops-orchestrator-runtime.hcl" in installer
    assert "provision-orchestrator-openbao" in installer
    assert "openbao_ca_source=/etc/openbao.d/tls/ca.crt" in installer
    assert "systemctl enable --now ops-orchestrator-secrets.service" not in installer


def test_provider_finance_timer_is_installed_and_only_uses_the_unix_api():
    installer = (REPOSITORY / "scripts" / "install-orchestrator.sh").read_text(
        encoding="utf-8"
    )
    unit = (
        REPOSITORY / "systemd" / "ops-orchestrator-provider-finance.service"
    ).read_text(encoding="utf-8")

    assert "ops-orchestrator-provider-finance.service" in installer
    assert "ops-orchestrator-provider-finance-daemon.service" in installer
    assert "ops-orchestrator-provider-finance.timer" in installer
    assert "systemctl enable --now ops-orchestrator-provider-finance.timer" in installer
    assert "refresh-finance --account all --project-id infra-shared" in unit
    assert "RestrictAddressFamilies=AF_UNIX\n" in unit
    assert "LoadCredential=" not in unit


def test_hermes_facade_is_loopback_only_budgeted_and_installed_with_shared_secrets():
    installer = (REPOSITORY / "scripts" / "install-orchestrator.sh").read_text(
        encoding="utf-8"
    )
    facade = (
        REPOSITORY / "systemd" / "ops-orchestrator-hermes-facade.service"
    ).read_text(encoding="utf-8")
    secret_unit = (
        REPOSITORY / "systemd" / "ops-orchestrator-secrets.service"
    ).read_text(encoding="utf-8")
    pyproject = (REPOSITORY / "orchestrator" / "pyproject.toml").read_text(
        encoding="utf-8"
    )

    assert (
        'ops-orchestrator-hermes-facade = "ops_orchestrator.hermes_facade:main"'
        in pyproject
    )
    assert "Requires=ops-orchestrator-secrets.service ops-orchestrator.service\n" in facade
    assert "Before=hermes-gateway.service\n" in facade
    assert (
        "--providers qwen-utility-api,alibaba-deepseek-ops-api,deepseek-ops-api"
        in facade
    )
    assert "--host 127.0.0.1 --port 8643" in facade
    assert "--token-file %d/hermes-facade-token" in facade
    assert "LoadCredential=providers:/run/ops-orchestrator-secrets" not in facade
    assert facade.count("LoadCredential=") == 3
    assert "SetCredential=qwen-api-key:\\n\n" in facade
    assert "SetCredential=deepseek-api-key:\\n\n" in facade
    assert "Environment=QWEN_API_KEY_FILE=%d/qwen-api-key\n" in facade
    assert "Environment=DEEPSEEK_API_KEY_FILE=%d/deepseek-api-key\n" in facade
    assert "IPAddressAllow=" not in facade
    assert "ReadWritePaths=/var/lib/ops-orchestrator\n" in facade
    assert "ops-orchestrator-hermes-facade.service" in installer
    assert "Before=ops-orchestrator-provider-finance-daemon.service ops-orchestrator.service ops-orchestrator-hermes-facade.service\n" in secret_unit
    assert "PartOf=openbao-unseal.service\n" in secret_unit
    assert "PartOf=ops-orchestrator.service" not in secret_unit


def test_openbao_bootstrap_uses_exact_runtime_token_counts():
    bootstrap = (REPOSITORY / "scripts" / "bootstrap-openbao.sh").read_text(
        encoding="utf-8"
    )

    orchestrator_marker = 'bao write "auth/approle/role/ops-orchestrator-runtime"'
    orchestrator_role = bootstrap[bootstrap.index(orchestrator_marker) :]
    orchestrator_role = orchestrator_role[: orchestrator_role.index("\n\n")]
    assert 'token_policies="ops-orchestrator-runtime"' in orchestrator_role
    assert "token_num_uses=23" in orchestrator_role
    assert "token_no_default_policy=true" in orchestrator_role

    hermes_marker = 'bao write "auth/approle/role/hermes-runtime"'
    hermes_role = bootstrap[bootstrap.index(hermes_marker) :]
    hermes_role = hermes_role[: hermes_role.index("\n\n")]
    assert "token_num_uses=2" in hermes_role


def test_hermes_policy_install_rejects_symlinks_and_precedes_provisioning():
    installer = (REPOSITORY / "scripts" / "install-hermes.sh").read_text(
        encoding="utf-8"
    )
    policy_dir = "/usr/local/share/ops-control-plane/openbao/policies"

    assert f"readonly openbao_policy_dir={policy_dir}" in installer
    assert '"$repository_dir/config/openbao"' in installer
    assert '"$repository_dir/config/openbao/policies"' in installer
    for name in (
        "hermes-runtime.hcl",
        "hermes-coordinator.hcl",
        "deepseek-client.hcl",
    ):
        assert f'"$repository_dir/config/openbao/policies/{name}"' in installer
        assert f'"$openbao_policy_dir/{name}"' in installer
    assert 'reject_symlink "$managed_path"' in installer
    assert 'reject_symlink "$policy_install_dir"' in installer
    assert 'reject_symlink "$policy_target"' in installer
    assert 'install -d -o root -g root -m 0755 "$policy_install_dir"' in installer
    assert 'install -o root -g root -m 0644 "$policy_source" "$policy_target"' in installer
    assert 'cmp -s -- "$policy_source" "$policy_target"' in installer
    assert installer.index(
        'install -o root -g root -m 0644 "$policy_source" "$policy_target"'
    ) < installer.index(
        'install -o root -g root -m 0755 '
        '"$repository_dir/scripts/provision-hermes-openbao"'
    )
