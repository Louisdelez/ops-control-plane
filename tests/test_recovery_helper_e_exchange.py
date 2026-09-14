from __future__ import annotations

import hashlib
import os
from pathlib import Path
import runpy
import stat
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "artifacts/recovery-patch/install-recovery-helper-e"
ENROLLER = ROOT / "artifacts/recovery-patch/enroll-recovery-helper-e"

D_FILES = {
    "release-manifest.v1.json": ROOT / "tests/fixtures/atlas-anchor-d/release-manifest.v1.json",
    "control-plane-bootstrap": ROOT / "tests/fixtures/atlas-anchor-d/control-plane-bootstrap",
    "launch-atlas-reviewed-rpm": ROOT / "tests/fixtures/atlas-anchor-d/launch-atlas-reviewed-rpm",
}
E_FILES = {
    "release-manifest.v1.json": ROOT / "artifacts/recovery-patch/release-manifest.e.v1.json",
    "control-plane-bootstrap": ROOT / "artifacts/recovery-patch/control-plane-bootstrap.e",
    "launch-atlas-reviewed-rpm": ROOT
    / "artifacts/recovery-patch/launch-atlas-reviewed-rpm.e",
}
FILE_MODES = {
    "release-manifest.v1.json": 0o444,
    "control-plane-bootstrap": 0o555,
    "launch-atlas-reviewed-rpm": 0o555,
}


def _payload(paths: dict[str, Path]) -> dict[str, bytes]:
    return {name: path.read_bytes() for name, path in paths.items()}


def _load_program(path: Path) -> dict[str, Any]:
    return runpy.run_path(str(path), run_name=f"test_{path.name.replace('-', '_')}")


@pytest.fixture
def payloads() -> tuple[dict[str, bytes], dict[str, bytes]]:
    d_payload = _payload(D_FILES)
    e_payload = _payload(E_FILES)
    assert {
        name: hashlib.sha256(raw).hexdigest() for name, raw in d_payload.items()
    } == {
        "release-manifest.v1.json": (
            "5bfb61f23b9948f2ba182bedc804e6ac247de33db5ca1c7f61a5d8dec8dddfd1"
        ),
        "control-plane-bootstrap": (
            "082d9ad7b6f5b6cb5111b66bf63f039237155523ac0ec0521c35f89de5558d23"
        ),
        "launch-atlas-reviewed-rpm": (
            "3c2bf03dfc18b7141edc7c1841dd633267d4bf1808eaf72c1211fe6862bb6b72"
        ),
    }
    assert e_payload != d_payload
    return d_payload, e_payload


@pytest.fixture
def harness(tmp_path: Path) -> dict[str, Any]:
    parent = tmp_path / "anchor-parent"
    parent.mkdir(mode=0o755)
    os.chmod(parent, 0o755)
    release_id = "atlas-api-zulip-2026.09.08.11"
    paths = {
        "ANCHOR_PARENT": parent,
        "ANCHOR_ROOT": parent / release_id,
        "PATCH_STAGE": parent / f".{release_id}.recovery-helper-e.staging",
        "PATCH_HISTORY_PARENT": parent / "recovery-patch-history",
        "PATCH_HISTORY_D": (
            parent / "recovery-patch-history" / f"{release_id}.anchor-d"
        ),
    }
    programs: dict[str, dict[str, Any]] = {
        "installer": _load_program(INSTALLER),
        "enroller": _load_program(ENROLLER),
    }
    for program in programs.values():
        globals_ = program["_read_anchor"].__globals__
        globals_.update(paths)
        globals_["ROOT_UID"] = os.getuid()
        globals_["ROOT_GID"] = os.getgid()
    return {**programs, **paths}


def _write_regular(path: Path, raw: bytes, mode: int) -> None:
    path.write_bytes(raw)
    os.chmod(path, mode)


def _write_anchor(path: Path, payload: dict[str, bytes], mode: int = 0o555) -> None:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    for name, raw in payload.items():
        _write_regular(path / name, raw, FILE_MODES[name])
    os.chmod(path, mode)


def _ensure_history_parent(harness: dict[str, Any]) -> None:
    history_parent = harness["PATCH_HISTORY_PARENT"]
    history_parent.mkdir(mode=0o755)
    os.chmod(history_parent, 0o755)


def _assert_complete(
    harness: dict[str, Any], d_payload: dict[str, bytes], e_payload: dict[str, bytes]
) -> None:
    installer = harness["installer"]
    assert installer["_read_anchor"](harness["ANCHOR_ROOT"]) == e_payload
    assert installer["_read_anchor"](harness["PATCH_HISTORY_D"]) == d_payload
    assert stat.S_IMODE(harness["PATCH_HISTORY_D"].stat().st_mode) == 0o555
    assert not os.path.lexists(harness["PATCH_STAGE"])
    assert harness["enroller"]["_validate_state"](e_payload) == "complete"


def _snapshot(path: Path) -> tuple[tuple[object, ...], ...]:
    observed: list[tuple[object, ...]] = []

    def visit(current: Path) -> None:
        metadata = current.lstat()
        relative = "." if current == path else str(current.relative_to(path))
        mode = stat.S_IMODE(metadata.st_mode)
        identity = (relative, mode, metadata.st_uid, metadata.st_gid)
        if stat.S_ISLNK(metadata.st_mode):
            observed.append((*identity, "symlink", os.readlink(current)))
            return
        if stat.S_ISREG(metadata.st_mode):
            observed.append((*identity, "file", current.read_bytes()))
            return
        if stat.S_ISDIR(metadata.st_mode):
            observed.append((*identity, "directory"))
            for child in sorted(current.iterdir(), key=lambda item: item.name):
                visit(child)
            return
        observed.append((*identity, "other", metadata.st_rdev))

    visit(path)
    return tuple(observed)


def test_full_publication_archives_d_cross_parent_without_eacces(
    harness: dict[str, Any], payloads: tuple[dict[str, bytes], dict[str, bytes]]
) -> None:
    """The normal path includes X and the formerly failing cross-parent rename."""
    d_payload, e_payload = payloads
    _write_anchor(harness["ANCHOR_ROOT"], d_payload)

    assert harness["enroller"]["_validate_state"](e_payload) == "publish"
    assert (
        harness["installer"]["_publish_or_resume"](d_payload, e_payload)
        == "published"
    )

    assert harness["PATCH_STAGE"].parent != harness["PATCH_HISTORY_D"].parent
    _assert_complete(harness, d_payload, e_payload)


@pytest.mark.parametrize(
    "temporary_raw",
    [
        pytest.param(b"", id="empty-next"),
        pytest.param(None, id="partial-next"),
    ],
)
def test_s1_partial_0700_stage_is_resumed(
    harness: dict[str, Any],
    payloads: tuple[dict[str, bytes], dict[str, bytes]],
    temporary_raw: bytes | None,
) -> None:
    d_payload, e_payload = payloads
    _write_anchor(harness["ANCHOR_ROOT"], d_payload)
    _ensure_history_parent(harness)
    stage = harness["PATCH_STAGE"]
    stage.mkdir(mode=0o700)
    os.chmod(stage, 0o700)
    _write_regular(
        stage / "release-manifest.v1.json",
        e_payload["release-manifest.v1.json"],
        0o444,
    )
    helper_raw = e_payload["control-plane-bootstrap"]
    next_raw = helper_raw[:257] if temporary_raw is None else temporary_raw
    _write_regular(stage / ".control-plane-bootstrap.next", next_raw, 0o600)

    assert harness["enroller"]["_validate_state"](e_payload) == "publish"
    assert (
        harness["installer"]["_publish_or_resume"](d_payload, e_payload)
        == "published"
    )
    _assert_complete(harness, d_payload, e_payload)


@pytest.mark.parametrize(
    ("state", "stage_mode", "history_mode"),
    [
        pytest.param("X", 0o555, None, id="X-exchanged"),
        pytest.param("A1", 0o755, None, id="A1-stage-made-movable"),
        pytest.param("A2", None, 0o755, id="A2-history-not-sealed"),
    ],
)
def test_x_a1_a2_states_resume_to_one_exact_archive(
    harness: dict[str, Any],
    payloads: tuple[dict[str, bytes], dict[str, bytes]],
    state: str,
    stage_mode: int | None,
    history_mode: int | None,
) -> None:
    del state  # Kept in the parametrization so failures identify the crash frontier.
    d_payload, e_payload = payloads
    _write_anchor(harness["ANCHOR_ROOT"], e_payload)
    _ensure_history_parent(harness)
    if stage_mode is not None:
        _write_anchor(harness["PATCH_STAGE"], d_payload, stage_mode)
    if history_mode is not None:
        _write_anchor(harness["PATCH_HISTORY_D"], d_payload, history_mode)

    assert harness["enroller"]["_validate_state"](e_payload) == "resume-archive"
    assert (
        harness["installer"]["_publish_or_resume"](d_payload, e_payload)
        == "archive-reconciled"
    )
    _assert_complete(harness, d_payload, e_payload)


@pytest.mark.parametrize("state", ["S2", "C"])
def test_s2_and_complete_states_are_idempotent(
    harness: dict[str, Any],
    payloads: tuple[dict[str, bytes], dict[str, bytes]],
    state: str,
) -> None:
    d_payload, e_payload = payloads
    _ensure_history_parent(harness)
    if state == "S2":
        _write_anchor(harness["ANCHOR_ROOT"], d_payload)
        _write_anchor(harness["PATCH_STAGE"], e_payload)
        expected_enrollment = "publish"
        expected_install = "published"
    else:
        _write_anchor(harness["ANCHOR_ROOT"], e_payload)
        _write_anchor(harness["PATCH_HISTORY_D"], d_payload)
        expected_enrollment = "complete"
        expected_install = "already-published"

    assert harness["enroller"]["_validate_state"](e_payload) == expected_enrollment
    assert (
        harness["installer"]["_publish_or_resume"](d_payload, e_payload)
        == expected_install
    )
    _assert_complete(harness, d_payload, e_payload)


def _make_invalid_state(
    case: str,
    harness: dict[str, Any],
    d_payload: dict[str, bytes],
    e_payload: dict[str, bytes],
) -> None:
    _write_anchor(harness["ANCHOR_ROOT"], d_payload)
    _ensure_history_parent(harness)
    if case == "duplicate-d":
        _write_anchor(harness["PATCH_HISTORY_D"], d_payload)
        return

    stage = harness["PATCH_STAGE"]
    stage.mkdir(mode=0o700)
    os.chmod(stage, 0o700)
    next_helper = stage / ".control-plane-bootstrap.next"
    if case == "extra":
        _write_regular(stage / "unexpected", b"not reviewed", 0o600)
    elif case == "symlink":
        next_helper.symlink_to("/dev/null")
    elif case == "bad-bytes":
        expected = e_payload["control-plane-bootstrap"]
        _write_regular(next_helper, expected[:128] + b"not-the-next-byte", 0o600)
    elif case == "final-and-next":
        _write_regular(
            stage / "control-plane-bootstrap",
            e_payload["control-plane-bootstrap"],
            0o555,
        )
        _write_regular(next_helper, e_payload["control-plane-bootstrap"][:128], 0o600)
    else:  # pragma: no cover - protects the table itself.
        raise AssertionError(f"unknown test case: {case}")


@pytest.mark.parametrize(
    "case",
    ["extra", "symlink", "bad-bytes", "final-and-next", "duplicate-d"],
)
def test_invalid_or_ambiguous_states_are_rejected_without_mutation(
    harness: dict[str, Any],
    payloads: tuple[dict[str, bytes], dict[str, bytes]],
    case: str,
) -> None:
    d_payload, e_payload = payloads
    _make_invalid_state(case, harness, d_payload, e_payload)
    before = _snapshot(harness["ANCHOR_PARENT"])

    if case == "duplicate-d":
        with pytest.raises(harness["enroller"]["PatchEnrollmentError"]):
            harness["enroller"]["_validate_state"](e_payload)
    else:
        # The unprivileged enroller deliberately treats root's private 0700
        # worktree as opaque; the root installer performs the byte-level check.
        assert harness["enroller"]["_validate_state"](e_payload) == "publish"
    assert _snapshot(harness["ANCHOR_PARENT"]) == before

    with pytest.raises(harness["installer"]["PatchInstallError"]):
        harness["installer"]["_publish_or_resume"](d_payload, e_payload)
    assert _snapshot(harness["ANCHOR_PARENT"]) == before
