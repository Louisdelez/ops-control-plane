from __future__ import annotations

import contextlib
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys

import pytest

from ops_broker.policy import ActionPolicy, RBACPolicy
from ops_broker.runbooks import ExecutablePolicy, RunbookRegistry


CONTROL_PLANE = Path(__file__).resolve().parents[2]
DEPLOY = CONTROL_PLANE / "broker" / "deploy"


@pytest.mark.parametrize(
    ("helper", "argument", "error"),
    [
        ("service-status", "evil;id", "unit_not_allowed"),
        ("restart-service", "../ops-broker.service", "unit_not_allowed"),
        ("nvidia-package-remediation", "install-anything", "operation_not_allowed"),
        ("minecraft-crash-triage", "../survival", "instance_not_allowed"),
    ],
)
def test_helpers_reject_non_allowlisted_arguments(
    helper: str,
    argument: str,
    error: str,
) -> None:
    completed = subprocess.run(
        [sys.executable, str(DEPLOY / "helpers" / helper), argument],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 64
    assert error in completed.stdout


def test_production_api_uses_private_systemd_socket() -> None:
    socket_unit = (DEPLOY / "systemd" / "ops-broker.socket").read_text(encoding="utf-8")
    service_unit = (DEPLOY / "systemd" / "ops-broker.service").read_text(encoding="utf-8")
    assert "ListenStream=/run/ops-broker/api.sock" in socket_unit
    assert "SocketMode=0660" in socket_unit
    assert "SocketGroup=opsbroker-api" in socket_unit
    assert "User=opsbroker" in service_unit
    assert "Environment=OPS_BROKER_FD=3" in service_unit
    assert "Environment=OPS_BROKER_API_MODE=approvals-only" in service_unit
    assert "127.0.0.1" not in service_unit
    assert "8788" not in service_unit
    environment = (DEPLOY / "broker.env").read_text(encoding="utf-8")
    assert "OPS_BROKER_API_MODE=approvals-only" in environment


def test_backup_metric_has_constrained_root_publisher() -> None:
    backup_unit = (DEPLOY / "systemd" / "ops-broker-backup.service").read_text(
        encoding="utf-8"
    )
    success_unit = (
        DEPLOY / "systemd" / "ops-broker-backup-metric-success.service"
    ).read_text(encoding="utf-8")
    metric_helper = (DEPLOY / "helpers" / "ops-broker-backup-metric").read_text(
        encoding="utf-8"
    )
    assert "OnSuccess=ops-broker-backup-metric-success.service" in backup_unit
    assert "OnFailure=ops-broker-backup-metric-failure.service" in backup_unit
    assert "User=root" in success_unit
    assert "Group=node-exporter" in success_unit
    assert "CapabilityBoundingSet=" in success_unit
    assert "ReadWritePaths=/var/lib/node-exporter/textfile" in success_unit
    assert "InaccessiblePaths=-/var/lib/ops-broker -/var/backups/ops-broker" in success_unit
    assert "/var/lib/node-exporter/textfile/broker-backup.prom" in metric_helper
    assert "ops_broker_backup_last_success_timestamp_seconds" in metric_helper
    assert "ops_broker_backup_last_verify_ok" in metric_helper
    assert "os.chmod(temporary, 0o640)" in metric_helper


def test_backup_metric_preserves_last_success_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(DEPLOY / "helpers" / "ops-broker-backup-metric"))
    main = namespace["main"]
    destination = tmp_path / "textfile" / "broker-backup.prom"
    destination.parent.mkdir()
    main.__globals__["DESTINATION"] = destination

    monkeypatch.setattr(sys, "argv", ["ops-broker-backup-metric", "success"])
    assert main() == 0
    success = destination.read_text(encoding="ascii")
    timestamp_line = next(
        line
        for line in success.splitlines()
        if line.startswith("ops_broker_backup_last_success_timestamp_seconds ")
    )
    assert "ops_broker_backup_last_verify_ok 1" in success

    monkeypatch.setattr(sys, "argv", ["ops-broker-backup-metric", "failure"])
    assert main() == 0
    failure = destination.read_text(encoding="ascii")
    assert timestamp_line in failure
    assert "ops_broker_backup_last_verify_ok 0" in failure


def test_api_group_membership_is_limited_to_bridge() -> None:
    sysusers = (DEPLOY / "ops-broker.sysusers").read_text(encoding="utf-8").splitlines()
    memberships = [line.split() for line in sysusers if line.startswith("m ")]
    assert memberships == [["m", "zulipbridge", "opsbroker-api"]]
    installer = (CONTROL_PLANE / "scripts" / "install-broker.sh").read_text(
        encoding="utf-8"
    )
    assert "Unexpected supplementary member in opsbroker-api" in installer
    assert "is a primary group for" in installer


def test_sudoers_assets_parse_when_visudo_is_available() -> None:
    visudo = shutil.which("visudo")
    if visudo is None:
        pytest.skip("visudo is unavailable")
    for policy in sorted((DEPLOY / "sudoers").iterdir()):
        completed = subprocess.run(
            [visudo, "-cf", str(policy)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr


def test_supervised_clients_have_distinct_fixed_identities() -> None:
    codex = (DEPLOY / "helpers" / "ops-broker-mcp-codex").read_text(encoding="utf-8")
    claude = (DEPLOY / "helpers" / "ops-broker-mcp-claude").read_text(encoding="utf-8")
    policy = (CONTROL_PLANE / "broker" / "config" / "rbac.yaml").read_text(
        encoding="utf-8"
    )
    assert "OPS_BROKER_MCP_ACTOR_ID=codex-supervised" in codex
    assert "OPS_BROKER_MCP_ACTOR_ID=claude-supervised" in claude
    assert "  codex-supervised:\n" in policy
    assert "  claude-supervised:\n" in policy


def test_nvidia_remediation_runbook_is_exact_class_c() -> None:
    executable_policy = ExecutablePolicy.load(
        CONTROL_PLANE / "broker" / "config" / "executables.yaml"
    )
    registry = RunbookRegistry.load(CONTROL_PLANE / "runbooks", executable_policy)
    runbook = registry.get("local.nvidia-package-remediation.v1")
    assert runbook.action_class == "C"
    assert runbook.timeout_seconds == 25
    assert runbook.parameters["operation"].values == ("ensure-580xx",)
    assert runbook.render_argv({"operation": "ensure-580xx"}) == (
        "/usr/bin/sudo",
        "-n",
        "/usr/local/libexec/ops-runbooks/nvidia-package-remediation",
        "ensure-580xx",
    )
    status_runbook = registry.get("local.nvidia-package-remediation-status.v1")
    assert status_runbook.action_class == "A"
    assert status_runbook.timeout_seconds == 10
    assert status_runbook.render_argv({}) == (
        "/usr/bin/sudo",
        "-n",
        "/usr/local/libexec/ops-runbooks/nvidia-package-remediation",
        "status",
    )

    rbac = RBACPolicy.load(CONTROL_PLANE / "broker" / "config" / "rbac.yaml")
    assert rbac.can_runbook(
        "codex-supervised",
        "infra-shared",
        "local.nvidia-package-remediation.v1",
        "C",
    )
    assert rbac.can_runbook(
        "codex-supervised",
        "infra-shared",
        "local.nvidia-package-remediation-status.v1",
        "A",
    )
    action_policy = ActionPolicy.load(CONTROL_PLANE / "policies" / "actions.yaml")
    assert action_policy.classes["C"].approval == "human_required"
    assert action_policy.approval_actor_ids == frozenset()

    sudoers = (
        DEPLOY / "sudoers" / "ops-broker-nvidia-package-remediation"
    ).read_text(encoding="utf-8")
    assert "/usr/local/libexec/ops-runbooks/nvidia-package-remediation ensure-580xx" in sudoers
    assert "/usr/local/libexec/ops-runbooks/nvidia-package-remediation status" in sudoers
    assert "/usr/bin/dnf" not in sudoers

    installer = (CONTROL_PLANE / "scripts" / "install-broker.sh").read_text(
        encoding="utf-8"
    )
    assert '"$broker_source/deploy/helpers/nvidia-package-remediation"' in installer
    assert '"$broker_source/deploy/helpers/nvidia-package-remediation-worker"' in installer
    assert '"$broker_source/deploy/sudoers/ops-broker-nvidia-package-remediation"' in installer

    broker_unit = (DEPLOY / "systemd" / "ops-broker.service").read_text(encoding="utf-8")
    remediation_unit = (
        DEPLOY / "systemd" / "ops-nvidia-package-remediation.service"
    ).read_text(encoding="utf-8")
    assert "ProtectSystem=strict" in broker_unit
    assert "ReadWritePaths=/var/lib/ops-broker -/run/sudo -/run/lock" in broker_unit
    assert "User=root" in remediation_unit
    assert (
        "ExecStartPre=/usr/bin/rm -f "
        "/run/ops-nvidia-package-remediation/result.json"
    ) in remediation_unit
    assert (
        "ExecStart=/usr/bin/systemd-inhibit --mode=block "
        "--what=idle:sleep:shutdown --who=ops-nvidia-remediation "
        "--why=bounded-nvidia-580xx-package-transaction "
        "/usr/local/libexec/ops-runbooks/"
        "nvidia-package-remediation-worker ensure-580xx"
    ) in remediation_unit
    assert "TimeoutStartSec=55min" in remediation_unit
    assert "TimeoutStopSec=15min" in remediation_unit


def test_nvidia_readiness_bootstrap_uses_a_root_owned_commit_snapshot() -> None:
    installer = (
        CONTROL_PLANE / "scripts" / "install-nvidia-reboot-readiness.sh"
    ).read_text(encoding="utf-8")

    assert "git_as_source_user()" in installer
    assert "/usr/bin/runuser -u ops-user -- /usr/bin/env -i" in installer
    assert "GIT_NO_REPLACE_OBJECTS=1" in installer
    assert "-c core.fsmonitor=false" in installer
    assert "-c core.hooksPath=/dev/null" in installer
    assert "-c tar.tar.command=/usr/bin/cat" in installer
    assert "safe.directory" not in installer
    assert 'archive --format=tar "$source_commit"' in installer
    assert 'staging_dir=$(mktemp -d /var/tmp/ops-nvidia-readiness.' in installer
    assert 'repository_dir="$staging_dir/tree"' in installer
    assert 'ls-tree -r -z --full-tree "$source_commit"' in installer
    assert "content mismatch in deployment snapshot" in installer
    assert "--untracked-files=all" in installer
    assert "/usr/local/libexec/ops-runbooks/nvidia-package-remediation-worker" in installer
    assert "/etc/sudoers.d/ops-broker-nvidia-package-remediation" in installer


def _nvidia_helper() -> dict[str, object]:
    return runpy.run_path(str(DEPLOY / "helpers" / "nvidia-package-remediation"))


def _nvidia_worker() -> dict[str, object]:
    return runpy.run_path(
        str(DEPLOY / "helpers" / "nvidia-package-remediation-worker")
    )


def _ready_nvidia_state() -> dict[str, bool]:
    return {
        "legacy_subpackages_present": False,
        "loaded_module_current": True,
        "packages_ready": True,
        "module_ready": True,
        "module_signed": True,
        "module_signer_matches": True,
        "module_sig_key_matches": True,
    }


def _ready_nvidia_payload(*, changed: bool = False) -> dict[str, object]:
    return {
        "changed": changed,
        "mok_enrolled": True,
        "module_sig_key_matches": True,
        "module_signer_matches": True,
        "module_signed": True,
        "operation": "ensure-580xx",
        "os": "fedora-44",
        "packages_ready": True,
        "pci_device": "10de:1c8d",
        "reboot_required": changed,
        "secure_boot_enabled": True,
        "status": "ready",
    }


def test_nvidia_dispatcher_cleans_and_queues_only_the_fixed_root_unit() -> None:
    namespace = _nvidia_helper()
    queue = namespace["queue_remediation"]
    globals_ = queue.__globals__
    commands: list[tuple[tuple[str, ...], int, bool]] = []

    class Completed:
        returncode = 0
        stdout = ""

    def record(argv: object, *, timeout: int, capture: bool = False) -> Completed:
        commands.append((tuple(argv), timeout, capture))  # type: ignore[arg-type]
        return Completed()

    globals_["operation_lock"] = contextlib.nullcontext
    globals_["_unit_state"] = lambda: {
        "ActiveState": "inactive",
        "SubState": "dead",
        "Result": "success",
    }
    globals_["run_command"] = record

    assert queue() == {
        "operation": "ensure-580xx",
        "status": "queued",
        "unit": "ops-nvidia-package-remediation.service",
    }
    assert commands == [
        (
            (
                "/usr/bin/systemctl",
                "clean",
                "--what=runtime",
                "ops-nvidia-package-remediation.service",
            ),
            5,
            False,
        ),
        (
            (
                "/usr/bin/systemctl",
                "--no-block",
                "start",
                "ops-nvidia-package-remediation.service",
            ),
            5,
            False,
        ),
    ]
    dispatcher = (DEPLOY / "helpers" / "nvidia-package-remediation").read_text(
        encoding="utf-8"
    )
    assert "/usr/bin/dnf5" not in dispatcher
    assert "RESULT_PATH.unlink" not in dispatcher


def test_nvidia_dispatcher_does_not_requeue_a_running_unit() -> None:
    namespace = _nvidia_helper()
    queue = namespace["queue_remediation"]
    globals_ = queue.__globals__
    globals_["operation_lock"] = contextlib.nullcontext
    globals_["_unit_state"] = lambda: {
        "ActiveState": "activating",
        "SubState": "start",
        "Result": "success",
    }
    globals_["run_command"] = lambda *_args, **_kwargs: pytest.fail(
        "a running unit must not be cleaned or requeued"
    )

    assert queue() == {
        "operation": "ensure-580xx",
        "status": "running",
        "unit": "ops-nvidia-package-remediation.service",
    }


def test_nvidia_status_ignores_preserved_result_while_unit_runs() -> None:
    namespace = _nvidia_helper()
    status = namespace["remediation_status"]
    globals_ = status.__globals__
    globals_["_unit_state"] = lambda: {
        "ActiveState": "active",
        "SubState": "running",
        "Result": "success",
    }
    globals_["_validated_result"] = lambda: pytest.fail(
        "preserved output must not be read from a running unit"
    )

    assert status() == (
        0,
        {
            "operation": "status",
            "status": "running",
            "unit": "ops-nvidia-package-remediation.service",
        },
    )


def test_nvidia_status_validates_and_relays_final_result(tmp_path: Path) -> None:
    namespace = _nvidia_helper()
    status = namespace["remediation_status"]
    globals_ = status.__globals__
    result_path = tmp_path / "result.json"
    payload = _ready_nvidia_payload()
    result_path.write_text(json.dumps(payload), encoding="utf-8")
    globals_["RESULT_PATH"] = result_path
    globals_["RESULT_OWNER_UID"] = result_path.stat().st_uid
    globals_["_unit_state"] = lambda: {
        "ActiveState": "inactive",
        "SubState": "dead",
        "Result": "success",
    }

    assert status() == (0, payload)


def test_nvidia_worker_is_idempotent_and_emits_bounded_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    namespace = _nvidia_worker()
    ensure = namespace["ensure_580xx"]
    globals_ = ensure.__globals__
    commands: list[tuple[str, ...]] = []
    globals_["inspect_state"] = lambda _kernel, _signer: _ready_nvidia_state()
    globals_["run_command"] = lambda argv, **_kwargs: commands.append(tuple(argv))

    assert ensure("test-kernel", "test-signer") == (False, _ready_nvidia_state())
    assert commands == []

    result_path = tmp_path / "runtime" / "result.json"
    result_path.parent.mkdir()
    emit = namespace["emit"]
    emit.__globals__["RESULT_PATH"] = result_path
    emit.__globals__["RESULT_OWNER_UID"] = result_path.parent.stat().st_uid
    payload = _ready_nvidia_payload()
    assert emit(payload) is True
    assert json.loads(capsys.readouterr().out) == payload
    assert json.loads(result_path.read_text(encoding="utf-8")) == payload
    assert len(result_path.read_bytes()) < 1024


def test_nvidia_worker_uses_proven_fixed_transactions_with_long_timeouts() -> None:
    namespace = _nvidia_worker()
    ensure = namespace["ensure_580xx"]
    globals_ = ensure.__globals__
    states = iter(
        [
            {
                "legacy_subpackages_present": True,
                "loaded_module_current": False,
                "packages_ready": False,
                "module_ready": False,
                "module_signed": False,
                "module_signer_matches": False,
                "module_sig_key_matches": False,
            },
        ]
    )
    commands: list[tuple[tuple[str, ...], int]] = []

    class Completed:
        returncode = 0

    def record(argv: object, *, timeout: int, **_kwargs: object) -> Completed:
        commands.append((tuple(argv), timeout))  # type: ignore[arg-type]
        return Completed()

    globals_["inspect_state"] = lambda _kernel, _signer: next(states)
    waits = iter([None, _ready_nvidia_state()])
    globals_["wait_for_ready"] = lambda *_args: next(waits)
    globals_["run_command"] = record

    assert ensure("test-kernel", "test-signer") == (True, _ready_nvidia_state())
    assert commands == [
        (
            (
                "/usr/bin/dnf5",
                "--assumeyes",
                "remove",
                "--no-autoremove",
                "xorg-x11-drv-nvidia-libs",
                "xorg-x11-drv-nvidia-cuda-libs",
                "xorg-x11-drv-nvidia-kmodsrc",
            ),
            600,
        ),
        (
            (
                "/usr/bin/dnf5",
                "--assumeyes",
                "install",
                "--allowerasing",
                "akmod-nvidia-580xx",
                "xorg-x11-drv-nvidia-580xx-cuda",
            ),
            600,
        ),
        (
            (
                "/usr/bin/akmods",
                "--force",
                "--rebuild",
                "--kernels",
                "test-kernel",
                "--akmod",
                "nvidia-580xx",
            ),
            600,
        ),
    ]


def test_nvidia_worker_skips_empty_legacy_removal_and_waits_for_posttrans() -> None:
    namespace = _nvidia_worker()
    ensure = namespace["ensure_580xx"]
    globals_ = ensure.__globals__
    initial = {
        "legacy_subpackages_present": False,
        "loaded_module_current": False,
        "packages_ready": False,
        "module_ready": False,
        "module_signed": False,
        "module_signer_matches": False,
        "module_sig_key_matches": False,
    }
    commands: list[tuple[tuple[str, ...], int]] = []

    class Completed:
        returncode = 0

    def record(argv: object, *, timeout: int, **_kwargs: object) -> Completed:
        commands.append((tuple(argv), timeout))  # type: ignore[arg-type]
        return Completed()

    globals_["inspect_state"] = lambda _kernel, _signer: initial
    globals_["wait_for_ready"] = lambda *_args: _ready_nvidia_state()
    globals_["run_command"] = record

    assert ensure("test-kernel", "test-signer") == (True, _ready_nvidia_state())
    assert commands == [
        (
            (
                "/usr/bin/dnf5",
                "--assumeyes",
                "install",
                "--allowerasing",
                "akmod-nvidia-580xx",
                "xorg-x11-drv-nvidia-580xx-cuda",
            ),
            600,
        )
    ]


def test_nvidia_worker_retries_akmods_a_bounded_number_of_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _nvidia_worker()
    ensure = namespace["ensure_580xx"]
    globals_ = ensure.__globals__
    initial = {
        "legacy_subpackages_present": False,
        "loaded_module_current": False,
        "packages_ready": False,
        "module_ready": False,
        "module_signed": False,
        "module_signer_matches": False,
        "module_sig_key_matches": False,
    }
    commands: list[tuple[tuple[str, ...], int]] = []
    returncodes = iter([0, 1, 0])

    class Completed:
        def __init__(self, returncode: int) -> None:
            self.returncode = returncode

    def record(argv: object, *, timeout: int, **_kwargs: object) -> Completed:
        commands.append((tuple(argv), timeout))  # type: ignore[arg-type]
        return Completed(next(returncodes))

    globals_["inspect_state"] = lambda _kernel, _signer: initial
    waits = iter([None, None, _ready_nvidia_state()])
    globals_["wait_for_ready"] = lambda *_args: next(waits)
    sleeps: list[int] = []
    monkeypatch.setattr(globals_["time"], "sleep", sleeps.append)
    globals_["run_command"] = record

    assert ensure("test-kernel", "test-signer") == (True, _ready_nvidia_state())
    akmods_commands = [
        command for command in commands if command[0][0] == "/usr/bin/akmods"
    ]
    assert len(akmods_commands) == 2
    assert all(timeout == 600 for _command, timeout in akmods_commands)
    assert sleeps == [10]


def test_nvidia_worker_requires_enrolled_mok_with_secure_boot(tmp_path: Path) -> None:
    namespace = _nvidia_worker()
    validate_trust = namespace["validate_secure_boot_trust"]
    globals_ = validate_trust.__globals__
    efivars = tmp_path / "efivars"
    efivars.mkdir()
    (efivars / "SecureBoot-test-guid").write_bytes(b"\x06\x00\x00\x00\x01")
    certificate = tmp_path / "public_key.der"
    calls: list[tuple[tuple[str, ...], int, bool]] = []

    class Completed:
        def __init__(self, returncode: int, stdout: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout

    def record(
        argv: object,
        *,
        timeout: int,
        capture: bool = False,
    ) -> Completed:
        command = tuple(argv)  # type: ignore[arg-type]
        calls.append((command, timeout, capture))
        if command[0] == "/usr/bin/openssl":
            if "-serial" in command:
                return Completed(
                    0,
                    "serial=710DA2B543118EC0EB68EF32E7E7E1DF34CB6EF4\n",
                )
            if "-ext" in command:
                return Completed(
                    0,
                    "X509v3 Subject Key Identifier:\n"
                    "    EC:2B:D7:A9:14:36:5F:30:1A:BF:59:B7:2C:2A:82:7B:BD:95:6A:D1\n",
                )
            return Completed(0, "subject=CN=fedora_test_mok\n")
        return Completed(0)

    globals_["SECURE_BOOT_EFIVARS"] = efivars
    globals_["AKMOD_CERTIFICATE"] = certificate
    globals_["run_command"] = record

    trust = validate_trust()
    assert trust.common_name == "fedora_test_mok"
    assert (
        trust.certificate_serial
        == "71:0D:A2:B5:43:11:8E:C0:EB:68:EF:32:E7:E7:E1:DF:34:CB:6E:F4"
    )
    assert calls == [
        (
            (
                "/usr/bin/openssl",
                "x509",
                "-inform",
                "DER",
                "-in",
                str(certificate),
                "-noout",
                "-subject",
                "-nameopt",
                "RFC2253",
            ),
            10,
            True,
        ),
        (
            (
                "/usr/bin/openssl",
                "x509",
                "-inform",
                "DER",
                "-in",
                str(certificate),
                "-noout",
                "-serial",
            ),
            10,
            True,
        ),
        (("/usr/bin/mokutil", "--test-key", str(certificate)), 10, False),
    ]


def test_nvidia_worker_rejects_matching_cn_with_nonmatching_certificate_serial() -> None:
    namespace = _nvidia_worker()
    inspect = namespace["inspect_state"]
    globals_ = inspect.__globals__
    certificate_serial = (
        "71:0D:A2:B5:43:11:8E:C0:EB:68:EF:32:E7:E7:E1:DF:34:CB:6E:F4"
    )
    target_packages = set(namespace["TARGET_PACKAGES"])
    globals_["_package_versions"] = lambda package: (
        ("580.1",) if package in target_packages else ()
    )
    module_fields = {
        "version": "580.1",
        "signer": "fedora_test_mok",
        "sig_key": certificate_serial.replace("71", "72", 1),
    }
    globals_["_module_field"] = lambda _kernel, field: module_fields[field]
    globals_["_loaded_module_version"] = lambda: "580.1"
    globals_["_nouveau_module_loaded"] = lambda: True
    trust = namespace["SigningTrust"]("fedora_test_mok", certificate_serial)

    state = inspect("test-kernel", trust)
    assert state["module_signed"] is True
    assert state["module_signer_matches"] is True
    assert state["module_sig_key_matches"] is False
    assert state["loaded_module_current"] is False
    assert namespace["state_ready"](state) is False


def test_nvidia_worker_reports_reboot_when_disk_is_ready_but_module_is_not_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = _nvidia_worker()
    main = namespace["main"]
    globals_ = main.__globals__
    final_state = _ready_nvidia_state() | {"loaded_module_current": False}
    payloads: list[dict[str, object]] = []
    globals_["effective_uid"] = lambda: 0
    globals_["validate_platform"] = lambda: None
    globals_["validate_commands"] = lambda: None
    trust = namespace["SigningTrust"](
        "fedora_test_mok",
        "71:0D:A2:B5:43:11:8E:C0:EB:68:EF:32:E7:E7:E1:DF:34:CB:6E:F4",
    )
    globals_["validate_secure_boot_trust"] = lambda: trust
    globals_["ensure_580xx"] = lambda _kernel, _signer: (False, final_state)

    def record_payload(payload: dict[str, object]) -> bool:
        payloads.append(payload)
        return True

    globals_["emit"] = record_payload
    monkeypatch.setattr(sys, "argv", ["nvidia-package-remediation-worker", "ensure-580xx"])

    assert main() == 0
    assert payloads[0]["changed"] is False
    assert payloads[0]["reboot_required"] is True


@pytest.mark.parametrize(
    ("os_release", "vendor", "device", "error"),
    [
        ('ID=fedora\nVERSION_ID="43"\n', "0x10de", "0x1c8d", "unsupported_operating_system"),
        ('ID=fedora\nVERSION_ID="44"\n', "0x10de", "0x9999", "expected_gpu_not_found"),
    ],
)
def test_nvidia_worker_enforces_fixed_platform_preconditions(
    tmp_path: Path,
    os_release: str,
    vendor: str,
    device: str,
    error: str,
) -> None:
    namespace = _nvidia_worker()
    validate_platform = namespace["validate_platform"]
    os_release_path = tmp_path / "os-release"
    os_release_path.write_text(os_release, encoding="utf-8")
    pci_root = tmp_path / "pci"
    pci_device = pci_root / "0000:01:00.0"
    pci_device.mkdir(parents=True)
    (pci_device / "vendor").write_text(f"{vendor}\n", encoding="ascii")
    (pci_device / "device").write_text(f"{device}\n", encoding="ascii")
    validate_platform.__globals__["OS_RELEASE"] = os_release_path
    validate_platform.__globals__["PCI_DEVICES_ROOT"] = pci_root

    with pytest.raises(namespace["RemediationError"], match=error):
        validate_platform()


def test_installer_shell_syntax() -> None:
    completed = subprocess.run(
        ["/usr/bin/bash", "-n", str(CONTROL_PLANE / "scripts" / "install-broker.sh")],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_installer_refuses_to_take_over_foreign_state() -> None:
    installer = (CONTROL_PLANE / "scripts" / "install-broker.sh").read_text(
        encoding="utf-8"
    )
    assert "Existing broker state has unexpected ownership" in installer
    assert "chown opsbroker:opsbroker \"$state_dir/state.db\"" not in installer
    assert 'chmod 0755 "$runbooks_stage"' in installer
