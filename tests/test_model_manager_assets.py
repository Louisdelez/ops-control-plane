from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import runpy
import stat
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "apps" / "model-manager"
ATLAS_LAUNCHER = ROOT / "scripts" / "launch-atlas-reviewed-rpm"
ATLAS_ANCHOR_INSTALLER = ROOT / "scripts" / "install-atlas-trust-anchor"
ATLAS_ANCHOR_ENROLLER = ROOT / "scripts" / "enroll-atlas-trust-anchor"
SOURCE_MANIFEST = ROOT / "deploy" / "control-plane" / "release-manifest.v1.json"


def test_reviewed_rpm_launcher_materializes_the_exact_sealed_payload() -> None:
    namespace = runpy.run_path(str(ATLAS_LAUNCHER))
    descriptor, contract = namespace["prepare_reviewed_payload"](
        SOURCE_MANIFEST,
        owner_uid=os.getuid(),
        owner_gid=os.getgid(),
        manifest_mode=0o644,
        enforce_root_anchor=False,
    )
    try:
        metadata = os.fstat(descriptor)
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        assert os.readlink(f"/proc/self/fd/{descriptor}") \
            == "/memfd:atlas-reviewed-rpm (deleted)"
        assert digest.hexdigest() == contract["payload_sha256"]
        assert metadata.st_uid == os.getuid()
        assert stat.S_IMODE(metadata.st_mode) == 0o500
        assert metadata.st_nlink == 0
        assert metadata.st_size > 0
        assert fcntl.fcntl(descriptor, fcntl.F_GET_SEALS) == 15
    finally:
        os.close(descriptor)


def test_reviewed_rpm_launcher_has_a_closed_non_secret_interface() -> None:
    source = ATLAS_LAUNCHER.read_text(encoding="utf-8")
    assert ATLAS_LAUNCHER.stat().st_mode & 0o777 == 0o755
    assert 'EXPECTED_PAYLOAD_MEMBER = "/usr/bin/ops-model-manager"' in source
    assert 'REVIEWED_MEMFD_NAME = "atlas-reviewed-rpm"' in source
    assert '"/usr/local/lib/ops-control-plane/atlas-api-zulip-2026.09.08.11"' in source
    assert "owner_uid=0" in source
    assert "owner_gid=0" in source
    assert "exact_mode=0o555" in source
    assert "os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING" in source
    assert "fcntl.F_SEAL_WRITE" in source
    assert "os.execve(executable" in source
    assert "if len(sys.argv) != 1:" in source
    assert "shell=True" not in source
    assert "tempfile" not in source
    assert "password" not in source.casefold()


def test_tauri_surface_is_vertical_offline_and_explicitly_permissioned() -> None:
    config = json.loads(
        (APP / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8")
    )
    window = config["app"]["windows"]
    assert window == [
        {
            "label": "main",
            "title": "Modeles IA",
            "width": 500,
            "height": 900,
            "minWidth": 420,
            "minHeight": 720,
            "center": True,
            "resizable": True,
            "fullscreen": False,
            "maximized": False,
            "visible": True,
        }
    ]
    assert config["build"]["frontendDist"] == "../ui"
    assert config["app"]["security"]["assetProtocol"]["enable"] is False
    csp = config["app"]["security"]["csp"]
    assert "default-src 'self'" in csp
    assert "connect-src ipc: http://ipc.localhost" in csp
    assert "https:" not in csp
    assert set(config["bundle"]["targets"]) == {"rpm", "deb", "appimage"}

    capability = json.loads(
        (APP / "src-tauri" / "capabilities" / "main.json").read_text(
            encoding="utf-8"
        )
    )
    assert capability["local"] is True
    assert capability["windows"] == ["main"]
    assert set(capability["permissions"]) == {
        "allow-get-catalogue",
        "allow-get-runtime-snapshot",
        "allow-simulate-cost",
        "allow-preview-candidates",
        "allow-refresh-provider-finance",
        "allow-get-bootstrap-status",
        "allow-run-bootstrap-preflight",
        "allow-run-bootstrap-corrective-preflight",
        "allow-run-bootstrap-recovery-preflight",
        "allow-begin-bootstrap",
        "allow-begin-corrective-bootstrap",
        "allow-begin-recovery-bootstrap",
        "allow-get-bootstrap-result",
        "allow-cancel-bootstrap",
        "allow-save-provider-credential",
        "allow-open-zulip",
    }
    assert not any(
        prefix in permission
        for permission in capability["permissions"]
        for prefix in ("shell:", "http:", "fs:")
    )


def test_rust_dependencies_have_no_shell_http_or_filesystem_plugin() -> None:
    manifest = tomllib.loads(
        (APP / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8")
    )
    dependencies = set(manifest["dependencies"])
    assert dependencies == {
        "serde",
        "serde_json",
        "sha2",
        "socket2",
        "rustix",
        "tauri",
        "zeroize",
    }
    assert not any(name.startswith("tauri-plugin-") for name in dependencies)


def test_tauri_ui_collects_credentials_ephemerally_and_uses_native_pipes() -> None:
    html = (APP / "ui" / "index.html").read_text(encoding="utf-8")
    javascript = (APP / "ui" / "app.js").read_text(encoding="utf-8")
    onboarding = (APP / "ui" / "onboarding.js").read_text(encoding="utf-8")
    rust_security = (APP / "src-tauri" / "src" / "security.rs").read_text(
        encoding="utf-8"
    )
    rust_onboarding = (APP / "src-tauri" / "src" / "onboarding.rs").read_text(
        encoding="utf-8"
    )
    combined_ui = html + javascript + onboarding
    assert 'id="bootstrap-onboarding"' in html
    assert 'id="credential-dialog"' in html
    assert 'src="./onboarding.js"' in html
    assert onboarding.count('type="password"') >= 4
    assert 'session.callNative("begin_recovery_bootstrap", request)' in onboarding
    assert '"run_bootstrap_recovery_preflight"' in onboarding
    assert 'callNative("save_provider_credential", request)' in javascript
    assert 'openBaoInput.value = "";' in javascript
    assert 'apiKeyInput.value = "";' in javascript
    assert 'request.openBaoPassword = "";' in javascript
    assert 'request.apiKey = "";' in javascript
    assert 'request.openBaoPassword = "";' in onboarding
    assert 'request.zulipPassword = "";' in onboarding
    assert "ATLASBOOT1\\n" in rust_onboarding
    assert "ATLASKEY1\\n" in rust_security
    assert "Zeroizing" in rust_onboarding
    assert "Zeroizing" in rust_security
    for forbidden in (
        "localStorage",
        "sessionStorage",
        "console.log",
        'target="_blank"',
        "window.open(",
    ):
        assert forbidden not in combined_ui


def test_root_anchor_enrollment_is_inert_atomic_and_credential_free() -> None:
    source = ATLAS_ANCHOR_INSTALLER.read_text(encoding="utf-8")
    assert ATLAS_ANCHOR_INSTALLER.stat().st_mode & 0o777 == 0o755
    assert str(runpy.run_path(str(ATLAS_ANCHOR_INSTALLER))['ANCHOR_ROOT']) == '/usr/local/lib/ops-control-plane/atlas-api-zulip-2026.09.08.11'
    assert 'os.rename(STAGING_ROOT, ANCHOR_ROOT)' in source
    assert 'os.rename(ANCHOR_ROOT, HISTORY_ANCHOR)' in source
    assert '_fsync_directory(ANCHOR_PARENT)' in source
    assert '_fsync_directory(HISTORY_PARENT)' in source
    assert 'HISTORY_PARENT = ANCHOR_PARENT / "history"' in source
    assert 'f"{RELEASE_ID}.pre-reconciliation"' in source
    assert 'f"{ANCHOR_ROOT.name}.pre-third-reconciliation"' in source
    assert "LEGACY_SHA256" in source
    assert "PRE_THIRD_SHA256" in source
    assert "fcntl.flock(lock_descriptor, fcntl.LOCK_EX)" in source
    assert "os.umask(0o077)" in source
    assert "_validate_manifest_structure(manifest)" in source
    assert "compile(source" in source
    assert "os.unlink(" not in source
    assert "os.remove(" not in source
    assert "rmtree(" not in source
    assert '"release-manifest.v1.json": 0o444' in source
    assert '"control-plane-bootstrap": 0o555' in source
    assert '"launch-atlas-reviewed-rpm": 0o555' in source
    assert "AUTHORITY_BYTES" in source
    assert "physical-polkit-tofu-v1" in source
    assert "_read_sealed_snapshot" in source
    assert "/proc/{parent_pid}/fd/{descriptor_number}" in source
    assert "SOURCE_MANIFEST" not in source
    lowered = source.casefold()
    for forbidden in (
        "getpass",
        "input(",
        "systemctl",
        "subprocess",
    ):
        assert forbidden not in lowered


def test_graphical_anchor_enroller_seals_confirmed_bytes_before_polkit() -> None:
    source = ATLAS_ANCHOR_ENROLLER.read_text(encoding="utf-8")
    assert ATLAS_ANCHOR_ENROLLER.stat().st_mode & 0o777 == 0o755
    assert "os.memfd_create(" in source
    assert "os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING" in source
    assert "fcntl.F_SEAL_WRITE" in source
    assert 'zenity = _trusted_tool("/usr/bin/zenity")' in source
    assert 'pkexec = _trusted_tool("/usr/bin/pkexec")' in source
    assert 'python = _trusted_tool("/usr/bin/python3.14")' in source
    assert 'f"/proc/{parent_pid}/fd/{installer_fd}"' in source
    assert "_confirmation_digest(nonce, snapshots)" in source
    assert "os.execve(str(ROOT_LAUNCHER)" in source
    assert "LEGACY_SHA256" in source
    assert "_validate_history()" in source
    assert "Réconcilier l’ancre sécurisée" in source
    assert "Historique : {history_target}" in source
    assert "INTERMEDIATE_HISTORY_ANCHOR" in source
    assert "INTERMEDIATE_SHA256" in source
    assert "THIRD_HISTORY_ANCHOR" in source
    assert "PRE_THIRD_SHA256" in source
    assert "shell=True" not in source
    assert source.index("if _validate_existing_anchor():") < source.index(
        "snapshots = tuple(_read_source"
    )


def test_existing_anchor_is_used_directly_and_partial_anchor_fails_closed(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(ATLAS_ANCHOR_ENROLLER))
    validate = namespace["_validate_existing_anchor"]
    globals_ = validate.__globals__
    parent = tmp_path / "ops-control-plane"
    anchor = parent / "release"
    launcher = anchor / "launch-atlas-reviewed-rpm"
    helper = anchor / "control-plane-bootstrap"
    manifest = anchor / "release-manifest.v1.json"
    history_parent = parent / "history"
    history_anchor = history_parent / "release.pre-reconciliation"
    staging = parent / ".release.staging"
    parent.mkdir(mode=0o755)
    anchor.mkdir(mode=0o755)
    launcher_raw = ATLAS_LAUNCHER.read_bytes()
    helper_raw = (ROOT / "deploy/control-plane/bin/control-plane-bootstrap").read_bytes()
    launcher.write_bytes(launcher_raw)
    helper.write_bytes(helper_raw)
    launcher.chmod(0o555)
    helper.chmod(0o555)
    manifest_document = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    manifest_document["bootstrap"]["helper_sha256"] = hashlib.sha256(
        helper_raw
    ).hexdigest()
    manifest_document["trust_anchor"] = {
        "version_directory": str(anchor),
        "manifest_path": str(manifest),
        "bootstrap_helper_path": str(helper),
        "launcher_path": str(launcher),
        "manifest_mode": "0444",
        "bootstrap_helper_mode": "0555",
        "launcher_mode": "0555",
        "launcher_sha256": hashlib.sha256(launcher_raw).hexdigest(),
        "enrollment": "physical-polkit-tofu-v1",
    }
    manifest.write_text(json.dumps(manifest_document), encoding="utf-8")
    manifest.chmod(0o444)
    anchor.chmod(0o555)
    globals_["ANCHOR_PARENT"] = parent
    globals_["ANCHOR_ROOT"] = anchor
    globals_["ROOT_LAUNCHER"] = launcher
    globals_["HISTORY_PARENT"] = history_parent
    globals_["HISTORY_ANCHOR"] = history_anchor
    globals_["INTERMEDIATE_HISTORY_ANCHOR"] = (
        history_parent / "release.pre-second-reconciliation"
    )
    globals_["THIRD_HISTORY_ANCHOR"] = (
        history_parent / "release.pre-third-reconciliation"
    )
    globals_["STAGING_ROOT"] = staging
    globals_["ROOT_UID"] = os.getuid()
    globals_["ROOT_GID"] = os.getgid()
    assert validate() is True

    anchor.chmod(0o755)
    launcher.unlink()
    anchor.chmod(0o555)
    with pytest.raises(namespace["EnrollmentLaunchError"]):
        validate()


def test_graphical_enroller_recognizes_only_the_exact_three_reconciliation_states(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(ATLAS_ANCHOR_ENROLLER))
    validate = namespace["_validate_existing_anchor"]
    globals_ = validate.__globals__
    parent = tmp_path / "ops-control-plane"
    anchor = parent / "release"
    history_parent = parent / "history"
    legacy_history = history_parent / "release.pre-reconciliation"
    intermediate_history = history_parent / "release.pre-second-reconciliation"
    third_history = history_parent / "release.pre-third-reconciliation"
    legacy = {
        "release-manifest.v1.json": b"manifest-a",
        "control-plane-bootstrap": b"helper-a",
        "launch-atlas-reviewed-rpm": b"launcher-a",
    }
    intermediate = {
        "release-manifest.v1.json": b"manifest-b",
        "control-plane-bootstrap": b"helper-b",
        "launch-atlas-reviewed-rpm": b"launcher-b",
    }
    pre_third = {
        "release-manifest.v1.json": b"manifest-c",
        "control-plane-bootstrap": b"helper-c",
        "launch-atlas-reviewed-rpm": b"launcher-c",
    }
    globals_["LEGACY_SHA256"] = {
        name: hashlib.sha256(raw).hexdigest() for name, raw in legacy.items()
    }
    globals_["INTERMEDIATE_SHA256"] = {
        name: hashlib.sha256(raw).hexdigest() for name, raw in intermediate.items()
    }
    globals_["PRE_THIRD_SHA256"] = {
        name: hashlib.sha256(raw).hexdigest() for name, raw in pre_third.items()
    }
    globals_["ANCHOR_PARENT"] = parent
    globals_["ANCHOR_ROOT"] = anchor
    globals_["ROOT_LAUNCHER"] = anchor / "launch-atlas-reviewed-rpm"
    globals_["HISTORY_PARENT"] = history_parent
    globals_["HISTORY_ANCHOR"] = legacy_history
    globals_["INTERMEDIATE_HISTORY_ANCHOR"] = intermediate_history
    globals_["THIRD_HISTORY_ANCHOR"] = third_history
    globals_["STAGING_ROOT"] = parent / ".release.staging"
    globals_["ROOT_UID"] = os.getuid()
    globals_["ROOT_GID"] = os.getgid()

    def write_anchor(root: Path, payload: dict[str, bytes]) -> None:
        root.mkdir(parents=True, mode=0o755)
        for name, raw in payload.items():
            path = root / name
            path.write_bytes(raw)
            path.chmod(namespace["ANCHOR_FILES"][name][1])
        root.chmod(0o555)

    parent.mkdir(mode=0o755)
    write_anchor(legacy_history, legacy)
    write_anchor(anchor, intermediate)
    assert validate() is False

    anchor.chmod(0o755)
    os.rename(anchor, intermediate_history)
    intermediate_history.chmod(0o555)
    assert validate() is False

    write_anchor(anchor, pre_third)
    assert validate() is False

    anchor.chmod(0o755)
    os.rename(anchor, third_history)
    third_history.chmod(0o555)
    assert validate() is False

    write_anchor(anchor, pre_third)
    with pytest.raises(namespace["EnrollmentLaunchError"], match="troisième"):
        validate()


def test_anchor_file_publication_recovers_exact_crash_frontiers(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(ATLAS_ANCHOR_INSTALLER))
    publish = namespace["_publish_stage_file"]
    validate = namespace["_validate_published"]
    error = namespace["EnrollmentError"]
    publish.__globals__["ROOT_UID"] = os.getuid()
    publish.__globals__["ROOT_GID"] = os.getgid()

    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    expected = {
        "release-manifest.v1.json": b"manifest-complet",
        "control-plane-bootstrap": b"helper-complet",
        "launch-atlas-reviewed-rpm": b"launcher-complet",
    }
    modes = namespace["ANCHOR_FILES"]

    partial = stage / ".release-manifest.v1.json.next"
    partial.write_bytes(b"manifest-")
    partial.chmod(0o600)
    publish(stage / "release-manifest.v1.json", expected["release-manifest.v1.json"], 0o444)

    sealed = stage / ".control-plane-bootstrap.next"
    sealed.write_bytes(expected["control-plane-bootstrap"])
    sealed.chmod(0o555)
    publish(stage / "control-plane-bootstrap", expected["control-plane-bootstrap"], 0o555)

    publish(stage / "launch-atlas-reviewed-rpm", expected["launch-atlas-reviewed-rpm"], 0o555)
    stage.chmod(0o555)
    validate(stage, expected)
    for name, mode in modes.items():
        assert stat.S_IMODE((stage / name).stat().st_mode) == mode

    corrupt_stage = tmp_path / "corrupt-stage"
    corrupt_stage.mkdir(mode=0o700)
    corrupt = corrupt_stage / ".release-manifest.v1.json.next"
    corrupt.write_bytes(b"autre-octet")
    corrupt.chmod(0o600)
    with pytest.raises(error):
        publish(
            corrupt_stage / "release-manifest.v1.json",
            expected["release-manifest.v1.json"],
            0o444,
        )
    assert corrupt.read_bytes() == b"autre-octet"


def test_anchor_reconciliation_archives_legacy_and_recovers_between_renames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(ATLAS_ANCHOR_INSTALLER))
    reconcile = namespace["_publish_or_reconcile"]
    prepare = namespace["_prepare_staging"]
    globals_ = reconcile.__globals__
    globals_["ROOT_UID"] = os.getuid()
    globals_["ROOT_GID"] = os.getgid()

    expected = {
        "release-manifest.v1.json": b"manifest-current",
        "control-plane-bootstrap": b"helper-current",
        "launch-atlas-reviewed-rpm": b"launcher-current",
    }
    legacy = {
        "release-manifest.v1.json": b"manifest-legacy",
        "control-plane-bootstrap": b"helper-legacy",
        "launch-atlas-reviewed-rpm": b"launcher-legacy",
    }
    globals_["LEGACY_SHA256"] = {
        name: hashlib.sha256(raw).hexdigest() for name, raw in legacy.items()
    }
    real_rename = os.rename

    def privileged_directory_rename(source: os.PathLike[str], target: os.PathLike[str]) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if (
            source_path == globals_["ANCHOR_ROOT"]
            and target_path == globals_["HISTORY_ANCHOR"]
        ):
            # enroll() is root in production.  The unprivileged unit test must
            # emulate CAP_DAC_OVERRIDE for the immutable 0555 directory move.
            source_path.chmod(0o755)
            real_rename(source_path, target_path)
            target_path.chmod(0o555)
            return
        real_rename(source_path, target_path)

    monkeypatch.setattr(os, "rename", privileged_directory_rename)

    def configure(case: str) -> tuple[Path, Path, Path, Path]:
        parent = tmp_path / case
        anchor = parent / "release"
        stage = parent / ".release.staging"
        history_parent = parent / "history"
        history_anchor = history_parent / "release.pre-reconciliation"
        parent.mkdir(mode=0o755)
        globals_["ANCHOR_PARENT"] = parent
        globals_["ANCHOR_ROOT"] = anchor
        globals_["STAGING_ROOT"] = stage
        globals_["HISTORY_PARENT"] = history_parent
        globals_["HISTORY_ANCHOR"] = history_anchor
        return parent, anchor, stage, history_anchor

    def write_anchor(root: Path, payload: dict[str, bytes]) -> None:
        root.mkdir(mode=0o755)
        for name, raw in payload.items():
            path = root / name
            path.write_bytes(raw)
            path.chmod(namespace["ANCHOR_FILES"][name])
        root.chmod(0o555)

    _, anchor, stage, history_anchor = configure("normal")
    write_anchor(anchor, legacy)
    assert reconcile(expected) == "reconciled"
    assert not stage.exists()
    assert {name: (anchor / name).read_bytes() for name in expected} == expected
    assert {
        name: (history_anchor / name).read_bytes() for name in legacy
    } == legacy
    assert stat.S_IMODE(history_anchor.stat().st_mode) == 0o555
    archived_identity = (
        history_anchor.stat().st_ino,
        history_anchor.stat().st_mtime_ns,
        {
            name: (
                (history_anchor / name).stat().st_ino,
                (history_anchor / name).read_bytes(),
            )
            for name in legacy
        },
    )
    # Observable crash frontier after staging -> anchor but before the caller
    # receives success: current + history and no staging.  It is idempotent and
    # does not touch the archived inode or bytes.
    assert reconcile(expected) == "reconciled"
    assert not stage.exists()
    assert archived_identity == (
        history_anchor.stat().st_ino,
        history_anchor.stat().st_mtime_ns,
        {
            name: (
                (history_anchor / name).stat().st_ino,
                (history_anchor / name).read_bytes(),
            )
            for name in legacy
        },
    )

    _, anchor, stage, history_anchor = configure("crash-after-archive")
    write_anchor(anchor, legacy)
    prepare(expected)
    history_anchor.parent.mkdir(mode=0o755)
    os.rename(anchor, history_anchor)
    assert not anchor.exists() and stage.exists() and history_anchor.exists()
    assert reconcile(expected) == "reconciled"
    assert not stage.exists()
    assert {name: (anchor / name).read_bytes() for name in expected} == expected
    assert {
        name: (history_anchor / name).read_bytes() for name in legacy
    } == legacy

    _, anchor, stage, history_anchor = configure("missing-confirmed-stage")
    history_anchor.parent.mkdir(mode=0o755)
    write_anchor(history_anchor, legacy)
    with pytest.raises(namespace["EnrollmentError"], match="staging physiquement confirmé"):
        reconcile(expected)
    assert not anchor.exists()
    assert not stage.exists()
    assert {
        name: (history_anchor / name).read_bytes() for name in legacy
    } == legacy


def test_anchor_second_reconciliation_preserves_both_generations_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(ATLAS_ANCHOR_INSTALLER))
    reconcile = namespace["_publish_or_reconcile"]
    prepare = namespace["_prepare_staging"]
    globals_ = reconcile.__globals__
    globals_["ROOT_UID"] = os.getuid()
    globals_["ROOT_GID"] = os.getgid()
    legacy = {
        "release-manifest.v1.json": b"manifest-a",
        "control-plane-bootstrap": b"helper-a",
        "launch-atlas-reviewed-rpm": b"launcher-a",
    }
    intermediate = {
        "release-manifest.v1.json": b"manifest-b",
        "control-plane-bootstrap": b"helper-b",
        "launch-atlas-reviewed-rpm": b"launcher-b",
    }
    expected = {
        "release-manifest.v1.json": b"manifest-c",
        "control-plane-bootstrap": b"helper-c",
        "launch-atlas-reviewed-rpm": b"launcher-c",
    }
    globals_["LEGACY_SHA256"] = {
        name: hashlib.sha256(raw).hexdigest() for name, raw in legacy.items()
    }
    globals_["INTERMEDIATE_SHA256"] = {
        name: hashlib.sha256(raw).hexdigest() for name, raw in intermediate.items()
    }
    real_rename = os.rename

    def privileged_directory_rename(source: os.PathLike[str], target: os.PathLike[str]) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if source_path == globals_["ANCHOR_ROOT"]:
            source_path.chmod(0o755)
            real_rename(source_path, target_path)
            target_path.chmod(0o555)
            return
        real_rename(source_path, target_path)

    monkeypatch.setattr(os, "rename", privileged_directory_rename)

    def write_anchor(root: Path, payload: dict[str, bytes]) -> None:
        root.mkdir(parents=True, mode=0o755)
        for name, raw in payload.items():
            path = root / name
            path.write_bytes(raw)
            path.chmod(namespace["ANCHOR_FILES"][name])
        root.chmod(0o555)

    def configure(case: str) -> tuple[Path, Path, Path, Path, Path]:
        parent = tmp_path / case
        anchor = parent / "release"
        stage = parent / ".release.staging"
        history_parent = parent / "history"
        legacy_history = history_parent / "release.pre-reconciliation"
        intermediate_history = history_parent / "release.pre-second-reconciliation"
        parent.mkdir(mode=0o755)
        globals_["ANCHOR_PARENT"] = parent
        globals_["ANCHOR_ROOT"] = anchor
        globals_["STAGING_ROOT"] = stage
        globals_["HISTORY_PARENT"] = history_parent
        globals_["HISTORY_ANCHOR"] = legacy_history
        return anchor, stage, legacy_history, intermediate_history, history_parent

    anchor, stage, legacy_history, intermediate_history, _ = configure("normal")
    write_anchor(legacy_history, legacy)
    write_anchor(anchor, intermediate)
    legacy_identity = (legacy_history.stat().st_ino, legacy_history.stat().st_mtime_ns)
    assert reconcile(expected) == "reconciled"
    assert not stage.exists()
    assert {name: (anchor / name).read_bytes() for name in expected} == expected
    assert {name: (legacy_history / name).read_bytes() for name in legacy} == legacy
    assert {
        name: (intermediate_history / name).read_bytes() for name in intermediate
    } == intermediate
    assert legacy_identity == (
        legacy_history.stat().st_ino,
        legacy_history.stat().st_mtime_ns,
    )
    intermediate_identity = (
        intermediate_history.stat().st_ino,
        intermediate_history.stat().st_mtime_ns,
    )
    assert reconcile(expected) == "reconciled"
    assert intermediate_identity == (
        intermediate_history.stat().st_ino,
        intermediate_history.stat().st_mtime_ns,
    )

    anchor, stage, legacy_history, intermediate_history, _ = configure("resume")
    write_anchor(legacy_history, legacy)
    write_anchor(anchor, intermediate)
    prepare(expected)
    os.rename(anchor, intermediate_history)
    assert reconcile(expected) == "reconciled"
    assert anchor.exists() and not stage.exists()
    assert {name: (anchor / name).read_bytes() for name in expected} == expected

    anchor, stage, legacy_history, intermediate_history, _ = configure("missing-stage")
    write_anchor(legacy_history, legacy)
    write_anchor(intermediate_history, intermediate)
    with pytest.raises(namespace["EnrollmentError"], match="staging physiquement confirmé"):
        reconcile(expected)
    assert not anchor.exists() and not stage.exists()


def test_anchor_third_reconciliation_is_append_only_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = runpy.run_path(str(ATLAS_ANCHOR_INSTALLER))
    reconcile = namespace["_publish_or_reconcile"]
    prepare = namespace["_prepare_staging"]
    globals_ = reconcile.__globals__
    globals_["ROOT_UID"] = os.getuid()
    globals_["ROOT_GID"] = os.getgid()
    legacy = {
        "release-manifest.v1.json": b"manifest-a",
        "control-plane-bootstrap": b"helper-a",
        "launch-atlas-reviewed-rpm": b"launcher-a",
    }
    intermediate = {
        "release-manifest.v1.json": b"manifest-b",
        "control-plane-bootstrap": b"helper-b",
        "launch-atlas-reviewed-rpm": b"launcher-b",
    }
    pre_third = {
        "release-manifest.v1.json": b"manifest-c",
        "control-plane-bootstrap": b"helper-c",
        "launch-atlas-reviewed-rpm": b"launcher-c",
    }
    expected = {
        "release-manifest.v1.json": b"manifest-d",
        "control-plane-bootstrap": b"helper-d",
        "launch-atlas-reviewed-rpm": b"launcher-d",
    }
    for constant, payload in (
        ("LEGACY_SHA256", legacy),
        ("INTERMEDIATE_SHA256", intermediate),
        ("PRE_THIRD_SHA256", pre_third),
    ):
        globals_[constant] = {
            name: hashlib.sha256(raw).hexdigest() for name, raw in payload.items()
        }
    real_rename = os.rename

    def privileged_directory_rename(source: os.PathLike[str], target: os.PathLike[str]) -> None:
        source_path = Path(source)
        target_path = Path(target)
        if source_path == globals_["ANCHOR_ROOT"]:
            source_path.chmod(0o755)
            real_rename(source_path, target_path)
            target_path.chmod(0o555)
            return
        real_rename(source_path, target_path)

    monkeypatch.setattr(os, "rename", privileged_directory_rename)

    def write_anchor(root: Path, payload: dict[str, bytes]) -> None:
        root.mkdir(parents=True, mode=0o755)
        for name, raw in payload.items():
            path = root / name
            path.write_bytes(raw)
            path.chmod(namespace["ANCHOR_FILES"][name])
        root.chmod(0o555)

    def configure(case: str) -> tuple[Path, Path, Path, Path, Path]:
        parent = tmp_path / case
        anchor = parent / "release"
        stage = parent / ".release.staging"
        history_parent = parent / "history"
        legacy_history = history_parent / "release.pre-reconciliation"
        intermediate_history = history_parent / "release.pre-second-reconciliation"
        third_history = history_parent / "release.pre-third-reconciliation"
        parent.mkdir(mode=0o755)
        globals_["ANCHOR_PARENT"] = parent
        globals_["ANCHOR_ROOT"] = anchor
        globals_["STAGING_ROOT"] = stage
        globals_["HISTORY_PARENT"] = history_parent
        globals_["HISTORY_ANCHOR"] = legacy_history
        return anchor, stage, legacy_history, intermediate_history, third_history

    anchor, stage, legacy_history, intermediate_history, third_history = configure(
        "normal"
    )
    write_anchor(legacy_history, legacy)
    write_anchor(intermediate_history, intermediate)
    write_anchor(anchor, pre_third)
    predecessor_identities = {
        path: (path.stat().st_ino, path.stat().st_mtime_ns)
        for path in (legacy_history, intermediate_history)
    }
    assert reconcile(expected) == "reconciled"
    assert anchor.exists() and not stage.exists()
    assert {name: (anchor / name).read_bytes() for name in expected} == expected
    assert {name: (third_history / name).read_bytes() for name in pre_third} == pre_third
    assert predecessor_identities == {
        path: (path.stat().st_ino, path.stat().st_mtime_ns)
        for path in (legacy_history, intermediate_history)
    }
    third_identity = (third_history.stat().st_ino, third_history.stat().st_mtime_ns)
    assert reconcile(expected) == "reconciled"
    assert third_identity == (third_history.stat().st_ino, third_history.stat().st_mtime_ns)

    anchor, stage, legacy_history, intermediate_history, third_history = configure(
        "resume"
    )
    write_anchor(legacy_history, legacy)
    write_anchor(intermediate_history, intermediate)
    write_anchor(anchor, pre_third)
    prepare(expected)
    os.rename(anchor, third_history)
    assert reconcile(expected) == "reconciled"
    assert anchor.exists() and not stage.exists()
    assert {name: (anchor / name).read_bytes() for name in expected} == expected

    anchor, stage, legacy_history, intermediate_history, third_history = configure(
        "missing-stage"
    )
    write_anchor(legacy_history, legacy)
    write_anchor(intermediate_history, intermediate)
    write_anchor(third_history, pre_third)
    with pytest.raises(namespace["EnrollmentError"], match="staging physiquement confirmé"):
        reconcile(expected)
    assert not anchor.exists() and not stage.exists()

    anchor, _, legacy_history, intermediate_history, third_history = configure(
        "duplicate"
    )
    write_anchor(legacy_history, legacy)
    write_anchor(intermediate_history, intermediate)
    write_anchor(third_history, pre_third)
    write_anchor(anchor, pre_third)
    with pytest.raises(namespace["EnrollmentError"], match="troisième"):
        reconcile(expected)

    anchor, _, legacy_history, _, third_history = configure("incomplete-history")
    write_anchor(legacy_history, legacy)
    write_anchor(third_history, pre_third)
    with pytest.raises(namespace["EnrollmentError"], match="intermédiaire"):
        reconcile(expected)
    assert not anchor.exists()


def test_anchor_manifest_contract_rejects_minimal_extra_duplicate_and_broken_programs() -> None:
    namespace = runpy.run_path(str(ATLAS_ANCHOR_INSTALLER))
    validate = namespace["_validate_contract"]
    error = namespace["EnrollmentError"]
    helper = (ROOT / "deploy/control-plane/bin/control-plane-bootstrap").read_bytes()
    launcher = ATLAS_LAUNCHER.read_bytes()
    document = json.loads(SOURCE_MANIFEST.read_text(encoding="utf-8"))
    document["bootstrap"]["helper_sha256"] = hashlib.sha256(helper).hexdigest()
    document["trust_anchor"]["launcher_sha256"] = hashlib.sha256(launcher).hexdigest()
    valid = json.dumps(document, separators=(",", ":")).encode("utf-8")
    validate(valid, helper, launcher)

    minimal = json.dumps(
        {
            "schema_version": 1,
            "release_id": namespace["RELEASE_ID"],
            "architecture": "x86_64",
            "trust_anchor": document["trust_anchor"],
            "bootstrap": {"helper_sha256": hashlib.sha256(helper).hexdigest()},
        },
        separators=(",", ":"),
    ).encode("utf-8")
    with pytest.raises(error, match="champs du manifeste"):
        validate(minimal, helper, launcher)

    extra_document = json.loads(valid)
    extra_document["unexpected"] = True
    with pytest.raises(error, match="champs du manifeste"):
        validate(
            json.dumps(extra_document, separators=(",", ":")).encode("utf-8"),
            helper,
            launcher,
        )

    duplicate = valid.replace(b"{", b'{"schema_version":1,', 1)
    with pytest.raises(error, match="clé dupliquée"):
        validate(duplicate, helper, launcher)

    broken_helper = b"#!/usr/bin/python3 -I\ndef main() -> int:\n gui-corrective-preflight\n  broken\n"
    broken_document = json.loads(valid)
    broken_document["bootstrap"]["helper_sha256"] = hashlib.sha256(
        broken_helper
    ).hexdigest()
    with pytest.raises(error, match="bootstrap"):
        validate(
            json.dumps(broken_document, separators=(",", ":")).encode("utf-8"),
            broken_helper,
            launcher,
        )

    broken_launcher = b"#!/usr/bin/python3 -I\ndef main() -> int:\n  broken syntax\n"
    broken_launcher_document = json.loads(valid)
    broken_launcher_document["trust_anchor"]["launcher_sha256"] = hashlib.sha256(
        broken_launcher
    ).hexdigest()
    with pytest.raises(error):
        validate(
            json.dumps(broken_launcher_document, separators=(",", ":")).encode(
                "utf-8"
            ),
            helper,
            broken_launcher,
        )

    for mutate in (
        lambda value: value["artifacts"]["hermes_source_archive"].__setitem__(
            "commit", "0" * 40
        ),
        lambda value: value["supply_chain"]["hermes"].__setitem__(
            "uv_version", "99.0.0"
        ),
        lambda value: value["host"]["docker"]["packages"].__setitem__(
            "moby-engine", "moby-engine-0:999-1.fc44.x86_64"
        ),
        lambda value: value["components"]["model_catalogue"]["runtime"].__setitem__(
            "static_cards", 999
        ),
        lambda value: value["source"]["roots"].reverse(),
        lambda value: value["runtime"]["encrypted_credentials"].reverse(),
        lambda value: value["runtime"]["services"].reverse(),
    ):
        mutated = json.loads(valid)
        mutate(mutated)
        with pytest.raises(error):
            validate(
                json.dumps(mutated, separators=(",", ":")).encode("utf-8"),
                helper,
                launcher,
            )


def test_anchor_reconciliation_rejects_unknown_or_duplicated_legacy_state(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(ATLAS_ANCHOR_INSTALLER))
    reconcile = namespace["_publish_or_reconcile"]
    globals_ = reconcile.__globals__
    globals_["ROOT_UID"] = os.getuid()
    globals_["ROOT_GID"] = os.getgid()
    expected = {
        "release-manifest.v1.json": b"new-manifest",
        "control-plane-bootstrap": b"new-helper",
        "launch-atlas-reviewed-rpm": b"new-launcher",
    }
    legacy = {
        "release-manifest.v1.json": b"old-manifest",
        "control-plane-bootstrap": b"old-helper",
        "launch-atlas-reviewed-rpm": b"old-launcher",
    }
    globals_["LEGACY_SHA256"] = {
        name: hashlib.sha256(raw).hexdigest() for name, raw in legacy.items()
    }

    def write_anchor(root: Path, payload: dict[str, bytes]) -> None:
        root.mkdir(parents=True, mode=0o755)
        for name, raw in payload.items():
            path = root / name
            path.write_bytes(raw)
            path.chmod(namespace["ANCHOR_FILES"][name])
        root.chmod(0o555)

    parent = tmp_path / "unknown"
    parent.mkdir(mode=0o755)
    anchor = parent / "release"
    write_anchor(anchor, {**legacy, "control-plane-bootstrap": b"corrupt"})
    globals_["ANCHOR_PARENT"] = parent
    globals_["ANCHOR_ROOT"] = anchor
    globals_["STAGING_ROOT"] = parent / ".release.staging"
    globals_["HISTORY_PARENT"] = parent / "history"
    globals_["HISTORY_ANCHOR"] = parent / "history" / "release.pre-reconciliation"
    with pytest.raises(namespace["EnrollmentError"]):
        reconcile(expected)
    assert anchor.exists()
    assert not globals_["HISTORY_ANCHOR"].exists()

    duplicate_parent = tmp_path / "duplicate"
    duplicate_parent.mkdir(mode=0o755)
    duplicate_anchor = duplicate_parent / "release"
    duplicate_history = duplicate_parent / "history" / "release.pre-reconciliation"
    write_anchor(duplicate_anchor, legacy)
    write_anchor(duplicate_history, legacy)
    duplicate_history.parent.chmod(0o755)
    globals_["ANCHOR_PARENT"] = duplicate_parent
    globals_["ANCHOR_ROOT"] = duplicate_anchor
    globals_["STAGING_ROOT"] = duplicate_parent / ".release.staging"
    globals_["HISTORY_PARENT"] = duplicate_history.parent
    globals_["HISTORY_ANCHOR"] = duplicate_history
    with pytest.raises(namespace["EnrollmentError"]):
        reconcile(expected)
    assert duplicate_anchor.exists() and duplicate_history.exists()


def test_anchor_enrollment_lock_rejects_a_concurrent_writer(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(ATLAS_ANCHOR_INSTALLER))
    open_lock = namespace["_open_lock"]
    globals_ = open_lock.__globals__
    globals_["LOCK_PATH"] = tmp_path / "atlas-anchor.lock"
    globals_["ROOT_UID"] = os.getuid()
    globals_["ROOT_GID"] = os.getgid()
    previous_umask = os.umask(0o777)
    try:
        first = open_lock()
        second = open_lock()
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(globals_["LOCK_PATH"].stat().st_mode) == 0o600
    try:
        fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            fcntl.flock(second, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(second)
        os.close(first)


def test_anchor_parent_mode_is_deterministic_and_recovers_an_empty_umask_frontier(
    tmp_path: Path,
) -> None:
    namespace = runpy.run_path(str(ATLAS_ANCHOR_INSTALLER))
    require_directory = namespace["_require_root_directory"]
    error = namespace["EnrollmentError"]
    require_directory.__globals__["ROOT_UID"] = os.getuid()
    require_directory.__globals__["ROOT_GID"] = os.getgid()

    created = tmp_path / "created-under-umask"
    previous_umask = os.umask(0o777)
    try:
        require_directory(created, 0o755, create=True)
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(created.stat().st_mode) == 0o755

    interrupted = tmp_path / "interrupted-after-mkdir"
    interrupted.mkdir(mode=0o700)
    require_directory(interrupted, 0o755, create=True)
    assert stat.S_IMODE(interrupted.stat().st_mode) == 0o755

    zero_mode = tmp_path / "interrupted-under-foreign-umask"
    zero_mode.mkdir(mode=0o700)
    zero_mode.chmod(0o000)
    require_directory(zero_mode, 0o755, create=True)
    assert stat.S_IMODE(zero_mode.stat().st_mode) == 0o755

    foreign_state = tmp_path / "non-empty-frontier"
    foreign_state.mkdir(mode=0o700)
    (foreign_state / "unexpected").write_bytes(b"foreign")
    with pytest.raises(error):
        require_directory(foreign_state, 0o755, create=True)


def test_anchor_staging_modes_ignore_an_inherited_umask_0777(tmp_path: Path) -> None:
    namespace = runpy.run_path(str(ATLAS_ANCHOR_INSTALLER))
    prepare = namespace["_prepare_staging"]
    globals_ = prepare.__globals__
    parent = tmp_path / "anchor-parent"
    parent.mkdir(mode=0o755)
    globals_["ROOT_UID"] = os.getuid()
    globals_["ROOT_GID"] = os.getgid()
    globals_["ANCHOR_PARENT"] = parent
    globals_["STAGING_ROOT"] = parent / ".release.staging"
    expected = {
        "release-manifest.v1.json": b"manifest",
        "control-plane-bootstrap": b"helper",
        "launch-atlas-reviewed-rpm": b"launcher",
    }
    previous_umask = os.umask(0o777)
    try:
        prepare(expected)
    finally:
        os.umask(previous_umask)
    assert stat.S_IMODE(globals_["STAGING_ROOT"].stat().st_mode) == 0o555
    for name, mode in namespace["ANCHOR_FILES"].items():
        assert stat.S_IMODE((globals_["STAGING_ROOT"] / name).stat().st_mode) == mode


def test_runtime_routing_uses_project_host_account_and_deployment_cost() -> None:
    html = (APP / "ui" / "index.html").read_text(encoding="utf-8")
    javascript = (APP / "ui" / "app.js").read_text(encoding="utf-8")
    assert 'id="routing-project" name="project"' in html
    assert 'source: runtimeOnly ? "runtime" : "local"' in javascript
    assert "request.project_id" in javascript
    assert "runtime_only: runtimeOnly" in javascript
    assert "source.selected_deployment_id" in javascript
    assert "source.provider_account_id" in javascript


def test_runtime_snapshot_rejects_a_stale_catalogue_revision() -> None:
    runtime = (APP / "src-tauri" / "src" / "runtime.rs").read_text(
        encoding="utf-8"
    )
    assert 'CatalogueMismatch => "catalogue_revision_mismatch"' in runtime
    assert "snapshot.catalogue.revision != expected_revision" in runtime
    assert "snapshot.catalogue.cards != expected_cards" in runtime
    assert (
        "snapshot.catalogue.provider_accounts != expected_provider_accounts" in runtime
    )


def test_runtime_snapshot_and_ui_expose_bounded_observed_performance() -> None:
    runtime = (APP / "src-tauri" / "src" / "runtime.rs").read_text(
        encoding="utf-8"
    )
    javascript = (APP / "ui" / "app.js").read_text(encoding="utf-8")
    html = (APP / "ui" / "index.html").read_text(encoding="utf-8")
    assert 'get_json::<PerformanceSnapshot>("/v1/model-performance")' in runtime
    assert "validate_performance(&value)" in runtime
    assert "policy.catalogue_mutation" in runtime
    assert "function renderPerformance()" in javascript
    assert "validated_outcomes" in javascript
    assert 'id="performance-list"' in html
    assert 'id="adaptive-policy"' in html


def test_runtime_snapshot_exposes_strict_provider_finance_and_native_refresh() -> None:
    runtime = (APP / "src-tauri" / "src" / "runtime.rs").read_text(
        encoding="utf-8"
    )
    rust = (APP / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    javascript = (APP / "ui" / "app.js").read_text(encoding="utf-8")
    capability = (APP / "src-tauri" / "capabilities" / "main.json").read_text(
        encoding="utf-8"
    )
    assert (
        'get_json::<ProviderIntegrationsSnapshot>("/v1/provider-integrations")'
        in runtime
    )
    assert "validate_provider_integrations(&value" in runtime
    assert '"/v1/provider-finance/refresh"' in runtime
    assert "currency_conversion" in runtime
    assert "refresh_provider_finance" in rust
    assert "refreshProviderFinance" in javascript
    assert "data-refresh-balance" in javascript
    assert "provider_integrations" in javascript
    assert "allow-refresh-provider-finance" in capability


def test_native_helper_contract_is_fixed_and_does_not_return_secret_output() -> None:
    security = (APP / "src-tauri" / "src" / "security.rs").read_text(
        encoding="utf-8"
    )
    assert 'const KEY_MANAGER: &str = "/usr/local/libexec/ops-model-key-manager";' in security
    assert '.arg(if cfg!(feature="native-desktop") {"store-stdin"} else {"set-stdin"})' in security
    assert '.arg(&provider_for_worker)' in security
    assert ".env_clear()" in security
    assert ".stdin(Stdio::piped())" in security
    assert ".stdout(Stdio::piped())" in security
    assert ".stderr(Stdio::null())" in security
    assert "MAX_HELPER_RESULT_BYTES" in security
    assert "invalid_helper_result" in security
    key_manager = (ROOT / "scripts" / "ops-model-key-manager").read_text(
        encoding="utf-8"
    )
    assert 'STDIN_MAGIC = b"ATLASKEY1\\n"' in key_manager
    assert 'argv[1] not in {"set", "set-stdin", "store-stdin"}' in key_manager
    assert "_validate_private_stdin_pipe()" in key_manager
    assert (
        'RELOAD_PROGRAM = Path("/usr/local/libexec/ops-model-credentials-reload")'
        in key_manager
    )
    assert '[SUDO_PROGRAM, "-n", "--", str(RELOAD_PROGRAM)]' in key_manager


def test_embedded_catalogue_and_icon_are_present() -> None:
    catalogue_source = (APP / "src-tauri" / "src" / "catalogue.rs").read_text(
        encoding="utf-8"
    )
    assert 'include_str!("../../../../catalog/model-catalog.v2.json")' in catalogue_source
    icon = APP / "src-tauri" / "icons" / "icon.png"
    assert icon.is_file() and not icon.is_symlink() and icon.stat().st_size > 1024


def test_model_cards_expose_honest_technical_metadata_and_accessible_groups() -> None:
    javascript = (APP / "ui" / "app.js").read_text(encoding="utf-8")
    assert "source.context_window_tokens" in javascript
    assert "source.latency_p50_ms" in javascript
    assert "source.latency_p95_ms" in javascript
    assert "source.technical_metadata_confidence" in javascript
    assert "source.technical_metadata_source" in javascript
    assert "schedule.cachedInputPerMillion" in javascript
    assert ".map(normalizePriceSchedule)" in javascript
    assert "conservativePricingSchedule(schedules)" in javascript
    assert "pricingVariantsMarkup(model" in javascript
    assert 'data-pricing-mode aria-label="Mode tarifaire simulé' in javascript
    assert "Simulation conservatrice / mois" in javascript
    assert 'input.closest(".range-field")?.querySelector("output")' in javascript
    assert "Source prix" in javascript
    assert "Source technique" in javascript
    assert "Latence" in javascript
    assert 'class="capability-grid" role="group"' in javascript
    assert 'class="multi-budget" role="group"' in javascript
    assert 'class="model-access-list" role="group"' in javascript
    assert 'class="finance-capabilities" role="group"' in javascript
    assert 'class="skeleton-grid" role="status"' in javascript
    assert 'class="dialog-header" aria-label=' in javascript
