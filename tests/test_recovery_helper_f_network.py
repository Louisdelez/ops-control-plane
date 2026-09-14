from __future__ import annotations

from pathlib import Path
import runpy
from types import SimpleNamespace
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "artifacts/recovery-patch/control-plane-bootstrap.f"


@pytest.fixture
def program() -> dict[str, Any]:
    return runpy.run_path(str(HELPER), run_name="test_recovery_helper_f")


def _identity_inputs(
    program: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], str]:
    marker = {
        **program["RESUME_PATCH_DEPLOYMENT"],
        "corrective_id": "corrective-test-id",
        "initial_transaction": {"successor_consumed_sha256": "a" * 64},
        "successor_consumed": {"schema_version": 1},
    }
    manifest = {
        "bootstrap": {
            "helper_sha256": "b" * 64,
            "resume_patch": program["RESUME_PATCH_CONTRACT"],
        }
    }
    return marker, manifest, "c" * 64


def _transaction(
    program: dict[str, Any], phase: str = "prepared"
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    marker, manifest, manifest_sha256 = _identity_inputs(program)
    document = {
        **program["_network_baseline_identity"](
            marker, manifest, manifest_sha256
        ),
        "status": program["_network_baseline_status_for_phase"](phase),
        "phase": phase,
        "created_at": 10,
        "updated_at": 10,
        "completed_at": 10 if phase in {"committed", "rolled-back"} else None,
    }
    return document, marker, manifest, manifest_sha256


def _successor(program: dict[str, Any], current: dict[str, Any], phase: str) -> dict[str, Any]:
    successor = dict(current)
    successor.update(
        {
            "phase": phase,
            "status": program["_network_baseline_status_for_phase"](phase),
            "updated_at": current["updated_at"] + 1,
            "completed_at": (
                current["updated_at"] + 1
                if phase in {"committed", "rolled-back"}
                else None
            ),
        }
    )
    return successor


def test_unit_is_host_namespace_setter_and_changes_only_through_helper(
    program: dict[str, Any],
) -> None:
    unit = program["NETWORK_BASELINE_UNIT_BYTES"]
    assert b"PrivateNetwork=no\n" in unit
    assert b"ProtectKernelTunables=no\n" in unit
    assert b"CapabilityBoundingSet=CAP_NET_ADMIN\n" in unit
    assert b"--network-baseline-apply\n" in unit
    assert b"--network-baseline-restore\n" in unit
    assert b"ExecStart=/usr/sbin/sysctl" not in unit
    assert b"ExecStopPost=/usr/sbin/sysctl" not in unit
    assert b"Before=containerd.service docker.service docker.socket\n" in unit
    assert b"RequiredBy=docker.service\n" in unit
    assert b"WantedBy=multi-user.target" not in unit
    assert b"ConditionPathExists=" not in unit
    assert b"TimeoutStartSec=180\n" in unit
    assert b"TimeoutStopSec=180\n" in unit
    assert program["NETWORK_BASELINE_UNIT_LINK"] == Path(
        "/etc/systemd/system/docker.service.requires/"
        "ops-control-plane-bootstrap-network-baseline.service"
    )
    assert program["RESUME_PATCH_CONTRACT"]["network_baseline"][
        "unit_link_parent"
    ] == "/etc/systemd/system/docker.service.requires"
    assert program["RESUME_PATCH_CONTRACT"]["network_baseline"][
        "unit_link_parent_mode"
    ] == 0o755


def test_apply_and_rollback_wal_edges_are_closed(program: dict[str, Any]) -> None:
    current, marker, manifest, manifest_sha256 = _transaction(program)
    for phase in program["NETWORK_BASELINE_APPLY_PHASES"][1:]:
        successor = _successor(program, current, phase)
        program["_validate_network_baseline_successor"](
            current, successor, marker, manifest, manifest_sha256
        )
        current = successor

    applied, marker, manifest, manifest_sha256 = _transaction(program, "applied")
    current = _successor(program, applied, "rollback-stop-planned")
    program["_validate_network_baseline_successor"](
        applied, current, marker, manifest, manifest_sha256
    )
    for phase in program["NETWORK_BASELINE_ROLLBACK_PHASES"][1:]:
        successor = _successor(program, current, phase)
        program["_validate_network_baseline_successor"](
            current, successor, marker, manifest, manifest_sha256
        )
        current = successor


def test_wal_rejects_skips_and_identity_rewrites(program: dict[str, Any]) -> None:
    current, marker, manifest, manifest_sha256 = _transaction(program)
    early_rollback = _successor(program, current, "rollback-stopped")
    program["_validate_network_baseline_successor"](
        current, early_rollback, marker, manifest, manifest_sha256
    )
    premature_stop = _successor(program, current, "rollback-stop-planned")
    with pytest.raises(program["BootstrapError"]):
        program["_validate_network_baseline_successor"](
            current, premature_stop, marker, manifest, manifest_sha256
        )

    skipped = _successor(program, current, "applied")
    with pytest.raises(program["BootstrapError"]):
        program["_validate_network_baseline_successor"](
            current, skipped, marker, manifest, manifest_sha256
        )

    successor = _successor(program, current, "unit-publish-planned")
    successor["unit_sha256"] = "d" * 64
    with pytest.raises(program["BootstrapError"]):
        program["_validate_network_baseline_successor"](
            current, successor, marker, manifest, manifest_sha256
        )


def test_network_wal_requires_terminal_corrective_handoff(
    program: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = program["_read_network_baseline_frontier"]
    globals_ = function.__globals__
    monkeypatch.setitem(
        globals_, "NETWORK_BASELINE_TRANSACTION_PATH", tmp_path / "network.json"
    )
    monkeypatch.setitem(
        globals_,
        "NETWORK_BASELINE_TRANSACTION_STAGING_PATH",
        tmp_path / ".network.json.next",
    )
    boolean_fields = program["CORRECTIVE_TRANSACTION_BOOLEAN_FIELDS"]
    assert len(boolean_fields) == 6
    completed = {
        "status": "completed",
        "phase": "completed",
        **{field: True for field in boolean_fields},
    }
    marker, manifest, manifest_sha256 = _identity_inputs(program)

    invalid_frontiers = [
        None,
        {**completed, "status": "running"},
        {**completed, "phase": "successor-marker-published"},
        {**completed, boolean_fields[-1]: False},
    ]
    for frontier in invalid_frontiers:
        monkeypatch.setitem(
            globals_,
            "_read_corrective_wal_frontier",
            lambda *_args, _frontier=frontier, **_kwargs: _frontier,
        )
        with pytest.raises(program["BootstrapError"]) as caught:
            function(
                marker,
                manifest,
                manifest_sha256,
                promote_staging=False,
            )
        assert caught.value.code == "network_baseline_recovery_order"

    monkeypatch.setitem(
        globals_,
        "_read_corrective_wal_frontier",
        lambda *_args, **_kwargs: completed,
    )
    assert (
        function(
            marker,
            manifest,
            manifest_sha256,
            promote_staging=False,
        )
        is None
    )


def test_requires_directory_intent_effect_and_inverse_are_ordered(
    program: dict[str, Any],
) -> None:
    apply_phases = program["NETWORK_BASELINE_APPLY_PHASES"]
    assert apply_phases[2:7] == (
        "unit-published",
        "requires-dir-publish-planned",
        "requires-dir-published",
        "link-publish-planned",
        "link-published",
    )
    rollback_phases = program["NETWORK_BASELINE_ROLLBACK_PHASES"]
    assert rollback_phases[2:7] == (
        "rollback-link-remove-planned",
        "rollback-link-removed",
        "rollback-requires-dir-remove-planned",
        "rollback-requires-dir-removed",
        "rollback-unit-remove-planned",
    )


def test_requires_directory_0700_crash_frontier_is_repaired_only_when_empty(
    program: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = program["_publish_network_baseline_link_parent"]
    globals_ = function.__globals__
    parent = tmp_path / "docker.service.requires"
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    monkeypatch.setitem(globals_, "NETWORK_BASELINE_UNIT_LINK_PARENT", parent)
    monkeypatch.setattr(globals_["os"], "chown", lambda *_args: None)

    original_lstat = globals_["os"].lstat

    def root_lstat(path: Any) -> Any:
        observed = original_lstat(path)
        if Path(path) != parent:
            return observed
        return SimpleNamespace(
            st_mode=observed.st_mode,
            st_uid=0,
            st_gid=0,
        )

    monkeypatch.setattr(globals_["os"], "lstat", root_lstat)
    monkeypatch.setitem(globals_, "_fsync_directory", lambda _path: None)
    function()
    assert parent.stat().st_mode & 0o777 == 0o755

    parent.chmod(0o700)
    (parent / "foreign").write_text("foreign", encoding="ascii")
    with pytest.raises(program["BootstrapError"]):
        function()


def test_requires_directory_rmdir_is_exact_empty_and_post_d_only(
    program: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remove = program["_remove_network_baseline_link_parent"]
    globals_ = remove.__globals__
    parent = tmp_path / "docker.service.requires"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    foreign = parent / "foreign"
    foreign.write_text("foreign", encoding="ascii")
    monkeypatch.setitem(globals_, "NETWORK_BASELINE_UNIT_LINK_PARENT", parent)
    monkeypatch.setitem(globals_, "_fsync_directory", lambda _path: None)
    original_lstat = globals_["os"].lstat

    def root_lstat(path: Any) -> Any:
        observed = original_lstat(path)
        if Path(path) != parent:
            return observed
        return SimpleNamespace(
            st_mode=observed.st_mode,
            st_uid=0,
            st_gid=0,
        )

    monkeypatch.setattr(globals_["os"], "lstat", root_lstat)
    with pytest.raises(program["BootstrapError"]):
        remove()
    assert parent.is_dir()

    foreign.unlink()
    remove()
    assert not parent.exists()

    transaction, marker, manifest, manifest_sha256 = _transaction(
        program, "rollback-requires-dir-remove-planned"
    )
    removal_attempted = False

    def forbidden_remove() -> None:
        nonlocal removal_attempted
        removal_attempted = True

    rollback = program["_rollback_network_baseline_transaction"]
    monkeypatch.setitem(
        rollback.__globals__,
        "_remove_network_baseline_link_parent",
        forbidden_remove,
    )
    with pytest.raises(program["BootstrapError"]) as caught:
        rollback(
            transaction,
            marker,
            manifest,
            manifest_sha256,
            {"status": "running"},
        )
    assert caught.value.code == "network_baseline_recovery_order"
    assert not removal_attempted


def test_preflight_relaxes_only_exact_empty_initial_frontier(
    program: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = program["_network_baseline_preflight_sysctls_allowed"]
    globals_ = function.__globals__
    for name in (
        "NETWORK_BASELINE_TRANSACTION_PATH",
        "NETWORK_BASELINE_TRANSACTION_STAGING_PATH",
        "NETWORK_BASELINE_UNIT",
        "NETWORK_BASELINE_UNIT_LINK_PARENT",
        "NETWORK_BASELINE_UNIT_LINK",
    ):
        monkeypatch.setitem(globals_, name, tmp_path / name.lower())
    monkeypatch.setitem(globals_, "_network_baseline_unit_is_absent", lambda: True)
    initial = {
        key: str(value).encode()
        for key, value in program["NETWORK_BASELINE_INITIAL_SYSCTLS"].items()
    }
    target = {
        key: str(value).encode()
        for key, value in program["NETWORK_BASELINE_TARGET_SYSCTLS"].items()
    }
    assert function(target, allow_network_baseline_initial=False)
    assert function(initial, allow_network_baseline_initial=True)
    assert not function(initial, allow_network_baseline_initial=False)
    drift = dict(initial)
    drift["net.ipv6.conf.all.forwarding"] = b"1"
    assert not function(drift, allow_network_baseline_initial=True)
    globals_["NETWORK_BASELINE_UNIT"].write_text("foreign", encoding="ascii")
    assert not function(initial, allow_network_baseline_initial=True)


def test_setter_writes_only_ipv4_and_requires_ipv6_invariant(
    program: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    function = program["_write_network_baseline_sysctls"]
    globals_ = function.__globals__
    target = program["NETWORK_BASELINE_TARGET_SYSCTLS"]
    readings = iter(
        [program["NETWORK_BASELINE_INITIAL_SYSCTLS"], target]
    )
    globals_["_network_baseline_has_host_network_namespace"] = lambda: True
    globals_["_read_network_baseline_sysctls"] = lambda: dict(next(readings))
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(globals_["subprocess"], "run", fake_run)
    function(target)
    assert calls == [
        ["/usr/sbin/sysctl", "-q", "-w", "net.ipv4.ip_forward=1"]
    ]

    globals_["_read_network_baseline_sysctls"] = lambda: {
        **program["NETWORK_BASELINE_INITIAL_SYSCTLS"],
        "net.ipv6.conf.all.forwarding": 1,
    }
    with pytest.raises(program["BootstrapError"]):
        function(target)


def test_private_watcher_uses_setter_result_not_private_sysctls(
    program: dict[str, Any]
) -> None:
    function = program["_start_network_baseline_unit"]
    globals_ = function.__globals__
    commands: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    globals_["_network_baseline_systemctl"] = (
        lambda *args, **kwargs: commands.append((args, kwargs))
    )
    globals_["_systemctl_state"] = lambda *_args: True
    globals_["_network_baseline_has_host_network_namespace"] = lambda: False
    globals_["_read_network_baseline_sysctls"] = lambda: (_ for _ in ()).throw(
        AssertionError("private /proc/sys must not be evidence")
    )
    globals_["_validate_network_baseline_unit"] = lambda **_kwargs: None
    globals_["_validate_network_baseline_service_result"] = lambda **_kwargs: None
    function()
    assert commands == [
        (("daemon-reload",), {}),
        (("reset-failed", program["NETWORK_BASELINE_UNIT_NAME"]), {}),
        (
            ("restart", program["NETWORK_BASELINE_UNIT_NAME"]),
            {"timeout": 390},
        ),
    ]


def test_rollback_start_and_stop_wait_for_setter_bounds(
    program: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    function = program["_stop_network_baseline_unit"]
    globals_ = function.__globals__
    unit = tmp_path / program["NETWORK_BASELINE_UNIT_NAME"]
    unit.write_bytes(program["NETWORK_BASELINE_UNIT_BYTES"])
    monkeypatch.setitem(globals_, "NETWORK_BASELINE_UNIT", unit)
    monkeypatch.setitem(
        globals_,
        "_read_exact_root_file",
        lambda *_args, **_kwargs: program["NETWORK_BASELINE_UNIT_BYTES"],
    )
    monkeypatch.setitem(
        globals_, "_validate_network_baseline_link_parent", lambda *_args: None
    )
    monkeypatch.setitem(globals_, "_validate_enablement_link", lambda *_args: None)
    monkeypatch.setitem(globals_, "_systemctl_state", lambda *_args: False)
    monkeypatch.setitem(
        globals_, "_network_baseline_has_host_network_namespace", lambda: False
    )
    monkeypatch.setitem(
        globals_, "_validate_network_baseline_service_result", lambda **_kwargs: None
    )
    commands: list[tuple[tuple[str, ...], dict[str, Any]]] = []
    monkeypatch.setitem(
        globals_,
        "_network_baseline_systemctl",
        lambda *args, **kwargs: commands.append((args, kwargs)),
    )
    function()
    assert commands == [
        (("daemon-reload",), {}),
        (("reset-failed", program["NETWORK_BASELINE_UNIT_NAME"]), {}),
        (
            ("start", program["NETWORK_BASELINE_UNIT_NAME"]),
            {"timeout": 210},
        ),
        (
            ("stop", program["NETWORK_BASELINE_UNIT_NAME"]),
            {"timeout": 210},
        ),
    ]


def test_restore_gate_requires_safe_pre_d_or_clean_rolled_back_d(
    program: dict[str, Any]
) -> None:
    function = program["_network_baseline_restore_is_authorized"]
    globals_ = function.__globals__
    marker, _manifest, _manifest_sha256 = _identity_inputs(program)
    standard = {"status": "running"}
    globals_["_read_standard_transaction_frontier"] = lambda **_kwargs: standard
    globals_["_read_consumed_identity_at"] = (
        lambda _path, _transaction: marker["successor_consumed"]
    )
    globals_["_transaction_matches_safe_enrollment_frontier"] = (
        lambda *_args, **_kwargs: True
    )
    globals_["_terminal_standard_recovery_is_clean"] = lambda _transaction: False
    assert function(marker, "start-planned")
    assert not function(marker, "rollback-stop-planned")

    standard["status"] = "rolled-back"
    globals_["_terminal_standard_recovery_is_clean"] = lambda _transaction: True
    assert function(marker, "rollback-stop-planned")
