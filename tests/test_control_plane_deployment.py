from __future__ import annotations

import ast
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import jsonschema
import pytest

from ops_broker.errors import NotFound
from ops_broker.policy import RBACPolicy
from ops_broker.runbooks import ExecutablePolicy, RunbookRegistry


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "deploy" / "control-plane"
MANIFEST_PATH = PACKAGE / "release-manifest.v1.json"
WORKER_PATH = PACKAGE / "bin" / "control-plane-deployment-worker"
DISPATCHER_PATH = ROOT / "broker" / "deploy" / "helpers" / "control-plane-deployment"


def worker_namespace() -> dict[str, object]:
    return runpy.run_path(str(WORKER_PATH))


def manifest() -> dict[str, object]:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def test_historical_release_is_api_first_and_rejects_updated_closures() -> None:
    namespace = worker_namespace()
    document = manifest()
    namespace["validate_manifest"](document)
    # The sealed historical closure must reject today's security updates.
    with pytest.raises(namespace["DeploymentError"], match="runtime closure differs"):
        namespace["validate_component_declarations"](ROOT, document)
    runpy.run_path(str(ROOT / "native_ops/release.py"))["verify"](ROOT)
    schema = json.loads(
        (PACKAGE / "release-manifest.v1.schema.json").read_text(encoding="utf-8")
    )
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(document, schema)
    components = document["components"]
    assert components["orchestrator"] == {"version": "0.2.0", "mode": "api-first"}
    assert "ollama" not in json.dumps(components, sort_keys=True).lower()
    assert document["cutover"] == {
        "local_inference": {
            "mode": "disabled",
            "units": ["ollama.service"],
            "preserve_historical_artifacts": True,
        }
    }
    runtime_config = json.loads(
        (ROOT / "orchestrator" / "config" / "orchestrator.json").read_text(
            encoding="utf-8"
        )
    )
    assert runtime_config["catalogue_runtime"]["enabled"] is True
    assert runtime_config["catalogue_auxiliary_deployments"] == [
        "qwen-coder-api",
        "embedding-api-backup",
        "reranker-api-backup",
    ]


def test_manifest_catalogue_inventory_is_human_readable_and_runtime_derived() -> None:
    namespace = worker_namespace()
    components = manifest()["components"]
    catalogue = components["model_catalogue"]
    integrations = components["provider_integrations"]
    assert catalogue == {
        "schema_version": 2,
        "revision": "catalogue-api-ia-2026-09-05.3",
        "sha256": hashlib.sha256(
            (ROOT / "catalog" / "model-catalog.v2.json").read_bytes()
        ).hexdigest(),
        "cards": 58,
        "provider_accounts": 21,
        "runtime": {
            "static_cards": 5,
            "static_deployments": 7,
            "dynamic_cards": 9,
            "invocable_cards": 14,
            "blocked_cards": 44,
            "provider_deployments": 19,
            "provider_inventory_sha256": "0679d0cda70e8ed0f3705cba7c4ca2d9ccce8ce016b0bef2aec4c95a89a45f7b",
            "blocked_reasons": {
                "token_prices_not_officially_verified": 28,
                "quarantined": 11,
                "exact_model_id_unverified": 4,
                "minimax_m3_chat_api_contract_unverified": 1,
            },
        },
    }
    assert integrations == {
        "schema_version": 1,
        "revision": "provider-integrations-2026-09-05.1",
        "sha256": hashlib.sha256(
            (ROOT / "catalog" / "provider-integrations.v1.json").read_bytes()
        ).hexdigest(),
        "provider_accounts": 21,
    }
    assert namespace["catalogue_runtime_inventory"](ROOT) == catalogue["runtime"]


def test_component_validation_rejects_logically_valid_runtime_inventory_drift() -> None:
    namespace = worker_namespace()
    document = manifest()
    runtime = document["components"]["model_catalogue"]["runtime"]
    runtime["static_cards"] = 4
    runtime["invocable_cards"] = 13
    runtime["blocked_cards"] = 45
    runtime["blocked_reasons"]["token_prices_not_officially_verified"] = 29
    namespace["validate_manifest"](document)
    with pytest.raises(namespace["DeploymentError"], match="inventory differs"):
        namespace["validate_component_declarations"](ROOT, document)


def test_legacy_footprint_and_current_native_release() -> None:
    namespace = worker_namespace()
    document = manifest()
    source = document["source"]
    assert isinstance(source, dict)
    privileged_assets = {
        "broker/config/executables.yaml",
        "broker/config/rbac.yaml",
        "broker/deploy/helpers/control-plane-deployment",
        "broker/deploy/sudoers/ops-broker-control-plane-deployment",
        "deploy/control-plane/README.md",
        "deploy/control-plane/bin/control-plane-deployment-worker",
        "deploy/control-plane/ops-control-plane-deployment.tmpfiles",
        "deploy/control-plane/release-manifest.v1.schema.json",
        "deploy/control-plane/systemd",
        "runbooks/local/control-plane-deployment-status.yaml",
    }
    assert privileged_assets.issubset(set(source["roots"]))
    assert "runbooks/local/control-plane-rollback.yaml" not in source["roots"]
    assert "provider-discovery" in source["roots"]
    assert "deploy/control-plane/release-manifest.v1.json" not in source["roots"]
    covered_paths = {
        relative.as_posix()
        for _absolute, relative in namespace["iter_source_paths"](
            ROOT, source["roots"]
        )
    }
    assert "provider-discovery/src/ops_provider_discovery/collector.py" in covered_paths
    assert "docs/provider-model-discovery.md" in covered_paths
    assert "broker/src/ops_broker/service.py" in covered_paths
    assert "broker/deploy/helpers/restart-service" in covered_paths
    assert "broker/deploy/systemd/ops-broker.service" in covered_paths
    assert "policies/actions.yaml" in covered_paths
    assert "runbooks/local/health.yaml" in covered_paths
    # The old manifest remains sealed historical evidence. The installed native
    # release has its own complete inventory, including the reused Atlas code.
    import runpy
    release = runpy.run_path(str(ROOT / "native_ops/release.py"))
    current = release["verify"](ROOT)
    import hashlib
    assert current["legacy_manifest_sha256"] == hashlib.sha256(
        (ROOT / "deploy/control-plane/release-manifest.v1.json").read_bytes()
    ).hexdigest()



def test_broker_bootstrap_rejects_a_copied_input_outside_the_source_footprint() -> None:
    namespace = worker_namespace()
    document = manifest()
    roots = [
        namespace["_relative_path"](item, "source.roots")
        for item in document["source"]["roots"]
        if item != "broker"
        and not item.startswith("broker/")
    ]
    with pytest.raises(namespace["DeploymentError"], match="bootstrap copy is outside"):
        namespace["validate_bootstrap_source_coverage"](roots)


def test_supply_chain_manifest_is_version_locked_without_overclaiming_hashes() -> None:
    document = manifest()
    supply = document["supply_chain"]
    assert supply["network_required"] is True
    assert supply["byte_for_byte_reproducible"] is False
    assert supply["python_binary_only"] is True
    assert supply["python_no_deps"] is True
    assert supply["python_artifact_hashes_present"] is False
    assert supply["atlas"] == {
        "exit_code": 0,
        "known_blocking_vulnerabilities": 0,
        "warnings": 7,
        "unmaintained_transitive_crates": 6,
        "advisories": [
            {
                "id": "RUSTSEC-2024-0429",
                "crate": "glib",
                "version": "0.18.5",
                "classification": "unsound-transitive",
            }
        ],
    }
    assert supply["hermes"] == {
        "uv_version": "0.12.0",
        "uv_lock_from_pinned_commit": True,
        "locked_sync": True,
        "bootstrap_script_pinned": False,
        "source_archive_vendored": True,
        "network_git_transport": False,
    }
    for name, closure in supply["python_closures"].items():
        content = (ROOT / "tests/fixtures/legacy-python-closures" / (name + ".txt")).read_bytes()
        assert hashlib.sha256(content).hexdigest() == closure["sha256"]
        distributions = [
            line for line in content.decode().splitlines()
            if line and not line.startswith("#")
        ]
        assert len(distributions) == closure["distributions"]
    worker = WORKER_PATH.read_text(encoding="utf-8")
    assert "--only-binary=:all:" in worker
    assert "Python runtime closure differs from review" in worker


def test_atlas_rpm_identity_and_digest_are_reviewed() -> None:
    namespace = worker_namespace()
    document = manifest()
    atlas = document["artifacts"]["atlas_rpm"]
    artifact = Path(atlas["source_path"])
    assert namespace["sha256_regular"](artifact) == atlas["sha256"]
    namespace["validate_atlas_rpm"](artifact, atlas)
    assert re.fullmatch(r"[0-9a-f]{64}", namespace["atlas_payload_sha256"](artifact))
    assert atlas["name"] == "modeles-ia"
    assert atlas["architecture"] == "x86_64"
    rpm_paths = subprocess.run(
        ["/usr/bin/rpm", "-qlp", str(artifact)],
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.splitlines()
    assert all("/__pycache__/" not in path and not path.endswith(".pyc") for path in rpm_paths)
    assert atlas["initial_state"] == "absent"
    assert atlas["signed"] is False
    worker = WORKER_PATH.read_text(encoding="utf-8")
    assert '"atlas_installation_attempted": False' in worker
    assert "validate_saved_atlas_artifact(metadata)" in worker
    assert "remove_attributable_rpm_temporary_payloads" in worker


def test_vendored_hermes_archive_has_three_bound_integrity_layers() -> None:
    namespace = worker_namespace()
    document = manifest()
    contract = document["artifacts"]["hermes_source_archive"]
    archive = Path(contract["source_path"])
    assert contract["commit"] == document["components"]["hermes"]["commit"]
    assert contract["git_tree_oid"] == "daaffc303ae437041b7f76be17c5f61b14f2ce99"
    assert contract["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert contract["canonical_tree_sha256"] == (
        "7ecc4e3f45d65d6ab33907f9220c06daa66b118af2a0fbbf5df3f2e019776ad1"
    )
    namespace["validate_hermes_source_archive"](archive, contract)
    worker = WORKER_PATH.read_text(encoding="utf-8")
    bootstrap = (PACKAGE / "bin" / "control-plane-bootstrap").read_text(
        encoding="utf-8"
    )
    installer = (ROOT / "scripts" / "install-hermes.sh").read_text(
        encoding="utf-8"
    )
    managed_source = "\n".join((worker, bootstrap, installer))
    assert "git ls-remote" not in managed_source
    assert "git fetch" not in managed_source
    assert '"/usr/bin/git"' not in managed_source
    assert '"--archive"' in worker
    assert '"--archive"' in bootstrap


def test_hermes_archive_validator_rejects_special_and_ambiguous_members(
    tmp_path: Path,
) -> None:
    namespace = worker_namespace()
    commit = "a" * 40
    prefix = f"hermes-agent-{commit}"

    def archive_contract(path: Path, member_name: str, *, symbolic: bool) -> dict[str, object]:
        with tarfile.open(
            path, "w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": commit}
        ) as archive:
            root = tarfile.TarInfo(prefix)
            root.type = tarfile.DIRTYPE
            root.mode = 0o775
            root.uid = root.gid = 0
            root.uname = root.gname = "root"
            root.pax_headers = {"comment": commit}
            archive.addfile(root)
            item = tarfile.TarInfo(member_name)
            item.mode = 0o775 if symbolic else 0o664
            item.uid = item.gid = 0
            item.uname = item.gname = "root"
            item.pax_headers = {"comment": commit}
            if symbolic:
                item.type = tarfile.SYMTYPE
                item.linkname = "/etc/passwd"
                payload = b""
                archive.addfile(item)
            else:
                import io

                payload = b"reviewed"
                item.size = len(payload)
                archive.addfile(item, io.BytesIO(payload))
        file_hash = hashlib.sha256(b"reviewed").hexdigest()
        canonical = hashlib.sha256(
            f"file.txt\0{0o644:o}\08\0{file_hash}\n".encode()
        ).hexdigest()
        return {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "commit": commit,
            "canonical_tree_sha256": canonical,
            "top_level_directory": prefix,
            "compressed_size": path.stat().st_size,
            "expanded_size": 0 if symbolic else 8,
            "members": 2,
            "regular_files": 0 if symbolic else 1,
            "directories": 1,
            "max_file_size": 0 if symbolic else 8,
        }

    special = tmp_path / "special.tar.gz"
    special_contract = archive_contract(
        special, f"{prefix}/file.txt", symbolic=True
    )
    with pytest.raises(namespace["DeploymentError"], match="unsupported"):
        namespace["validate_hermes_source_archive"](special, special_contract)

    ambiguous = tmp_path / "ambiguous.tar.gz"
    ambiguous_contract = archive_contract(
        ambiguous, f"{prefix}//file.txt", symbolic=False
    )
    with pytest.raises(namespace["DeploymentError"], match="unsafe"):
        namespace["validate_hermes_source_archive"](ambiguous, ambiguous_contract)


def test_generated_python_caches_cannot_change_the_source_release_digest(
    tmp_path: Path,
) -> None:
    namespace = worker_namespace()
    source = tmp_path / "source"
    cache = source / "component" / "__pycache__"
    cache.mkdir(parents=True)
    (source / "component" / "reviewed.py").write_text("VALUE = 1\n", encoding="utf-8")
    generated = cache / "reviewed.cpython-314.pyc"
    generated.write_bytes(b"first generated cache")
    first = namespace["source_tree_digest"](source, ["component"])
    generated.write_bytes(b"different generated cache")
    second = namespace["source_tree_digest"](source, ["component"])
    assert first == second
    covered = {
        relative.as_posix()
        for _absolute, relative in namespace["iter_source_paths"](
            source, ["component"]
        )
    }
    assert covered == {"component/reviewed.py"}


def test_zulip_manifest_digests_match_reviewed_compose() -> None:
    compose = (ROOT / "deploy" / "zulip-local" / "compose.yaml").read_text(
        encoding="utf-8"
    )
    discovered = dict(
        re.findall(
            r'^\s*image:\s*"([^"@]+)@sha256:([0-9a-f]{64})"\s*$',
            compose,
            re.MULTILINE,
        )
    )
    images = manifest()["components"]["zulip"]["images"]
    expected = {
        "ghcr.io/zulip/zulip-server:12.2-0": images["zulip"],
        "zulip/zulip-postgresql:14": images["postgresql"],
        "memcached:alpine": images["memcached"],
        "rabbitmq:4.2": images["rabbitmq"],
        "redis:alpine": images["redis"],
    }
    assert discovered == expected


def test_broker_registry_exposes_only_fixed_deployment_argv() -> None:
    executable_policy = ExecutablePolicy.load(ROOT / "broker" / "config" / "executables.yaml")
    registry = RunbookRegistry.load(ROOT / "runbooks", executable_policy)
    status = registry.get("local.control-plane-deployment-status.v1")
    with pytest.raises(NotFound, match="runbook was not found"):
        registry.get("local.control-plane-deployment.v1")
    with pytest.raises(NotFound, match="runbook was not found"):
        registry.get("local.control-plane-deployment-preflight.v1")
    with pytest.raises(NotFound, match="runbook was not found"):
        registry.get("local.control-plane-rollback.v1")
    assert status.action_class == "A"
    assert status.render_argv({})[-1] == "status"

    rbac = RBACPolicy.load(ROOT / "broker" / "config" / "rbac.yaml")
    for runbook_id, action_class in (
        (status.id, "A"),
    ):
        assert rbac.can_runbook("codex-supervised", "infra-shared", runbook_id, action_class)
    assert not rbac.can_runbook(
        "zulip-mobile", "infra-shared", "local.control-plane-deployment.v1", "C"
    )
    assert not rbac.can_runbook(
        "claude-supervised", "infra-shared", "local.control-plane-deployment.v1", "C"
    )


def test_dispatcher_rejects_every_unreviewed_operation_before_privilege_check() -> None:
    completed = subprocess.run(
        [sys.executable, str(DISPATCHER_PATH), "apply;id"],
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert completed.returncode == 64
    assert json.loads(completed.stderr) == {
        "error": "operation_not_allowed",
        "ok": False,
    }


def test_deployment_assets_are_root_installable_bounded_and_dormant() -> None:
    installer = (ROOT / "scripts" / "install-broker.sh").read_text(encoding="utf-8")
    dispatcher = DISPATCHER_PATH.read_text(encoding="utf-8")
    worker = WORKER_PATH.read_text(encoding="utf-8")
    sudoers = (
        ROOT / "broker" / "deploy" / "sudoers" / "ops-broker-control-plane-deployment"
    ).read_text(encoding="utf-8")
    assert '"$deployment_source/bin/control-plane-deployment-worker"' in installer
    assert '"$deployment_source/release-manifest.v1.json"' in installer
    assert '"$deployment_source/release-manifest.v1.schema.json"' in installer
    assert "ops-broker-control-plane-deployment" in installer
    assert "shell=True" not in dispatcher
    assert "shell=True" not in worker
    assert "eval(" not in dispatcher
    assert "eval(" not in worker
    assert "/bin/sh" not in sudoers
    for operation in ("status",):
        assert f"control-plane-deployment {operation}" in sudoers
    for operation in ("apply", "preflight", "rollback"):
        assert f"control-plane-deployment {operation}" not in sudoers
    assert not (PACKAGE / "systemd" / "ops-control-plane-deployment.service").exists()
    assert not (PACKAGE / "systemd" / "ops-control-plane-rollback.service").exists()
    assert "ops-control-plane-deployment.service" not in installer
    assert "ops-control-plane-rollback.service" not in installer


def test_backup_allowlist_excludes_every_secret_store() -> None:
    namespace = worker_namespace()
    paths = {str(path) for path in namespace["MANAGED_BACKUP_PATHS"]}
    assert "/etc/credstore.encrypted" not in paths
    assert "/var/lib/openbao" not in paths
    assert "/etc/passwd" not in paths
    assert "/etc/shadow" not in paths
    assert all(not path.startswith("/run/") for path in paths)
    assert all(not path.endswith("/workspace") for path in paths)
    assert "/var/lib/hermes/profiles" not in paths
    assert {
        "/etc/hosts",
        "/etc/pki/ca-trust/source/anchors/zulip-ops-local-ca.crt",
        "/usr/local/share/ops-control-plane/catalog",
        "/usr/local/libexec/zulip-openbao-launcher",
        "/usr/local/sbin/provision-zulip-openbao",
        "/var/lib/node-exporter/textfile/ops-orchestrator.prom",
    }.issubset(paths)
    document = manifest()
    assert document["rollback"]["secret_material_backed_up"] is False
    worker = WORKER_PATH.read_text(encoding="utf-8")
    assert "stdout=subprocess.DEVNULL" in worker
    assert "stderr=subprocess.DEVNULL" in worker


def test_provider_finance_timer_is_activated_health_checked_and_rollbackable() -> None:
    namespace = worker_namespace()
    runtime = manifest()["runtime"]
    services = runtime["services"]
    assert "ops-orchestrator-provider-finance-daemon.service" in services
    assert "ops-orchestrator-provider-finance.timer" in services
    assert tuple(services) == namespace["SERVICE_NAMES"]
    assert runtime["dynamic_services"] == list(namespace["DYNAMIC_SERVICE_NAMES"])
    backup_paths = {str(path) for path in namespace["MANAGED_BACKUP_PATHS"]}
    assert "/etc/systemd/system/ops-orchestrator-provider-finance.service" in backup_paths
    assert "/etc/systemd/system/ops-orchestrator-provider-finance.timer" in backup_paths
    assert (
        "/etc/systemd/system/ops-orchestrator-provider-finance-daemon.service"
        in backup_paths
    )
    worker = WORKER_PATH.read_text(encoding="utf-8")
    assert '"ops-orchestrator-provider-finance.timer",' in worker
    assert '"/run/ops-orchestrator-finance/api.sock"' in worker
    assert (
        '"health-provider-finance",\n        [\n            "/usr/bin/runuser",\n'
        '            "-u",\n            "opsorchestrator",\n            "--",\n'
        '            "/usr/bin/curl",'
    ) in worker
    assert "ops-orchestrator-secrets.service" in services
    assert "zulip-local-secrets.service" in services
    assert runtime["finance_isolation"] == {
        "worker_user": "opsfinance",
        "worker_group": "opsfinance",
        "client_user": "opsorchestrator",
        "socket_path": "/run/ops-orchestrator-finance/api.sock",
        "socket_mode": "0660",
        "stateless": True,
    }
    assert runtime["provider_credential_scopes"] == {
        "ops-orchestrator.service": [
            "qwen-api-key",
            "deepseek-api-key",
            "z-ai-api-key",
            "mistral-ai-api-key",
            "minimax-api-key",
            "google-api-key",
            "cohere-api-key",
            "moonshot-api-key",
            "tencent-api-key",
            "xai-api-key",
            "openai-api-key",
            "anthropic-api-key",
        ],
        "ops-orchestrator-provider-finance-daemon.service": [
            "deepseek-api-key",
            "moonshot-api-key",
            "stepfun-api-key",
        ],
        "ops-orchestrator-provider-finance.service": [],
        "ops-orchestrator-hermes-facade.service": [
            "qwen-api-key",
            "deepseek-api-key",
            "hermes-facade-token",
        ],
    }
    assert runtime["provider_optional_credential_scopes"] == {
        "ops-orchestrator.service": [
            "qwen-api-key",
            "deepseek-api-key",
            "z-ai-api-key",
            "mistral-ai-api-key",
            "minimax-api-key",
            "google-api-key",
            "cohere-api-key",
            "moonshot-api-key",
            "tencent-api-key",
            "xai-api-key",
            "openai-api-key",
            "anthropic-api-key",
        ],
        "ops-orchestrator-provider-finance-daemon.service": [
            "deepseek-api-key",
            "moonshot-api-key",
            "stepfun-api-key",
        ],
        "ops-orchestrator-provider-finance.service": [],
        "ops-orchestrator-hermes-facade.service": [
            "qwen-api-key",
            "deepseek-api-key",
        ],
    }
    assert "validate_finance_socket()" in worker
    assert '"finance_identity_before": validate_finance_identity(allow_absent=True)' in worker
    assert '["/usr/sbin/userdel", FINANCE_WORKER_USER]' in worker
    assert '["/usr/sbin/groupdel", FINANCE_WORKER_GROUP]' in worker
    assert worker.count("restore_finance_identity(metadata)") == 2


def test_docker_fedora_release_is_pinned_and_transactionally_undoable() -> None:
    document = manifest()
    docker = document["host"]["docker"]
    assert docker["mode"] == "rootful"
    assert docker["repositories"] == ["fedora", "updates"]
    assert docker["install_weak_dependencies"] is False
    assert docker["packages"] == {
        "moby-engine": "moby-engine-0:29.7.2-1.fc44.x86_64",
        "docker-cli": "docker-cli-0:29.7.2-1.fc44.x86_64",
        "docker-compose": "docker-compose-0:5.5.0-1.fc44.x86_64",
    }
    assert docker["dependencies"]["containerd"] == "containerd-0:2.3.4-1.fc44.x86_64"
    assert docker["dependencies"]["runc"] == "runc-2:1.5.1-1.fc44.x86_64"
    assert len(docker["dependencies"]) == 31
    assert docker["initial_state"] == "absent"
    assert docker["in_place_upgrade_supported"] is False
    assert docker["rollback_preserves_named_volumes"] is True
    assert docker["rollback_named_volumes_root"] == "/var/backups/zulip-local"
    assert docker["preinstalled_dependencies"] == [
        "container-selinux", "iptables-nft", "nftables",
    ]
    assert docker["network"]["fixed_networks"] == {
        "zulip-local_backend": {
            "bridge_name": "zulip-backend",
            "compose_network": "backend",
            "internal": True,
            "masquerade": False,
            "subnet": "172.30.10.0/24",
        },
        "zulip-local_frontend": {
            "bridge_name": "zulip-frontend",
            "compose_network": "frontend",
            "internal": False,
            "masquerade": False,
            "subnet": "172.30.11.0/24",
        },
    }
    assert docker["dependencies"]["systemd"] == "systemd-0:259.8-1.fc44.x86_64"
    assert (
        docker["dependencies"]["selinux-policy-targeted"]
        == "selinux-policy-targeted-0:44.8-1.fc44.noarch"
    )
    worker = WORKER_PATH.read_text(encoding="utf-8")
    assert '"--disablerepo=*"' in worker
    assert '"--enablerepo=fedora"' in worker
    assert '"--enablerepo=updates"' in worker
    assert '"download",\n            "--resolve"' in worker
    assert '["/usr/bin/rpmkeys", "--checksig", str(path)]' in worker
    assert '"--setopt=localpkg_gpgcheck=True"' in worker
    assert "Docker dependency resolution differs from the reviewed allowlist" in worker
    assert '"remove",\n                    "--no-autoremove"' in worker
    assert '["/usr/bin/docker", "compose", "version"]' in worker


def test_dnf5_no_autoremove_is_a_supported_remove_subcommand_option() -> None:
    completed = subprocess.run(
        ["/usr/bin/dnf", "remove", "--help"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert b"--no-autoremove" in completed.stdout

    tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"), filename=str(WORKER_PATH))
    remove_argvs: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        constants = [
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        ]
        if "/usr/bin/dnf" in constants and "remove" in constants:
            remove_argvs.append(constants)
    assert len(remove_argvs) == 2
    for argv in remove_argvs:
        assert "--noautoremove" not in argv
        assert argv.index("remove") < argv.index("--no-autoremove")


def test_worker_cli_mutations_require_the_outer_physical_bootstrap() -> None:
    for operation in ("apply", "preflight", "rollback"):
        completed = subprocess.run(
            [sys.executable, str(WORKER_PATH), operation],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert completed.returncode == 1
        assert json.loads(completed.stderr) == {
            "error": "bootstrap_required",
            "ok": False,
            "status": "failed",
        }


def test_dead_worker_preflight_package_probe_has_a_closed_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    query = namespace["installed_package_nevra"]
    globals_ = query.__globals__
    monkeypatch.setattr(
        globals_["subprocess"],
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=b"dnf5-plugins-0:5.2.15.0-2.fc44.x86_64\n"
        ),
    )
    assert query("dnf5-plugins") == "dnf5-plugins-0:5.2.15.0-2.fc44.x86_64"
    with pytest.raises(namespace["DeploymentError"], match="unreviewed package"):
        query("foreign-package")


def test_readonly_source_and_atlas_review_never_stage_under_var_tmp() -> None:
    namespace = worker_namespace()
    inspect = __import__("inspect")
    source_digest = inspect.getsource(namespace["source_tree_digest"])
    atlas_review = inspect.getsource(namespace["validate_atlas_rpm"])
    assert "TemporaryDirectory" not in source_digest
    assert 'dir="/var/tmp"' not in source_digest
    assert "memfd_create" in atlas_review
    assert "F_ADD_SEALS" in atlas_review
    assert 'dir="/var/tmp"' not in atlas_review


def test_volume_preservation_replays_a_crash_after_atomic_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    preserve = namespace["preserve_transaction_docker_volumes"]
    verify = namespace["verify_preserved_docker_volumes"]
    globals_ = preserve.__globals__
    source = tmp_path / "var/lib/docker/volumes"
    source.mkdir(parents=True)
    (source / "metadata.db").write_bytes(b"volume-metadata")
    recovered = tmp_path / "var/backups/zulip-local"
    recovered.parent.mkdir(parents=True)
    backup = tmp_path / "transaction"
    backup.mkdir()
    transaction_id = "release-1"
    metadata = {
        "transaction_id": transaction_id,
        "backup_path": str(backup),
        "docker": {
            "volumes_preservation_started": False,
            "volumes_preservation_path": None,
            "volumes_preserved": False,
        },
    }
    real_lstat = os.lstat

    def root_lstat(path: object) -> os.stat_result:
        item = real_lstat(path)
        values = list(item)
        values[4] = 0
        values[5] = 0
        return os.stat_result(values)

    monkeypatch.setitem(globals_, "DOCKER_VOLUMES_PATH", source)
    monkeypatch.setitem(globals_, "ZULIP_RECOVERED_DATA_ROOT", recovered)
    monkeypatch.setitem(globals_, "_mount_inventory", lambda: set())
    monkeypatch.setitem(globals_, "_fsync_directory_path", lambda _path: None)
    monkeypatch.setattr(globals_["os"], "lstat", root_lstat)
    monkeypatch.setattr(globals_["os"], "chown", lambda *_args: None)

    writes = {"count": 0}

    def crash_after_rename(_path: Path, _document: dict[str, object], **_kwargs: object) -> None:
        writes["count"] += 1
        if writes["count"] == 2:
            raise RuntimeError("power loss after rename")

    monkeypatch.setitem(globals_, "atomic_json", crash_after_rename)
    with pytest.raises(RuntimeError, match="power loss"):
        preserve(metadata)
    destination = recovered / f"recovered-{transaction_id}" / "docker-volumes"
    assert not source.exists()
    assert (destination / "metadata.db").read_bytes() == b"volume-metadata"
    assert metadata["docker"]["volumes_preserved"] is True

    metadata["docker"]["volumes_preserved"] = False
    monkeypatch.setitem(globals_, "atomic_json", lambda *_args, **_kwargs: None)
    preserve(metadata)
    verify(metadata)
    assert metadata["docker"]["volumes_preserved"] is True
    assert (destination / "metadata.db").read_bytes() == b"volume-metadata"


def test_docker_firewalld_reload_waits_for_the_async_moby_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    reload_barrier = namespace["reload_firewalld_with_docker_barrier"]
    globals_ = reload_barrier.__globals__
    observations = iter((None, None, "2026-09-07T18:00:01+02:00"))
    commands: list[tuple[str, tuple[str, ...]]] = []
    clock = SimpleNamespace(
        time=iter((100.0, 101.0)).__next__,
        monotonic=iter((1.0, 1.1, 1.2, 1.3, 1.4)).__next__,
        sleep=lambda _seconds: None,
    )
    monkeypatch.setitem(
        globals_, "docker_firewalld_reloaded_at", lambda: next(observations)
    )
    monkeypatch.setitem(globals_, "time", clock)
    monkeypatch.setitem(
        globals_,
        "run_command",
        lambda name, argv, *, timeout: commands.append((name, tuple(argv))),
    )
    reload_barrier()
    assert commands == [
        ("reload-docker-firewall", ("/usr/bin/firewall-cmd", "--reload"))
    ]


def test_transaction_file_replays_a_private_partial_fixed_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    publish = namespace["transaction_atomic_bytes"]
    globals_ = publish.__globals__
    parent = tmp_path / "etc/docker"
    parent.mkdir(parents=True)
    target = parent / "daemon.json"
    staging = parent / ".daemon.json.ops-control-plane-staging"
    payload = b'{"bridge":"none"}\n'
    staging.write_bytes(payload[:4])
    staging.chmod(0o600)
    real_lstat = os.lstat

    def root_lstat(path: object) -> os.stat_result:
        item = real_lstat(path)
        values = list(item)
        if Path(path) == staging:
            values[4] = 0
            values[5] = 0
        return os.stat_result(values)

    synced: list[Path] = []
    monkeypatch.setattr(globals_["os"], "lstat", root_lstat)
    monkeypatch.setattr(globals_["os"], "fchown", lambda *_args: None)
    monkeypatch.setitem(
        globals_, "_fsync_directory_path", lambda path: synced.append(Path(path))
    )
    publish(target, staging, payload, mode=0o600)
    assert target.read_bytes() == payload
    assert not staging.exists()
    assert synced == [parent, parent]


def test_containerd_accepts_only_the_reviewed_rpm_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    validate = namespace["validate_containerd_package_configuration"]
    globals_ = validate.__globals__
    root = tmp_path / "etc/containerd"
    config = root / "config.toml"
    root.mkdir(parents=True)
    config.write_text("version = 2\n", encoding="utf-8")
    real_lstat = os.lstat

    def root_lstat(path: object) -> os.stat_result:
        item = real_lstat(path)
        values = list(item)
        if Path(path) in {root, config}:
            values[4] = 0
            values[5] = 0
        return os.stat_result(values)

    monkeypatch.setitem(globals_, "CONTAINERD_CONFIG_ROOT", root)
    monkeypatch.setitem(globals_, "CONTAINERD_CONFIG_FILE", config)
    monkeypatch.setattr(globals_["os"], "lstat", root_lstat)
    monkeypatch.setattr(
        globals_["subprocess"],
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=b"containerd"
        ),
    )
    validate(required=True)

    (root / "foreign.toml").write_text("foreign = true\n", encoding="utf-8")
    with pytest.raises(namespace["DeploymentError"], match="differs from review"):
        validate(required=True)


def test_rpm_temporary_payload_scrub_accepts_reviewed_symlink_and_hardlink_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    scrub = namespace["remove_attributable_rpm_temporary_payloads"]
    globals_ = scrub.__globals__
    parent = tmp_path / "payload"
    parent.mkdir()
    regular = parent / "binary;deadbeef"
    retained_hardlink = parent / "retained-hardlink"
    regular.write_bytes(b"rpm temporary payload")
    os.link(regular, retained_hardlink)
    symlink = parent / "docker-init;1234abcd"
    symlink.symlink_to("/usr/libexec/docker/docker-init")
    inventory = [
        ("pkg-1", stat.S_IFREG | 0o755, parent / "binary"),
        ("pkg-1", stat.S_IFLNK | 0o777, parent / "docker-init"),
    ]
    real_lstat = os.lstat

    def root_lstat(path: object) -> os.stat_result:
        item = real_lstat(path)
        values = list(item)
        if Path(path) in {parent, regular, symlink}:
            values[4] = 0
            values[5] = 0
        return os.stat_result(values)

    monkeypatch.setattr(globals_["os"], "lstat", root_lstat)
    monkeypatch.setitem(globals_, "_fsync_directory_path", lambda _path: None)
    scrub(inventory, error_code="rollback_failed")
    assert not regular.exists()
    assert not symlink.exists()
    assert retained_hardlink.read_bytes() == b"rpm temporary payload"


def test_runtime_route_monitor_allows_only_exact_reviewed_bridge_routes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    validate = namespace["validate_docker_route_baseline"]
    globals_ = validate.__globals__
    networks = globals_["ZULIP_DOCKER_NETWORKS"]
    routes = [
        {
            "dst": contract["subnet"],
            "dev": contract["bridge_name"],
            "prefsrc": str(
                __import__("ipaddress").ip_network(contract["subnet"]).network_address
                + 1
            ),
        }
        for contract in networks.values()
    ]
    addresses = [
        {
            "ifname": contract["bridge_name"],
            "addr_info": [
                {
                    "family": "inet",
                    "local": str(
                        __import__("ipaddress").ip_network(contract["subnet"]).network_address
                        + 1
                    ),
                    "prefixlen": 24,
                }
            ],
        }
        for contract in networks.values()
    ]

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        payload = routes if "route" in argv else addresses
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(payload).encode("utf-8")
        )

    monkeypatch.setattr(globals_["subprocess"], "run", fake_run)
    monkeypatch.setitem(globals_, "_effective_dns_servers", lambda: set())
    validate(allow_reviewed_docker_routes=True)

    routes.append({"dst": "172.30.10.0/25", "dev": "tun0", "prefsrc": "172.30.10.2"})
    with pytest.raises(namespace["DeploymentError"], match="overlaps the host network"):
        validate(allow_reviewed_docker_routes=True)


def test_effective_dns_collision_is_rejected_after_network_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    validate = namespace["validate_docker_route_baseline"]
    globals_ = validate.__globals__

    def fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, stdout=b"[]")

    monkeypatch.setattr(globals_["subprocess"], "run", fake_run)
    monkeypatch.setitem(
        globals_,
        "_effective_dns_servers",
        lambda: {__import__("ipaddress").ip_address("172.30.11.53")},
    )
    with pytest.raises(namespace["DeploymentError"], match="effective DNS resolver"):
        validate(allow_reviewed_docker_routes=True)


def test_identity_gate_is_rechecked_before_snapshot_and_terminal_success() -> None:
    namespace = worker_namespace()
    inspect = __import__("inspect")
    preflight_source = inspect.getsource(namespace["preflight"])
    capture_source = inspect.getsource(namespace["capture_backup"])
    assert "validate_noop_identity_baseline()" in preflight_source
    assert "validate_initial_zulip_absent()" in preflight_source
    assert 'error_code="installed_state_unsafe"' in capture_source
    assert (
        "validate_initial_zulip_absent(allow_bootstrap_recovery_barrier=True)"
        in capture_source
    )
    assert 'error_code="rollback_failed"' in inspect.getsource(
        namespace["verify_terminal_rollback"]
    )
    bootstrap_source = (PACKAGE / "bin/control-plane-bootstrap").read_text(
        encoding="utf-8"
    )
    assert "_validate_noop_identity_baseline()" in bootstrap_source


def test_hermes_crash_staging_is_release_bound_and_rollback_managed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    validate_absent = namespace["validate_hermes_transaction_temps_absent"]
    remove_managed = namespace["_safe_remove_managed"]
    globals_ = validate_absent.__globals__
    reviewed = set(namespace["HERMES_TRANSACTION_TEMP_PATHS"])
    assert reviewed <= set(namespace["MANAGED_BACKUP_PATHS"])
    assert all("atlas-api-zulip-2026.09.08.11" in path.name for path in reviewed)
    residue = tmp_path / "release-bound-hermes-staging"
    residue.mkdir()
    monkeypatch.setitem(globals_, "HERMES_TRANSACTION_TEMP_PATHS", (residue,))
    monkeypatch.setitem(globals_, "MANAGED_BACKUP_PATHS", (residue,))

    with pytest.raises(namespace["DeploymentError"], match="staging path remains"):
        validate_absent("rollback_failed")
    remove_managed(residue)
    validate_absent("rollback_failed")
    assert not residue.exists()

    rollback_source = __import__("inspect").getsource(namespace["rollback_transaction"])
    terminal_source = __import__("inspect").getsource(
        namespace["verify_terminal_rollback"]
    )
    assert "for path in MANAGED_BACKUP_PATHS" in rollback_source
    assert "validate_hermes_transaction_temps_absent" in terminal_source


def test_health_refuses_a_release_bound_hermes_staging_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    health = namespace["health_check"]
    globals_ = health.__globals__
    residue = tmp_path / "release-bound-hermes-staging"
    residue.mkdir()
    monkeypatch.setitem(globals_, "HERMES_TRANSACTION_TEMP_PATHS", (residue,))

    with pytest.raises(namespace["DeploymentError"], match="staging path remains"):
        health({})


def test_legacy_zulip_units_require_exact_fragment_state_and_global_dropin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    validate = namespace["validate_initial_zulip_absent"]
    globals_ = validate.__globals__
    units = {
        "zulip-alertmanager-query.service": (b"query\n", "static"),
        "zulip-alertmanager-query.socket": (b"socket\n", "disabled"),
        "zulip-approval-bridge.service": (b"bridge\n", "disabled"),
    }
    systemd_root = tmp_path / "etc/systemd/system"
    systemd_root.mkdir(parents=True)
    for unit, (payload, _state) in units.items():
        (systemd_root / unit).write_bytes(payload)
    global_dropin = tmp_path / "usr/lib/systemd/system/service.d/10-timeout-abort.conf"
    global_dropin.parent.mkdir(parents=True)
    global_dropin.write_bytes(b"[Service]\nTimeoutAbortSec=1min\n")
    expected_hashes = {
        unit: hashlib.sha256(payload).hexdigest()
        for unit, (payload, _state) in units.items()
    }
    real_lstat = os.lstat

    def root_lstat(path: object) -> os.stat_result:
        item = real_lstat(path)
        values = list(item)
        candidate = Path(path)
        if candidate == global_dropin or candidate.parent == systemd_root:
            values[4] = 0
            values[5] = 0
        return os.stat_result(values)

    fragment_drift = {"value": False}

    def systemctl(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        unit = argv[2]
        property_name = next(
            argument.removeprefix("--property=")
            for argument in argv
            if argument.startswith("--property=")
        )
        _payload, state = units[unit]
        values = {
            "ActiveState": "inactive",
            "UnitFileState": state,
            "FragmentPath": str(systemd_root / unit),
            "DropInPaths": str(global_dropin) if unit.endswith(".service") else "",
        }
        if fragment_drift["value"] and property_name == "FragmentPath":
            values[property_name] = "/run/systemd/system/foreign.service"
        return subprocess.CompletedProcess(
            argv, 0, stdout=(values[property_name] + "\n").encode("ascii")
        )

    monkeypatch.setitem(globals_, "ZULIP_INITIAL_ABSENT_PATHS", ())
    monkeypatch.setitem(globals_, "ZULIP_INITIAL_UNITS", tuple(units))
    monkeypatch.setitem(globals_, "ZULIP_LEGACY_UNIT_SHA256", expected_hashes)
    monkeypatch.setitem(
        globals_,
        "ZULIP_LEGACY_UNIT_FILE_STATE",
        {unit: state for unit, (_payload, state) in units.items()},
    )
    monkeypatch.setitem(globals_, "SYSTEMD_GLOBAL_SERVICE_DROPIN", global_dropin)
    monkeypatch.setitem(
        globals_,
        "SYSTEMD_GLOBAL_SERVICE_DROPIN_SHA256",
        hashlib.sha256(global_dropin.read_bytes()).hexdigest(),
    )
    monkeypatch.setitem(globals_, "unit_is_loaded", lambda _unit: True)
    monkeypatch.setitem(
        globals_,
        "Path",
        lambda value: systemd_root
        if str(value) == "/etc/systemd/system"
        else Path(value),
    )
    monkeypatch.setattr(globals_["os"], "lstat", root_lstat)
    monkeypatch.setattr(globals_["subprocess"], "run", systemctl)
    validate()

    fragment_drift["value"] = True
    with pytest.raises(namespace["DeploymentError"], match="reviewed dormant baseline"):
        validate()


def test_post_guard_legacy_zulip_service_and_socket_require_only_exact_barriers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    bootstrap = runpy.run_path(
        str(PACKAGE / "bin/control-plane-bootstrap")
    )
    assert (
        namespace["BOOTSTRAP_RECOVERY_BARRIER_DROPIN"]
        == bootstrap["RECOVERY_BARRIER_DROPIN"]
    )
    validate = namespace["validate_initial_zulip_absent"]
    globals_ = validate.__globals__
    units = {
        "zulip-alertmanager-query.service": (b"query\n", "static"),
        "zulip-alertmanager-query.socket": (b"socket\n", "disabled"),
    }
    systemd_root = tmp_path / "etc/systemd/system"
    systemd_root.mkdir(parents=True)
    unit_paths: dict[str, Path] = {}
    barrier_paths: dict[str, Path] = {}
    barrier_payload = namespace["BOOTSTRAP_RECOVERY_BARRIER_DROPIN"]
    barrier_filename = namespace["BOOTSTRAP_RECOVERY_BARRIER_FILENAME"]
    for unit, (payload, _state) in units.items():
        unit_path = systemd_root / unit
        unit_path.write_bytes(payload)
        unit_paths[unit] = unit_path
        barrier_path = systemd_root / f"{unit}.d" / barrier_filename
        barrier_path.parent.mkdir()
        barrier_path.write_bytes(barrier_payload)
        barrier_paths[unit] = barrier_path
    global_dropin = tmp_path / "usr/lib/systemd/system/service.d/10-timeout-abort.conf"
    global_dropin.parent.mkdir(parents=True)
    global_dropin.write_bytes(b"[Service]\nTimeoutAbortSec=1min\n")
    reviewed_paths = {global_dropin, *unit_paths.values(), *barrier_paths.values()}
    bad_uid: set[Path] = set()
    bad_gid: set[Path] = set()
    real_lstat = os.lstat
    real_fstat = os.fstat
    reported_barrier_paths = dict(barrier_paths)

    def reviewed_metadata(item: os.stat_result, candidate: Path) -> os.stat_result:
        values = list(item)
        if candidate in reviewed_paths:
            values[4] = 0
            values[5] = 0
        if candidate in bad_uid:
            values[4] = 1000
        if candidate in bad_gid:
            values[5] = 1000
        return os.stat_result(values)

    def root_lstat(path: object) -> os.stat_result:
        candidate = Path(path)
        return reviewed_metadata(real_lstat(path), candidate)

    def root_fstat(descriptor: int) -> os.stat_result:
        candidate = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        return reviewed_metadata(real_fstat(descriptor), candidate)

    extra_dropin = {"value": ""}

    def systemctl(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        unit = argv[2]
        property_name = next(
            argument.removeprefix("--property=")
            for argument in argv
            if argument.startswith("--property=")
        )
        _payload, state = units[unit]
        dropins = []
        if unit.endswith(".service"):
            dropins.append(str(global_dropin))
        dropins.append(str(reported_barrier_paths[unit]))
        if extra_dropin["value"]:
            dropins.append(extra_dropin["value"])
        values = {
            "ActiveState": "inactive",
            "UnitFileState": state,
            "FragmentPath": str(unit_paths[unit]),
            "DropInPaths": " ".join(dropins),
        }
        return subprocess.CompletedProcess(
            argv, 0, stdout=(values[property_name] + "\n").encode("ascii")
        )

    monkeypatch.setitem(globals_, "ZULIP_INITIAL_ABSENT_PATHS", ())
    monkeypatch.setitem(globals_, "ZULIP_INITIAL_UNITS", tuple(units))
    monkeypatch.setitem(
        globals_,
        "ZULIP_LEGACY_UNIT_SHA256",
        {
            unit: hashlib.sha256(payload).hexdigest()
            for unit, (payload, _state) in units.items()
        },
    )
    monkeypatch.setitem(
        globals_,
        "ZULIP_LEGACY_UNIT_FILE_STATE",
        {unit: state for unit, (_payload, state) in units.items()},
    )
    monkeypatch.setitem(globals_, "SYSTEMD_GLOBAL_SERVICE_DROPIN", global_dropin)
    monkeypatch.setitem(
        globals_,
        "SYSTEMD_GLOBAL_SERVICE_DROPIN_SHA256",
        hashlib.sha256(global_dropin.read_bytes()).hexdigest(),
    )
    monkeypatch.setitem(globals_, "unit_is_loaded", lambda _unit: True)
    monkeypatch.setitem(
        globals_,
        "Path",
        lambda value: systemd_root
        if str(value) == "/etc/systemd/system"
        else Path(value),
    )
    monkeypatch.setattr(globals_["os"], "lstat", root_lstat)
    monkeypatch.setattr(globals_["os"], "fstat", root_fstat)
    monkeypatch.setattr(globals_["subprocess"], "run", systemctl)

    # The ordinary preflight remains fail-closed once the recovery guard has
    # added its drop-ins. Only the post-guard snapshot mode accepts them.
    with pytest.raises(namespace["DeploymentError"], match="reviewed dormant baseline"):
        validate()
    validate(allow_bootstrap_recovery_barrier=True)

    socket_unit = "zulip-alertmanager-query.socket"
    reported_barrier_paths[socket_unit] = systemd_root / "foreign.conf"
    with pytest.raises(namespace["DeploymentError"], match="reviewed dormant baseline"):
        validate(allow_bootstrap_recovery_barrier=True)
    reported_barrier_paths[socket_unit] = barrier_paths[socket_unit]

    extra_dropin["value"] = "/etc/systemd/system/foreign.conf"
    with pytest.raises(namespace["DeploymentError"], match="reviewed dormant baseline"):
        validate(allow_bootstrap_recovery_barrier=True)
    extra_dropin["value"] = ""

    socket_barrier = barrier_paths[socket_unit]
    altered_payload = bytearray(barrier_payload)
    altered_payload[-2] ^= 1
    socket_barrier.write_bytes(altered_payload)
    with pytest.raises(namespace["DeploymentError"], match="barrier differs"):
        validate(allow_bootstrap_recovery_barrier=True)
    socket_barrier.write_bytes(barrier_payload)

    service_barrier = barrier_paths["zulip-alertmanager-query.service"]
    service_barrier.chmod(0o600)
    with pytest.raises(namespace["DeploymentError"], match="barrier differs"):
        validate(allow_bootstrap_recovery_barrier=True)
    service_barrier.chmod(0o644)

    bad_uid.add(socket_barrier)
    with pytest.raises(namespace["DeploymentError"], match="barrier differs"):
        validate(allow_bootstrap_recovery_barrier=True)
    bad_uid.clear()

    bad_gid.add(socket_barrier)
    with pytest.raises(namespace["DeploymentError"], match="barrier differs"):
        validate(allow_bootstrap_recovery_barrier=True)
    bad_gid.clear()

    service_barrier.unlink()
    service_barrier.symlink_to(socket_barrier)
    with pytest.raises(namespace["DeploymentError"], match="barrier differs"):
        validate(allow_bootstrap_recovery_barrier=True)
    service_barrier.unlink()
    service_barrier.write_bytes(barrier_payload)

    hardlink = tmp_path / "barrier-hardlink"
    os.link(socket_barrier, hardlink)
    with pytest.raises(namespace["DeploymentError"], match="barrier differs"):
        validate(allow_bootstrap_recovery_barrier=True)
    hardlink.unlink()

    socket_barrier.unlink()
    with pytest.raises(namespace["DeploymentError"], match="barrier differs"):
        validate(allow_bootstrap_recovery_barrier=True)


def test_recovery_guard_then_broker_quiesce_reaches_snapshot_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = runpy.run_path(str(PACKAGE / "bin/control-plane-bootstrap"))
    worker = worker_namespace()
    install_guard = bootstrap["_install_recovery_guard"]
    quiesce_broker = bootstrap["_quiesce_broker_with_recovery_intent"]
    capture_backup = worker["capture_backup"]
    bootstrap_globals = install_guard.__globals__
    worker_globals = capture_backup.__globals__
    events: list[tuple[str, object]] = []
    units = {
        "zulip-alertmanager-query.service": (b"query\n", "static"),
        "zulip-alertmanager-query.socket": (b"socket\n", "disabled"),
    }
    systemd_root = tmp_path / "etc/systemd/system"
    systemd_root.mkdir(parents=True)
    unit_paths: dict[str, Path] = {}
    for unit, (payload, _state) in units.items():
        unit_path = systemd_root / unit
        unit_path.write_bytes(payload)
        unit_paths[unit] = unit_path
    global_dropin = tmp_path / "usr/lib/systemd/system/service.d/10-timeout-abort.conf"
    global_dropin.parent.mkdir(parents=True)
    global_dropin.write_bytes(b"[Service]\nTimeoutAbortSec=1min\n")
    release = tmp_path / "release"
    (release / "tree").mkdir(parents=True)
    transaction: dict[str, object] = {}
    quiesced = {"value": False}

    def bootstrap_run_command(
        name: str, _argv: list[str], *, timeout: int,
    ) -> None:
        events.append(("command", name))
        assert timeout > 0
        if name == "bootstrap-quiesce-broker-services":
            quiesced["value"] = True

    def bootstrap_systemctl_state(_unit: str, operation: str) -> bool:
        if operation == "is-enabled":
            return True
        if operation == "is-active":
            return not quiesced["value"]
        raise AssertionError(f"unexpected systemctl operation: {operation}")

    def write_barrier(path: Path, payload: bytes, *, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        path.chmod(mode)
        events.append(("barrier-written", str(path)))

    monkeypatch.setitem(bootstrap_globals, "RECOVERY_UNITS", ())
    monkeypatch.setitem(
        bootstrap_globals, "RECOVERY_DEPENDENT_UNITS", tuple(units)
    )
    monkeypatch.setitem(
        bootstrap_globals, "PREBACKUP_QUIESCED_UNITS", tuple(units)
    )
    monkeypatch.setitem(bootstrap_globals, "RECOVERY_ENABLED_UNITS", ())
    monkeypatch.setitem(
        bootstrap_globals,
        "Path",
        lambda value: systemd_root
        if str(value) == "/etc/systemd/system"
        else Path(value),
    )
    monkeypatch.setitem(bootstrap_globals, "_atomic_bytes", write_barrier)
    monkeypatch.setitem(
        bootstrap_globals, "_fsync_recovery_core", lambda *_args: None
    )
    monkeypatch.setitem(
        bootstrap_globals, "_fsync_recovery_anchor", lambda *_args: None
    )
    monkeypatch.setitem(
        bootstrap_globals, "_systemctl_state", bootstrap_systemctl_state
    )
    monkeypatch.setitem(
        bootstrap_globals,
        "_set_transaction",
        lambda target, **changes: (
            events.append(("bootstrap-wal", changes.get("phase"))),
            target.update(changes),
        )[-1],
    )
    monkeypatch.setitem(
        bootstrap_globals,
        "_start_standard_recovery_supervisor",
        lambda _transaction: events.append(("supervisor", "started")),
    )
    monkeypatch.setitem(
        bootstrap_globals,
        "_wait_standard_recovery_supervisor",
        lambda: events.append(("supervisor", "attested")),
    )
    bootstrap_worker = {"run_command": bootstrap_run_command, "_unit_is_loaded": lambda unit: True}

    install_guard(release, bootstrap_worker, transaction)
    quiesce_broker(bootstrap_worker, transaction)
    assert transaction["recovery_guard_installed"] is True
    assert transaction["phase"] == "broker-quiesced"
    assert quiesced["value"] is True

    barrier_paths = {
        unit: systemd_root
        / f"{unit}.d"
        / worker["BOOTSTRAP_RECOVERY_BARRIER_FILENAME"]
        for unit in units
    }
    for barrier in barrier_paths.values():
        assert barrier.read_bytes() == worker["BOOTSTRAP_RECOVERY_BARRIER_DROPIN"]
        assert stat.S_IMODE(barrier.stat().st_mode) == 0o644

    reviewed_paths = {global_dropin, *unit_paths.values(), *barrier_paths.values()}
    real_lstat = os.lstat
    real_fstat = os.fstat

    def root_metadata(item: os.stat_result, candidate: Path) -> os.stat_result:
        values = list(item)
        if candidate in reviewed_paths:
            values[4] = 0
            values[5] = 0
        return os.stat_result(values)

    def root_lstat(path: object) -> os.stat_result:
        candidate = Path(path)
        return root_metadata(real_lstat(path), candidate)

    def root_fstat(descriptor: int) -> os.stat_result:
        candidate = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        return root_metadata(real_fstat(descriptor), candidate)

    def systemctl(
        argv: list[str], **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        unit = argv[2]
        property_name = next(
            argument.removeprefix("--property=")
            for argument in argv
            if argument.startswith("--property=")
        )
        _payload, state = units[unit]
        dropins = []
        if unit.endswith(".service"):
            dropins.append(str(global_dropin))
        dropins.append(str(barrier_paths[unit]))
        values = {
            "ActiveState": "inactive",
            "UnitFileState": state,
            "FragmentPath": str(unit_paths[unit]),
            "DropInPaths": " ".join(dropins),
        }
        return subprocess.CompletedProcess(
            argv, 0, stdout=(values[property_name] + "\n").encode("ascii")
        )

    worker_state = tmp_path / "worker-state"
    backup_root = tmp_path / "backups"
    atlas_artifact = tmp_path / "atlas.rpm"
    atlas_artifact.write_bytes(b"reviewed Atlas RPM")
    manifest = {
        "release_id": "release-composition-test",
        "artifacts": {
            "atlas_rpm": {
                "name": "modeles-ia",
                "version": "0.1.0",
                "release": "1",
                "architecture": "x86_64",
                "sha256": hashlib.sha256(atlas_artifact.read_bytes()).hexdigest(),
            },
            "hermes_source_archive": {"sha256": "7" * 64},
        },
    }
    real_atomic_json = worker["atomic_json"]

    def record_atomic_json(
        path: Path, document: dict[str, object], *, mode: int = 0o600,
    ) -> None:
        events.append(("worker-wal", (path.name, document.get("phase"))))
        real_atomic_json(path, document, mode=mode)

    real_barrier_check = worker["_bootstrap_recovery_barrier_is_exact"]

    def record_barrier_check(path: Path) -> bool:
        result = real_barrier_check(path)
        events.append(("barrier-validated", (str(path), result)))
        return result

    monkeypatch.setitem(worker_globals, "BACKUP_ROOT", backup_root)
    monkeypatch.setitem(worker_globals, "STATE_ROOT", worker_state)
    monkeypatch.setitem(worker_globals, "CURRENT_PATH", worker_state / "current.json")
    monkeypatch.setitem(worker_globals, "AUDIT_PATH", worker_state / "events.jsonl")
    monkeypatch.setitem(worker_globals, "MANAGED_BACKUP_PATHS", ())
    monkeypatch.setitem(worker_globals, "ATLAS_PAYLOAD_PATHS", ())
    monkeypatch.setitem(worker_globals, "ZULIP_INITIAL_ABSENT_PATHS", ())
    monkeypatch.setitem(worker_globals, "ZULIP_INITIAL_UNITS", tuple(units))
    monkeypatch.setitem(
        worker_globals,
        "ZULIP_LEGACY_UNIT_SHA256",
        {
            unit: hashlib.sha256(payload).hexdigest()
            for unit, (payload, _state) in units.items()
        },
    )
    monkeypatch.setitem(
        worker_globals,
        "ZULIP_LEGACY_UNIT_FILE_STATE",
        {unit: state for unit, (_payload, state) in units.items()},
    )
    monkeypatch.setitem(worker_globals, "SYSTEMD_GLOBAL_SERVICE_DROPIN", global_dropin)
    monkeypatch.setitem(
        worker_globals,
        "SYSTEMD_GLOBAL_SERVICE_DROPIN_SHA256",
        hashlib.sha256(global_dropin.read_bytes()).hexdigest(),
    )
    monkeypatch.setitem(
        worker_globals,
        "Path",
        lambda value: systemd_root
        if str(value) == "/etc/systemd/system"
        else Path(value),
    )
    monkeypatch.setitem(worker_globals, "unit_is_loaded", lambda _unit: True)
    monkeypatch.setitem(
        worker_globals,
        "validate_noop_identity_baseline",
        lambda **_kwargs: events.append(("capture", "entered")),
    )
    monkeypatch.setitem(
        worker_globals,
        "_bootstrap_recovery_barrier_is_exact",
        record_barrier_check,
    )
    monkeypatch.setitem(
        worker_globals, "validate_docker_installation", lambda **_kwargs: "absent"
    )
    monkeypatch.setitem(worker_globals, "docker_transaction_package_snapshot", lambda: {})
    monkeypatch.setitem(worker_globals, "dnf_docker_history", lambda: {})
    monkeypatch.setitem(worker_globals, "docker_service_snapshot", lambda: {})
    monkeypatch.setitem(worker_globals, "docker_group_snapshot", lambda: None)
    monkeypatch.setitem(
        worker_globals, "docker_rollback_preset_parent_exists", lambda: False
    )
    monkeypatch.setitem(
        worker_globals,
        "validate_docker_network_baseline",
        lambda **_kwargs: ({}, {}, {}, "0" * 64),
    )
    monkeypatch.setitem(worker_globals, "local_inference_snapshot", lambda: {})
    monkeypatch.setitem(worker_globals, "service_snapshot", lambda: {})
    monkeypatch.setitem(worker_globals, "TRANSIENT_SERVICE_NAMES", ())
    monkeypatch.setitem(worker_globals, "ALL_TRANSIENT_UNITS", ())
    monkeypatch.setitem(worker_globals, "atlas_installed_identity", lambda: None)
    monkeypatch.setitem(worker_globals, "validate_staged_release", lambda _manifest: release)
    monkeypatch.setitem(
        worker_globals,
        "atlas_artifact_in_release",
        lambda _release, _manifest: atlas_artifact,
    )
    monkeypatch.setitem(
        worker_globals,
        "rpm_non_directory_payload_inventory",
        lambda *_args, **_kwargs: ([], "1" * 64),
    )
    monkeypatch.setitem(
        worker_globals, "assert_no_rpm_temporary_payloads", lambda *_args, **_kwargs: None
    )
    monkeypatch.setitem(
        worker_globals, "validate_finance_identity", lambda **_kwargs: "absent"
    )
    monkeypatch.setitem(
        worker_globals,
        "stop_dependents",
        lambda: events.append(("capture", "dependents-stopped")),
    )
    monkeypatch.setitem(
        worker_globals, "suppress_zulip_container_restarts", lambda _metadata: None
    )
    monkeypatch.setitem(
        worker_globals,
        "_sync_rollback_filesystems",
        lambda: events.append(("capture", "synced")),
    )
    monkeypatch.setitem(worker_globals, "atomic_json", record_atomic_json)
    monkeypatch.setattr(worker_globals["os"], "lstat", root_lstat)
    monkeypatch.setattr(worker_globals["os"], "fstat", root_fstat)
    monkeypatch.setattr(worker_globals["subprocess"], "run", systemctl)

    backup, metadata = capture_backup(manifest)
    wal = backup / "transaction.json"
    current = worker_state / "current.json"
    assert wal.is_file()
    assert current.is_file()
    assert json.loads(wal.read_text(encoding="utf-8"))["phase"] == "snapshotted"
    assert json.loads(current.read_text(encoding="utf-8"))["status"] == "deploying"
    assert metadata["phase"] == "snapshotted"
    assert metadata["hermes_transaction_temp_paths"] == [
        str(path) for path in worker["HERMES_TRANSACTION_TEMP_PATHS"]
    ]
    written = [index for index, event in enumerate(events) if event[0] == "barrier-written"]
    quiesce = next(
        index
        for index, event in enumerate(events)
        if event == ("command", "bootstrap-quiesce-broker-services")
    )
    validated = [
        index
        for index, event in enumerate(events)
        if event[0] == "barrier-validated" and event[1][1] is True
    ]
    first_wal = next(
        index for index, event in enumerate(events) if event[0] == "worker-wal"
    )
    quiescing_wal = events.index(("bootstrap-wal", "broker-quiescing"))
    quiesced_wal = events.index(("bootstrap-wal", "broker-quiesced"))
    supervisor_attested = events.index(("supervisor", "attested"))
    assert len(written) == len(units)
    assert len(validated) == len(units)
    assert max(written) < supervisor_attested < quiescing_wal
    assert quiescing_wal < quiesce < quiesced_wal < min(validated) < first_wal


def test_docker_rollback_metadata_rejects_partial_or_foreign_packages() -> None:
    namespace = worker_namespace()
    validate = namespace["validate_saved_docker_packages"]
    expected = namespace["DOCKER_TRANSACTION_NEVRAS"]
    absent = {name: None for name in expected}
    assert validate(absent, "synthetic") == absent

    partial = dict(absent)
    partial["moby-engine"] = expected["moby-engine"]
    with pytest.raises(namespace["DeploymentError"], match="partial"):
        validate(partial, "synthetic")

    foreign = dict(absent)
    foreign["systemd"] = "systemd-0:999-1.fc44.x86_64"
    with pytest.raises(namespace["DeploymentError"], match="invalid"):
        validate(foreign, "synthetic")


def test_tmpfiles_and_worker_use_private_fixed_roots() -> None:
    tmpfiles = (PACKAGE / "ops-control-plane-deployment.tmpfiles").read_text(
        encoding="utf-8"
    )
    assert "d /var/lib/ops-control-plane-deployment 0700 root root -" in tmpfiles
    assert "d /var/backups/ops-control-plane-deployment 0700 root root -" in tmpfiles
    assert "d /run/ops-control-plane-deployment 0700 root root -" in tmpfiles
    assert manifest()["runtime"]["zulip_owner_password_file"].startswith(
        "/run/ops-control-plane-deployment/"
    )


def test_manifest_rejects_a_local_inference_mode_and_credential_drift() -> None:
    namespace = worker_namespace()
    document = manifest()
    document["components"]["orchestrator"]["mode"] = "local"
    with pytest.raises(namespace["DeploymentError"], match="local model inference"):
        namespace["validate_manifest"](document)

    document = manifest()
    document["cutover"]["local_inference"]["mode"] = "available"
    with pytest.raises(namespace["DeploymentError"], match="cutover differs"):
        namespace["validate_manifest"](document)

    document = manifest()
    document["runtime"]["encrypted_credentials"].pop()
    with pytest.raises(namespace["DeploymentError"], match="credentials differ"):
        namespace["validate_manifest"](document)

    document = manifest()
    document["runtime"]["provider_optional_credential_scopes"][
        "ops-orchestrator.service"
    ].pop()
    with pytest.raises(
        namespace["DeploymentError"], match="optional provider credential scopes differ"
    ):
        namespace["validate_manifest"](document)


def test_worker_persists_atlas_install_intent_before_invoking_dnf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = worker_namespace()
    install = worker["install_release"]
    globals_ = install.__globals__
    release = tmp_path / "release"
    backup = tmp_path / "backup"
    metadata: dict[str, object] = {"atlas_installation_attempted": False}
    manifest = {
        "artifacts": {
            "atlas_rpm": {
                "source_path": "/reviewed/Atlas-modeles-IA.rpm",
            }
        }
    }
    events: list[tuple[str, bool | None]] = []
    real_path = Path

    monkeypatch.setitem(globals_, "preserve_legacy_hermes_source", lambda _backup, _metadata: None)
    monkeypatch.setitem(globals_, "disable_local_inference", lambda _metadata: None)
    monkeypatch.setitem(globals_, "ensure_docker", lambda _backup, _metadata: None)
    monkeypatch.setitem(globals_, "stop_dependents", lambda: None)
    monkeypatch.setitem(
        globals_,
        "Path",
        lambda value: (
            tmp_path / "hermes-gateway.enabled"
            if str(value) == "/etc/hermes/hermes-gateway.enabled"
            else real_path(value)
        ),
    )

    def record_wal(_path: Path, document: dict[str, object], **_kwargs: object) -> None:
        events.append(("wal", document.get("atlas_installation_attempted")))

    def fail_at_dnf(name: str, _argv: list[str], *, timeout: int) -> None:
        assert timeout > 0
        events.append((name, metadata.get("atlas_installation_attempted")))
        if name == "install-atlas":
            raise worker["DeploymentError"]("simulated_dnf_failure", "DNF failed")

    monkeypatch.setitem(globals_, "atomic_json", record_wal)
    monkeypatch.setitem(globals_, "run_command", fail_at_dnf)

    with pytest.raises(worker["DeploymentError"], match="DNF failed"):
        install(release, manifest, backup, metadata)

    assert metadata["atlas_installation_attempted"] is True
    assert ("wal", True) in events
    assert ("install-atlas", True) in events
    assert events.index(("wal", True)) < events.index(("install-atlas", True))


def test_atlas_attempted_flag_forces_completion_then_absent_baseline_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = worker_namespace()
    restore = worker["restore_atlas"]
    globals_ = restore.__globals__
    expected = "modeles-ia-0.1.0-1.x86_64"
    metadata = {
        "atlas_before": None,
        "atlas_expected": expected,
        "atlas_installation_attempted": True,
    }
    commands: list[str] = []
    identities = iter((expected, None))

    monkeypatch.setitem(
        globals_, "validate_saved_atlas_artifact", lambda _metadata: (tmp_path / "atlas.rpm", [])
    )
    monkeypatch.setitem(
        globals_, "remove_attributable_rpm_temporary_payloads", lambda *_args, **_kwargs: None
    )
    monkeypatch.setitem(
        globals_, "assert_no_rpm_temporary_payloads", lambda *_args, **_kwargs: None
    )
    monkeypatch.setitem(globals_, "atlas_installed_identity", lambda: next(identities))
    monkeypatch.setitem(globals_, "atlas_installation_is_pristine", lambda: True)
    monkeypatch.setitem(globals_, "ATLAS_PAYLOAD_PATHS", ())
    monkeypatch.setitem(
        globals_,
        "run_command",
        lambda name, _argv, *, timeout: commands.append(name),
    )

    restore(metadata)

    assert commands == [
        "rollback-complete-atlas-package",
        "rollback-reinstall-atlas-package",
        "rollback-atlas-package",
    ]

def test_manifest_corrective_contract_is_exact_and_one_shot() -> None:
    namespace = worker_namespace()
    document = manifest()
    corrective = document["bootstrap"]["corrective"]
    assert corrective == namespace["BOOTSTRAP_CORRECTIVE_CONTRACT"]
    assert corrective["max_attempts"] == 1
    assert corrective["recovery_from_release_id"].endswith(".10")
    assert document["release_id"] == "atlas-api-zulip-2026.09.09.29"

    changed = manifest()
    changed["bootstrap"]["corrective"]["max_attempts"] = 2
    with pytest.raises(namespace["DeploymentError"], match="corrective contract"):
        namespace["validate_manifest"](changed)

    changed = manifest()
    changed["bootstrap"]["gui_protocol"]["public_operations"].remove(
        "gui-corrective-apply"
    )
    with pytest.raises(namespace["DeploymentError"], match="GUI protocol"):
        namespace["validate_manifest"](changed)

    changed = manifest()
    changed["release_id"] = "atlas-api-zulip-2026.09.08.12"
    with pytest.raises(namespace["DeploymentError"], match="release identity"):
        namespace["validate_manifest"](changed)


def test_local_inference_cutover_is_reported_disabled_and_reversible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    disable = namespace["disable_local_inference"]
    restore = namespace["restore_local_inference"]
    globals_ = disable.__globals__
    state = {"loaded": True, "active": False, "enabled": True}
    prior = {"ollama.service": dict(state)}
    steps: list[tuple[str, ...]] = []

    monkeypatch.setitem(
        globals_,
        "local_inference_snapshot",
        lambda: {"ollama.service": dict(state)},
    )
    monkeypatch.setitem(globals_, "unit_is_loaded", lambda _unit: state["loaded"])
    monkeypatch.setitem(
        globals_,
        "systemctl_state",
        lambda _unit, operation: state["active"]
        if operation == "is-active"
        else state["enabled"],
    )

    def run_step(_name: str, argv: list[str], **_kwargs: object) -> None:
        steps.append(tuple(argv[1:]))
        operation = argv[1]
        if operation == "disable":
            state["enabled"] = False
            if "--now" in argv:
                state["active"] = False
        elif operation == "enable":
            state["enabled"] = True
        elif operation == "start":
            state["active"] = True
        elif operation == "stop":
            state["active"] = False

    monkeypatch.setitem(globals_, "run_command", run_step)
    disable({"local_inference_before": prior})
    assert state == {"loaded": True, "active": False, "enabled": False}
    assert ("disable", "--now", "ollama.service") in steps

    restore({"local_inference_before": prior})
    assert state == prior["ollama.service"]
    assert ("enable", "ollama.service") in steps
    assert ("stop", "ollama.service") in steps

    worker = WORKER_PATH.read_text(encoding="utf-8")
    dispatcher = DISPATCHER_PATH.read_text(encoding="utf-8")
    assert '"local_inference_before": local_inference_before' in worker
    assert "restore_local_inference(metadata)" in worker
    assert '"local_inference": _local_inference_status()' in dispatcher
    assert Path("/var/lib/ollama") not in namespace["MANAGED_BACKUP_PATHS"]


def test_apply_always_rolls_back_and_erases_password_on_install_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    apply_release = namespace["apply"]
    globals_ = apply_release.__globals__
    password = tmp_path / "zulip-owner-password"
    password.write_text("never-inspected-by-this-test", encoding="utf-8")
    backup = tmp_path / "backup"
    backup.mkdir()
    transaction = {
        "transaction_id": "transaction-1",
        "release_id": manifest()["release_id"],
    }
    rollbacks: list[str] = []

    monkeypatch.setitem(globals_, "ZULIP_PASSWORD_FILE", password)
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    monkeypatch.setitem(globals_, "recover_interrupted_transaction", lambda: None)
    monkeypatch.setitem(globals_, "preflight", lambda *_args, **_kwargs: {})
    monkeypatch.setitem(globals_, "current_is_healthy", lambda *_args: False)
    monkeypatch.setitem(globals_, "stage_release", lambda *_args: tmp_path / "release")
    monkeypatch.setitem(globals_, "capture_backup", lambda *_args: (backup, transaction))
    monkeypatch.setitem(globals_, "atomic_json", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(globals_, "append_audit", lambda *_args: None)

    def fail_install(*_args: object) -> None:
        raise namespace["DeploymentError"]("synthetic_failure", "synthetic")

    monkeypatch.setitem(globals_, "install_release", fail_install)
    monkeypatch.setitem(
        globals_,
        "rollback_transaction",
        lambda *_args: rollbacks.append("completed"),
    )
    with pytest.raises(namespace["DeploymentError"]) as raised:
        apply_release(manifest())
    assert raised.value.code == "synthetic_failure"
    assert rollbacks == ["completed"]
    assert not password.exists()


def test_apply_erases_one_shot_password_when_preflight_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    apply_release = namespace["apply"]
    globals_ = apply_release.__globals__
    password = tmp_path / "zulip-owner-password"
    password.write_text("never-inspected-by-this-test", encoding="utf-8")
    monkeypatch.setitem(globals_, "ZULIP_PASSWORD_FILE", password)
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    monkeypatch.setitem(globals_, "recover_interrupted_transaction", lambda: None)
    monkeypatch.setitem(globals_, "current_is_healthy", lambda *_args: False)

    def fail_preflight(*_args: object, **_kwargs: object) -> None:
        raise namespace["DeploymentError"]("preflight_failed", "synthetic")

    monkeypatch.setitem(globals_, "preflight", fail_preflight)
    with pytest.raises(namespace["DeploymentError"]) as raised:
        apply_release(manifest())
    assert raised.value.code == "preflight_failed"
    assert not password.exists()


def test_apply_recovers_an_interrupted_transaction_before_requiring_bootstrap_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    apply_release = namespace["apply"]
    globals_ = apply_release.__globals__
    password = tmp_path / "zulip-owner-password"
    order: list[str] = []

    monkeypatch.setitem(globals_, "ZULIP_PASSWORD_FILE", password)
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    monkeypatch.setitem(
        globals_,
        "recover_interrupted_transaction",
        lambda: order.append("recover"),
    )
    monkeypatch.setitem(
        globals_,
        "current_is_healthy",
        lambda *_args: order.append("health") or False,
    )

    def reject_missing_bootstrap(*_args: object, **_kwargs: object) -> None:
        order.append("preflight")
        raise namespace["DeploymentError"]("preflight_failed", "synthetic")

    monkeypatch.setitem(globals_, "preflight", reject_missing_bootstrap)
    with pytest.raises(namespace["DeploymentError"]) as raised:
        apply_release(manifest())
    assert raised.value.code == "preflight_failed"
    assert order == ["recover", "health", "preflight"]


def test_interrupted_transaction_is_rolled_back_before_reapply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    recover = namespace["recover_interrupted_transaction"]
    globals_ = recover.__globals__
    current_path = tmp_path / "current.json"
    current_path.write_text("{}", encoding="utf-8")
    backup = tmp_path / "backup"
    metadata = {
        "release_id": "prior-release",
        "transaction_id": "prior-transaction",
    }
    events: list[str] = []
    monkeypatch.setitem(globals_, "CURRENT_PATH", current_path)
    monkeypatch.setitem(
        globals_,
        "read_json",
        lambda *_args, **_kwargs: {"status": "deploying"},
    )
    monkeypatch.setitem(globals_, "latest_transaction", lambda: (backup, metadata))
    monkeypatch.setitem(
        globals_,
        "rollback_transaction",
        lambda *_args: events.append("rollback"),
    )
    monkeypatch.setitem(
        globals_,
        "atomic_json",
        lambda *_args, **_kwargs: events.append("state"),
    )
    monkeypatch.setitem(globals_, "append_audit", lambda *_args: events.append("audit"))
    recover()
    assert events == ["rollback", "state", "audit"]


def test_explicit_rollback_reproves_a_terminal_rollback_and_syncs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    explicit_rollback = namespace["explicit_rollback"]
    globals_ = explicit_rollback.__globals__
    backup = tmp_path / "backup"
    metadata = {
        "release_id": manifest()["release_id"],
        "hermes_source_archive_sha256": manifest()["artifacts"][
            "hermes_source_archive"
        ]["sha256"],
        "hermes_transaction_temp_paths": [
            str(path) for path in namespace["HERMES_TRANSACTION_TEMP_PATHS"]
        ],
        "transaction_id": "already-rolled-back-transaction",
        "rolled_back": True,
    }
    events: list[str] = []

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setitem(globals_, "latest_transaction", lambda: (backup, metadata))
    monkeypatch.setitem(
        globals_,
        "_sync_rollback_filesystems",
        lambda: events.append("synced-filesystems"),
    )

    def verify_terminal(_metadata: dict[str, object]) -> None:
        events.append("verified-terminal")
        globals_["_sync_rollback_filesystems"]()

    monkeypatch.setitem(globals_, "verify_terminal_rollback", verify_terminal)
    monkeypatch.setitem(
        globals_, "atomic_json", lambda *_args, **_kwargs: events.append("current")
    )
    monkeypatch.setitem(globals_, "local_inference_status", lambda: "disabled")

    result = explicit_rollback(manifest())

    assert result["status"] == "already-rolled-back"
    assert result["transaction_id"] == metadata["transaction_id"]
    assert events == ["verified-terminal", "synced-filesystems", "current"]


def test_explicit_rollback_refuses_terminal_state_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    explicit_rollback = namespace["explicit_rollback"]
    globals_ = explicit_rollback.__globals__
    metadata = {
        "release_id": manifest()["release_id"],
        "hermes_source_archive_sha256": manifest()["artifacts"][
            "hermes_source_archive"
        ]["sha256"],
        "hermes_transaction_temp_paths": [
            str(path) for path in namespace["HERMES_TRANSACTION_TEMP_PATHS"]
        ],
        "transaction_id": "drifted-terminal-transaction",
        "rolled_back": True,
    }
    current_writes: list[object] = []

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setitem(
        globals_, "latest_transaction", lambda: (tmp_path / "backup", metadata)
    )

    def reject_drift(_metadata: dict[str, object]) -> None:
        raise namespace["DeploymentError"](
            "rollback_failed", "terminal rollback state differs"
        )

    monkeypatch.setitem(globals_, "verify_terminal_rollback", reject_drift)
    monkeypatch.setitem(
        globals_, "atomic_json", lambda *args, **_kwargs: current_writes.append(args)
    )

    with pytest.raises(namespace["DeploymentError"], match="terminal rollback state differs"):
        explicit_rollback(manifest())
    assert current_writes == []


def test_interrupted_snapshot_never_extracts_a_partial_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    rollback = namespace["rollback_transaction"]
    globals_ = rollback.__globals__
    steps: list[str] = []
    metadata = {
        "phase": "snapshotting",
        "rolled_back": False,
        "release_id": manifest()["release_id"],
        "hermes_source_archive_sha256": namespace[
            "HERMES_SOURCE_ARCHIVE_SHA256"
        ],
        "hermes_transaction_temp_paths": [
            str(path) for path in namespace["HERMES_TRANSACTION_TEMP_PATHS"]
        ],
        "transaction_id": "snapshot-transaction",
        "service_state": {},
    }
    monkeypatch.setitem(
        globals_, "restore_service_state", lambda _state: steps.append("services")
    )
    monkeypatch.setitem(globals_, "restore_docker", lambda _state: steps.append("docker"))
    monkeypatch.setitem(
        globals_, "restore_local_inference", lambda _state: steps.append("local-inference")
    )
    monkeypatch.setitem(
        globals_, "restore_finance_identity", lambda _state: steps.append("finance-identity")
    )
    monkeypatch.setitem(
        globals_, "verify_service_state", lambda _state: steps.append("verified-services")
    )
    monkeypatch.setitem(
        globals_, "_sync_rollback_filesystems", lambda: steps.append("synced-filesystems")
    )
    monkeypatch.setitem(
        globals_, "verify_terminal_rollback", lambda _metadata: steps.append("verified-terminal")
    )
    monkeypatch.setitem(globals_, "atomic_json", lambda *_args, **_kwargs: steps.append("state"))
    monkeypatch.setitem(globals_, "append_audit", lambda *_args: steps.append("audit"))
    monkeypatch.setitem(
        globals_,
        "run_command",
        lambda name, *_args, **_kwargs: steps.append(name),
    )

    rollback(tmp_path, metadata)
    assert steps == [
        "services",
        "docker",
        "local-inference",
        "finance-identity",
        "verified-services",
        "synced-filesystems",
        "verified-terminal",
        "state",
        "audit",
    ]
    assert metadata["phase"] == "rolled-back"
    assert metadata["rolled_back"] is True
    assert "restore-managed-files" not in steps
    assert "rollback-zulip-data" not in steps


def test_rollback_restores_active_services_in_dependency_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    restore = namespace["restore_service_state"]
    globals_ = restore.__globals__
    service_names = namespace["SERVICE_NAMES"]
    snapshot = {
        unit: {"active": True, "enabled": True}
        for unit in service_names
    }
    steps: list[str] = []
    monkeypatch.setattr(
        globals_["subprocess"],
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"loaded\n"
        ),
    )
    monkeypatch.setitem(
        globals_,
        "run_command",
        lambda name, *_args, **_kwargs: steps.append(name),
    )
    restore(snapshot)
    starts = [step.removeprefix("restore-start-") for step in steps if step.startswith("restore-start-")]
    assert starts == [
        unit for unit in service_names
        if unit not in namespace["DYNAMIC_SERVICE_NAMES"]
    ]


def test_dynamic_query_active_state_is_not_rollback_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    verify = namespace["verify_service_state"]
    globals_ = verify.__globals__
    dynamic = namespace["DYNAMIC_SERVICE_NAMES"][0]
    snapshot = {
        unit: {"active": unit != dynamic, "enabled": True}
        for unit in namespace["SERVICE_NAMES"]
    }

    def state(unit: str, operation: str) -> bool:
        if operation == "is-enabled":
            return True
        if unit == dynamic:
            return True  # inverse of the captured idle state is legitimate
        return snapshot[unit]["active"]

    monkeypatch.setitem(globals_, "systemctl_state", state)
    verify(snapshot)


def test_health_accepts_idle_query_proxy_and_requires_functional_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    health = namespace["health_check"]
    globals_ = health.__globals__
    document = manifest()
    release = tmp_path / "release"
    release.mkdir()
    commands: list[str] = []

    monkeypatch.setitem(globals_, "local_inference_status", lambda: "disabled")
    monkeypatch.setitem(
        globals_, "validate_docker_installation", lambda **_kwargs: "active"
    )
    monkeypatch.setitem(globals_, "validate_docker_runtime_network", lambda: None)
    monkeypatch.setitem(globals_, "validate_staged_release", lambda _manifest: release)
    monkeypatch.setitem(
        globals_, "source_tree_digest",
        lambda *_args: document["source"]["tree_sha256"],
    )
    monkeypatch.setitem(
        globals_, "atlas_artifact_in_release", lambda *_args: release / "atlas.rpm"
    )
    monkeypatch.setitem(
        globals_, "sha256_regular",
        lambda _path: document["artifacts"]["atlas_rpm"]["sha256"],
    )
    monkeypatch.setitem(
        globals_, "validate_hermes_source_archive", lambda *_args, **_kwargs: None
    )
    monkeypatch.setitem(
        globals_, "systemctl_state",
        lambda unit, operation: (
            operation == "is-enabled" and unit == "zulip-alertmanager-query.socket"
        )
        or (
            operation == "is-active"
            and unit not in namespace["TRANSIENT_SERVICE_NAMES"]
            and unit not in namespace["ALL_TRANSIENT_UNITS"]
            and unit not in namespace["DYNAMIC_SERVICE_NAMES"]
        ),
    )
    monkeypatch.setitem(globals_, "validate_finance_socket", lambda: None)
    monkeypatch.setitem(globals_, "validate_alertmanager_query_socket", lambda: None)
    monkeypatch.setitem(
        globals_, "run_command", lambda name, *_args, **_kwargs: commands.append(name)
    )
    atlas = document["artifacts"]["atlas_rpm"]
    monkeypatch.setitem(
        globals_, "atlas_installed_identity",
        lambda: f"{atlas['name']}-{atlas['version']}-{atlas['release']}.{atlas['architecture']}",
    )
    monkeypatch.setitem(globals_, "atlas_payload_sha256", lambda *_args: "a" * 64)

    health(document)
    assert "health-alertmanager-query" in commands

    def reject_query(name: str, *_args: object, **_kwargs: object) -> None:
        if name == "health-alertmanager-query":
            raise namespace["DeploymentError"](
                "health_failed", "synthetic query failure"
            )

    monkeypatch.setitem(globals_, "run_command", reject_query)
    with pytest.raises(namespace["DeploymentError"], match="synthetic query failure"):
        health(document)


def test_network_health_does_not_require_source_checkout(monkeypatch, capsys):
    namespace = worker_namespace()
    environment = namespace['main'].__globals__
    checked = []
    def unavailable_manifest(*args, **kwargs):
        raise PermissionError('source checkout is hidden by ProtectHome')
    monkeypatch.setitem(environment, 'load_manifest', unavailable_manifest)
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setitem(environment, 'validate_docker_installation', lambda **kw: checked.append(('installation', kw)))
    monkeypatch.setitem(environment, 'validate_docker_runtime_network', lambda: checked.append(('network', {})))
    assert namespace['main'](['network-health']) == 0
    assert checked == [('installation', {'require_active': True}), ('network', {})]
    assert json.loads(capsys.readouterr().out) == {'ok': True, 'status': 'healthy'}


def test_network_health_still_rejects_runtime_policy_violation(monkeypatch, capsys):
    namespace = worker_namespace()
    environment = namespace['main'].__globals__
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setitem(environment, 'validate_docker_installation', lambda **kw: None)
    def reject_network():
        raise namespace['DeploymentError']('docker_network_conflict', 'unreviewed network')
    monkeypatch.setitem(environment, 'validate_docker_runtime_network', reject_network)
    assert namespace['main'](['network-health']) == 1
    result = json.loads(capsys.readouterr().err)
    assert result['ok'] is False
    assert result['error'] == 'docker_network_conflict'
