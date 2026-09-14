from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import runpy
import stat
import subprocess
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "artifacts/recovery-patch/install-recovery-helper-f"
ENROLLER = ROOT / "artifacts/recovery-patch/enroll-recovery-helper-f"
FILE_MODES = {
    "release-manifest.v1.json": 0o444,
    "control-plane-bootstrap": 0o555,
    "launch-atlas-reviewed-rpm": 0o555,
}


def _load(path: Path) -> dict[str, Any]:
    return runpy.run_path(str(path), run_name=f"test_{path.name.replace('-', '_')}")


def _payload(paths: dict[str, Path]) -> dict[str, bytes]:
    return {name: path.read_bytes() for name, path in paths.items()}


def _write_anchor(path: Path, payload: dict[str, bytes], mode: int = 0o555) -> None:
    path.mkdir(mode=0o700)
    for name, raw in payload.items():
        (path / name).write_bytes(raw)
        os.chmod(path / name, FILE_MODES[name])
    os.chmod(path, mode)


def test_e_to_f_exchange_preserves_d_and_archives_e(tmp_path: Path) -> None:
    installer = _load(INSTALLER)
    enroller = _load(ENROLLER)
    parent = tmp_path / "anchors"
    parent.mkdir(mode=0o755)
    history = parent / "recovery-patch-history"
    history.mkdir(mode=0o755)
    release = parent / "atlas-api-zulip-2026.09.08.11"
    stage = parent / ".atlas-api-zulip-2026.09.08.11.recovery-helper-f.staging"
    archive_d = history / "atlas-api-zulip-2026.09.08.11.anchor-d"
    archive_e = history / "atlas-api-zulip-2026.09.08.11.anchor-e"
    d_payload = _payload(
        {
            "release-manifest.v1.json": ROOT / "tests/fixtures/atlas-anchor-d/release-manifest.v1.json",
            "control-plane-bootstrap": ROOT / "tests/fixtures/atlas-anchor-d/control-plane-bootstrap",
            "launch-atlas-reviewed-rpm": ROOT / "tests/fixtures/atlas-anchor-d/launch-atlas-reviewed-rpm",
        }
    )
    e_payload = _payload(
        {
            "release-manifest.v1.json": ROOT / "artifacts/recovery-patch/release-manifest.e.v1.json",
            "control-plane-bootstrap": ROOT / "artifacts/recovery-patch/control-plane-bootstrap.e",
            "launch-atlas-reviewed-rpm": ROOT / "artifacts/recovery-patch/launch-atlas-reviewed-rpm.e",
        }
    )
    f_payload = {
        "release-manifest.v1.json": b'{"fixture":"manifest-f"}\n',
        "control-plane-bootstrap": b"#!/usr/bin/python3 -I\n# fixture F\n",
        "launch-atlas-reviewed-rpm": e_payload["launch-atlas-reviewed-rpm"],
    }
    _write_anchor(release, e_payload)
    _write_anchor(archive_d, d_payload)
    d_inode = archive_d.stat().st_ino

    paths = {
        "ANCHOR_PARENT": parent,
        "ANCHOR_ROOT": release,
        "PATCH_STAGE": stage,
        "PATCH_HISTORY_PARENT": history,
        "PATCH_HISTORY_D": archive_d,
        "PATCH_HISTORY_E": archive_e,
        "ROOT_UID": os.getuid(),
        "ROOT_GID": os.getgid(),
    }
    for program in (installer, enroller):
        program["_read_anchor"].__globals__.update(paths)

    assert enroller["_validate_state"](f_payload) == "publish"
    assert installer["_publish_or_resume"](e_payload, f_payload) == "published"
    assert installer["_read_anchor"](release) == f_payload
    assert installer["_read_anchor"](archive_e) == e_payload
    assert stat.S_IMODE(archive_e.stat().st_mode) == 0o555
    assert installer["_read_anchor"](archive_d) == d_payload
    assert archive_d.stat().st_ino == d_inode
    assert not os.path.lexists(stage)
    assert enroller["_validate_state"](f_payload) == "complete"


def test_enroller_has_no_complete_fast_path() -> None:
    source = ENROLLER.read_text(encoding="utf-8")
    assert 'if state == "complete":\n        _launch(environment)' not in source
    assert source.index("installed = _bounded_run(") < source.index(
        "if _validate_state(f_payload) != \"complete\":"
    )


def _anchor_payloads() -> tuple[dict[str, bytes], dict[str, bytes], dict[str, bytes]]:
    d_payload = _payload(
        {
            "release-manifest.v1.json": ROOT / "tests/fixtures/atlas-anchor-d/release-manifest.v1.json",
            "control-plane-bootstrap": ROOT / "tests/fixtures/atlas-anchor-d/control-plane-bootstrap",
            "launch-atlas-reviewed-rpm": ROOT / "tests/fixtures/atlas-anchor-d/launch-atlas-reviewed-rpm",
        }
    )
    e_payload = _payload(
        {
            "release-manifest.v1.json": ROOT / "artifacts/recovery-patch/release-manifest.e.v1.json",
            "control-plane-bootstrap": ROOT / "artifacts/recovery-patch/control-plane-bootstrap.e",
            "launch-atlas-reviewed-rpm": ROOT / "artifacts/recovery-patch/launch-atlas-reviewed-rpm.e",
        }
    )
    f_payload = {
        "release-manifest.v1.json": b'{"fixture":"manifest-f"}\n',
        "control-plane-bootstrap": b"#!/usr/bin/python3 -I\n# exact fixture F\n",
        "launch-atlas-reviewed-rpm": e_payload["launch-atlas-reviewed-rpm"],
    }
    return d_payload, e_payload, f_payload


def _tree_snapshot(root: Path) -> tuple[tuple[Any, ...], ...]:
    result: list[tuple[Any, ...]] = []
    for path in sorted((root, *root.rglob("*")), key=str):
        metadata = path.lstat()
        relative = "." if path == root else str(path.relative_to(root))
        if stat.S_ISLNK(metadata.st_mode):
            result.append((relative, "link", os.readlink(path)))
        elif stat.S_ISREG(metadata.st_mode):
            result.append((relative, "file", stat.S_IMODE(metadata.st_mode), path.read_bytes()))
        elif stat.S_ISDIR(metadata.st_mode):
            result.append((relative, "dir", stat.S_IMODE(metadata.st_mode)))
    return tuple(result)


@pytest.mark.parametrize(
    ("anchor", "watcher", "stage", "allowed"),
    [
        pytest.param("e", "d", False, True, id="e-requires-d"),
        pytest.param("e", "f", False, False, id="e-rejects-f"),
        pytest.param("e", "d", True, False, id="e-rejects-watcher-stage"),
        pytest.param("f", "d", False, True, id="f-allows-d-frontier"),
        pytest.param("f", "f", False, True, id="f-allows-f"),
        pytest.param("f", "d", True, True, id="f-allows-exact-f-stage"),
    ],
)
def test_anchor_watcher_cross_product_is_fail_closed_without_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    anchor: str,
    watcher: str,
    stage: bool,
    allowed: bool,
) -> None:
    program = _load(INSTALLER)
    globals_ = program["_validate_anchor_watcher_frontier"].__globals__
    _d, e_payload, f_payload = _anchor_payloads()
    anchor_root = tmp_path / "anchor"
    _write_anchor(anchor_root, e_payload if anchor == "e" else f_payload)
    watcher_stage = tmp_path / ".watcher-f.next"
    if stage:
        watcher_stage.write_bytes(f_payload["control-plane-bootstrap"][:17])
        os.chmod(watcher_stage, 0o600)
    globals_.update(
        {
            "ANCHOR_ROOT": anchor_root,
            "WATCHER_STAGE": watcher_stage,
            "ROOT_UID": os.getuid(),
            "ROOT_GID": os.getgid(),
        }
    )
    monkeypatch.setitem(
        globals_, "_validate_corrective_static", lambda _raw: watcher
    )
    before = _tree_snapshot(tmp_path)
    if allowed:
        assert program["_validate_anchor_watcher_frontier"](
            f_payload, f_payload["control-plane-bootstrap"]
        ) == (anchor, watcher)
    else:
        with pytest.raises(program["PatchInstallError"]):
            program["_validate_anchor_watcher_frontier"](
                f_payload, f_payload["control-plane-bootstrap"]
            )
    assert _tree_snapshot(tmp_path) == before


def _corrective_runtime(
    *, active: str, sub: str, pid: str, exec_main_code: str = "1",
    invocation_id: str = "1" * 32,
) -> dict[str, str]:
    return {
        "ActiveState": active,
        "SubState": sub,
        "MainPID": pid,
        "InvocationID": invocation_id,
        "Result": "success",
        "ExecMainCode": exec_main_code,
        "ExecMainStatus": "0",
    }


def test_systemctl_show_accepts_real_path_output_without_main_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = _load(INSTALLER)
    globals_ = program["_systemctl_show"].__globals__
    properties = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "UnitFileState": "enabled",
        "FragmentPath": "/etc/systemd/system/watcher.path",
        "DropInPaths": "",
    }
    stdout = "".join(f"{key}={value}\n" for key, value in properties.items()).encode()
    observed_command: list[str] = []

    def _run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        observed_command.extend(command)
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")

    monkeypatch.setitem(globals_, "subprocess", subprocess)
    monkeypatch.setattr(subprocess, "run", _run)
    assert program["_systemctl_show"]("watcher.path") == properties
    assert "--property=MainPID" not in observed_command


def test_systemctl_show_rejects_missing_required_path_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = _load(INSTALLER)
    globals_ = program["_systemctl_show"].__globals__
    stdout = (
        b"LoadState=loaded\n"
        b"ActiveState=active\n"
        b"SubState=running\n"
        b"UnitFileState=enabled\n"
        b"FragmentPath=/etc/systemd/system/watcher.path\n"
    )

    def _run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")

    monkeypatch.setitem(globals_, "subprocess", subprocess)
    monkeypatch.setattr(subprocess, "run", _run)
    with pytest.raises(program["PatchInstallError"]):
        program["_systemctl_show"]("watcher.path")


@pytest.mark.parametrize("path_substate", ["waiting", "running"])
@pytest.mark.parametrize("exec_main_code", ["1", "exited"])
def test_pid1_prevalidation_accepts_healthy_path_and_service(
    monkeypatch: pytest.MonkeyPatch,
    path_substate: str,
    exec_main_code: str,
) -> None:
    program = _load(INSTALLER)
    globals_ = program["_validate_pre_mutation_runtime"].__globals__
    monkeypatch.setitem(globals_, "_validate_loaded_units", lambda: None)
    monkeypatch.setitem(
        globals_,
        "_systemctl_show",
        lambda _unit: {
            "ActiveState": "active", "SubState": path_substate,
        },
    )
    monkeypatch.setitem(
        globals_,
        "_service_runtime",
        lambda: _corrective_runtime(
            active="active", sub="exited", pid="0",
            exec_main_code=exec_main_code,
        ),
    )
    program["_validate_pre_mutation_runtime"]()


@pytest.mark.parametrize(
    ("path_active", "path_substate"),
    [
        ("inactive", "dead"),
        ("active", "dead"),
        ("active", "failed"),
        ("activating", "start"),
    ],
)
def test_pid1_prevalidation_rejects_unhealthy_path_states(
    monkeypatch: pytest.MonkeyPatch,
    path_active: str,
    path_substate: str,
) -> None:
    program = _load(INSTALLER)
    globals_ = program["_validate_pre_mutation_runtime"].__globals__
    monkeypatch.setitem(globals_, "_validate_loaded_units", lambda: None)
    monkeypatch.setitem(
        globals_,
        "_systemctl_show",
        lambda _unit: {
            "ActiveState": path_active, "SubState": path_substate,
        },
    )
    monkeypatch.setitem(
        globals_,
        "_service_runtime",
        lambda: _corrective_runtime(active="active", sub="exited", pid="0"),
    )
    with pytest.raises(program["PatchInstallError"]):
        program["_validate_pre_mutation_runtime"]()


@pytest.mark.parametrize("exec_main_code", ["", "0", "2", "killed"])
def test_pid1_prevalidation_rejects_non_exit_main_code(
    monkeypatch: pytest.MonkeyPatch,
    exec_main_code: str,
) -> None:
    program = _load(INSTALLER)
    globals_ = program["_validate_pre_mutation_runtime"].__globals__
    monkeypatch.setitem(globals_, "_validate_loaded_units", lambda: None)
    monkeypatch.setitem(
        globals_,
        "_systemctl_show",
        lambda _unit: {"ActiveState": "active", "SubState": "running"},
    )
    monkeypatch.setitem(
        globals_,
        "_service_runtime",
        lambda: _corrective_runtime(
            active="active", sub="exited", pid="0",
            exec_main_code=exec_main_code,
        ),
    )
    with pytest.raises(program["PatchInstallError"]):
        program["_validate_pre_mutation_runtime"]()


def test_pid1_prevalidation_live_service_requires_exact_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program = _load(INSTALLER)
    globals_ = program["_validate_pre_mutation_runtime"].__globals__
    monkeypatch.setitem(globals_, "_validate_loaded_units", lambda: None)
    monkeypatch.setitem(
        globals_,
        "_systemctl_show",
        lambda _unit: {
            "ActiveState": "active", "SubState": "waiting",
        },
    )
    monkeypatch.setitem(
        globals_,
        "_service_runtime",
        lambda: _corrective_runtime(active="activating", sub="start", pid="4123"),
    )
    monkeypatch.setitem(globals_, "_corrective_process_is_exact", lambda _pid: True)
    program["_validate_pre_mutation_runtime"]()
    monkeypatch.setitem(globals_, "_corrective_process_is_exact", lambda _pid: False)
    with pytest.raises(program["PatchInstallError"]):
        program["_validate_pre_mutation_runtime"]()


@pytest.mark.parametrize("path_substate", ["waiting", "running"])
def test_arm_and_prove_accepts_healthy_path_substates(
    monkeypatch: pytest.MonkeyPatch,
    path_substate: str,
) -> None:
    program = _load(INSTALLER)
    globals_ = program["_arm_and_prove_corrective_f"].__globals__
    service_states = iter(
        [
            _corrective_runtime(
                active="active", sub="exited", pid="0",
                invocation_id="1" * 32,
            ),
            _corrective_runtime(
                active="active", sub="exited", pid="0",
                invocation_id="2" * 32,
            ),
        ]
    )
    monkeypatch.setitem(globals_, "_service_runtime", lambda: next(service_states))
    monkeypatch.setitem(globals_, "_run_system", lambda _command, _label: None)
    monkeypatch.setitem(globals_, "_validate_loaded_units", lambda: None)
    monkeypatch.setitem(
        globals_, "_systemctl_show",
        lambda _unit: {"ActiveState": "active", "SubState": path_substate},
    )
    monkeypatch.setitem(
        globals_, "time",
        SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda _delay: None),
    )
    program["_arm_and_prove_corrective_f"](b"helper-f")


@pytest.mark.parametrize("path_substate", ["dead", "failed", "start"])
def test_arm_and_prove_rejects_unhealthy_path_substates(
    monkeypatch: pytest.MonkeyPatch,
    path_substate: str,
) -> None:
    program = _load(INSTALLER)
    globals_ = program["_arm_and_prove_corrective_f"].__globals__
    service_states = iter(
        [
            _corrective_runtime(
                active="active", sub="exited", pid="0",
                invocation_id="1" * 32,
            ),
            _corrective_runtime(
                active="active", sub="exited", pid="0",
                invocation_id="2" * 32,
            ),
        ]
    )
    ticks = iter([0.0, 16.0])
    monkeypatch.setitem(globals_, "_service_runtime", lambda: next(service_states))
    monkeypatch.setitem(globals_, "_run_system", lambda _command, _label: None)
    monkeypatch.setitem(globals_, "_validate_loaded_units", lambda: None)
    monkeypatch.setitem(
        globals_, "_systemctl_show",
        lambda _unit: {"ActiveState": "active", "SubState": path_substate},
    )
    monkeypatch.setitem(
        globals_, "time",
        SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda _delay: None),
    )
    with pytest.raises(program["PatchInstallError"]):
        program["_arm_and_prove_corrective_f"](b"helper-f")


def _network_identity_fixture(
    program: dict[str, Any], phase: str, *, updated_at: int = 10
) -> tuple[dict[str, Any], dict[str, Any], str, str, dict[str, Any]]:
    globals_ = program["_network_baseline_identity"].__globals__
    helper_sha = "a" * 64
    manifest_sha = "b" * 64
    marker = {
        **globals_["RESUME_PATCH_DEPLOYMENT"],
        "corrective_id": "corrective-0123456789ab",
        "initial_transaction": {"successor_consumed_sha256": "c" * 64},
    }
    manifest = {
        "bootstrap": {
            "helper_sha256": helper_sha,
            "resume_patch": globals_["EXPECTED_RESUME_PATCH"],
        }
    }
    identity = program["_network_baseline_identity"](
        marker, manifest, manifest_sha, helper_sha
    )
    terminal = phase in {"committed", "rolled-back"}
    document = {
        **identity,
        "status": program["_network_baseline_status_for_phase"](phase),
        "phase": phase,
        "created_at": 1,
        "updated_at": updated_at,
        "completed_at": updated_at if terminal else None,
    }
    return marker, manifest, manifest_sha, helper_sha, document


@pytest.mark.parametrize("raw", [b"0\n", b"1\n"])
def test_installer_sysctl_reader_accepts_only_boolean_values(
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
) -> None:
    program = _load(INSTALLER)
    globals_ = program["_read_network_baseline_sysctls"].__globals__
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def _run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout=raw, stderr=b"")

    monkeypatch.setattr(globals_["subprocess"], "run", _run)
    expected_value = int(raw.strip())
    assert program["_read_network_baseline_sysctls"]() == {
        key: expected_value
        for key in globals_["NETWORK_BASELINE_INITIAL_SYSCTLS"]
    }
    assert [command for command, _kwargs in calls] == [
        ["/usr/sbin/sysctl", "-n", key]
        for key in globals_["NETWORK_BASELINE_INITIAL_SYSCTLS"]
    ]
    for _command, kwargs in calls:
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["timeout"] == 15
        assert kwargs["env"] == {
            "PATH": "/usr/sbin:/usr/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        assert kwargs["cwd"] == "/"


@pytest.mark.parametrize(
    ("returncode", "raw"),
    [
        (1, b"0\n"),
        (0, b""),
        (0, b"2\n"),
        (0, b"-1\n"),
        (0, b"0\n1\n"),
        (0, b"true\n"),
    ],
)
def test_installer_sysctl_reader_rejects_command_and_output_failures(
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    raw: bytes,
) -> None:
    program = _load(INSTALLER)
    globals_ = program["_read_network_baseline_sysctls"].__globals__

    def _run(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            command, returncode, stdout=raw, stderr=b"ignored",
        )

    monkeypatch.setattr(globals_["subprocess"], "run", _run)
    with pytest.raises(program["PatchInstallError"]):
        program["_read_network_baseline_sysctls"]()


def test_network_wal_stage_must_be_the_canonical_next_successor() -> None:
    program = _load(INSTALLER)
    marker, manifest, manifest_sha, helper_sha, current = _network_identity_fixture(
        program, "applied"
    )
    successor = dict(current)
    successor.update(
        phase="committed", status="committed", updated_at=11, completed_at=11
    )
    program["_validate_network_baseline_successor"](
        current, successor, marker, manifest, manifest_sha, helper_sha
    )
    successor["resume_helper_sha256"] = "d" * 64
    with pytest.raises(program["PatchInstallError"]):
        program["_validate_network_baseline_successor"](
            current, successor, marker, manifest, manifest_sha, helper_sha
        )


def _network_properties(
    *, unit: Path, dropin: Path, active: bool, enabled: bool = True
) -> dict[str, str]:
    return {
        "LoadState": "loaded",
        "ActiveState": "active" if active else "inactive",
        "SubState": "exited" if active else "dead",
        "UnitFileState": "enabled" if enabled else "disabled",
        "FragmentPath": str(unit),
        "DropInPaths": str(dropin),
        "MainPID": "0",
        "InvocationID": "2" * 32 if active else "",
        "Result": "success",
        "ExecMainCode": "1" if active else "0",
        "ExecMainStatus": "0",
    }


@pytest.mark.parametrize("exec_main_code", ["1", "exited"])
def test_successful_oneshot_accepts_systemd_exit_code_spellings(
    exec_main_code: str,
) -> None:
    program = _load(INSTALLER)
    properties = _network_properties(
        unit=Path("/unit"), dropin=Path("/dropin"), active=True,
    )
    properties["ExecMainCode"] = exec_main_code
    assert program["_execution_is_successful_oneshot"](properties)


@pytest.mark.parametrize("exec_main_code", ["", "0", "2", "killed"])
def test_successful_oneshot_rejects_non_exit_main_code(
    exec_main_code: str,
) -> None:
    program = _load(INSTALLER)
    properties = _network_properties(
        unit=Path("/unit"), dropin=Path("/dropin"), active=True,
    )
    properties["ExecMainCode"] = exec_main_code
    assert not program["_execution_is_successful_oneshot"](properties)


def test_f_f_committed_network_frontier_is_idempotently_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = _load(INSTALLER)
    globals_ = program["_validate_network_baseline_frontier"].__globals__
    unit = tmp_path / "network.service"
    link = (
        tmp_path
        / "required.target"
        / globals_["NETWORK_BASELINE_UNIT_NAME"]
    )
    dropin = tmp_path / "global-service.conf"
    unit_raw = b"[Unit]\nDescription=fixture\n"
    unit.write_bytes(unit_raw)
    os.chmod(unit, 0o644)
    link.parent.mkdir()
    link.symlink_to(unit)
    globals_.update(
        {
            "NETWORK_BASELINE_UNIT": unit,
            "NETWORK_BASELINE_UNIT_LINK_PARENT": link.parent,
            "NETWORK_BASELINE_UNIT_LINK": link,
            "SYSTEMD_GLOBAL_SERVICE_DROPIN": dropin,
            "ROOT_UID": os.getuid(),
            "ROOT_GID": os.getgid(),
        }
    )
    globals_["EXPECTED_RESUME_PATCH"]["network_baseline"]["unit_sha256"] = (
        hashlib.sha256(unit_raw).hexdigest()
    )
    monkeypatch.setitem(
        globals_,
        "_read_network_baseline_sysctls",
        lambda: dict(globals_["NETWORK_BASELINE_TARGET_SYSCTLS"]),
    )
    monkeypatch.setitem(
        globals_,
        "_unit_execution",
        lambda _unit: _network_properties(unit=unit, dropin=dropin, active=True),
    )
    before = _tree_snapshot(tmp_path)
    program["_validate_network_baseline_frontier"]({"phase": "committed"})
    program["_validate_network_baseline_frontier"]({"phase": "committed"})
    assert _tree_snapshot(tmp_path) == before


def test_prevalidation_dominates_every_publication_mutation() -> None:
    source = INSTALLER.read_text(encoding="utf-8")
    install_body = source[source.index("def install()") :]
    gate = install_body.index("_validate_pre_mutation_runtime()")
    immutable = install_body.index("_immutable_corrective_state(")
    assert gate < immutable < install_body.index("_publish_or_resume(")
    assert gate < install_body.index("_publish_watcher_f(")
