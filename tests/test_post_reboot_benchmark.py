from __future__ import annotations

import importlib.machinery
import importlib.util
import http.client
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ops-post-reboot-benchmark"
loader = importlib.machinery.SourceFileLoader("ops_post_reboot_benchmark", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
post_reboot = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = post_reboot
loader.exec_module(post_reboot)


def make_repository(path: Path) -> Path:
    files = {
        "benchmarks/ollama_benchmark.py": "#!/usr/bin/env python3\n",
        "benchmarks/prompts.json": "{}\n",
        "benchmarks/decision.schema.json": "{}\n",
        "config/ollama/benchmark.json": "{}\n",
    }
    for relative, contents in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
        target.chmod(0o600)
    return path


def make_modules(path: Path, *, version: str = "580.142", nouveau: bool = False) -> Path:
    nvidia = path / "nvidia"
    nvidia.mkdir(parents=True)
    (nvidia / "version").write_text(version + "\n", encoding="utf-8")
    if nouveau:
        (path / "nouveau").mkdir()
    return path


def healthy_commands(arguments: tuple[str, ...] | list[str], _: float) -> post_reboot.CommandResult:
    command = tuple(arguments)
    if command == ("mokutil", "--sb-state"):
        return post_reboot.CommandResult(0, "SecureBoot enabled\n")
    if command == ("mokutil", "--list-enrolled"):
        return post_reboot.CommandResult(
            0,
            "Subject: O=fedora, CN=fedora_local_signing_key\n",
        )
    if command[:4] == ("modinfo", "-F", "version", "nvidia"):
        return post_reboot.CommandResult(0, "580.142\n")
    if command[:4] == ("modinfo", "-F", "signer", "nvidia"):
        return post_reboot.CommandResult(0, "fedora_local_signing_key\n")
    if command[0] == "nvidia-smi":
        return post_reboot.CommandResult(0, "580.142\n")
    raise AssertionError(f"unexpected command: {command}")


def test_ollama_readiness_treats_abrupt_http_close_as_unavailable(
    monkeypatch,
) -> None:
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _maximum_bytes):
            raise http.client.IncompleteRead(b"partial")

    monkeypatch.setattr(
        post_reboot.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    assert post_reboot.wait_for_ollama(0) == (False, 0)


def test_nouveau_blocks_benchmark_without_claiming_marker(tmp_path: Path) -> None:
    repository = make_repository(tmp_path / "repo")
    modules = make_modules(tmp_path / "modules", nouveau=True)
    state = tmp_path / "state"
    calls: list[str] = []

    def benchmark(*_: object) -> post_reboot.CommandResult:
        calls.append("benchmark")
        return post_reboot.CommandResult(0)

    result = post_reboot.execute_once(
        post_reboot.Paths(repository, state, modules),
        ollama_wait_seconds=0,
        benchmark_timeout_seconds=60,
        command_runner=healthy_commands,
        ollama_probe=lambda _: (True, 1),
        benchmark_runner=benchmark,
    )

    assert result == 3
    assert calls == []
    assert not (state / post_reboot.MARKER_NAME).exists()
    report = json.loads((state / post_reboot.REPORT_NAME).read_text(encoding="utf-8"))
    assert report["status"] == "blocked_by_gate"
    assert report["benchmark"]["attempted"] is False
    assert next(item for item in report["gates"] if item["name"] == "nouveau_absent")["passed"] is False


def test_healthy_gates_run_exactly_once_and_keep_bounded_report(tmp_path: Path) -> None:
    repository = make_repository(tmp_path / "repo")
    modules = make_modules(tmp_path / "modules")
    state = tmp_path / "state"
    calls: list[str] = []

    def benchmark(repo: Path, run_name: str, _: float) -> post_reboot.CommandResult:
        calls.append(run_name)
        result_dir = repo / "benchmarks" / "results" / run_name
        result_dir.mkdir(parents=True)
        (result_dir / "report.json").write_text("{}\n", encoding="utf-8")
        (result_dir / "report.md").write_text("ok\n", encoding="utf-8")
        return post_reboot.CommandResult(0)

    paths = post_reboot.Paths(repository, state, modules)
    first = post_reboot.execute_once(
        paths,
        ollama_wait_seconds=0,
        benchmark_timeout_seconds=60,
        command_runner=healthy_commands,
        ollama_probe=lambda _: (True, 1),
        benchmark_runner=benchmark,
    )
    second = post_reboot.execute_once(
        paths,
        ollama_wait_seconds=0,
        benchmark_timeout_seconds=60,
        command_runner=healthy_commands,
        ollama_probe=lambda _: (True, 1),
        benchmark_runner=benchmark,
    )

    assert first == 0
    assert second == 0
    assert len(calls) == 1
    marker = json.loads(paths.marker.read_text(encoding="utf-8"))
    assert marker["state"] == "completed"
    assert marker["benchmark_exit_code"] == 0
    report_bytes = paths.report.read_bytes()
    assert len(report_bytes) <= post_reboot.MAX_REPORT_BYTES
    report = json.loads(report_bytes)
    assert report["status"] == "benchmark_completed"
    assert report["benchmark"]["promotion_performed"] is False
    assert report["gate_summary"]["failed"] == 0
    assert stat_mode(paths.marker) == 0o600
    assert stat_mode(paths.report) == 0o600


def test_failed_benchmark_still_consumes_one_shot_marker(tmp_path: Path) -> None:
    repository = make_repository(tmp_path / "repo")
    modules = make_modules(tmp_path / "modules")
    state = tmp_path / "state"
    calls = 0

    def benchmark(*_: object) -> post_reboot.CommandResult:
        nonlocal calls
        calls += 1
        return post_reboot.CommandResult(1)

    paths = post_reboot.Paths(repository, state, modules)
    first = post_reboot.execute_once(
        paths,
        ollama_wait_seconds=0,
        benchmark_timeout_seconds=60,
        command_runner=healthy_commands,
        ollama_probe=lambda _: (True, 1),
        benchmark_runner=benchmark,
    )
    second = post_reboot.execute_once(
        paths,
        ollama_wait_seconds=0,
        benchmark_timeout_seconds=60,
        command_runner=healthy_commands,
        ollama_probe=lambda _: (True, 1),
        benchmark_runner=benchmark,
    )

    assert first == 5
    assert second == 4
    assert calls == 1
    assert json.loads(paths.marker.read_text(encoding="utf-8"))["state"] == "failed"


def test_interrupted_benchmark_stops_and_can_be_explicitly_rearmed(tmp_path: Path) -> None:
    repository = make_repository(tmp_path / "repo")
    modules = make_modules(tmp_path / "modules")
    state = tmp_path / "state"
    calls = 0

    def benchmark(repo: Path, run_name: str, _: float) -> post_reboot.CommandResult:
        nonlocal calls
        calls += 1
        result_dir = repo / "benchmarks" / "results" / run_name
        result_dir.mkdir(parents=True)
        (result_dir / "report.json").write_text("partial\n", encoding="utf-8")
        (result_dir / "report.md").write_text("partial\n", encoding="utf-8")
        return post_reboot.CommandResult(post_reboot.BENCHMARK_INTERRUPTED_EXIT_CODE)

    paths = post_reboot.Paths(repository, state, modules)
    first = post_reboot.execute_once(
        paths,
        ollama_wait_seconds=0,
        benchmark_timeout_seconds=60,
        command_runner=healthy_commands,
        ollama_probe=lambda _: (True, 1),
        benchmark_runner=benchmark,
    )
    second = post_reboot.execute_once(
        paths,
        ollama_wait_seconds=0,
        benchmark_timeout_seconds=60,
        command_runner=healthy_commands,
        ollama_probe=lambda _: (True, 1),
        benchmark_runner=benchmark,
    )

    assert first == post_reboot.BENCHMARK_INTERRUPTED_EXIT_CODE
    assert second == post_reboot.BENCHMARK_INTERRUPTED_EXIT_CODE
    assert calls == 1
    marker = json.loads(paths.marker.read_text(encoding="utf-8"))
    report = json.loads(paths.report.read_text(encoding="utf-8"))
    assert marker["state"] == "interrupted"
    assert marker["benchmark_exit_code"] == post_reboot.BENCHMARK_INTERRUPTED_EXIT_CODE
    assert report["status"] == "benchmark_interrupted"
    assert report["benchmark"]["interruption_reason"] == "benchmark_campaign_interrupted"

    detailed = repository / "benchmarks" / "results" / marker["run_name"] / "report.json"
    assert post_reboot.rearm_interrupted(paths) == 0
    assert not paths.marker.exists()
    assert detailed.read_text(encoding="utf-8") == "partial\n"
    archive = paths.archive / marker["run_name"]
    assert json.loads((archive / post_reboot.MARKER_NAME).read_text(encoding="utf-8")) == marker
    assert json.loads((archive / post_reboot.REPORT_NAME).read_text(encoding="utf-8")) == report
    assert stat_mode(archive / post_reboot.MARKER_NAME) == 0o600
    assert stat_mode(archive / post_reboot.REPORT_NAME) == 0o600


def test_rearm_preserves_marker_if_archive_sync_fails(
    tmp_path: Path, monkeypatch
) -> None:
    repository = make_repository(tmp_path / "repo")
    modules = make_modules(tmp_path / "modules")
    paths = post_reboot.Paths(repository, tmp_path / "state", modules)
    result = post_reboot.execute_once(
        paths,
        ollama_wait_seconds=0,
        benchmark_timeout_seconds=60,
        command_runner=healthy_commands,
        ollama_probe=lambda _: (True, 1),
        benchmark_runner=lambda *_: post_reboot.CommandResult(
            post_reboot.BENCHMARK_INTERRUPTED_EXIT_CODE
        ),
    )
    assert result == post_reboot.BENCHMARK_INTERRUPTED_EXIT_CODE
    marker = json.loads(paths.marker.read_text(encoding="utf-8"))
    original_sync = post_reboot.fsync_private_directory

    def fail_run_archive_sync(path: Path) -> None:
        if path == paths.archive / marker["run_name"]:
            raise OSError("simulated directory fsync failure")
        original_sync(path)

    monkeypatch.setattr(
        post_reboot, "fsync_private_directory", fail_run_archive_sync
    )
    assert post_reboot.rearm_interrupted(paths) == 4
    assert paths.marker.exists()


def test_claim_sync_failure_never_starts_the_benchmark(
    tmp_path: Path, monkeypatch
) -> None:
    repository = make_repository(tmp_path / "repo")
    modules = make_modules(tmp_path / "modules")
    paths = post_reboot.Paths(repository, tmp_path / "state", modules)
    calls = 0

    def benchmark(*_args) -> post_reboot.CommandResult:
        nonlocal calls
        calls += 1
        return post_reboot.CommandResult(0)

    def fail_sync(_path: Path) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(
        post_reboot,
        "fsync_private_directory",
        fail_sync,
    )
    result = post_reboot.execute_once(
        paths,
        ollama_wait_seconds=0,
        benchmark_timeout_seconds=60,
        command_runner=healthy_commands,
        ollama_probe=lambda _: (True, 1),
        benchmark_runner=benchmark,
    )
    assert result == 4
    assert calls == 0
    assert paths.marker.exists()


def test_rearm_refuses_non_interrupted_and_mismatched_state(tmp_path: Path) -> None:
    for return_code in (0, 1, 2):
        repository = make_repository(tmp_path / f"repo-{return_code}")
        modules = make_modules(tmp_path / f"modules-{return_code}")
        paths = post_reboot.Paths(repository, tmp_path / f"state-{return_code}", modules)
        post_reboot.execute_once(
            paths,
            ollama_wait_seconds=0,
            benchmark_timeout_seconds=60,
            command_runner=healthy_commands,
            ollama_probe=lambda _: (True, 1),
            benchmark_runner=lambda *_: post_reboot.CommandResult(return_code),
        )
        assert post_reboot.rearm_interrupted(paths) == 4
        assert paths.marker.exists()

    repository = make_repository(tmp_path / "repo-mismatch")
    modules = make_modules(tmp_path / "modules-mismatch")
    paths = post_reboot.Paths(repository, tmp_path / "state-mismatch", modules)
    post_reboot.execute_once(
        paths,
        ollama_wait_seconds=0,
        benchmark_timeout_seconds=60,
        command_runner=healthy_commands,
        ollama_probe=lambda _: (True, 1),
        benchmark_runner=lambda *_: post_reboot.CommandResult(
            post_reboot.BENCHMARK_INTERRUPTED_EXIT_CODE
        ),
    )
    report = json.loads(paths.report.read_text(encoding="utf-8"))
    report["benchmark"]["run_name"] = "different-run"
    post_reboot.atomic_write(paths.report, report, post_reboot.MAX_REPORT_BYTES)
    assert post_reboot.rearm_interrupted(paths) == 4
    assert paths.marker.exists()


def test_mismatched_or_unsigned_module_fails_closed(tmp_path: Path) -> None:
    repository = make_repository(tmp_path / "repo")
    modules = make_modules(tmp_path / "modules", version="610.57.04")

    def commands(arguments: tuple[str, ...] | list[str], _: float) -> post_reboot.CommandResult:
        command = tuple(arguments)
        if command == ("mokutil", "--sb-state"):
            return post_reboot.CommandResult(0, "SecureBoot enabled\n")
        if command == ("mokutil", "--list-enrolled"):
            return post_reboot.CommandResult(0, "")
        if command[:4] == ("modinfo", "-F", "version", "nvidia"):
            return post_reboot.CommandResult(0, "610.57.04\n")
        if command[:4] == ("modinfo", "-F", "signer", "nvidia"):
            return post_reboot.CommandResult(0, "\n")
        return post_reboot.CommandResult(0, "610.57.04\n")

    checks = post_reboot.collect_nvidia_gates(
        post_reboot.Paths(repository, tmp_path / "state", modules), commands
    )
    values = {item["name"]: item["passed"] for item in checks}
    assert values["nvidia_loaded_version_580xx"] is False
    assert values["nvidia_module_file_version_580xx"] is False
    assert values["nvidia_module_signed"] is False
    assert values["nvidia_signer_enrolled"] is False
    assert values["nvidia_smi_580xx"] is False


def test_secure_boot_or_unenrolled_signer_fails_closed(tmp_path: Path) -> None:
    repository = make_repository(tmp_path / "repo")
    modules = make_modules(tmp_path / "modules")

    def commands(arguments: tuple[str, ...] | list[str], _: float) -> post_reboot.CommandResult:
        command = tuple(arguments)
        if command == ("mokutil", "--sb-state"):
            return post_reboot.CommandResult(0, "SecureBoot disabled\n")
        if command == ("mokutil", "--list-enrolled"):
            return post_reboot.CommandResult(0, "Subject: CN=some_other_key\n")
        return healthy_commands(arguments, _)

    checks = post_reboot.collect_nvidia_gates(
        post_reboot.Paths(repository, tmp_path / "state", modules), commands
    )
    values = {item["name"]: item["passed"] for item in checks}
    assert values["secure_boot_enabled"] is False
    assert values["nvidia_module_signed"] is True
    assert values["nvidia_signer_enrolled"] is False


def test_mok_signer_matches_only_an_exact_subject_cn() -> None:
    signer = "fedora_local_signing_key"
    assert post_reboot.mok_contains_signer(f"Subject: CN={signer}\n", signer)
    assert post_reboot.mok_contains_signer(
        f"Subject: O=fedora, OU=akmods, CN={signer}\n",
        signer,
    )
    assert not post_reboot.mok_contains_signer(f"Issuer: CN={signer}\n", signer)
    assert not post_reboot.mok_contains_signer(
        f"Subject: CN={signer}-other\n",
        signer,
    )


def test_readiness_is_rechecked_during_early_boot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = make_repository(tmp_path / "repo")
    modules = tmp_path / "modules"
    modules.mkdir()
    state = tmp_path / "state"
    probes = 0
    benchmark_calls = 0

    monotonic_values = iter((0.0, 0.0, 0.1))
    monkeypatch.setattr(post_reboot.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(post_reboot.time, "sleep", lambda _seconds: None)

    def probe(_timeout: float) -> tuple[bool, int]:
        nonlocal probes
        probes += 1
        if probes == 1:
            make_modules(modules)
        return probes > 1, 1 if probes > 1 else 0

    def benchmark(*_: object) -> post_reboot.CommandResult:
        nonlocal benchmark_calls
        benchmark_calls += 1
        return post_reboot.CommandResult(0)

    result = post_reboot.execute_once(
        post_reboot.Paths(repository, state, modules),
        ollama_wait_seconds=2,
        benchmark_timeout_seconds=60,
        command_runner=healthy_commands,
        ollama_probe=probe,
        benchmark_runner=benchmark,
    )

    assert result == 0
    assert probes == 2
    assert benchmark_calls == 1


def test_unit_and_installer_are_user_scoped_and_do_not_start_immediately() -> None:
    unit = (ROOT / "systemd/user/ops-post-reboot-benchmark.service").read_text(encoding="utf-8")
    installer = (ROOT / "scripts/install-post-reboot-benchmark.sh").read_text(encoding="utf-8")
    assert "WantedBy=default.target" in unit
    assert "ConditionPathExists=!%h/.local/state/ops-control-plane/post-reboot/benchmark-v1.marker" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectKernelModules=yes" not in unit
    assert "IPAddressAllow=localhost" in unit
    assert "SuccessExitStatus=2" in unit
    assert "SuccessExitStatus=75" not in unit
    assert "systemctl --user enable ops-post-reboot-benchmark.service" in installer
    assert "enable --now" not in installer
    assert "sudo" not in installer
    assert "--rearm-interrupted" in SCRIPT.read_text(encoding="utf-8")


def test_metrics_unit_keeps_module_tree_read_only_but_visible() -> None:
    unit = (ROOT / "systemd/ops-local-metrics.service").read_text(encoding="utf-8")
    assert "ProtectSystem=strict" in unit
    assert "ProtectKernelModules=yes" not in unit
    assert "CAP_SYS_MODULE" not in unit


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
