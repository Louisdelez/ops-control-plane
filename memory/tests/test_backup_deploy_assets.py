from __future__ import annotations

import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[2]


def test_backup_scripts_parse_and_are_executable() -> None:
    installer = ROOT / "scripts" / "install-memory-backup.sh"
    subprocess.run(["bash", "-n", str(installer)], check=True)
    assert os.access(installer, os.X_OK)

    implementation = ROOT / "memory" / "backup" / "ops_memory_backup.py"
    compile(implementation.read_bytes(), str(implementation), "exec")
    assert os.access(implementation, os.X_OK)


def test_backup_units_verify() -> None:
    units = [
        ROOT / "systemd" / "ops-memory-backup.service",
        ROOT / "systemd" / "ops-memory-backup.timer",
        ROOT / "systemd" / "ops-memory-restore-test.service",
        ROOT / "systemd" / "ops-memory-restore-test.timer",
        ROOT / "systemd" / "ops-memory-backup-metric-success.service",
        ROOT / "systemd" / "ops-memory-backup-metric-failure.service",
        ROOT / "systemd" / "ops-memory-restore-test-metric-success.service",
        ROOT / "systemd" / "ops-memory-restore-test-metric-failure.service",
    ]
    completed = subprocess.run(
        ["systemd-analyze", "verify", *(str(path) for path in units)],
        text=True,
        capture_output=True,
    )
    unexpected = "\n".join(
        line
        for line in completed.stderr.splitlines()
        if "is not executable: No such file" not in line
    )
    assert completed.returncode in {0, 1}, completed.stderr
    assert not unexpected, unexpected


def test_backup_units_use_volatile_openbao_resolved_credential() -> None:
    service = (ROOT / "systemd" / "ops-memory-backup.service").read_text(
        encoding="utf-8"
    )
    assert "Requires=ops-memory-secrets.service ops-memory-qdrant.service" in service
    assert (
        "LoadCredential=qdrant-api-key:/run/ops-memory-secrets/qdrant-api-key" in service
    )
    assert "LoadCredentialEncrypted" not in service
    assert "/etc/credstore" not in service
    assert "ReadWritePaths=/var/lib/ops-memory /var/backups/ops-memory" in service


def test_restore_unit_allows_only_the_host_podman_selinux_transition() -> None:
    service = (ROOT / "systemd" / "ops-memory-restore-test.service").read_text(
        encoding="utf-8"
    )
    implementation = (ROOT / "memory" / "backup" / "ops_memory_backup.py").read_text(
        encoding="utf-8"
    )
    directives = {
        line.strip()
        for line in service.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "NoNewPrivileges=yes" not in directives
    assert "PrivateDevices=yes" not in directives
    assert "ProtectHostname=yes" not in directives
    assert "ProtectControlGroups=yes" not in directives
    assert "ProtectKernelTunables=yes" not in directives
    assert "ProcSubset=pid" not in directives
    assert "ProtectSystem=full" in service
    assert "ReadOnlyPaths=/var " in service
    assert '"--security-opt=no-new-privileges"' in implementation
    assert '"--cap-drop=all"' in implementation


def test_backup_jobs_publish_constrained_metrics_and_preserve_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = ROOT / "memory" / "backup" / "ops_memory_job_metric.py"
    namespace = runpy.run_path(str(helper))
    main = namespace["main"]
    textfile = tmp_path / "textfile"
    textfile.mkdir()
    destination_backup = textfile / "ops-memory-backup.prom"
    destination_restore = textfile / "ops-memory-restore-test.prom"
    main.__globals__["TEXTFILE_DIRECTORY"] = textfile
    main.__globals__["JOBS"] = {
        "backup": {
            "destination": destination_backup,
            "timestamp": "ops_memory_backup_last_success_timestamp_seconds",
            "result": "ops_memory_backup_last_result_ok",
            "description": "ops-memory backup",
        },
        "restore-test": {
            "destination": destination_restore,
            "timestamp": "ops_memory_restore_test_last_success_timestamp_seconds",
            "result": "ops_memory_restore_test_last_result_ok",
            "description": "ops-memory isolated restore test",
        },
    }
    # The real publisher requires the root:node-exporter production directory;
    # the atomic publication behavior itself is exercised in an unprivileged
    # temporary directory.
    main.__globals__["_safe_textfile_directory"] = lambda: textfile

    monkeypatch.setattr(sys, "argv", ["ops-memory-job-metric", "backup", "success"])
    assert main() == 0
    backup_success = destination_backup.read_text(encoding="ascii")
    timestamp_line = next(
        line
        for line in backup_success.splitlines()
        if line.startswith("ops_memory_backup_last_success_timestamp_seconds ")
    )
    assert "ops_memory_backup_last_result_ok 1" in backup_success
    assert destination_backup.stat().st_mode & 0o777 == 0o640

    monkeypatch.setattr(sys, "argv", ["ops-memory-job-metric", "backup", "failure"])
    assert main() == 0
    backup_failure = destination_backup.read_text(encoding="ascii")
    assert timestamp_line in backup_failure
    assert "ops_memory_backup_last_result_ok 0" in backup_failure

    monkeypatch.setattr(
        sys, "argv", ["ops-memory-job-metric", "restore-test", "success"]
    )
    assert main() == 0
    restore_success = destination_restore.read_text(encoding="ascii")
    assert "ops_memory_restore_test_last_result_ok 1" in restore_success
    assert "ops_memory_backup_last_result_ok" not in restore_success

    assert main(["invalid", "success"]) == 64
    assert main(["backup", "invalid"]) == 64


def test_backup_metric_units_are_root_confined_and_triggered() -> None:
    backup = (ROOT / "systemd" / "ops-memory-backup.service").read_text(
        encoding="utf-8"
    )
    restore = (ROOT / "systemd" / "ops-memory-restore-test.service").read_text(
        encoding="utf-8"
    )
    success = (ROOT / "systemd" / "ops-memory-backup-metric-success.service").read_text(
        encoding="utf-8"
    )
    failure = (ROOT / "systemd" / "ops-memory-restore-test-metric-failure.service").read_text(
        encoding="utf-8"
    )
    helper = (ROOT / "memory" / "backup" / "ops_memory_job_metric.py").read_text(
        encoding="utf-8"
    )
    assert "OnSuccess=ops-memory-backup-metric-success.service" in backup
    assert "OnFailure=ops-memory-backup-metric-failure.service" in backup
    assert "OnSuccess=ops-memory-restore-test-metric-success.service" in restore
    assert "OnFailure=ops-memory-restore-test-metric-failure.service" in restore
    for unit in (success, failure):
        assert "User=root" in unit
        assert "Group=node-exporter" in unit
        assert "CapabilityBoundingSet=" in unit
        assert "ReadWritePaths=/var/lib/node-exporter/textfile" in unit
        assert "InaccessiblePaths=-/var/lib/ops-memory -/var/backups/ops-memory" in unit
    assert "/var/lib/node-exporter/textfile" in helper
    assert "ops_memory_backup_last_success_timestamp_seconds" in helper
    assert "ops_memory_restore_test_last_success_timestamp_seconds" in helper
    assert "os.replace(temporary, destination)" in helper
    assert "os.chmod(temporary, 0o640)" in helper


def test_installer_keeps_backup_stack_separate() -> None:
    installer = (ROOT / "scripts" / "install-memory-backup.sh").read_text(
        encoding="utf-8"
    )
    assert "install-memory-stack.sh" not in installer
    assert "systemctl stop ops-memory" not in installer
    assert "ops-memory-restore-test.service" in installer
    assert "ops-memory-job-metric" in installer
    assert "ops-memory-backup-metric-success.service" in installer
