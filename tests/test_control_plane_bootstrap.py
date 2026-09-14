from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "deploy/control-plane/bin/control-plane-bootstrap"
RESUME_BOOTSTRAP = ROOT / "artifacts/recovery-patch/control-plane-bootstrap.e"
RESUME_MANIFEST = ROOT / "artifacts/recovery-patch/release-manifest.e.v1.json"
WORKER = ROOT / "deploy/control-plane/bin/control-plane-deployment-worker"
MANIFEST = ROOT / "deploy/control-plane/release-manifest.v1.json"


def bootstrap_namespace() -> dict[str, object]:
    return runpy.run_path(str(BOOTSTRAP))


def resume_bootstrap_namespace() -> dict[str, object]:
    return runpy.run_path(str(RESUME_BOOTSTRAP))


def worker_namespace() -> dict[str, object]:
    return runpy.run_path(str(WORKER))


def new_transaction(namespace: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    create = namespace["_new_transaction"]
    globals_ = create.__globals__
    monkeypatch.setitem(globals_, "_systemctl_state", lambda _unit, _operation: False)
    manifest = {
        "release_id": "atlas-api-zulip-2026.09.07.8",
        "source": {"tree_sha256": "1" * 64},
        "artifacts": {
            "atlas_rpm": {"sha256": "2" * 64},
            "hermes_source_archive": {"sha256": "7" * 64},
        },
    }
    preflight = {
        "credential_groups_present": {
            name: False for name in globals_["CREDENTIAL_GROUPS"]
        }
    }
    return create(manifest, preflight)


def completed_transaction_and_consumed(
    namespace: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, object], dict[str, object]]:
    transaction = new_transaction(namespace, monkeypatch)
    initial = json.loads(json.dumps(transaction))
    completed_at = 1_789_000_000
    proof = {
        "schema_version": 1,
        "release_id": transaction["release_id"],
        "source_tree_sha256": transaction["source_tree_sha256"],
        "atlas_rpm_sha256": transaction["atlas_rpm_sha256"],
        "hermes_source_archive_sha256": transaction["hermes_source_archive_sha256"],
        "status": "healthy-degraded",
        "provider_accounts_configured": 21,
        "provider_keys_configured": 0,
        "provider_routes_available": 0,
        "provider_network_probe": False,
        "finance_status": "ok",
        "broker_status": "ok",
        "broker_policy_sha256": "3" * 64,
        "zulip_approver_reconciled": True,
        "bootstrap_consumed": True,
        "password_recovery_persists_on_rollback": True,
        "completed_at": completed_at,
        "audit_tail_sha256": "4" * 64,
    }
    transaction.update({
        "status": "completed",
        "phase": "cleanup-pending",
        "proof": proof,
        "completed_at": completed_at,
        "recovery_guard_installing": False,
        "recovery_guard_installed": False,
    })
    consumed = {
        "schema_version": 1,
        "consumed": True,
        "consumed_at": 1_788_999_999,
        "actor": "codex-supervised",
        "physical_uid": 1000,
        "physical_session": "3",
        "release_id": transaction["release_id"],
        "source_tree_sha256": transaction["source_tree_sha256"],
        "atlas_rpm_sha256": transaction["atlas_rpm_sha256"],
        "hermes_source_archive_sha256": transaction["hermes_source_archive_sha256"],
        "bootstrap_helper_sha256": "5" * 64,
        "manifest_sha256": "6" * 64,
        "initial_transaction": initial,
    }
    return transaction, consumed


def configure_public_proof_test_paths(
    namespace: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> tuple[dict[str, object], Path, Path, Path]:
    publish = namespace["_publish_public_installed_proof"]
    globals_ = publish.__globals__
    consumed_path = tmp_path / "consumed.json"
    private_path = tmp_path / "private-proof.json"
    public_path = tmp_path / "installed.json"
    monkeypatch.setitem(globals_, "CONSUMED_PATH", consumed_path)
    monkeypatch.setitem(globals_, "PROOF_PATH", private_path)
    monkeypatch.setitem(globals_, "PUBLIC_INSTALLED_PROOF_PATH", public_path)
    monkeypatch.setattr(
        globals_["pwd"], "getpwnam", lambda _name: SimpleNamespace(pw_uid=1000)
    )
    real_lstat = os.lstat
    real_fstat = os.fstat
    monkeypatch.setattr(
        globals_["os"], "lstat", lambda path: _as_root_owned(real_lstat(path))
    )
    monkeypatch.setattr(
        globals_["os"], "fstat", lambda descriptor: _as_root_owned(real_fstat(descriptor))
    )
    return globals_, consumed_path, private_path, public_path


def _as_root_owned(value: os.stat_result) -> os.stat_result:
    fields = list(value)
    fields[4] = 0
    fields[5] = 0
    return os.stat_result(fields)


def test_transaction_schema_is_exact_and_declares_bounded_remint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    transaction = new_transaction(namespace, monkeypatch)
    validate = namespace["_validate_transaction_document"]
    validate(transaction)
    assert transaction["root_generation_remints"] == 0
    assert transaction["root_remint_required"] is False
    assert transaction["root_final_share_windows"] == []
    assert transaction["userpass_password_escrowed"] is False

    unexpected = dict(transaction, unreviewed=True)
    with pytest.raises(namespace["BootstrapError"], match="fields differ"):
        validate(unexpected)
    missing = dict(transaction)
    missing.pop("root_remint_required")
    with pytest.raises(namespace["BootstrapError"], match="fields differ"):
        validate(missing)


def test_corrective_predecessor_accepts_only_the_exact_legacy_wal_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    current = new_transaction(namespace, monkeypatch)
    validate_current = namespace["_validate_transaction_document"]
    validate_predecessor = namespace[
        "_validate_corrective_predecessor_transaction_document"
    ]
    legacy = dict(current)
    legacy.pop("hermes_source_archive_sha256")

    validate_predecessor(legacy)
    validate_current(current)

    with pytest.raises(namespace["BootstrapError"], match="legacy schema"):
        validate_predecessor(current)
    with pytest.raises(namespace["BootstrapError"], match="legacy schema"):
        validate_predecessor(dict(legacy, unreviewed=True))
    with pytest.raises(namespace["BootstrapError"], match="fields differ"):
        validate_current(legacy)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("hermes_source_archive_sha256", "7" * 64),
        ("unreviewed", True),
    ),
)
def test_corrective_predecessor_marker_accepts_only_the_exact_legacy_schema(
    field: str,
    value: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    current = new_transaction(namespace, monkeypatch)
    legacy = dict(current)
    legacy.pop("hermes_source_archive_sha256")
    marker = {
        "schema_version": 1,
        "consumed": True,
        "consumed_at": 1,
        "actor": "codex-supervised",
        "physical_uid": namespace["pwd"].getpwnam("ops-user").pw_uid,
        "physical_session": "3",
        "release_id": legacy["release_id"],
        "source_tree_sha256": legacy["source_tree_sha256"],
        "atlas_rpm_sha256": legacy["atlas_rpm_sha256"],
        "bootstrap_helper_sha256": "5" * 64,
        "manifest_sha256": "6" * 64,
        "initial_transaction": legacy,
    }
    read_legacy = namespace[
        "_read_corrective_predecessor_consumed_identity_at"
    ]
    read_current = namespace["_read_consumed_identity_at"]
    globals_ = read_legacy.__globals__
    encoded = [globals_["_canonical"](marker) + b"\n"]
    monkeypatch.setitem(
        globals_, "_read_exact_root_file", lambda *_args, **_kwargs: encoded[0]
    )

    assert read_legacy(Path("/ignored"), legacy) == marker
    with pytest.raises(namespace["BootstrapError"], match="fields differ"):
        read_current(Path("/ignored"), legacy)

    changed = dict(marker)
    changed[field] = value
    encoded[0] = globals_["_canonical"](changed) + b"\n"
    with pytest.raises(namespace["BootstrapError"], match="identity differs"):
        read_legacy(Path("/ignored"), legacy)


def test_recovery_classification_accepts_the_exact_legacy_predecessor_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    classify = namespace["_classify_gui_recovery_mode"]
    globals_ = classify.__globals__
    predecessor = dict(globals_["CORRECTIVE_PREDECESSOR"])
    legacy_helper = b"legacy helper\n"
    legacy_janitor = b"legacy janitor\n"
    predecessor["bootstrap_helper_sha256"] = hashlib.sha256(
        legacy_helper
    ).hexdigest()
    monkeypatch.setitem(globals_, "CORRECTIVE_PREDECESSOR", predecessor)
    monkeypatch.setitem(globals_, "_systemctl_state", lambda *_args: False)

    target_manifest = {
        "release_id": globals_["CORRECTIVE_TARGET_RELEASE_ID"],
        "source": {"tree_sha256": "a" * 64},
        "artifacts": {
            "atlas_rpm": {"sha256": "b" * 64, "source_path": "atlas.rpm"},
            "hermes_source_archive": {"sha256": "7" * 64},
        },
    }
    legacy_manifest = {
        "release_id": predecessor["release_id"],
        "source": {"tree_sha256": predecessor["source_tree_sha256"]},
        "artifacts": {
            "atlas_rpm": {"sha256": predecessor["atlas_rpm_sha256"]},
            "hermes_source_archive": {"sha256": "9" * 64},
        },
    }
    preflight = {
        "credential_groups_present": {
            name: False for name in globals_["CREDENTIAL_GROUPS"]
        }
    }
    initial = namespace["_new_transaction"](legacy_manifest, preflight)
    initial.pop("hermes_source_archive_sha256")
    initial["updated_at"] = 1

    releases_root = tmp_path / "deployment/releases"
    staged = releases_root / predecessor["release_id"]
    staged_tree = staged / "tree"
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    transaction_path = state_root / "transaction.json"
    audit_path = state_root / "audit.jsonl"
    transaction_path.touch()
    audit_path.touch()
    staged_tree.mkdir(parents=True)

    terminal = dict(initial)
    terminal.update(
        status="rolled-back",
        phase="rolled-back",
        staged_release=str(staged),
        openbao_undo_complete=True,
        rolled_back_at=3,
        updated_at=4,
    )
    marker = {
        "schema_version": 1,
        "consumed": True,
        "consumed_at": 2,
        "actor": "codex-supervised",
        "physical_uid": globals_["pwd"].getpwnam("ops-user").pw_uid,
        "physical_session": "3",
        **predecessor,
        "initial_transaction": initial,
    }
    audit_records = [
        {
            "event": "bootstrap_consumed",
            "release_id": predecessor["release_id"],
            "timestamp": 2,
            "details": {"physical_uid": marker["physical_uid"], "physical_session": "3"},
        },
        {
            "event": "bootstrap_rolled_back",
            "release_id": predecessor["release_id"],
            "timestamp": 3,
            "details": {"password_recovery_persists": True},
        },
    ]
    staged_marker = {
        "schema_version": 1,
        "release_id": predecessor["release_id"],
        "source_tree_sha256": predecessor["source_tree_sha256"],
        "atlas_rpm_sha256": predecessor["atlas_rpm_sha256"],
    }
    staged_manifest = {
        "release_id": predecessor["release_id"],
        "source": {"tree_sha256": predecessor["source_tree_sha256"]},
        "artifacts": {
            "atlas_rpm": {"sha256": predecessor["atlas_rpm_sha256"]}
        },
    }
    canonical = globals_["_canonical"]
    monkeypatch.setitem(
        globals_,
        "CORRECTIVE_PREDECESSOR_STAGED_MANIFEST_SHA256",
        hashlib.sha256(canonical(staged_manifest) + b"\n").hexdigest(),
    )
    monkeypatch.setitem(globals_, "STATE_ROOT", state_root)
    monkeypatch.setitem(globals_, "TRANSACTION_PATH", transaction_path)
    monkeypatch.setitem(globals_, "AUDIT_PATH", audit_path)
    consumed_path = tmp_path / "consumed.json"
    monkeypatch.setitem(globals_, "CONSUMED_PATH", consumed_path)
    monkeypatch.setitem(globals_, "DEPLOYMENT_RELEASES_ROOT", releases_root)
    monkeypatch.setitem(globals_, "CORRECTIVE_CONSUMED_PATH", tmp_path / "corrective.json")
    monkeypatch.setitem(globals_, "CORRECTIVE_STATE_ROOT", tmp_path / "corrective-state")
    monkeypatch.setitem(globals_, "CORRECTIVE_HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setitem(globals_, "_validate_corrective_authority", lambda: None)
    monkeypatch.setitem(globals_, "_validate_root_directory", lambda *_args: None)
    monkeypatch.setitem(
        globals_,
        "_strict_corrective_service_state",
        lambda unit: dict(terminal["service_state_before"][unit]),
    )
    monkeypatch.setitem(
        globals_, "_read_exact_audit", lambda _path: (b"audit\n", audit_records, "8" * 64)
    )
    monkeypatch.setitem(
        globals_,
        "_read_json",
        lambda path, *_args: staged_marker
        if Path(path) == staged / ".deployment-release.json"
        else pytest.fail(f"unexpected JSON read: {path}"),
    )
    monkeypatch.setitem(
        globals_, "_independent_source_digest",
        lambda path, **_kwargs: (predecessor["source_tree_sha256"], b"")
        if Path(path) == staged_tree
        else pytest.fail(f"unexpected source digest: {path}"),
    )

    staged_helper = staged_tree / "deploy/control-plane/bin/control-plane-bootstrap"
    staged_rpm = staged / "artifacts/atlas.rpm"
    staged_manifest_path = staged_tree / "deploy/control-plane/release-manifest.v1.json"
    installed_helper = globals_["INSTALLED_HELPER"]
    installed_janitor = globals_["RECOVERY_ASSETS"][4]
    staged_janitor = (
        staged_tree / "deploy/control-plane/systemd" / globals_["RECOVERY_JANITOR_UNIT"]
    )
    payloads = {
        transaction_path: canonical(terminal) + b"\n",
        consumed_path: canonical(marker) + b"\n",
        staged_manifest_path: canonical(staged_manifest) + b"\n",
        installed_helper: legacy_helper,
        installed_janitor: legacy_janitor,
        staged_janitor: legacy_janitor,
    }
    monkeypatch.setitem(
        globals_,
        "_read_exact_root_file",
        lambda path, *_args, **_kwargs: payloads[Path(path)],
    )
    monkeypatch.setitem(
        globals_,
        "_independent_regular_sha256",
        lambda path, *_args: predecessor["bootstrap_helper_sha256"]
        if Path(path) == staged_helper
        else predecessor["atlas_rpm_sha256"]
        if Path(path) == staged_rpm
        else pytest.fail(f"unexpected artifact digest: {path}"),
    )
    monkeypatch.setitem(
        globals_, "_validate_corrective_predecessor_recovery_units", lambda: None
    )
    real_lexists = os.path.lexists
    residual = {installed_helper, installed_janitor}

    def reviewed_lexists(path: object) -> bool:
        candidate = Path(path)
        if candidate in globals_["RECOVERY_ASSETS"]:
            return candidate in residual
        if candidate in globals_["RECOVERY_ENABLEMENT_LINKS"]:
            return False
        if candidate in {
            globals_["CORRECTIVE_CONSUMED_PATH"],
            globals_["CORRECTIVE_STATE_ROOT"],
            globals_["CORRECTIVE_HISTORY_ROOT"],
            globals_["PUBLIC_INSTALLED_PROOF_PATH"],
            globals_["PROOF_PATH"],
            globals_["APPROVER_PATH"],
            globals_["ROOT_TOKEN_ESCROW"],
            globals_["OPENBAO_UNDO_ESCROW"],
            globals_["USERPASS_PASSWORD_ESCROW"],
            globals_["OPENBAO_RECOVERY_SOCKET"],
            globals_["LIVE_TRANSACTION_PATH"],
            globals_["DEPLOYMENT_CURRENT_PATH"],
        }:
            return False
        return real_lexists(path)

    monkeypatch.setattr(globals_["os"].path, "lexists", reviewed_lexists)
    monkeypatch.setitem(globals_, "_review_enrollment_resume", lambda *_args, **_kwargs: None)

    mode, corrective, enrollment = classify(
        target_manifest, "a" * 64, "d" * 64, "e" * 64
    )
    assert mode == "corrective"
    assert corrective is not None and corrective["kind"] == "new"
    assert corrective["predecessor"]["transaction"] == terminal
    assert enrollment is None

    review = namespace["_review_corrective_predecessor"]
    audit_records[1]["timestamp"] = 5
    with pytest.raises(namespace["BootstrapError"], match="audit history differs"):
        review(target_manifest)
    audit_records[1]["timestamp"] = 3
    audit_records[0]["timestamp"] = 4
    with pytest.raises(namespace["BootstrapError"], match="audit history differs"):
        review(target_manifest)


def test_one_shot_marker_is_never_published_partially(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    publish = namespace["_exclusive_atomic_bytes"]
    globals_ = publish.__globals__
    target = tmp_path / "consumed.json"
    real_lstat = os.lstat
    real_write = os.write
    writes = 0

    def root_lstat(path: object) -> os.stat_result:
        value = real_lstat(path)
        return _as_root_owned(value) if Path(path) == tmp_path else value

    def interrupted_write(descriptor: int, payload: object) -> int:
        nonlocal writes
        writes += 1
        if writes == 1:
            data = bytes(payload)
            return real_write(descriptor, data[:1])
        raise OSError("simulated power loss")

    monkeypatch.setattr(globals_["os"], "lstat", root_lstat)
    monkeypatch.setattr(globals_["os"], "fchown", lambda *_args: None)
    monkeypatch.setattr(globals_["os"], "write", interrupted_write)
    with pytest.raises(OSError, match="simulated power loss"):
        publish(target, b'{"complete":true}\n')
    assert not target.exists()


def test_enrollment_marker_only_can_recover_exact_initial_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    review = namespace["_review_enrollment_resume"]
    globals_ = review.__globals__
    transaction = new_transaction(namespace, monkeypatch)
    manifest = {
        "release_id": transaction["release_id"],
        "artifacts": {
            "atlas_rpm": {"sha256": transaction["atlas_rpm_sha256"]},
            "hermes_source_archive": {
                "sha256": transaction["hermes_source_archive_sha256"]
            },
        },
    }
    consumed = tmp_path / "consumed.json"
    state_root = tmp_path / "state"
    source_digest = str(transaction["source_tree_sha256"])
    manifest_digest = "3" * 64
    helper_digest = "4" * 64
    marker = {
        "schema_version": 1,
        "consumed": True,
        "consumed_at": 1,
        "actor": "codex-supervised",
        "physical_uid": 1000,
        "physical_session": "3",
        "release_id": transaction["release_id"],
        "source_tree_sha256": source_digest,
        "atlas_rpm_sha256": transaction["atlas_rpm_sha256"],
        "hermes_source_archive_sha256": transaction["hermes_source_archive_sha256"],
        "bootstrap_helper_sha256": helper_digest,
        "manifest_sha256": manifest_digest,
        "initial_transaction": transaction,
    }
    consumed.write_text(json.dumps(marker), encoding="utf-8")
    monkeypatch.setitem(globals_, "CONSUMED_PATH", consumed)
    monkeypatch.setitem(globals_, "STATE_ROOT", state_root)
    monkeypatch.setitem(globals_, "TRANSACTION_PATH", state_root / "transaction.json")
    monkeypatch.setitem(
        globals_,
        "CORRECTIVE_CONSUMED_PATH",
        tmp_path / "corrective-consumed-absent.json",
    )
    monkeypatch.setitem(globals_, "_read_json", lambda path, *_args: json.loads(Path(path).read_text()))
    monkeypatch.setattr(globals_["pwd"], "getpwnam", lambda _name: type("P", (), {"pw_uid": 1000})())

    resumed = review(manifest, source_digest, manifest_digest, helper_digest)
    assert resumed == transaction
    assert not state_root.exists()


def test_public_installed_proof_is_exact_idempotent_and_conflict_refusing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    publish = namespace["_publish_public_installed_proof"]
    globals_, consumed_path, private_path, public_path = configure_public_proof_test_paths(
        namespace, tmp_path, monkeypatch
    )
    transaction, consumed = completed_transaction_and_consumed(namespace, monkeypatch)
    canonical = globals_["_canonical"]
    consumed_path.write_bytes(canonical(consumed) + b"\n")
    consumed_path.chmod(0o600)
    private_path.write_bytes(canonical(transaction["proof"]) + b"\n")
    private_path.chmod(0o600)
    publications: list[bytes] = []

    def exclusive(path: Path, payload: bytes, mode: int) -> None:
        if os.path.lexists(path):
            raise globals_["BootstrapError"](
                "bootstrap_consumed", "simulated concurrent publication"
            )
        Path(path).write_bytes(payload)
        Path(path).chmod(mode)
        publications.append(payload)

    synced: list[Path] = []
    monkeypatch.setitem(globals_, "_exclusive_atomic_bytes", exclusive)
    monkeypatch.setitem(globals_, "_fsync_directory", lambda path: synced.append(Path(path)))

    publish(transaction)
    first = public_path.read_bytes()
    assert stat.S_IMODE(public_path.stat().st_mode) == 0o644
    assert json.loads(first) == {
        "schema_version": 1,
        "status": "installed",
        "release_id": transaction["release_id"],
        "bootstrap_helper_sha256": consumed["bootstrap_helper_sha256"],
        "manifest_sha256": consumed["manifest_sha256"],
        "source_tree_sha256": transaction["source_tree_sha256"],
        "atlas_rpm_sha256": transaction["atlas_rpm_sha256"],
        "hermes_source_archive_sha256": transaction["hermes_source_archive_sha256"],
        "completed_at": transaction["completed_at"],
    }
    assert first == canonical(json.loads(first)) + b"\n"
    publish(transaction)
    assert public_path.read_bytes() == first
    assert len(publications) == 1

    conflicting = json.loads(first)
    conflicting["release_id"] = "foreign-release"
    public_path.write_bytes(canonical(conflicting) + b"\n")
    public_path.chmod(0o644)
    with pytest.raises(globals_["BootstrapError"], match="conflicts"):
        publish(transaction)


def test_public_proof_publication_requires_completed_health_and_matching_private_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    publish = namespace["_publish_public_installed_proof"]
    globals_, consumed_path, private_path, public_path = configure_public_proof_test_paths(
        namespace, tmp_path, monkeypatch
    )
    transaction, consumed = completed_transaction_and_consumed(namespace, monkeypatch)
    canonical = globals_["_canonical"]
    consumed_path.write_bytes(canonical(consumed) + b"\n")
    consumed_path.chmod(0o600)
    private_path.write_bytes(canonical(transaction["proof"]) + b"\n")
    private_path.chmod(0o600)
    monkeypatch.setitem(
        globals_, "_exclusive_atomic_bytes",
        lambda path, payload, mode: (Path(path).write_bytes(payload), Path(path).chmod(mode)),
    )

    incomplete = json.loads(json.dumps(transaction))
    incomplete["status"] = "running"
    with pytest.raises(globals_["BootstrapError"], match="completion proof"):
        publish(incomplete)
    assert not public_path.exists()

    private_path.write_bytes(canonical({**transaction["proof"], "completed_at": 1}) + b"\n")
    with pytest.raises(globals_["BootstrapError"], match="differs"):
        publish(transaction)
    assert not public_path.exists()


def test_public_installed_proof_is_removed_safely_for_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    remove = namespace["_remove_public_installed_proof"]
    globals_, consumed_path, _private_path, public_path = configure_public_proof_test_paths(
        namespace, tmp_path, monkeypatch
    )
    completed, consumed = completed_transaction_and_consumed(namespace, monkeypatch)
    transaction = json.loads(json.dumps(consumed["initial_transaction"]))
    canonical = globals_["_canonical"]
    consumed_path.write_bytes(canonical(consumed) + b"\n")
    consumed_path.chmod(0o600)
    public = {
        "schema_version": 1,
        "status": "installed",
        "release_id": transaction["release_id"],
        "bootstrap_helper_sha256": consumed["bootstrap_helper_sha256"],
        "manifest_sha256": consumed["manifest_sha256"],
        "source_tree_sha256": transaction["source_tree_sha256"],
        "atlas_rpm_sha256": transaction["atlas_rpm_sha256"],
        "hermes_source_archive_sha256": transaction["hermes_source_archive_sha256"],
        "completed_at": completed["completed_at"],
    }
    public_path.write_bytes(canonical(public) + b"\n")
    public_path.chmod(0o644)
    synced: list[Path] = []
    monkeypatch.setitem(globals_, "_fsync_directory", lambda path: synced.append(Path(path)))
    remove(transaction)
    assert not public_path.exists()
    assert synced == [tmp_path]

    target = tmp_path / "foreign-proof"
    target.write_bytes(canonical(public) + b"\n")
    target.chmod(0o644)
    public_path.symlink_to(target)
    with pytest.raises(globals_["BootstrapError"], match="identity file"):
        remove(transaction)
    assert public_path.is_symlink()


def test_fresh_preflight_rejects_any_public_installed_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    preflight = namespace["bootstrap_preflight"]
    globals_ = preflight.__globals__
    public_path = tmp_path / "installed.json"
    public_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setitem(globals_, "PUBLIC_INSTALLED_PROOF_PATH", public_path)
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    with pytest.raises(globals_["BootstrapError"], match="fresh enrollment is forbidden"):
        preflight({}, "1" * 64, "2" * 64)


def test_completed_cleanup_publishes_after_wal_flags_and_before_last_janitor() -> None:
    namespace = bootstrap_namespace()
    source = __import__("inspect").getsource(namespace["_cleanup_recovery_guard"])
    flags = source.index("recovery_guard_installing=False")
    publication = source.index("_publish_public_installed_proof(transaction)", flags)
    janitor = source.index("janitor_path = RECOVERY_ASSETS[4]")
    assert flags < publication < janitor


def test_preconsent_hermes_check_uses_only_the_vendored_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    validate = namespace["_validate_network_dependencies"]
    globals_ = validate.__globals__
    commit = "a" * 40
    archive = tmp_path / "hermes.tar.gz"
    archive.write_bytes(b"reviewed archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()

    def forbidden_run(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("pre-consent archive verification invoked a subprocess")

    monkeypatch.setattr(globals_["subprocess"], "run", forbidden_run)
    validate({
        "components": {"hermes": {"tag": "v1", "commit": commit}},
        "artifacts": {
            "hermes_source_archive": {
                "source_path": str(archive),
                "sha256": digest,
                "commit": commit,
                "compressed_size": archive.stat().st_size,
            },
        },
        "supply_chain": {
            "hermes": {
                "source_archive_vendored": True,
                "network_git_transport": False,
            },
        },
    })

    with pytest.raises(
        globals_["BootstrapError"], match="vendored Hermes release differs"
    ):
        validate({
            "components": {"hermes": {"tag": "v1", "commit": commit}},
            "artifacts": {
                "hermes_source_archive": {
                    "source_path": str(archive),
                    "sha256": digest,
                    "commit": commit,
                    "compressed_size": archive.stat().st_size + 1,
                },
            },
            "supply_chain": {
                "hermes": {
                    "source_archive_vendored": True,
                    "network_git_transport": False,
                },
            },
        })


def test_response_loss_remint_invalidates_old_escrow_before_new_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    recover = namespace["_recover_or_cancel_root_generation"]
    globals_ = recover.__globals__
    state_root = tmp_path / "state"
    state_root.mkdir()
    escrow_path = tmp_path / "root-token.cred"
    escrow_path.write_bytes(b"encrypted")
    transaction = {
        "root_generation_pending": True,
        "root_generation_started_at": 1,
        "root_attempt_closed": False,
        "root_generation_remints": 0,
        "root_remint_required": False,
        "root_token_escrowed": True,
        "openbao_backup": str(tmp_path / "openbao.hcl"),
        "openbao_restore_required": False,
    }
    events: list[str] = []
    monkeypatch.setitem(globals_, "STATE_ROOT", state_root)
    monkeypatch.setitem(globals_, "ROOT_TOKEN_ESCROW", escrow_path)
    monkeypatch.setitem(globals_, "OPENBAO_CONFIG", tmp_path / "installed.hcl")
    monkeypatch.setitem(globals_, "_root_escrow_document", lambda: {
        "nonce": "a" * 16, "otp": "b" * 16, "progress": 1,
        "encoded_token": None, "started_at": 1,
    })
    monkeypatch.setitem(globals_, "_read_root_file", lambda *_args, **_kwargs: b"config\n")
    monkeypatch.setitem(globals_, "_prepare_recovery_socket_directory", lambda _tx: None)
    monkeypatch.setitem(globals_, "_write_openbao_config", lambda _raw: None)
    monkeypatch.setitem(globals_, "_restart_openbao", lambda _worker: None)
    monkeypatch.setitem(globals_, "_wait_openbao", lambda: None)
    monkeypatch.setitem(globals_, "_cleanup_recovery_socket_directory", lambda _tx: None)
    monkeypatch.setitem(
        globals_, "openbao_recovery_request",
        lambda *_args, **_kwargs: (200, {"started": False}),
    )

    def set_transaction(target: dict[str, object], **changes: object) -> None:
        target.update(changes)

    def remove_escrow() -> None:
        events.append("remove")
        escrow_path.unlink(missing_ok=True)

    def generate(target: dict[str, object]) -> tuple[str, bytearray]:
        assert not escrow_path.exists()
        assert target["root_remint_required"] is True
        assert target["root_generation_remints"] == 0
        events.append("generate")
        return "encoded", bytearray(b"replacement")

    monkeypatch.setitem(globals_, "_set_transaction", set_transaction)
    monkeypatch.setitem(globals_, "_remove_escrow", remove_escrow)
    monkeypatch.setitem(globals_, "generate_root_material", generate)
    recover({}, transaction)
    assert events == ["remove", "generate"]
    assert transaction["root_token_escrowed"] is False


def test_remint_progress_zero_without_escrow_is_cancelled_then_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    recover = namespace["_recover_or_cancel_root_generation"]
    globals_ = recover.__globals__
    state_root = tmp_path / "state"
    state_root.mkdir()
    transaction = {
        "root_generation_pending": True,
        "root_attempt_closed": False,
        "root_generation_remints": 0,
        "root_remint_required": True,
        "root_token_escrowed": False,
        "openbao_backup": str(tmp_path / "openbao.hcl"),
        "openbao_restore_required": False,
    }
    responses = iter(
        [
            (200, {"started": True, "required": 2, "progress": 0, "complete": False}),
            (204, None),
            (200, {"started": False}),
        ]
    )
    generated: list[bool] = []
    monkeypatch.setitem(globals_, "STATE_ROOT", state_root)
    monkeypatch.setitem(globals_, "ROOT_TOKEN_ESCROW", tmp_path / "missing.cred")
    monkeypatch.setitem(globals_, "OPENBAO_CONFIG", tmp_path / "installed.hcl")
    monkeypatch.setitem(globals_, "_read_root_file", lambda *_args, **_kwargs: b"config\n")
    monkeypatch.setitem(globals_, "_prepare_recovery_socket_directory", lambda _tx: None)
    monkeypatch.setitem(globals_, "_write_openbao_config", lambda _raw: None)
    monkeypatch.setitem(globals_, "_restart_openbao", lambda _worker: None)
    monkeypatch.setitem(globals_, "_wait_openbao", lambda: None)
    monkeypatch.setitem(globals_, "_cleanup_recovery_socket_directory", lambda _tx: None)
    monkeypatch.setitem(globals_, "openbao_recovery_request", lambda *_a, **_k: next(responses))
    monkeypatch.setitem(globals_, "_set_transaction", lambda target, **changes: target.update(changes))
    monkeypatch.setitem(
        globals_, "generate_root_material",
        lambda _tx: (generated.append(True) or "encoded", bytearray(b"replacement")),
    )
    recover({}, transaction)
    assert generated == [True]
    assert transaction["root_remint_required"] is True


def test_worker_reaps_interrupted_child_and_child_inherits_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    run_command = namespace["run_command"]
    globals_ = run_command.__globals__
    waits = 0
    killed: list[tuple[int, int]] = []

    class Process:
        pid = 4242

        def wait(self, timeout: int | None = None) -> int:
            nonlocal waits
            waits += 1
            if waits == 1:
                raise KeyboardInterrupt
            return -15

        def poll(self) -> None:
            return None

    observed: dict[str, object] = {}

    def popen(argv: list[str], **kwargs: object) -> Process:
        observed.update(kwargs)
        return Process()

    monkeypatch.setitem(globals_, "INHERITED_LOCK_FD", 9)
    monkeypatch.setattr(globals_["subprocess"], "Popen", popen)
    monkeypatch.setattr(globals_["os"], "killpg", lambda pid, sig: killed.append((pid, sig)))
    with pytest.raises(KeyboardInterrupt):
        run_command("interrupt", ["/bin/true"], timeout=1)
    assert observed["pass_fds"] == (9,)
    assert observed["start_new_session"] is True
    assert killed and killed[0][0] == 4242
    assert waits == 2


def test_rollback_discovers_backup_even_when_outer_wal_lacks_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    rollback = namespace["rollback_bootstrap"]
    globals_ = rollback.__globals__
    state_root = tmp_path / "state"
    state_root.mkdir()
    backup = tmp_path / "backup"
    metadata = {"transaction_id": "release-1"}
    transaction = {
        "release_id": "release-1", "deployment_backup": None,
        "credentials_before": {name: True for name in globals_["CREDENTIAL_GROUPS"]},
        "openbao_provisioning_started": False, "openbao_poststate_recorded": False,
        "approver_state_created": False,
    }
    calls: list[object] = []
    synced: list[Path] = []
    monkeypatch.setitem(globals_, "STATE_ROOT", state_root)
    for name in (
        "_quiesce_broker", "_recover_or_cancel_root_generation",
        "restore_openbao_from_transaction", "_finalize_recovered_userpass",
        "_undo_openbao_state", "_restore_broker_state", "_finalize_root_token",
        "_cleanup_recovery_guard",
    ):
        monkeypatch.setitem(globals_, name, lambda *_args, **_kwargs: None)
    monkeypatch.setitem(globals_, "_record_openbao_poststate", lambda _tx: None)
    monkeypatch.setitem(globals_, "_fsync_directory", lambda path: synced.append(Path(path)))
    monkeypatch.setitem(globals_, "_discover_deployment_backup", lambda _tx: backup)
    monkeypatch.setitem(globals_, "_set_transaction", lambda target, **changes: target.update(changes))
    monkeypatch.setitem(globals_, "append_audit", lambda *_args, **_kwargs: "0" * 64)
    monkeypatch.setitem(
        globals_, "_append_rollback_audit_exact_once", lambda _tx: "0" * 64
    )
    monkeypatch.setitem(globals_, "APPROVER_PATH", tmp_path / "approver")
    monkeypatch.setitem(globals_, "PROOF_PATH", tmp_path / "proof")
    worker = {
        "read_json": lambda *_args, **_kwargs: metadata,
        "rollback_transaction": lambda path, record: calls.append((path, record)),
        "atomic_json": lambda path, record: calls.append((path, record)),
        "_sync_rollback_filesystems": lambda: calls.append("syncfs"),
        "CURRENT_PATH": tmp_path / "current.json",
    }
    rollback(worker, transaction)
    assert calls[0] == (backup, metadata)
    assert "syncfs" in calls
    assert Path("/etc/credstore.encrypted") in synced
    assert transaction["status"] == "rolled-back"


def test_recovery_cleanup_replays_partial_asset_removal_before_terminal_wal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    cleanup = namespace["_cleanup_recovery_guard"]
    globals_ = cleanup.__globals__
    helper = tmp_path / "helper"
    assets = [helper, *(tmp_path / f"asset-{index}" for index in range(4))]
    for asset in assets:
        asset.write_text("reviewed", encoding="utf-8")
        asset.chmod(0o700 if asset == helper else 0o644)
    transaction = {"recovery_guard_installed": True, "recovery_guard_installing": False}
    fail_primary = {"value": True}
    real_lstat = os.lstat

    def controlled_lstat(path: object) -> os.stat_result:
        value = real_lstat(path)
        if Path(path) == assets[2] and fail_primary["value"]:
            return value
        return _as_root_owned(value)

    monkeypatch.setitem(globals_, "INSTALLED_HELPER", helper)
    monkeypatch.setitem(globals_, "RECOVERY_ASSETS", tuple(assets))
    monkeypatch.setitem(globals_, "RECOVERY_UNITS", tuple(f"u{index}" for index in range(3)))
    monkeypatch.setitem(globals_, "RECOVERY_PRIMARY_UNITS", ("u0", "u1"))
    monkeypatch.setitem(globals_, "RECOVERY_JANITOR_UNIT", "u2")
    monkeypatch.setitem(globals_, "RECOVERY_BARRIER_ASSETS", ())
    links_dir = tmp_path / "links"
    links_dir.mkdir()
    links = [links_dir / f"u{index}.service" for index in range(3)]
    for link, target in zip(links, assets[2:5], strict=True):
        link.symlink_to(target)
    enablement = dict(zip(links, assets[2:5], strict=True))
    monkeypatch.setitem(globals_, "RECOVERY_ENABLEMENT_LINKS", enablement)
    monkeypatch.setattr(globals_["os"], "lstat", controlled_lstat)
    monkeypatch.setitem(globals_, "_remove_recovery_barriers", lambda: None)
    monkeypatch.setitem(globals_, "_fsync_directory", lambda _path: None)
    monkeypatch.setitem(globals_, "_systemctl_state", lambda *_args: False)

    def systemctl(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if argv[:2] == ["/usr/bin/systemctl", "disable"]:
            unit = argv[2]
            index = int(unit[1:])
            links[index].unlink(missing_ok=True)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(globals_["subprocess"], "run", systemctl)
    monkeypatch.setitem(globals_, "_set_transaction", lambda target, **changes: target.update(changes))

    with pytest.raises(namespace["BootstrapError"], match="unsafe to remove"):
        cleanup(transaction)
    assert transaction["recovery_guard_installed"] is True
    assert helper.exists()
    assert not assets[1].exists()
    assert all(asset.exists() for asset in assets[2:])

    fail_primary["value"] = False
    cleanup(transaction)
    assert helper.exists()
    assert assets[4].exists()
    assert all(not asset.exists() for asset in assets[1:4])
    assert transaction["recovery_guard_installed"] is False
    assert transaction["recovery_guard_installing"] is False


def test_terminal_rollback_keeps_janitor_replayable_after_last_disable_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    cleanup = namespace["_cleanup_recovery_guard"]
    globals_ = cleanup.__globals__
    helper = tmp_path / "helper"
    assets = [helper, *(tmp_path / f"asset-{index}" for index in range(4))]
    for asset in assets:
        asset.write_text("reviewed", encoding="utf-8")
        asset.chmod(0o700 if asset == helper else 0o644)
    links_dir = tmp_path / "links"
    links_dir.mkdir()
    links = [links_dir / f"u{index}.service" for index in range(3)]
    for link, target in zip(links, assets[2:5], strict=True):
        link.symlink_to(target)
    transaction = {
        "status": "rolled-back",
        "phase": "rolled-back",
        "recovery_guard_installed": True,
        "recovery_guard_installing": False,
    }
    janitor_failures = {"remaining": 1}
    real_lstat = os.lstat

    def root_lstat(path: object) -> os.stat_result:
        value = real_lstat(path)
        return _as_root_owned(value) if Path(path) in set(assets) | set(links) else value

    def systemctl(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if argv[:2] != ["/usr/bin/systemctl", "disable"]:
            return subprocess.CompletedProcess(argv, 0)
        index = int(argv[2][1:])
        if index == 2 and janitor_failures["remaining"]:
            janitor_failures["remaining"] -= 1
            return subprocess.CompletedProcess(argv, 1)
        links[index].unlink(missing_ok=True)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setitem(globals_, "INSTALLED_HELPER", helper)
    monkeypatch.setitem(globals_, "RECOVERY_ASSETS", tuple(assets))
    monkeypatch.setitem(globals_, "RECOVERY_UNITS", ("u0", "u1", "u2"))
    monkeypatch.setitem(globals_, "RECOVERY_PRIMARY_UNITS", ("u0", "u1"))
    monkeypatch.setitem(globals_, "RECOVERY_JANITOR_UNIT", "u2")
    monkeypatch.setitem(globals_, "RECOVERY_BARRIER_ASSETS", ())
    monkeypatch.setitem(
        globals_, "RECOVERY_ENABLEMENT_LINKS",
        dict(zip(links, assets[2:5], strict=True)),
    )
    monkeypatch.setattr(globals_["os"], "lstat", root_lstat)
    monkeypatch.setattr(globals_["subprocess"], "run", systemctl)
    monkeypatch.setitem(globals_, "_remove_recovery_barriers", lambda: None)
    monkeypatch.setitem(globals_, "_fsync_directory", lambda _path: None)
    monkeypatch.setitem(
        globals_, "_systemctl_state",
        lambda unit, operation: (
            operation == "is-enabled" and os.path.lexists(links[int(unit[1:])])
        ),
    )
    monkeypatch.setitem(
        globals_, "_set_transaction", lambda target, **changes: target.update(changes)
    )

    with pytest.raises(namespace["BootstrapError"], match="janitor"):
        cleanup(transaction)
    assert transaction["status"] == "rolled-back"
    assert os.path.lexists(links[2])

    cleanup(transaction)
    assert transaction["status"] == "rolled-back"
    assert not os.path.lexists(links[2])


def test_terminal_cleanup_kill_after_every_mutation_is_valid_and_replayable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    cleanup = namespace["_cleanup_recovery_guard"]
    validate = namespace["_validate_recovery_scaffold_transition"]
    globals_ = cleanup.__globals__

    class SimulatedPowerLoss(RuntimeError):
        pass

    expected_events = [
        "unlink-barrier-0",
        "unlink-barrier-1",
        "unlink-barrier-2",
        "disable-u0",
        "disable-u1",
        "unlink-asset-1",
        "unlink-asset-2",
        "unlink-asset-3",
        "wal-flags-inert",
        "proof-reconciled",
        "disable-u2",
    ]

    def exercise(crash_at: int | None) -> list[str]:
        label = "complete" if crash_at is None else f"crash-{crash_at}"
        root = tmp_path / label
        root.mkdir()
        helper = root / "helper"
        assets = [helper, *(root / f"asset-{index}" for index in range(1, 5))]
        barriers = tuple(root / f"barrier-{index}" for index in range(3))
        for path in (*assets, *barriers):
            path.write_bytes(b"reviewed")
            path.chmod(0o700 if path == helper else 0o644)
        links_root = root / "links"
        links_root.mkdir()
        links = tuple(links_root / f"u{index}.service" for index in range(3))
        for link, target in zip(links, assets[2:5], strict=True):
            link.symlink_to(target)
        enablement = dict(zip(links, assets[2:5], strict=True))
        unit_links = {f"u{index}": link for index, link in enumerate(links)}
        transaction = {
            "status": "rolled-back",
            "phase": "rolled-back",
            "recovery_guard_installing": False,
            "recovery_guard_installed": True,
        }
        events: list[str] = []
        crash_enabled = {"value": crash_at is not None}
        real_lstat = os.lstat
        real_unlink = Path.unlink
        removable_labels = {
            barriers[index]: f"unlink-barrier-{index}" for index in range(3)
        } | {
            assets[index]: f"unlink-asset-{index}" for index in range(1, 4)
        }

        def checkpoint(event: str) -> None:
            events.append(event)
            if crash_enabled["value"] and len(events) == crash_at:
                raise SimulatedPowerLoss(event)

        def root_lstat(path: object) -> os.stat_result:
            value = real_lstat(path)
            try:
                inside = Path(path).is_relative_to(root)
            except (TypeError, ValueError):
                inside = False
            return _as_root_owned(value) if inside else value

        def tracked_unlink(path: Path, *args: object, **kwargs: object) -> None:
            real_unlink(path, *args, **kwargs)
            event = removable_labels.get(path)
            if event is not None:
                checkpoint(event)

        def systemctl(
            argv: list[str], **_kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            if argv[:2] == ["/usr/bin/systemctl", "disable"]:
                unit = argv[2]
                link = unit_links[unit]
                if os.path.lexists(link):
                    real_unlink(link)
                checkpoint(f"disable-{unit}")
            return subprocess.CompletedProcess(argv, 0)

        def set_transaction(target: dict[str, object], **changes: object) -> None:
            target.update(changes)
            checkpoint("wal-flags-inert")

        with monkeypatch.context() as scoped:
            scoped.setitem(globals_, "INSTALLED_HELPER", helper)
            scoped.setitem(
                globals_, "RECOVERY_ASSETS", tuple((*assets, *barriers))
            )
            scoped.setitem(globals_, "RECOVERY_UNITS", ("u0", "u1", "u2"))
            scoped.setitem(globals_, "RECOVERY_PRIMARY_UNITS", ("u0", "u1"))
            scoped.setitem(globals_, "RECOVERY_JANITOR_UNIT", "u2")
            scoped.setitem(globals_, "RECOVERY_BARRIER_ASSETS", barriers)
            scoped.setitem(globals_, "RECOVERY_ENABLEMENT_LINKS", enablement)
            scoped.setattr(globals_["os"], "lstat", root_lstat)
            scoped.setattr(Path, "unlink", tracked_unlink)
            scoped.setattr(globals_["subprocess"], "run", systemctl)
            scoped.setitem(globals_, "_fsync_directory", lambda _path: None)
            scoped.setitem(
                globals_, "_systemctl_state",
                lambda unit, operation: (
                    operation == "is-enabled" and os.path.lexists(unit_links[unit])
                ),
            )
            scoped.setitem(globals_, "_set_transaction", set_transaction)
            scoped.setitem(
                globals_, "_remove_public_installed_proof",
                lambda _transaction: checkpoint("proof-reconciled"),
            )

            if crash_at is None:
                cleanup(transaction)
            else:
                with pytest.raises(SimulatedPowerLoss):
                    cleanup(transaction)
                states = {
                    path: "new" if os.path.lexists(path) else "missing"
                    for path in (*assets, *barriers)
                }
                link_states = {
                    link: os.path.lexists(link) for link in enablement
                }
                validate(
                    states,
                    link_states,
                    predecessor_declared=False,
                    successor_declared=True,
                    cleanup_flags=(
                        transaction["recovery_guard_installing"],
                        transaction["recovery_guard_installed"],
                    ),
                )
                crash_enabled["value"] = False
                cleanup(transaction)

        assert helper.exists()
        assert assets[4].exists()
        assert all(not path.exists() for path in (*assets[1:4], *barriers))
        assert not any(os.path.lexists(link) for link in links)
        assert transaction["recovery_guard_installing"] is False
        assert transaction["recovery_guard_installed"] is False
        return events[: len(expected_events)]

    assert exercise(None) == expected_events
    for crash_at in range(1, len(expected_events) + 1):
        assert exercise(crash_at)[:crash_at] == expected_events[:crash_at]


def test_recovery_activation_replays_cleanup_for_terminal_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    entry = namespace["recovery_activation_entry"]
    globals_ = entry.__globals__
    state_root = tmp_path / "state"
    state_root.mkdir()
    helper = tmp_path / "helper"
    helper.write_text("helper", encoding="utf-8")
    helper.chmod(0o700)
    transaction_path = tmp_path / "transaction.json"
    transaction_path.write_text("{}", encoding="utf-8")
    transaction = {"status": "rolled-back"}
    lock_fd = os.open(tmp_path / "lock", os.O_WRONLY | os.O_CREAT, 0o600)
    cleanup_calls: list[str] = []
    real_lstat = os.lstat
    real_stat = os.stat

    monkeypatch.setitem(globals_, "INSTALLED_HELPER", helper)
    monkeypatch.setitem(globals_, "STATE_ROOT", state_root)
    monkeypatch.setitem(globals_, "TRANSACTION_PATH", transaction_path)
    monkeypatch.setitem(globals_, "__file__", str(helper))
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    monkeypatch.setattr(
        globals_["os"], "lstat",
        lambda path: _as_root_owned(real_lstat(helper if Path(path) == helper else path)),
    )
    monkeypatch.setattr(
        globals_["os"], "stat",
        lambda path, *args, **kwargs: real_stat(
            helper if Path(path) == helper else path, *args, **kwargs
        ),
    )
    monkeypatch.setitem(globals_, "_read_json", lambda *_args, **_kwargs: transaction)
    monkeypatch.setitem(
        globals_,
        "_read_standard_transaction_frontier",
        lambda **_kwargs: transaction,
    )
    monkeypatch.setitem(globals_, "_validate_transaction_document", lambda _tx: None)
    monkeypatch.setitem(
        globals_, "_ensure_bootstrap_consumed_audit_exact_once",
        lambda _tx: "0" * 64,
    )
    monkeypatch.setitem(globals_, "_acquire_lock", lambda **_kwargs: lock_fd)
    monkeypatch.setitem(
        globals_, "_cleanup_recovery_guard",
        lambda _tx: cleanup_calls.append("cleanup"),
    )
    try:
        assert entry() == 0
        assert cleanup_calls == ["cleanup"]
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


def test_post_rollback_cleanup_failure_cannot_downgrade_terminal_wal() -> None:
    namespace = bootstrap_namespace()
    source = __import__("inspect").getsource(namespace["apply_bootstrap"])
    terminal_guard = 'transaction.get("status") in {"completed", "rolled-back"}'
    assert terminal_guard in source
    assert source.index(terminal_guard) < source.index("rollback_error: BaseException")


def test_failure_diagnostic_accepts_only_bounded_identifiers() -> None:
    namespace = bootstrap_namespace()
    diagnostic = namespace["_bootstrap_failure_diagnostic"]

    failure = RuntimeError("a secret must never become diagnostic data")
    failure.code = "docker_conflict"
    assert diagnostic(failure, {"phase": "broker-quiesced"}) == (
        "docker_conflict",
        "broker-quiesced",
    )

    failure.code = "secret\nvalue"
    assert diagnostic(failure, {"phase": "x" * 65}) == (
        "bootstrap_failed",
        "bootstrap-running",
    )


def test_apply_failure_audits_worker_code_before_rollback_and_preserves_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    apply = namespace["apply_bootstrap"]
    globals_ = apply.__globals__
    manifest = {"release_id": "release-1"}
    worker_bytes = b"reviewed worker"
    preflight = {
        "bootstrap_helper_sha256": "1" * 64,
        "source_tree_sha256": "2" * 64,
        "manifest_sha256": "3" * 64,
    }
    transaction = {"status": "running", "phase": "consumed"}
    events: list[tuple[str, object]] = []

    class WorkerFailure(RuntimeError):
        code = "docker_conflict"

    def capture_backup(_manifest: dict[str, object]) -> object:
        raise WorkerFailure("sensitive exception text")

    worker = {
        "STATE_ROOT": tmp_path / "state",
        "RELEASES_ROOT": tmp_path / "releases",
        "BACKUP_ROOT": tmp_path / "backups",
        "validate_manifest": lambda _manifest: None,
        "stage_release": lambda _manifest: tmp_path / "release",
        "_sync_rollback_filesystems": lambda: None,
        "capture_backup": capture_backup,
    }
    monkeypatch.setitem(
        globals_,
        "load_reviewed_release",
        lambda _helper: (manifest, "2" * 64, "3" * 64, worker_bytes),
    )
    monkeypatch.setitem(globals_, "_review_enrollment_resume", lambda *_args: None)
    monkeypatch.setitem(globals_, "bootstrap_preflight", lambda *_args, **_kwargs: dict(preflight))
    monkeypatch.setitem(globals_, "_new_transaction", lambda *_args: transaction)
    monkeypatch.setitem(globals_, "consume_exception", lambda *_args: None)
    monkeypatch.setitem(globals_, "_write_live_transaction", lambda *_args: None)
    monkeypatch.setitem(globals_, "_load_verified_worker", lambda _raw: worker)
    monkeypatch.setitem(
        globals_, "_set_transaction", lambda target, **changes: target.update(changes)
    )
    monkeypatch.setitem(globals_, "_install_recovery_guard", lambda *_args: None)
    monkeypatch.setitem(
        globals_, "_start_standard_recovery_supervisor", lambda *_args: None
    )
    monkeypatch.setitem(
        globals_, "_wait_standard_recovery_supervisor", lambda: None
    )
    monkeypatch.setitem(globals_, "_quiesce_broker", lambda _worker: None)

    def audit(event: str, **details: object) -> str:
        events.append(("audit", {"event": event, **details}))
        return "4" * 64

    def rollback(_worker: dict[str, object], _transaction: dict[str, object]) -> None:
        events.append(("rollback", None))

    monkeypatch.setitem(globals_, "append_audit", audit)
    monkeypatch.setitem(globals_, "rollback_bootstrap", rollback)

    with pytest.raises(namespace["BootstrapError"]) as failure:
        apply(
            worker_bytes,
            manifest,
            preflight,
            "session-1",
            1000,
            bytearray(b"openbao secret"),
            bytearray(b"zulip secret"),
            9,
        )
    assert failure.value.code == "docker_conflict"
    assert failure.value.phase == "broker-quiesced"
    assert "sensitive exception text" not in str(failure.value)
    assert events == [
        (
            "audit",
            {
                "event": "bootstrap_failure_detected",
                "release_id": "release-1",
                "details": {
                    "code": "docker_conflict",
                    "phase": "broker-quiesced",
                },
            },
        ),
        ("rollback", None),
    ]
    assert "sensitive exception text" not in repr(events)
    assert "openbao secret" not in repr(events)
    assert "zulip secret" not in repr(events)


def test_gui_failure_result_keeps_safe_code_and_failure_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    main = namespace["main"]
    globals_ = main.__globals__
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fail(_arguments: list[str]) -> int:
        raise namespace["BootstrapError"](
            "docker_conflict", "sensitive exception text", phase="broker-quiesced"
        )

    monkeypatch.setattr(globals_["sys"], "argv", ["bootstrap", "gui-apply"])
    monkeypatch.setitem(globals_, "public_entry", fail)
    monkeypatch.setitem(
        globals_, "_emit_gui_event", lambda *args, **kwargs: emitted.append((args, kwargs))
    )

    assert main() == 1
    assert emitted == [
        (
            ("result", "broker-quiesced", "Échec sécurisé.", 100, "failed"),
            {"error_code": "docker_conflict", "operation_mode": "fresh"},
        )
    ]


def test_openbao_undo_conflict_performs_no_mutating_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    undo_state = namespace["_undo_openbao_state"]
    globals_ = undo_state.__globals__
    before = {
        "roles": {name: None for name in globals_["OPENBAO_UNDO_ROLES"]},
        "policies": {name: None for name in globals_["OPENBAO_UNDO_POLICIES"]},
        "kv": {name: None for name in globals_["OPENBAO_UNDO_KV"]},
    }
    escrow = {
        "schema_version": 1,
        "release_id": "release-1",
        "captured_at": 1,
        **before,
        "poststate": {"roles": {}, "policies": {}, "kv": {}},
    }
    transaction = {
        "release_id": "release-1",
        "openbao_undo_complete": False,
        "openbao_undo_escrowed": True,
        "openbao_provisioning_started": True,
        "openbao_poststate_recorded": True,
        "openbao_undo_started": False,
    }
    mutations: list[tuple[object, ...]] = []
    monkeypatch.setitem(globals_, "_read_encrypted_document", lambda *_args: escrow)
    monkeypatch.setitem(globals_, "_root_token_from_escrow", lambda: "root-token")
    monkeypatch.setitem(
        globals_, "_capture_openbao_managed_state",
        lambda _token: {"roles": {"changed": True}, "policies": {}, "kv": {}},
    )
    monkeypatch.setitem(
        globals_, "openbao_request",
        lambda *args, **kwargs: mutations.append((*args, kwargs)) or (204, None),
    )
    with pytest.raises(namespace["BootstrapError"], match="fail-closed"):
        undo_state(transaction)
    assert mutations == []
    assert transaction["openbao_undo_started"] is False


def test_unused_openbao_undo_escrow_is_validated_and_removed_after_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    undo_state = namespace["_undo_openbao_state"]
    globals_ = undo_state.__globals__
    escrow_path = tmp_path / "openbao-undo.cred"
    escrow_path.write_bytes(b"encrypted")
    escrow_path.chmod(0o400)
    transaction: dict[str, object] = {
        "release_id": "release-1",
        "openbao_undo_complete": False,
        "openbao_undo_escrowed": False,
        "openbao_undo_write_started": True,
        "openbao_provisioning_started": False,
    }
    document = {
        "schema_version": 1,
        "release_id": "release-1",
        "captured_at": 1,
        "roles": {name: None for name in globals_["OPENBAO_UNDO_ROLES"]},
        "policies": {name: None for name in globals_["OPENBAO_UNDO_POLICIES"]},
        "kv": {name: None for name in globals_["OPENBAO_UNDO_KV"]},
        "poststate": None,
    }
    real_lstat = os.lstat

    def root_lstat(path: object) -> os.stat_result:
        value = real_lstat(path)
        return _as_root_owned(value) if Path(path) == escrow_path else value

    monkeypatch.setitem(globals_, "OPENBAO_UNDO_ESCROW", escrow_path)
    monkeypatch.setattr(globals_["os"], "lstat", root_lstat)
    monkeypatch.setitem(globals_, "_read_encrypted_document", lambda *_args: document)
    monkeypatch.setitem(globals_, "_fsync_directory", lambda _path: None)
    monkeypatch.setitem(globals_, "_set_transaction", lambda target, **changes: target.update(changes))
    undo_state(transaction)
    assert not escrow_path.exists()
    assert transaction["openbao_undo_write_started"] is False
    assert transaction["openbao_undo_complete"] is True


def test_provider_zero_key_routes_are_exactly_21_data_metadata_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    derive = namespace["_provider_credential_paths"]
    globals_ = derive.__globals__
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    registry = (ROOT / "catalog/provider-integrations.v1.json").read_bytes()
    monkeypatch.setitem(globals_, "_read_root_file", lambda *_args, **_kwargs: registry)
    paths = derive(document)
    assert len(paths) == 21
    assert len(set(paths)) == 21
    assert all(data.startswith("/v1/") and "/data/" in data for data, _metadata in paths)
    assert all(metadata == data.replace("/data/", "/metadata/", 1) for data, metadata in paths)


def _runtime_provider_health() -> list[dict[str, object]]:
    sys.path.insert(0, str(ROOT / "orchestrator/src"))
    try:
        from ops_orchestrator.config import expand_catalogue_runtime, load_config

        config = expand_catalogue_runtime(
            load_config(ROOT / "orchestrator/config/orchestrator.json"),
            catalogue_path=ROOT / "catalog/model-catalog.v2.json",
            provider_integrations_path=ROOT / "catalog/provider-integrations.v1.json",
        )
        return [
            {
                "id": provider.provider_id,
                "provider_account_id": provider.account_id,
                "role": provider.role.value,
                "location": provider.location,
                "available": False,
                "reason": "credential_unavailable",
            }
            for provider in config.providers
        ]
    finally:
        sys.path.remove(str(ROOT / "orchestrator/src"))


def test_bootstrap_health_proves_absent_provider_metadata_and_accepts_auxiliary_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    health = namespace["_bootstrap_health"]
    globals_ = health.__globals__
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    providers = _runtime_provider_health()
    assert len(providers) == 19
    # Three deployments are auxiliary, but qwen-coder-api is still bound to
    # the Alibaba account; only embedding/reranker intentionally have no
    # provider account.
    assert sum(provider["provider_account_id"] is None for provider in providers) == 2
    catalogue = document["components"]["model_catalogue"]
    orchestrator = {
        "status": "degraded",
        "database": "ok",
        "mode": "api-first",
        "provider_availability_scope": "configuration-and-secret-only",
        "provider_network_probe": False,
        "providers": providers,
        "limits": {"max_iterations": 1, "max_provider_calls": 1, "max_context_tokens": 1},
        "catalogue": {
            "schema_version": catalogue["schema_version"],
            "revision": catalogue["revision"],
            "cards": catalogue["cards"],
            "provider_accounts": catalogue["provider_accounts"],
        },
    }
    approver_id = 41
    approver = tmp_path / "approver.json"
    approver.write_text(
        json.dumps({"zulip_user_id": approver_id, "actor_id": f"zulip:{approver_id}"}),
        encoding="utf-8",
    )
    paths = tuple(
        (f"/v1/kv-infra-shared/data/llm/providers/p{index}",
         f"/v1/kv-infra-shared/metadata/llm/providers/p{index}")
        for index in range(21)
    )
    metadata_present = {"value": False}

    def api_request(method: str, path: str, **_kwargs: object) -> tuple[int, object]:
        if path == "/v1/kv-infra-shared/data/zulip/bot":
            return 200, {"data": {"data": {"approver_user_ids": "41", "bot_user_id": "42"}}}
        if "/metadata/" in path and metadata_present["value"] and path == paths[0][1]:
            return 200, {"data": {}}
        return 404, None

    maintenance_fault: dict[str, str | None] = {"disabled": None, "active": None}
    health_commands: list[tuple[str, tuple[str, ...]]] = []

    def system_state(unit: str, operation: str) -> bool:
        if operation == "is-active" and unit == maintenance_fault["active"]:
            return True
        if operation == "is-enabled" and unit == maintenance_fault["disabled"]:
            return False
        return (
            unit in globals_["BOOTSTRAP_ACTIVE_SERVICES"] and operation == "is-active"
        ) or (
            unit
            in (
                *globals_["BROKER_MAINTENANCE_UNITS"][:2],
                "zulip-alertmanager-query.socket",
            )
            and operation == "is-enabled"
        )

    monkeypatch.setitem(globals_, "_systemctl_state", system_state)
    monkeypatch.setitem(globals_, "_validate_encrypted_file", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(
        globals_, "_validate_alertmanager_query_socket", lambda: None
    )
    monkeypatch.setitem(globals_, "_provider_credential_paths", lambda _manifest: paths)
    monkeypatch.setitem(globals_, "_root_token_from_escrow", lambda: "root-token")
    monkeypatch.setitem(globals_, "openbao_request", api_request)
    monkeypatch.setitem(globals_, "APPROVER_PATH", approver)
    monkeypatch.setitem(globals_, "_read_json", lambda path, *_args: json.loads(Path(path).read_text()))
    monkeypatch.setitem(
        globals_, "_unix_health",
        lambda path, _request: orchestrator if "ops-orchestrator" in str(path) else {"status": "ok"},
    )
    monkeypatch.setitem(
        globals_, "_bounded_json_command",
        lambda argv, **_kwargs: (
            {
                "status": "ok",
                "registry_revision": document["components"]["provider_integrations"]["revision"],
                "direct_connectors": ["deepseek", "moonshot", "stepfun"],
                "credential_scope": "finance_direct_only",
            }
            if "/usr/bin/runuser" in argv
            else {"ok": True, "policy_sha256": "5" * 64}
        ),
    )
    atlas = document["artifacts"]["atlas_rpm"]
    worker = {
        "local_inference_status": lambda: "disabled",
        "validate_docker_installation": lambda **_kwargs: "active",
            "validate_docker_runtime_network": lambda: None,
            "reload_firewalld_with_docker_barrier": lambda: None,
        "validate_zulip_precommit_restart_state": lambda _metadata: None,
        "run_command": lambda name, argv, **_kwargs: health_commands.append(
            (name, tuple(argv))
        ),
        "atlas_installed_identity": lambda: (
            f"{atlas['name']}-{atlas['version']}-{atlas['release']}.{atlas['architecture']}"
        ),
        "atlas_installation_is_pristine": lambda: True,
    }
    proof = health(worker, document, approver_id, {})
    assert proof["provider_keys_configured"] == 0
    assert proof["provider_routes_available"] == 0
    assert any(
        name == "bootstrap-health-alertmanager-query"
        and "/run/zulip-alertmanager-query/api.sock" in argv
        and "http://localhost/api/v2/alerts" in argv
        for name, argv in health_commands
    )

    metadata_present["value"] = True
    with pytest.raises(namespace["BootstrapError"], match="provider credential exists"):
        health(worker, document, approver_id, {})
    metadata_present["value"] = False

    maintenance_fault["disabled"] = globals_["BROKER_MAINTENANCE_UNITS"][0]
    with pytest.raises(namespace["BootstrapError"], match="maintenance timer"):
        health(worker, document, approver_id, {})
    maintenance_fault["disabled"] = None

    maintenance_fault["active"] = globals_["BROKER_MAINTENANCE_UNITS"][2]
    with pytest.raises(namespace["BootstrapError"], match="maintenance oneshot"):
        health(worker, document, approver_id, {})
    maintenance_fault["active"] = globals_["ZULIP_TRIGGERED_SERVICES"][0]
    with pytest.raises(namespace["BootstrapError"], match="triggered Zulip oneshot"):
        health(worker, document, approver_id, {})


def test_recovery_enablement_accepts_exact_absolute_and_relative_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    validate = namespace["_validate_enablement_link"]
    globals_ = validate.__globals__
    target = tmp_path / "unit.service"
    target.write_text("[Unit]\n", encoding="utf-8")
    absolute = tmp_path / "absolute.service"
    relative_dir = tmp_path / "wants"
    relative_dir.mkdir()
    relative = relative_dir / "relative.service"
    absolute.symlink_to(target)
    relative.symlink_to(Path("../unit.service"))
    real_lstat = os.lstat

    def root_lstat(path: object) -> os.stat_result:
        value = real_lstat(path)
        return _as_root_owned(value) if Path(path) in {absolute, relative} else value

    monkeypatch.setattr(globals_["os"], "lstat", root_lstat)
    validate(absolute, target)
    validate(relative, target)


@pytest.mark.parametrize("entry_name", ["recovery_entry", "recovery_activation_entry"])
def test_recovery_entry_injects_live_lock_into_verified_worker(
    entry_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    entry = namespace[entry_name]
    globals_ = entry.__globals__
    state_root = tmp_path / "state"
    state_root.mkdir()
    helper = tmp_path / "helper"
    helper.write_text("helper", encoding="utf-8")
    helper.chmod(0o700)
    transaction_path = tmp_path / "transaction.json"
    transaction_path.write_text("{}", encoding="utf-8")
    consumed_path = tmp_path / "consumed.json"
    consumed_path.write_text("{}", encoding="utf-8")
    live_path = tmp_path / "live.json"
    lock_fd = os.open(tmp_path / "lock", os.O_WRONLY | os.O_CREAT, 0o600)
    transaction = {
        "status": "running" if entry_name == "recovery_entry" else "activation-pending",
        "staged_release": "/var/lib/ops-control-plane-deployment/releases/release-1",
        "openbao_restore_required": False,
        "recovery_activation_units": [],
        "broker_state_before": {
            "ops-broker.socket": {"active": False, "enabled": False},
            "ops-broker.service": {"active": False, "enabled": False},
        },
        "service_state_before": {
            unit: {"active": False, "enabled": False}
            for unit in globals_["RECOVERY_DEPENDENT_UNITS"]
        },
        "release_id": "release-1",
    }
    worker: dict[str, object] = {
        "SERVICE_NAMES": (),
        "run_command": lambda *_args, **_kwargs: None,
        "_unit_is_loaded": lambda _unit: False,
        "_sync_rollback_filesystems": lambda: worker.update({"synced": True}),
    }
    real_lstat = os.lstat
    real_stat = os.stat

    def root_lstat(path: object) -> os.stat_result:
        value = real_lstat(helper if Path(path) == helper else path)
        return _as_root_owned(value) if Path(path) == helper else value

    monkeypatch.setitem(globals_, "INSTALLED_HELPER", helper)
    monkeypatch.setitem(globals_, "STATE_ROOT", state_root)
    monkeypatch.setitem(globals_, "TRANSACTION_PATH", transaction_path)
    monkeypatch.setitem(globals_, "CONSUMED_PATH", consumed_path)
    monkeypatch.setitem(globals_, "LIVE_TRANSACTION_PATH", live_path)
    monkeypatch.setitem(globals_, "__file__", str(helper))
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    monkeypatch.setattr(globals_["os"], "lstat", root_lstat)
    monkeypatch.setattr(globals_["os"], "stat", lambda path, *a, **k: real_stat(helper if Path(path) == helper else path, *a, **k))
    monkeypatch.setitem(globals_, "_read_json", lambda path, *_args: (
        transaction if Path(path) == transaction_path else {"bootstrap_helper_sha256": "6" * 64}
    ))
    monkeypatch.setitem(
        globals_,
        "_read_standard_transaction_frontier",
        lambda **_kwargs: transaction,
    )
    monkeypatch.setitem(globals_, "_validate_transaction_document", lambda _tx: None)
    # Full recovery now repairs the immutable marker-bound first audit record
    # before loading the staged worker.
    monkeypatch.setitem(
        globals_, "_ensure_bootstrap_consumed_audit_exact_once",
        lambda _tx: "0" * 64,
    )
    monkeypatch.setitem(globals_, "_acquire_lock", lambda **_kwargs: lock_fd)
    monkeypatch.setitem(globals_, "_write_live_transaction", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(globals_, "_verified_staged_worker", lambda _tx: worker)
    monkeypatch.setitem(globals_, "_discover_deployment_backup", lambda _tx: None)
    monkeypatch.setitem(globals_, "_planned_recovery_activations", lambda *_args: [])
    monkeypatch.setitem(globals_, "_set_transaction", lambda target, **changes: target.update(changes))
    monkeypatch.setitem(globals_, "append_audit", lambda *_args, **_kwargs: "0" * 64)
    monkeypatch.setitem(
        globals_, "_append_rollback_audit_exact_once", lambda _tx: "0" * 64
    )
    monkeypatch.setitem(globals_, "_restore_broker_state", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(globals_, "_defer_barriered_starts", lambda _worker, callback: callback([]) or [])
    monkeypatch.setitem(globals_, "_remove_recovery_barriers", lambda: None)
    monkeypatch.setitem(globals_, "_systemctl_state", lambda *_args: False)
    monkeypatch.setitem(globals_, "_cleanup_recovery_guard", lambda _tx: None)
    try:
        assert entry(openbao_only=False) == 0 if entry_name == "recovery_entry" else entry() == 0
        assert worker["INHERITED_LOCK_FD"] == lock_fd
        if entry_name == "recovery_entry":
            assert worker["synced"] is True
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass


def test_recovery_workers_and_mutators_keep_the_same_ofd_lock() -> None:
    bootstrap = BOOTSTRAP.read_text(encoding="utf-8")
    worker = WORKER.read_text(encoding="utf-8")
    assert bootstrap.count('worker["INHERITED_LOCK_FD"] = lock') == 3
    assert 'worker["INHERITED_LOCK_FD"] = deployment_lock_fd' in bootstrap
    assert "pass_fds=_deployment_pass_fds()" in bootstrap
    assert "pass_fds=inherited" in worker
    assert "except BaseException:" in worker
    assert BOOTSTRAP.stat().st_mode & stat.S_IXUSR


def test_no_backup_recovery_preserves_initial_non_broker_active_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    planned = namespace["_planned_recovery_activations"]
    globals_ = planned.__globals__
    initial = {
        unit: {"active": unit == "ops-orchestrator.service", "enabled": True}
        for unit in globals_["RECOVERY_DEPENDENT_UNITS"]
    }
    transaction = {
        "service_state_before": initial,
        "broker_state_before": {
            unit: dict(initial[unit])
            for unit in ("ops-broker.socket", "ops-broker.service")
        },
    }
    assert planned({}, transaction, None) == ["ops-orchestrator.service"]

    commands: list[list[str]] = []
    worker = {
        "_unit_is_loaded": lambda _unit: True,
        "run_command": lambda _name, argv, *, timeout: commands.append(argv),
    }
    transaction["recovery_activation_units"] = ["ops-orchestrator.service"]
    namespace["_queue_recovery_unit_jobs"](worker, transaction)
    orchestrator = next(argv for argv in commands if argv[-1] == "ops-orchestrator.service")
    assert orchestrator == [
        "/usr/bin/systemctl", "--no-block", "start", "ops-orchestrator.service"
    ]


def test_root_accessor_reconciliation_rechecks_revoked_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    reconcile = namespace["_reconcile_generated_root_tokens"]
    globals_ = reconcile.__globals__
    own = "a" * 16
    orphan = "b" * 16
    revoked = {"value": False}
    events: list[tuple[str, str, str | None]] = []

    def profile(accessor: str, created: int) -> dict[str, object]:
        return {
            "data": {
                "accessor": accessor,
                "policies": ["root"],
                "display_name": "root",
                "path": "auth/token/root",
                "orphan": True,
                "ttl": 0,
                "creation_time": created,
            }
        }

    def request(method: str, path: str, **kwargs: object) -> tuple[int, object]:
        payload = kwargs.get("payload")
        accessor = payload.get("accessor") if isinstance(payload, dict) else None
        events.append((method, path, accessor if isinstance(accessor, str) else None))
        if path.endswith("lookup-self"):
            return 200, profile(own, 100)
        if method == "LIST":
            return 200, {"data": {"keys": [own, orphan]}}
        if path.endswith("revoke-accessor"):
            revoked["value"] = True
            # Deliberately ambiguous: the implementation must not trust 400.
            return 400, None
        if accessor == own:
            return 200, profile(own, 100)
        if accessor == orphan and revoked["value"]:
            return 400, None
        if accessor == orphan:
            return 200, profile(orphan, 99)
        raise AssertionError((method, path, kwargs))

    monkeypatch.setitem(globals_, "openbao_request", request)
    reconcile("root-token", [{"start": 90, "end": 100}])
    assert events[-2:] == [
        ("POST", "/v1/auth/token/revoke-accessor", orphan),
        ("POST", "/v1/auth/token/lookup-accessor", orphan),
    ]

    revoked["value"] = False

    def never_disappears(method: str, path: str, **kwargs: object) -> tuple[int, object]:
        status, document = request(method, path, **kwargs)
        payload = kwargs.get("payload")
        if (
            path.endswith("lookup-accessor")
            and isinstance(payload, dict)
            and payload.get("accessor") == orphan
            and revoked["value"]
        ):
            return 200, profile(orphan, 99)
        return status, document

    monkeypatch.setitem(globals_, "openbao_request", never_disappears)
    with pytest.raises(namespace["BootstrapError"], match="remains after revocation"):
        reconcile("root-token", [{"start": 90, "end": 100}])


def test_userpass_password_is_escrowed_before_recovery_wal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    escrow = namespace["_escrow_userpass_password"]
    globals_ = escrow.__globals__
    events: list[str] = []
    observed: dict[str, object] = {}

    def write(_path: Path, _name: str, document: dict[str, object], *, create: bool) -> None:
        events.append("ciphertext")
        observed.update(document)
        assert create is True

    def update(target: dict[str, object], **changes: object) -> None:
        events.append("wal")
        target.update(changes)

    monkeypatch.setitem(globals_, "_write_encrypted_document", write)
    monkeypatch.setitem(globals_, "_set_transaction", update)
    transaction: dict[str, object] = {"release_id": "release-1"}
    password = bytearray(b"a-strong-password")
    escrow(password, transaction)
    assert events == ["ciphertext", "wal"]
    assert observed["release_id"] == "release-1"
    assert observed["password_base64"] == "YS1zdHJvbmctcGFzc3dvcmQ="
    assert transaction["userpass_password_escrowed"] is True


def test_userpass_recovery_replays_escrow_when_wal_flag_was_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    finalize = namespace["_finalize_recovered_userpass"]
    globals_ = finalize.__globals__
    escrow = tmp_path / "userpass-password.cred"
    escrow.write_bytes(b"encrypted")
    transaction: dict[str, object] = {
        "release_id": "release-1",
        "password_recovery_started": False,
        "password_recovery_persists": False,
        "userpass_password_escrowed": False,
        "userpass_finalized": False,
    }
    calls: list[tuple[str, str, object]] = []
    revoked: list[str] = []

    def request(method: str, path: str, **kwargs: object) -> tuple[int, object]:
        payload = kwargs.get("payload")
        calls.append((method, path, payload))
        if method == "POST" and path.endswith("login/ops-user"):
            assert payload == {"password": "a-strong-password"}
            return 200, {"auth": {"client_token": "h" * 16}}
        if method == "GET" and path.endswith("lookup-self"):
            return 200, {
                "data": {
                    "policies": ["default", "token-self", "human-admin"],
                    "ttl": 120,
                }
            }
        if method == "GET" and path.endswith("users/ops-user"):
            return 200, {
                "data": {
                    "token_policies": ["token-self", "human-admin"],
                    "token_ttl": 3600,
                    "token_max_ttl": 28800,
                }
            }
        return 204, None

    monkeypatch.setitem(globals_, "USERPASS_PASSWORD_ESCROW", escrow)
    monkeypatch.setitem(
        globals_, "_userpass_password_from_escrow",
        lambda _transaction: bytearray(b"a-strong-password"),
    )
    monkeypatch.setitem(globals_, "_root_token_from_escrow", lambda: "r" * 16)
    monkeypatch.setitem(globals_, "openbao_request", request)
    monkeypatch.setitem(globals_, "_revoke_token", lambda token: revoked.append(token))
    monkeypatch.setitem(globals_, "_validate_encrypted_file", lambda *_args, **_kwargs: None)
    monkeypatch.setitem(globals_, "_fsync_directory", lambda _path: None)
    monkeypatch.setitem(globals_, "_set_transaction", lambda target, **changes: target.update(changes))
    finalize(transaction)
    assert calls[0] == (
        "PUT",
        "/v1/auth/userpass/users/ops-user",
        {
            "password": "a-strong-password",
            "token_policies": ["token-self", "human-admin"],
            "token_ttl": "2m",
            "token_max_ttl": "2m",
        },
    )
    assert revoked == ["h" * 16]
    assert transaction["password_recovery_started"] is True
    assert transaction["password_recovery_persists"] is True
    assert transaction["userpass_finalized"] is True
    assert transaction["userpass_password_escrowed"] is False
    assert not escrow.exists()


def test_backup_and_success_records_follow_global_sync_barriers() -> None:
    worker = worker_namespace()
    capture_source = __import__("inspect").getsource(worker["capture_backup"])
    apply_source = __import__("inspect").getsource(worker["_apply_transaction"])
    bootstrap = bootstrap_namespace()
    bootstrap_source = __import__("inspect").getsource(bootstrap["apply_bootstrap"])
    stage_call = 'release = worker["stage_release"](manifest)'
    stage_sync = 'worker["_sync_rollback_filesystems"]()'
    stage_wal = '_set_transaction(transaction, phase="release-staged"'
    assert bootstrap_source.index(stage_call) < bootstrap_source.index(stage_sync)
    assert bootstrap_source.index(stage_sync) < bootstrap_source.index(stage_wal)
    assert bootstrap_source.index(stage_sync) < bootstrap_source.index(
        "_install_recovery_guard(release, worker, transaction)"
    )
    assert capture_source.index("os.fsync(directory_fd)") < capture_source.index(
        "atomic_json(backup / \"transaction.json\", metadata)"
    )
    assert capture_source.index("os.fsync(archive_fd)") < capture_source.index(
        'metadata["phase"] = "snapshotted"'
    )
    assert capture_source.index("_sync_rollback_filesystems()") < capture_source.index(
        'metadata["phase"] = "snapshotted"'
    )
    assert apply_source.index("health_check(manifest)") < apply_source.index(
        "_sync_rollback_filesystems()"
    ) < apply_source.index('metadata["phase"] = "healthy"')
    health_index = bootstrap_source.index("_bootstrap_health(")
    commit_sync_index = bootstrap_source.index(
        'worker["_sync_rollback_filesystems"]()', health_index
    )
    assert health_index < commit_sync_index < bootstrap_source.index(
        'metadata["phase"] = "healthy"'
    )


def test_global_sync_uses_bounded_locked_worker_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = worker_namespace()
    sync = namespace["_sync_rollback_filesystems"]
    globals_ = sync.__globals__
    calls: list[tuple[str, list[str], int]] = []
    monkeypatch.setitem(
        globals_, "run_command",
        lambda name, argv, *, timeout: calls.append((name, argv, timeout)),
    )
    sync()
    assert calls == [("sync-control-plane-filesystems", ["/usr/bin/sync"], 300)]


def test_approver_intent_and_recovery_verifier_order_are_explicit() -> None:
    bootstrap = bootstrap_namespace()
    install_source = __import__("inspect").getsource(bootstrap["_install_and_activate"])
    assert install_source.index("approver_state_created=True") < install_source.index(
        "_write_approver_state(manifest, approver_id)"
    )
    unit = (
        ROOT
        / "deploy/control-plane/systemd/ops-control-plane-bootstrap-recovery-activate.service"
    ).read_text(encoding="utf-8")
    after = next(line for line in unit.splitlines() if line.startswith("After="))
    assert "ops-control-plane-bootstrap-recovery.service" in after
    assert "ops-broker.socket" in after
    assert "zulip-local.service" in after
    assert "ops-orchestrator.service" in after
    assert "hermes-gateway.service" in after
    recovery = (
        ROOT
        / "deploy/control-plane/systemd/ops-control-plane-bootstrap-recovery.service"
    ).read_text(encoding="utf-8")
    for durable_unit in (recovery, unit):
        assert "StartLimitIntervalSec=0" in durable_unit
        assert "Restart=on-failure" in durable_unit
        assert "RestartSec=5s" in durable_unit
    queue_source = __import__("inspect").getsource(bootstrap["_queue_recovery_unit_jobs"])
    assert '"--no-block", action, unit' in queue_source


def test_public_apply_runs_inside_local_suspend_inhibitor() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert '"/usr/bin/systemd-inhibit"' in source
    assert '"--what=idle:sleep:shutdown"' in source
    assert source.index('"/usr/bin/systemd-inhibit"') < source.index(
        '"--what=idle:sleep:shutdown"'
    )


def test_fedora_install_replaces_symlink_and_hardlink_without_unsupported_flag(
    tmp_path: Path,
) -> None:
    payload = b"reviewed enrolled helper\n"
    victim = tmp_path / "victim"
    victim.write_bytes(b"must remain unchanged\n")
    symlink_destination = tmp_path / "enrolled-symlink"
    symlink_destination.symlink_to(victim)
    completed = subprocess.run(
        ["/usr/bin/install", "--mode=0700", "/dev/stdin", str(symlink_destination)],
        check=False,
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert not symlink_destination.is_symlink()
    assert symlink_destination.read_bytes() == payload
    assert victim.read_bytes() == b"must remain unchanged\n"

    hardlink_destination = tmp_path / "enrolled-hardlink"
    os.link(victim, hardlink_destination)
    victim_inode = victim.stat().st_ino
    completed = subprocess.run(
        ["/usr/bin/install", "--mode=0700", "/dev/stdin", str(hardlink_destination)],
        check=False,
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert hardlink_destination.read_bytes() == payload
    assert hardlink_destination.stat().st_ino != victim_inode
    assert victim.read_bytes() == b"must remain unchanged\n"

    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert '"--remove-destination"' not in source
    assert (
        '"/usr/bin/pkexec", "/usr/bin/install",\n'
        '                "--owner=root", "--group=root", "--mode=0700",'
    ) in source


def test_pts_identity_is_resolved_from_real_inherited_fd_without_ttyname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    resolve = namespace["_resolved_pts_index"]
    globals_ = resolve.__globals__
    master_fd, slave_fd = os.openpty()
    try:
        resolved = Path(f"/proc/self/fd/{slave_fd}").resolve(strict=True)
        monkeypatch.setattr(
            globals_["os"],
            "ttyname",
            lambda _fd: (_ for _ in ()).throw(AssertionError("ttyname alias must not be used")),
        )
        tty_index = resolve(slave_fd)
        assert resolved == Path(f"/dev/pts/{tty_index}")
    finally:
        os.close(slave_fd)
        os.close(master_fd)


def test_pts_identity_rejects_a_device_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    resolve = namespace["_resolved_pts_index"]
    globals_ = resolve.__globals__
    real_fstat = os.fstat
    master_fd, slave_fd = os.openpty()
    try:
        expected = real_fstat(slave_fd)

        def mismatched_fstat(descriptor: int) -> object:
            observed = real_fstat(descriptor)
            if descriptor != slave_fd:
                return observed
            return SimpleNamespace(
                st_mode=observed.st_mode,
                st_dev=observed.st_dev,
                st_ino=observed.st_ino,
                st_rdev=expected.st_rdev + 1,
            )

        monkeypatch.setattr(globals_["os"], "fstat", mismatched_fstat)
        with pytest.raises(namespace["BootstrapError"], match="exact local pts"):
            resolve(slave_fd)
    finally:
        os.close(slave_fd)
        os.close(master_fd)


def test_enrolled_helper_cleanup_is_exact_durable_and_rejects_hardlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    remove = namespace["_remove_enrolled_helper_exact"]
    globals_ = remove.__globals__
    payload = b"reviewed enrolled helper\n"
    digest = hashlib.sha256(payload).hexdigest()
    prefix = str(tmp_path / "ops-control-plane-bootstrap-")
    enrolled = Path(prefix + digest)
    enrolled.write_bytes(payload)
    enrolled.chmod(0o700)
    real_lstat = os.lstat
    real_fstat = os.fstat
    syncs: list[Path] = []

    def root_lstat(path: object) -> os.stat_result:
        observed = real_lstat(path)
        return _as_root_owned(observed) if Path(path) == enrolled else observed

    def root_fstat(descriptor: int) -> os.stat_result:
        return _as_root_owned(real_fstat(descriptor))

    monkeypatch.setitem(globals_, "ENROLLED_HELPER_PREFIX", prefix)
    monkeypatch.setattr(globals_["os"], "lstat", root_lstat)
    monkeypatch.setattr(globals_["os"], "fstat", root_fstat)
    monkeypatch.setitem(globals_, "_fsync_directory", lambda path: syncs.append(Path(path)))
    assert remove(digest, required=True) is True
    assert not enrolled.exists()
    assert syncs == [tmp_path]

    enrolled.write_bytes(payload)
    enrolled.chmod(0o700)
    other_link = tmp_path / "unrelated-hardlink"
    os.link(enrolled, other_link)
    with pytest.raises(namespace["BootstrapError"], match="root-owned and exact"):
        remove(digest, required=True)
    assert enrolled.exists()
    assert other_link.exists()


def test_preseal_failure_cleanup_and_stale_residue_are_narrowly_bounded() -> None:
    namespace = bootstrap_namespace()
    expected_stale = {
        "6cbb9d6ba22a30e27ac176e4e9a4bc4c1a0aae442a69db07b6ee90536366e463"
    }
    assert namespace["STALE_ENROLLED_HELPER_SHA256S"] == expected_stale
    source = __import__("inspect").getsource(namespace["public_entry"])
    elevated = source[source.index('arguments[0] == "--bootstrap-elevated"'):]
    assert elevated.index("_validate_local_session(invoker_uid)") < elevated.index(
        "_cleanup_stale_enrolled_helpers(helper_sha256)"
    )
    assert elevated.index("_cleanup_stale_enrolled_helpers(helper_sha256)") < elevated.index(
        "_seal_and_exec("
    )
    assert "finally:" in elevated
    assert "_remove_enrolled_helper_exact(helper_sha256, required=False)" in elevated
    assert "_remove_enrolled_helper_exact(helper_sha256, required=True)" in elevated


def test_recovery_guard_publishes_durable_boot_anchor_before_barriers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    install_guard = namespace["_install_recovery_guard"]
    globals_ = install_guard.__globals__
    events: list[tuple[str, object]] = []
    worker = {
        "run_command": lambda name, _argv, *, timeout: events.append(("command", name)),
    }
    monkeypatch.setitem(
        globals_, "_fsync_recovery_core",
        lambda _source, units: events.append(("core-sync", tuple(units))),
    )
    monkeypatch.setitem(
        globals_, "_fsync_recovery_anchor",
        lambda _source: events.append(("full-sync", "anchor")),
    )
    monkeypatch.setitem(
        globals_,
        "_start_standard_recovery_supervisor",
        lambda _transaction: events.append(("supervisor", "started")),
    )
    monkeypatch.setitem(
        globals_,
        "_wait_standard_recovery_supervisor",
        lambda: events.append(("supervisor", "active")),
    )
    monkeypatch.setitem(
        globals_, "_atomic_bytes",
        lambda _path, _payload, *, mode: events.append(("barrier", mode)),
    )
    monkeypatch.setitem(globals_, "_systemctl_state", lambda _unit, _operation: True)
    monkeypatch.setitem(
        globals_, "_set_transaction",
        lambda target, **changes: (target.update(changes), events.append(("wal", changes.get("phase"))))[0],
    )
    transaction: dict[str, object] = {}
    install_guard(tmp_path / "release", worker, transaction)

    core_files = events.index(("core-sync", ()))
    recovery_enable = events.index(("command", "enable-bootstrap-recovery"))
    recovery_link = events.index(
        ("core-sync", ("ops-control-plane-bootstrap-recovery.service",))
    )
    janitor_enable = events.index(("command", "enable-bootstrap-recovery-janitor"))
    janitor_link = events.index(
        (
            "core-sync",
            (
                "ops-control-plane-bootstrap-recovery.service",
                "ops-control-plane-bootstrap-recovery-activate.service",
            ),
        )
    )
    guard_publish = events.index(("command", "install-bootstrap-openbao-guard"))
    barrier_publish = next(index for index, event in enumerate(events) if event[0] == "barrier")
    full_sync = events.index(("full-sync", "anchor"))
    armed_wal = events.index(("wal", "recovery-armed"))
    supervisor_started = events.index(("supervisor", "started"))
    supervisor_active = events.index(("supervisor", "active"))
    assert (
        core_files
        < recovery_enable
        < recovery_link
        < janitor_enable
        < janitor_link
        < guard_publish
        <= barrier_publish
        < full_sync
        < supervisor_started
        < supervisor_active
        < armed_wal
    )


def test_root_staging_only_is_promoted_monotonically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    reconcile = namespace["_reconcile_root_escrow_staging"]
    globals_ = reconcile.__globals__
    final = tmp_path / "root-token.cred"
    staging = tmp_path / ".root-token.cred.staging"
    staging.write_bytes(b"ciphertext")
    document = {
        "schema_version": 1,
        "nonce": "n" * 16,
        "otp": "o" * 16,
        "encoded_token": "e" * 16,
        "progress": 2,
        "started_at": 123,
    }
    monkeypatch.setitem(globals_, "STATE_ROOT", tmp_path)
    monkeypatch.setitem(globals_, "ROOT_TOKEN_ESCROW", final)
    monkeypatch.setitem(globals_, "OPENBAO_UNDO_ESCROW", tmp_path / "openbao-undo.cred")
    monkeypatch.setitem(globals_, "USERPASS_PASSWORD_ESCROW", tmp_path / "userpass-password.cred")
    monkeypatch.setitem(globals_, "_root_escrow_document_raw", lambda _path: dict(document))
    monkeypatch.setitem(globals_, "_fsync_directory", lambda _path: None)
    reconcile()
    assert final.read_bytes() == b"ciphertext"
    assert not staging.exists()


def test_nonroot_staging_is_removed_before_mutation_or_promoted_from_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    reconcile = namespace["_reconcile_nonroot_escrow_staging"]
    globals_ = reconcile.__globals__
    root = tmp_path / "root-token.cred"
    undo = tmp_path / "openbao-undo.cred"
    userpass = tmp_path / "userpass-password.cred"
    userpass_stage = tmp_path / ".userpass-password.cred.staging"
    undo_stage = tmp_path / ".openbao-undo.cred.staging"
    userpass_stage.write_bytes(b"userpass-ciphertext")
    userpass_stage.chmod(0o400)
    transaction = {
        "release_id": "release-1",
        "password_recovery_started": False,
        "userpass_password_escrowed": False,
        "openbao_undo_write_started": False,
        "openbao_undo_escrowed": False,
        "openbao_provisioning_started": False,
    }
    userpass_document = {
        "schema_version": 1,
        "release_id": "release-1",
        "password_base64": "YS1zdHJvbmctcGFzc3dvcmQ=",
    }
    undo_document = {
        "schema_version": 1,
        "release_id": "release-1",
        "captured_at": 1,
        "roles": {name: None for name in globals_["OPENBAO_UNDO_ROLES"]},
        "policies": {name: None for name in globals_["OPENBAO_UNDO_POLICIES"]},
        "kv": {name: None for name in globals_["OPENBAO_UNDO_KV"]},
        "poststate": None,
    }
    real_lstat = os.lstat

    def root_lstat(path: object) -> os.stat_result:
        value = real_lstat(path)
        return _as_root_owned(value) if Path(path) == userpass_stage else value

    monkeypatch.setitem(globals_, "STATE_ROOT", tmp_path)
    monkeypatch.setitem(globals_, "ROOT_TOKEN_ESCROW", root)
    monkeypatch.setitem(globals_, "OPENBAO_UNDO_ESCROW", undo)
    monkeypatch.setitem(globals_, "USERPASS_PASSWORD_ESCROW", userpass)
    monkeypatch.setattr(globals_["os"], "lstat", root_lstat)
    monkeypatch.setitem(
        globals_, "_read_encrypted_document",
        lambda path, _name: userpass_document if Path(path) == userpass_stage else undo_document,
    )
    monkeypatch.setitem(globals_, "_fsync_directory", lambda _path: None)
    reconcile(transaction)
    assert not userpass_stage.exists()
    assert not userpass.exists()

    undo_stage.write_bytes(b"undo-ciphertext")
    transaction["openbao_undo_write_started"] = True
    reconcile(transaction)
    assert undo.read_bytes() == b"undo-ciphertext"
    assert not undo_stage.exists()


def test_transient_snapshot_is_rejected_before_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    create = namespace["_new_transaction"]
    globals_ = create.__globals__
    transient = globals_["RECOVERY_TRANSIENT_UNITS"][0]
    monkeypatch.setitem(
        globals_, "_systemctl_state",
        lambda unit, operation: unit == transient and operation == "is-active",
    )
    manifest = {
        "release_id": "release-1",
        "source": {"tree_sha256": "1" * 64},
        "artifacts": {
            "atlas_rpm": {"sha256": "2" * 64},
            "hermes_source_archive": {"sha256": "7" * 64},
        },
    }
    preflight = {
        "credential_groups_present": {name: False for name in globals_["CREDENTIAL_GROUPS"]}
    }
    with pytest.raises(namespace["BootstrapError"], match="non-restorable"):
        create(manifest, preflight)


def test_socket_activated_query_snapshot_is_rejected_before_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    create = namespace["_new_transaction"]
    globals_ = create.__globals__
    query_service = globals_["DYNAMIC_SERVICE_UNITS"][0]
    monkeypatch.setitem(
        globals_, "_systemctl_state",
        lambda unit, operation: unit == query_service and operation == "is-active",
    )
    document = {
        "release_id": "release-1",
        "source": {"tree_sha256": "1" * 64},
        "artifacts": {
            "atlas_rpm": {"sha256": "2" * 64},
            "hermes_source_archive": {"sha256": "7" * 64},
        },
    }
    preflight = {
        "credential_groups_present": {
            name: False for name in globals_["CREDENTIAL_GROUPS"]
        }
    }
    with pytest.raises(namespace["BootstrapError"], match="non-restorable"):
        create(document, preflight)


def test_broker_maintenance_and_installer_targets_are_transactional() -> None:
    bootstrap = bootstrap_namespace()
    worker = worker_namespace()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    maintenance = set(bootstrap["BROKER_MAINTENANCE_UNITS"])
    assert maintenance <= set(bootstrap["RECOVERY_DEPENDENT_UNITS"])
    assert maintenance <= set(worker["SERVICE_NAMES"])
    assert maintenance <= set(manifest["runtime"]["services"])
    recovery = (
        ROOT / "deploy/control-plane/systemd/ops-control-plane-bootstrap-recovery.service"
    ).read_text(encoding="utf-8")
    activation = (
        ROOT
        / "deploy/control-plane/systemd/ops-control-plane-bootstrap-recovery-activate.service"
    ).read_text(encoding="utf-8")
    assert all(unit in recovery and unit in activation for unit in maintenance)
    managed = set(worker["MANAGED_BACKUP_PATHS"])
    assert {
        Path("/etc/tmpfiles.d/ops-control-plane-deployment.conf"),
        Path("/usr/local/share/ops-control-plane/deployment"),
        Path("/usr/local/libexec/ops-orchestrator"),
    } <= managed


def test_zulip_triggers_and_writers_are_quiesced_before_rollback() -> None:
    bootstrap = bootstrap_namespace()
    worker = worker_namespace()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    triggered = set(bootstrap["ZULIP_TRIGGERED_SERVICES"])
    assert triggered <= set(bootstrap["RECOVERY_DEPENDENT_UNITS"])
    assert triggered <= set(bootstrap["RECOVERY_TRANSIENT_UNITS"])
    assert triggered <= set(worker["SERVICE_NAMES"])
    assert triggered <= set(worker["TRANSIENT_SERVICE_NAMES"])
    assert triggered <= set(manifest["runtime"]["services"])
    query_service = "zulip-alertmanager-query.service"
    assert query_service not in bootstrap["BOOTSTRAP_ACTIVE_SERVICES"]
    assert query_service in bootstrap["DYNAMIC_SERVICE_UNITS"]
    assert query_service in bootstrap["RECOVERY_PRESTATE_UNRESTORABLE_UNITS"]
    assert query_service in bootstrap["RECOVERY_DEPENDENT_UNITS"]
    assert query_service not in bootstrap["RECOVERY_TRANSIENT_UNITS"]
    assert query_service in worker["SERVICE_NAMES"]
    assert query_service in worker["DYNAMIC_SERVICE_NAMES"]
    assert query_service not in worker["TRANSIENT_SERVICE_NAMES"]
    assert manifest["runtime"]["dynamic_services"] == [query_service]
    stop_source = __import__("inspect").getsource(worker["stop_dependents"])
    assert stop_source.index('"zulip-alertmanager-query.socket"') < stop_source.index(
        '"zulip-alertmanager-query.service"'
    )
    assert stop_source.index('"zulip-local-health.timer"') < stop_source.index(
        '"zulip-local-health.service"'
    )
    assert stop_source.index('"zulip-local-backup.timer"') < stop_source.index(
        '"zulip-local-backup.service"'
    )
    rollback_source = __import__("inspect").getsource(worker["rollback_transaction"])
    assert rollback_source.index("stop_dependents()") < rollback_source.index(
        "for unit in reversed(SERVICE_NAMES)"
    )


def test_prebackup_quiesced_timer_snapshot_uses_outer_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = bootstrap_namespace()
    worker_namespace_value = worker_namespace()
    planned = bootstrap["_planned_recovery_activations"]
    globals_ = planned.__globals__
    initial = {
        unit: {
            "active": unit in {
                "ops-broker-audit-verify.timer", "ops-broker-backup.timer"
            },
            "enabled": unit in {
                "ops-broker-audit-verify.timer", "ops-broker-backup.timer"
            },
        }
        for unit in globals_["RECOVERY_DEPENDENT_UNITS"]
    }
    transaction = {
        "service_state_before": initial,
        "broker_state_before": {
            unit: dict(initial[unit])
            for unit in ("ops-broker.socket", "ops-broker.service")
        },
    }
    inner = {
        unit: dict(initial[unit]) for unit in worker_namespace_value["SERVICE_NAMES"]
    }
    for unit in globals_["PREBACKUP_QUIESCED_UNITS"]:
        if unit in inner:
            inner[unit]["active"] = False
    worker = {"SERVICE_NAMES": worker_namespace_value["SERVICE_NAMES"]}
    plan = planned(worker, transaction, {"service_state": inner})
    assert plan == ["ops-broker-audit-verify.timer", "ops-broker-backup.timer"]


def test_gui_protocol_binds_two_distinct_private_anonymous_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    bind = namespace["_gui_channel_digest"]
    globals_ = bind.__globals__

    def pipe_stat(descriptor: int) -> object:
        return SimpleNamespace(
            st_mode=stat.S_IFIFO | 0o600,
            st_uid=1000,
            st_nlink=1,
            st_dev=16,
            st_ino=100 + descriptor,
        )

    monkeypatch.setattr(globals_["os"], "fstat", pipe_stat)
    monkeypatch.setattr(globals_["os"], "isatty", lambda _fd: False)
    monkeypatch.setattr(
        globals_["fcntl"],
        "fcntl",
        lambda descriptor, _operation: os.O_RDONLY if descriptor == 0 else os.O_WRONLY,
    )
    observed = bind(1000)
    assert len(observed) == 64

    monkeypatch.setattr(
        globals_["os"],
        "fstat",
        lambda descriptor: SimpleNamespace(
            st_mode=stat.S_IFIFO | 0o600,
            st_uid=1000,
            st_nlink=1,
            st_dev=16,
            st_ino=100,
        ),
    )
    with pytest.raises(namespace["BootstrapError"], match="distinct pipes"):
        bind(1000)


def test_gui_launcher_reads_exact_sealed_memfd_and_never_reopens_source_path() -> None:
    source = BOOTSTRAP.read_bytes()
    expected = hashlib.sha256(source).hexdigest()
    harness = f"""
import fcntl
import hashlib
import os
from pathlib import Path
import runpy

source_path = Path({str(BOOTSTRAP)!r})
raw = source_path.read_bytes()
namespace = runpy.run_path(str(source_path))
descriptor = os.memfd_create("atlas-bootstrap-helper", os.MFD_ALLOW_SEALING)
os.fchmod(descriptor, 0o500)
os.write(descriptor, raw)
os.lseek(descriptor, 0, os.SEEK_SET)
fcntl.fcntl(
    descriptor,
    fcntl.F_ADD_SEALS,
    fcntl.F_SEAL_WRITE | fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL,
)
os.dup2(descriptor, 2, inheritable=True)
function = namespace["_read_gui_launcher_memfd"]
function.__globals__["__file__"] = "/proc/self/fd/2"
captured = function(os.getuid())
print(hashlib.sha256(captured).hexdigest())
"""
    completed = subprocess.run(
        ["/usr/bin/python3", "-I", "-c", harness],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 0
    assert completed.stdout.decode("ascii").strip() == expected
    assert completed.stderr == b""

    namespace = bootstrap_namespace()
    public_source = __import__("inspect").getsource(namespace["public_entry"])
    gui_source = public_source[: public_source.index('if len(arguments) == 1 and arguments[0] in {"preflight", "apply"}')]
    assert "_read_gui_launcher_memfd(invoker_uid)" in gui_source
    assert "Path(__file__).resolve" not in gui_source


def test_gui_apply_binary_frame_binds_confirmation_and_returns_wipeable_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    read_request = namespace["_read_gui_apply_request"]
    globals_ = read_request.__globals__
    release_id = "release-1"
    helper = "1" * 64
    manifest = "2" * 64
    source = "3" * 64
    hermes_archive = "4" * 64
    suffix = hashlib.sha256(
        f"{helper}:{manifest}:{source}:{hermes_archive}".encode("ascii")
    ).hexdigest()[:12]
    metadata = json.dumps(
        {
            "schema_version": 1,
            "confirmed_release_id": release_id,
            "confirmed_digest_suffix": suffix,
            "operation_mode": "fresh",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    openbao_raw = b"OpenBao-password-123"
    zulip_raw = b"Zulip-password-4567"
    queued = bytearray(
        b"ATLASBOOT1\n"
        + len(metadata).to_bytes(4, "big")
        + metadata
        + len(openbao_raw).to_bytes(4, "big")
        + openbao_raw
        + len(zulip_raw).to_bytes(4, "big")
        + zulip_raw
    )

    def read(_descriptor: int, size: int) -> bytes:
        chunk = bytes(queued[:size])
        del queued[:size]
        return chunk

    monkeypatch.setattr(globals_["select"], "select", lambda *_args: ([0], [], []))
    monkeypatch.setattr(globals_["os"], "read", read)
    openbao, zulip = read_request(
        release_id, helper, manifest, source, hermes_archive
    )
    assert bytes(openbao) == openbao_raw
    assert bytes(zulip) == zulip_raw
    globals_["_wipe"](openbao)
    globals_["_wipe"](zulip)
    assert set(openbao) == {0}
    assert set(zulip) == {0}


def test_gui_apply_reader_accepts_cancel_instead_of_waiting_for_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    read_request = namespace["_read_gui_apply_request"]
    globals_ = read_request.__globals__
    queued = bytearray(b"ATLASCTL1\n\x01")

    def read(_descriptor: int, size: int) -> bytes:
        if not queued:
            return b""
        chunk = bytes(queued[:size])
        del queued[:size]
        return chunk

    monkeypatch.setattr(globals_["select"], "select", lambda *_args: ([0], [], []))
    monkeypatch.setattr(globals_["os"], "read", read)
    with pytest.raises(namespace["BootstrapError"]) as failure:
        read_request("release-1", "1" * 64, "2" * 64, "3" * 64, "4" * 64)
    assert failure.value.code == "cancelled"
    assert queued == bytearray()


def test_gui_apply_frame_rejects_a_confirmation_for_another_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    read_request = namespace["_read_gui_apply_request"]
    globals_ = read_request.__globals__
    metadata = b'{"schema_version":1,"confirmed_release_id":"other","confirmed_digest_suffix":"aaaaaaaaaaaa","operation_mode":"fresh"}'
    queued = bytearray(
        b"ATLASBOOT1\n" + len(metadata).to_bytes(4, "big") + metadata
    )

    def read(_descriptor: int, size: int) -> bytes:
        chunk = bytes(queued[:size])
        del queued[:size]
        return chunk

    monkeypatch.setattr(globals_["select"], "select", lambda *_args: ([0], [], []))
    monkeypatch.setattr(globals_["os"], "read", read)
    with pytest.raises(namespace["BootstrapError"], match="confirmation differs"):
        read_request("release-1", "1" * 64, "2" * 64, "3" * 64, "4" * 64)


def test_gui_events_are_bounded_closed_and_never_contain_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    emit = namespace["_emit_gui_event"]
    globals_ = emit.__globals__
    writes: list[bytes] = []
    monkeypatch.setitem(globals_, "_write_all", lambda _fd, payload: writes.append(payload))
    emit("progress", "preflight", "Vérification locale", 7, "running")
    assert len(writes) == 1
    assert len(writes[0]) <= globals_["GUI_EVENT_MAX_BYTES"]
    document = json.loads(writes[0])
    assert set(document) == {
        "schema_version", "event", "phase", "message", "percent", "status",
        "mutation_started", "cancellation_allowed",
    }
    assert document["schema_version"] == 1
    assert document["mutation_started"] is False
    assert document["cancellation_allowed"] is True


def test_gui_invoker_binding_is_exact_and_revalidated_as_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    identity = {
        "pid": 4242,
        "start_time": 123456,
        "path_id": "sealed-rpm-payload-memfd",
        "device": 9,
        "inode": 10,
        "sha256": "a" * 64,
    }
    binding = namespace["_encode_gui_atlas_binding"](identity)
    assert namespace["_decode_gui_atlas_binding"](binding) == identity
    validate = namespace["_validate_gui_atlas_binding"]
    globals_ = validate.__globals__
    monkeypatch.setitem(globals_, "_gui_process_is_ancestor", lambda pid: pid == 4242)
    monkeypatch.setitem(
        globals_,
        "_current_gui_atlas_identity",
        lambda pid, uid, expected_sha256=None: (
            dict(identity)
            if expected_sha256 in {None, identity["sha256"]}
            else (_ for _ in ()).throw(
                namespace["BootstrapError"](
                    "gui_invoker_invalid", "The reviewed Atlas digest differs."
                )
            )
        ),
    )
    validate(1000, binding)
    validate(1000, binding, "a" * 64)
    with pytest.raises(namespace["BootstrapError"], match="digest differs"):
        validate(1000, binding, "b" * 64)
    monkeypatch.setitem(globals_, "_gui_process_is_ancestor", lambda _pid: False)
    with pytest.raises(namespace["BootstrapError"], match="not an ancestor"):
        validate(1000, binding)


def test_gui_invoker_accepts_only_reviewed_sealed_rpm_payload_memfd() -> None:
    namespace = bootstrap_namespace()
    identify = namespace["_current_gui_atlas_identity"]
    executable = Path("/usr/bin/sleep").read_bytes()
    descriptor = os.memfd_create("atlas-reviewed-rpm", os.MFD_ALLOW_SEALING)
    process: subprocess.Popen[bytes] | None = None
    try:
        os.fchmod(descriptor, 0o500)
        view = memoryview(executable)
        while view:
            written = os.write(descriptor, view)
            assert written > 0
            view = view[written:]
        os.lseek(descriptor, 0, os.SEEK_SET)
        required_seals = (
            namespace["fcntl"].F_SEAL_WRITE
            | namespace["fcntl"].F_SEAL_GROW
            | namespace["fcntl"].F_SEAL_SHRINK
            | namespace["fcntl"].F_SEAL_SEAL
        )
        namespace["fcntl"].fcntl(descriptor, namespace["fcntl"].F_ADD_SEALS, required_seals)
        process = subprocess.Popen(
            [f"/proc/self/fd/{descriptor}", "30"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(descriptor,),
        )
        expected = hashlib.sha256(executable).hexdigest()
        identity = identify(process.pid, os.getuid(), expected)
        assert identity["path_id"] == "sealed-rpm-payload-memfd"
        assert identity["sha256"] == expected
        with pytest.raises(namespace["BootstrapError"], match="identity is unsafe"):
            identify(process.pid, os.getuid(), "0" * 64)
    finally:
        if process is not None:
            process.terminate()
            process.wait(timeout=10)
        os.close(descriptor)


def test_gui_binding_rejects_legacy_repo_build_path_ids() -> None:
    namespace = bootstrap_namespace()
    identity = {
        "pid": 4242,
        "start_time": 123456,
        "path_id": "debug",
        "device": 9,
        "inode": 10,
        "sha256": "a" * 64,
    }
    binding = namespace["_encode_gui_atlas_binding"](identity)
    with pytest.raises(namespace["BootstrapError"], match="fields differ"):
        namespace["_decode_gui_atlas_binding"](binding)


def test_gui_mutation_boundary_is_after_repeated_preflight_and_before_wal() -> None:
    namespace = bootstrap_namespace()
    source = __import__("inspect").getsource(namespace["apply_bootstrap"])
    repeated = source.index("repeated = bootstrap_preflight(")
    boundary = source.index("mutation_boundary()")
    consume = source.index("consume_exception(")
    repair_audit = source.index(
        "_ensure_bootstrap_consumed_audit_exact_once(resume_transaction)"
    )
    assert repeated < boundary < consume
    assert boundary < repair_audit
    public = __import__("inspect").getsource(namespace["public_entry"])
    assert '"gui-preflight", "gui-apply"' in public
    assert '"--bootstrap-elevated-gui"' in public
    assert '"--bootstrap-sealed-gui"' in public
    assert "_read_gui_apply_request(" in public
    assert "_confirmed_secrets()" in public  # retained only for the terminal compatibility mode


def test_manifest_pins_the_closed_gui_protocol() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    protocol = manifest["bootstrap"]["gui_protocol"]
    payload_sha256 = protocol.pop("atlas_payload_sha256")
    assert protocol == {
        "schema_version": 1,
        "public_operations": [
            "gui-preflight", "gui-apply", "gui-corrective-preflight",
            "gui-corrective-apply", "gui-recovery-preflight",
            "gui-recovery-apply",
        ],
        "apply_magic": "ATLASBOOT1",
        "control_magic": "ATLASCTL1",
        "stdout": "bounded-jsonl",
        "atlas_ancestry_required": True,
        "anonymous_pipes_required": True,
        "mutation_boundary_event": "mutation-started",
        "secrets_outside_process_memory": False,
        "launcher_mode": "sealed-rpm-payload-memfd",
        "atlas_payload_member": "/usr/bin/ops-model-manager",
    }
    assert re.fullmatch(r"[0-9a-f]{64}", payload_sha256)
    assert payload_sha256 == manifest["artifacts"]["atlas_rpm"][
        "payload_executable_sha256"
    ]
    assert manifest["artifacts"]["atlas_rpm"]["payload_executable_path"] \
        == protocol["atlas_payload_member"]
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["bootstrap"]["helper_sha256"])


def test_reviewed_release_binds_the_sealed_helper_to_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    load = namespace["load_reviewed_release"]
    globals_ = load.__globals__
    expected = "a" * 64
    manifest = {
        "bootstrap": {"helper_sha256": "b" * 64},
        "source": {"tree_sha256": "c" * 64},
        "artifacts": {"atlas_rpm": {"source_path": "/unused", "sha256": "d" * 64}},
    }
    monkeypatch.setitem(
        globals_, "_independent_regular_sha256", lambda *_args, **_kwargs: expected
    )
    monkeypatch.setitem(globals_, "_reviewed_manifest", lambda: (manifest, "e" * 64))
    monkeypatch.setitem(
        globals_,
        "_independent_source_digest",
        lambda *_args, **_kwargs: ("c" * 64, b"worker"),
    )
    with pytest.raises(namespace["BootstrapError"], match="helper digest differs"):
        load(expected)


def test_reviewed_manifest_uses_only_the_versioned_root_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    review = namespace["_reviewed_manifest"]
    globals_ = review.__globals__
    anchor = Path("/anchor/atlas-api-zulip-2026.09.08.11")
    manifest_path = anchor / "release-manifest.v1.json"
    helper_path = anchor / "control-plane-bootstrap"
    launcher_path = anchor / "launch-atlas-reviewed-rpm"
    helper = b"reviewed helper\n"
    launcher = b"reviewed launcher\n"
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["bootstrap"]["helper_sha256"] = hashlib.sha256(helper).hexdigest()
    document["trust_anchor"] = {
        "version_directory": str(anchor),
        "manifest_path": str(manifest_path),
        "bootstrap_helper_path": str(helper_path),
        "launcher_path": str(launcher_path),
        "manifest_mode": "0444",
        "bootstrap_helper_mode": "0555",
        "launcher_mode": "0555",
        "launcher_sha256": hashlib.sha256(launcher).hexdigest(),
        "enrollment": "physical-polkit-tofu-v1",
    }
    raw_manifest = json.dumps(document, separators=(",", ":")).encode("utf-8")
    checkout_manifest = tmp_path / "checkout-manifest.json"
    checkout_manifest.write_text('{"attacker":"replacement"}', encoding="utf-8")
    reads: list[tuple[Path, int]] = []
    payloads = {
        manifest_path: raw_manifest,
        helper_path: helper,
        launcher_path: launcher,
    }

    def read_root(path: Path, _maximum: int, mode: int, **_kwargs: object) -> bytes:
        normalized = Path(path)
        reads.append((normalized, mode))
        if normalized == checkout_manifest:
            raise AssertionError("mutable checkout manifest was read")
        return payloads[normalized]

    monkeypatch.setitem(globals_, "TRUST_ANCHOR_ROOT", anchor)
    monkeypatch.setitem(globals_, "SOURCE_MANIFEST", manifest_path)
    monkeypatch.setitem(globals_, "TRUST_ANCHOR_BOOTSTRAP_HELPER", helper_path)
    monkeypatch.setitem(globals_, "TRUST_ANCHOR_LAUNCHER", launcher_path)
    monkeypatch.setitem(
        globals_, "_validate_root_directory", lambda *_args, **_kwargs: None
    )
    monkeypatch.setitem(globals_, "_read_exact_root_file", read_root)
    monkeypatch.setitem(
        globals_,
        "_read_bounded_regular",
        lambda path, *_args: (
            (_ for _ in ()).throw(AssertionError(f"mutable read: {path}"))
        ),
    )

    observed, digest = review()
    assert observed == document
    assert digest == hashlib.sha256(raw_manifest).hexdigest()
    assert reads == [
        (manifest_path, 0o444),
        (helper_path, 0o555),
        (launcher_path, 0o555),
    ]
    load_source = __import__("inspect").getsource(namespace["load_reviewed_release"])
    assert "source_helper = TRUST_ANCHOR_BOOTSTRAP_HELPER" in load_source
    assert str(MANIFEST) not in load_source


def test_atlas_rpm_preflight_binds_the_exact_executable_payload() -> None:
    namespace = bootstrap_namespace()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    atlas = manifest["artifacts"]["atlas_rpm"]
    protocol = manifest["bootstrap"]["gui_protocol"]
    namespace["_validate_atlas_memfd"](atlas, protocol)

    changed = dict(protocol, atlas_payload_sha256="0" * 64)
    with pytest.raises(namespace["BootstrapError"], match="payload differs"):
        namespace["_validate_atlas_memfd"](atlas, changed)


def test_gui_cancel_frame_is_exact_and_requires_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    cancel_pending = namespace["_gui_cancel_is_pending"]
    globals_ = cancel_pending.__globals__
    queued = bytearray(b"ATLASCTL1\n\x01")

    def read(_descriptor: int, size: int) -> bytes:
        if not queued:
            return b""
        chunk = bytes(queued[:size])
        del queued[:size]
        return chunk

    monkeypatch.setattr(globals_["select"], "select", lambda *_args: ([0], [], []))
    monkeypatch.setattr(globals_["os"], "read", read)
    assert cancel_pending() is True
    assert queued == bytearray()


def test_gui_cancel_probe_is_nonblocking_when_no_command_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    cancel_pending = namespace["_gui_cancel_is_pending"]
    globals_ = cancel_pending.__globals__
    monkeypatch.setattr(globals_["select"], "select", lambda *_args: ([], [], []))
    monkeypatch.setattr(
        globals_["os"], "read", lambda *_args: pytest.fail("stdin must not block")
    )
    assert cancel_pending() is False


def test_gui_mutation_decision_accepts_only_exact_ack_or_cancel_with_eof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    decision = namespace["_read_gui_mutation_commit_decision"]
    globals_ = decision.__globals__

    def run_frame(frame: bytes) -> None:
        queued = bytearray(frame)

        def read(_descriptor: int, size: int) -> bytes:
            if not queued:
                return b""
            chunk = bytes(queued[:size])
            del queued[:size]
            return chunk

        monkeypatch.setattr(globals_["select"], "select", lambda *_args: ([0], [], []))
        monkeypatch.setattr(globals_["os"], "read", read)
        decision()
        assert queued == bytearray()

    run_frame(b"ATLASCTL1\n\x02")
    with pytest.raises(namespace["BootstrapError"]) as cancelled:
        run_frame(b"ATLASCTL1\n\x01")
    assert cancelled.value.code == "cancelled"
    with pytest.raises(namespace["BootstrapError"]) as malformed:
        run_frame(b"ATLASCTL1\n\x03")
    assert malformed.value.code == "gui_input_invalid"


def test_gui_cancel_wins_commit_boundary_without_starting_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    boundary = namespace["_enter_gui_mutation_boundary"]
    globals_ = boundary.__globals__
    emitted: list[tuple[str, str]] = []
    monkeypatch.setitem(globals_, "GUI_MUTATION_STARTED", False)
    monkeypatch.setitem(
        globals_,
        "GUI_RESULT_IDENTITY",
        {
            "release_id": "release-1",
            "bootstrap_helper_sha256": "1" * 64,
            "manifest_sha256": "2" * 64,
            "source_tree_sha256": "3" * 64,
            "atlas_rpm_sha256": "4" * 64,
            "hermes_source_archive_sha256": "5" * 64,
            "confirmation_digest_suffix": "0123456789ab",
        },
    )
    monkeypatch.setitem(
        globals_, "_validate_local_gui_session", lambda *_args, **_kwargs: "7"
    )
    monkeypatch.setitem(globals_, "_gui_cancel_is_pending", lambda: False)
    monkeypatch.setitem(
        globals_,
        "_emit_gui_event",
        lambda _event, phase, _message, _percent, status, **_details: emitted.append(
            (phase, status)
        ),
    )
    error = namespace["BootstrapError"]("cancelled", "cancel won")
    monkeypatch.setitem(
        globals_, "_read_gui_mutation_commit_decision", lambda: (_ for _ in ()).throw(error)
    )
    with pytest.raises(namespace["BootstrapError"]) as cancelled:
        boundary(1000, "binding", "1" * 64, "7", "5" * 64)
    assert cancelled.value.code == "cancelled"
    assert globals_["GUI_MUTATION_STARTED"] is False
    assert emitted == [("mutation-commit-ready", "ready-to-commit")]


def test_gui_release_event_contains_only_the_native_closed_identity_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    emit_release = namespace["_emit_gui_release"]
    globals_ = emit_release.__globals__
    writes: list[bytes] = []
    monkeypatch.setitem(globals_, "_write_all", lambda _fd, payload: writes.append(payload))
    preflight = {
        "release_id": "release-1",
        "bootstrap_helper_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "source_tree_sha256": "3" * 64,
        "atlas_rpm_sha256": "4" * 64,
        "hermes_source_archive_sha256": "7" * 64,
        "operation_mode": "fresh",
        # These internal fields must never cross the closed native protocol.
        "credential_groups_present": {"deepseek": False},
        "catalogue_sha256": "5" * 64,
        "provider_integrations_sha256": "6" * 64,
    }
    emit_release(preflight)
    document = json.loads(writes[0])
    assert set(document) == {
        "schema_version", "event", "phase", "message", "percent", "status",
        "mutation_started", "cancellation_allowed", "release_id",
        "bootstrap_helper_sha256", "manifest_sha256", "source_tree_sha256",
        "atlas_rpm_sha256", "hermes_source_archive_sha256",
        "confirmation_digest_suffix", "operation_mode",
        "rollback_finalize_resume",
    }
    assert document["operation_mode"] == "fresh"
    encoded = writes[0].decode("utf-8")
    assert "credential_groups_present" not in encoded
    assert "catalogue_sha256" not in encoded
    assert "provider_integrations_sha256" not in encoded


def test_gui_secret_readiness_is_a_root_only_full_identity_attestation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    emit_release = namespace["_emit_gui_release"]
    emit_readiness = namespace["_emit_gui_secret_readiness"]
    globals_ = emit_release.__globals__
    writes: list[bytes] = []
    monkeypatch.setitem(globals_, "_write_all", lambda _fd, payload: writes.append(payload))
    preflight = {
        "release_id": "release-1",
        "bootstrap_helper_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "source_tree_sha256": "3" * 64,
        "atlas_rpm_sha256": "4" * 64,
        "hermes_source_archive_sha256": "7" * 64,
        "operation_mode": "fresh",
    }
    emit_release(preflight)
    emit_readiness(preflight)
    readiness = json.loads(writes[1])
    assert readiness == {
        "schema_version": 1,
        "event": "progress",
        "phase": "secret-channel-ready",
        "message": "Canal racine scellé et release entièrement vérifiés.",
        "percent": 11,
        "status": "ready-for-secrets",
        "mutation_started": False,
        "cancellation_allowed": True,
        "release_id": "release-1",
        "bootstrap_helper_sha256": "1" * 64,
        "manifest_sha256": "2" * 64,
        "source_tree_sha256": "3" * 64,
        "atlas_rpm_sha256": "4" * 64,
        "hermes_source_archive_sha256": "7" * 64,
        "confirmation_digest_suffix": hashlib.sha256(
            f"{'1' * 64}:{'2' * 64}:{'3' * 64}:{'7' * 64}".encode("ascii")
        ).hexdigest()[:12],
        "operation_mode": "fresh",
        "rollback_finalize_resume": False,
    }


def _corrective_marker_fixture(
    namespace: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    new_transaction = namespace["_new_transaction"]
    globals_ = new_transaction.__globals__
    monkeypatch.setitem(globals_, "CORRECTIVE_HISTORY_ROOT", tmp_path / "history")
    monkeypatch.setitem(globals_, "_systemctl_state", lambda _unit, _operation: False)
    manifest = {
        "release_id": globals_["CORRECTIVE_TARGET_RELEASE_ID"],
        "source": {"tree_sha256": "a" * 64},
        "artifacts": {
            "atlas_rpm": {"sha256": "b" * 64},
            "hermes_source_archive": {"sha256": "7" * 64},
        },
    }
    preflight = {
        "credential_groups_present": {
            name: False for name in globals_["CREDENTIAL_GROUPS"]
        }
    }
    successor = new_transaction(manifest, preflight)
    predecessor = {
        "marker_sha256": "e" * 64,
        "transaction_sha256": "f" * 64,
        "audit_sha256": "1" * 64,
        "audit_tail_sha256": "2" * 64,
    }
    uid = globals_["pwd"].getpwnam("ops-user").pw_uid
    marker = namespace["_new_corrective_marker"](
        manifest,
        "3",
        uid,
        "c" * 64,
        "d" * 64,
        predecessor,
        successor,
    )
    return marker, manifest, globals_


def test_corrective_authority_requires_the_exact_single_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    validate = namespace["_validate_corrective_authority"]
    globals_ = validate.__globals__
    authority = globals_["CORRECTIVE_AUTHORITY_BLOCK"]
    (tmp_path / "AGENTS.md").write_bytes(b"prefix\n" + authority + b"suffix\n")
    monkeypatch.setitem(globals_, "EXPECTED_SOURCE_ROOT", tmp_path)
    validate()
    (tmp_path / "AGENTS.md").write_bytes(authority + authority)
    with pytest.raises(namespace["BootstrapError"], match="authority"):
        validate()


def test_corrective_marker_wal_is_closed_linked_and_secret_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    marker, _manifest, globals_ = _corrective_marker_fixture(
        namespace, monkeypatch, tmp_path
    )
    namespace["_validate_corrective_marker_document"](marker)
    expected_suffix = hashlib.sha256(
        ":".join(
            (
                "corrective-v1",
                globals_["CORRECTIVE_PREDECESSOR"]["release_id"],
                globals_["CORRECTIVE_PREDECESSOR"]["source_tree_sha256"],
                globals_["CORRECTIVE_PREDECESSOR"]["atlas_rpm_sha256"],
                globals_["CORRECTIVE_PREDECESSOR"]["bootstrap_helper_sha256"],
                globals_["CORRECTIVE_PREDECESSOR"]["manifest_sha256"],
                globals_["CORRECTIVE_TARGET_RELEASE_ID"],
                "c" * 64,
                "d" * 64,
                "a" * 64,
                "7" * 64,
            )
        ).encode("ascii")
    ).hexdigest()[:12]
    assert marker["corrective_id"] == f"corrective-{expected_suffix}"
    assert marker["operation_mode"] == "corrective"
    assert marker["recovery_from_release_id"] == globals_["CORRECTIVE_PREDECESSOR"]["release_id"]
    encoded = json.dumps(marker, sort_keys=True).lower()
    assert "openbao-password-123" not in encoded
    assert "zulip-password-4567" not in encoded
    changed = json.loads(json.dumps(marker))
    changed["successor_consumed"]["release_id"] = "other-release"
    with pytest.raises(namespace["BootstrapError"], match="successor"):
        namespace["_validate_corrective_marker_document"](changed)


def test_corrective_scaffold_units_are_marker_gated_and_hardened() -> None:
    namespace = bootstrap_namespace()
    service = namespace["CORRECTIVE_RECOVERY_UNIT_BYTES"].decode("ascii")
    watcher = namespace["CORRECTIVE_RECOVERY_PATH_BYTES"].decode("ascii")
    marker = str(namespace["CORRECTIVE_CONSUMED_PATH"])
    assert f"ConditionPathExists={marker}" in service
    assert "RemainAfterExit=yes" in service
    assert "StartLimitIntervalSec=0" in service
    assert "StartLimitBurst" not in service
    assert "Restart=on-failure" in service
    assert "NoNewPrivileges=yes" in service
    assert "ProtectSystem=strict" in service
    assert "PrivateNetwork=yes" in service
    assert "RestrictSUIDSGID=yes" in service
    assert f"PathExists={marker}" in watcher
    assert f"Unit={namespace['CORRECTIVE_RECOVERY_UNIT_NAME']}" in watcher
    assert "ConditionPathExists" not in watcher


def test_corrective_consumption_validates_scaffold_before_atomic_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    marker, _manifest, globals_ = _corrective_marker_fixture(
        namespace, monkeypatch, tmp_path
    )
    events: list[str] = []
    monkeypatch.setitem(
        globals_,
        "_validate_corrective_scaffold",
        lambda *_args, **_kwargs: events.append("scaffold"),
    )
    monkeypatch.setitem(
        globals_,
        "_exclusive_atomic_bytes",
        lambda *_args, **_kwargs: events.append("marker"),
    )
    monkeypatch.setitem(
        globals_,
        "_activate_corrective_recovery_service",
        lambda *_args, **_kwargs: events.append("recovery-active"),
    )
    monkeypatch.setitem(
        globals_,
        "_reconcile_corrective_handoff",
        lambda document: events.append("reconcile") or document["successor_consumed"]["initial_transaction"],
    )
    namespace["_consume_corrective_exception"](marker, "c" * 64)
    assert events == ["scaffold", "marker", "recovery-active", "reconcile"]


def test_corrective_scaffold_rearms_watcher_before_resume_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    install = namespace["_install_corrective_scaffold"]
    globals_ = install.__globals__
    helper = b"#!/usr/bin/python3 -I\n"
    helper_sha256 = hashlib.sha256(helper).hexdigest()
    marker_path = tmp_path / "corrective-consumed.json"
    monkeypatch.setitem(globals_, "CORRECTIVE_CONSUMED_PATH", marker_path)
    commands: list[tuple[str, ...]] = []
    events: list[tuple[str, object]] = []

    monkeypatch.setitem(globals_, "_validate_root_directory", lambda *_args: None)
    monkeypatch.setitem(
        globals_, "_publish_corrective_scaffold_file", lambda *_args: None
    )
    monkeypatch.setitem(
        globals_, "_publish_corrective_scaffold_link", lambda *_args: None
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        recorded = tuple(command)
        commands.append(recorded)
        events.append(("command", recorded))
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(globals_["subprocess"], "run", run)
    monkeypatch.setitem(
        globals_,
        "_validate_corrective_scaffold",
        lambda digest, *, require_active_path: events.append(
            ("validated", (digest, require_active_path))
        ),
    )
    monkeypatch.setitem(
        globals_,
        "_wait_corrective_recovery_job",
        lambda: events.append(("recovery-job", "active")),
    )

    install(helper, helper_sha256)

    service = globals_["CORRECTIVE_RECOVERY_UNIT_NAME"]
    watcher = globals_["CORRECTIVE_RECOVERY_PATH_NAME"]
    assert ("/usr/bin/systemctl", "reset-failed", service, watcher) in commands
    assert ("/usr/bin/systemctl", "restart", watcher) in commands
    assert events.index(("command", ("/usr/bin/systemctl", "restart", watcher))) \
        < events.index(("validated", (helper_sha256, True)))

    marker_path.write_text("consumed\n", encoding="ascii")
    commands.clear()
    events.clear()
    install(helper, helper_sha256)
    assert (
        "/usr/bin/systemctl", "restart", "--no-block", service
    ) in commands
    assert events.index(
        ("command", ("/usr/bin/systemctl", "restart", "--no-block", service))
    ) < events.index(("recovery-job", "active")) < events.index(
        ("validated", (helper_sha256, True))
    )

    source = __import__("inspect").getsource(namespace["apply_bootstrap"])
    resume_branch = source[source.index("        else:\n            marker = second_corrective_context") :]
    assert resume_branch.index("_install_corrective_scaffold(") < resume_branch.index(
        "_reconcile_corrective_handoff(marker)"
    )


def test_corrective_empty_wal_root_is_a_replayable_crash_frontier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    marker, _manifest, globals_ = _corrective_marker_fixture(
        namespace, monkeypatch, tmp_path
    )
    state_root = tmp_path / "corrective-state"
    state_root.mkdir(mode=0o700)
    transaction_path = state_root / "transaction.json"
    monkeypatch.setitem(globals_, "CORRECTIVE_STATE_ROOT", state_root)
    monkeypatch.setitem(globals_, "CORRECTIVE_TRANSACTION_PATH", transaction_path)
    real_lstat = os.lstat
    real_fstat = os.fstat
    monkeypatch.setattr(
        globals_["os"], "lstat", lambda path: _as_root_owned(real_lstat(path))
    )
    monkeypatch.setattr(
        globals_["os"], "fstat", lambda fd: _as_root_owned(real_fstat(fd))
    )
    assert namespace["_read_corrective_transaction_readonly"](marker) == marker["initial_transaction"]
    (state_root / "foreign").write_text("no", encoding="utf-8")
    with pytest.raises(namespace["BootstrapError"], match="foreign"):
        namespace["_read_corrective_transaction_readonly"](marker)


def test_corrective_wal_unnamed_and_fixed_staging_crash_frontiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    marker, _manifest, globals_ = _corrective_marker_fixture(
        namespace, monkeypatch, tmp_path
    )
    state_root = tmp_path / "corrective-wal"
    state_root.mkdir(mode=0o700)
    transaction_path = state_root / "transaction.json"
    staging_path = state_root / ".transaction.json.next"
    monkeypatch.setitem(globals_, "CORRECTIVE_STATE_ROOT", state_root)
    monkeypatch.setitem(globals_, "CORRECTIVE_TRANSACTION_PATH", transaction_path)
    monkeypatch.setitem(
        globals_, "CORRECTIVE_TRANSACTION_STAGING_PATH", staging_path
    )
    monkeypatch.setitem(globals_, "_validate_root_directory", lambda *_args: None)
    monkeypatch.setitem(
        globals_,
        "_read_exact_root_file",
        lambda path, *_args, **_kwargs: Path(path).read_bytes(),
    )
    monkeypatch.setattr(globals_["os"], "fchown", lambda *_args: None)
    initial = dict(marker["initial_transaction"])
    transaction_path.write_bytes(globals_["_canonical"](initial) + b"\n")
    transaction_path.chmod(0o600)
    first = dict(initial)
    first.update(
        predecessor_marker_archived=True,
        phase="predecessor-marker-archived",
    )
    first_raw = globals_["_canonical"](first) + b"\n"
    replace_wal = namespace["_replace_durable_wal"]
    real_write = os.write
    real_replace = os.replace

    def fail_unnamed_write(_descriptor: int, _payload: object) -> int:
        raise OSError("kill-before-link")

    monkeypatch.setattr(globals_["os"], "write", fail_unnamed_write)
    with pytest.raises(namespace["BootstrapError"], match="replacement failed"):
        replace_wal(transaction_path, staging_path, first_raw)
    assert {entry.name for entry in state_root.iterdir()} == {"transaction.json"}
    assert transaction_path.read_bytes() == globals_["_canonical"](initial) + b"\n"
    monkeypatch.setattr(globals_["os"], "write", real_write)

    def fail_after_staging(source: object, destination: object) -> None:
        if Path(source) == staging_path and Path(destination) == transaction_path:
            raise OSError("kill-after-staging")
        real_replace(source, destination)

    monkeypatch.setattr(globals_["os"], "replace", fail_after_staging)
    with pytest.raises(namespace["BootstrapError"], match="replacement failed"):
        replace_wal(transaction_path, staging_path, first_raw)
    assert {entry.name for entry in state_root.iterdir()} == {
        "transaction.json", ".transaction.json.next"
    }
    assert staging_path.read_bytes() == first_raw
    assert namespace["_read_corrective_transaction_readonly"](marker) == first

    monkeypatch.setattr(globals_["os"], "replace", real_replace)
    assert namespace["_materialize_corrective_state"](marker) == first
    assert {entry.name for entry in state_root.iterdir()} == {"transaction.json"}

    second = dict(first)
    second.update(
        predecessor_state_archived=True,
        phase="predecessor-state-archived",
    )
    second_raw = globals_["_canonical"](second) + b"\n"
    real_fsync = os.fsync
    syncs = 0

    def fail_after_replace(descriptor: int) -> None:
        nonlocal syncs
        syncs += 1
        if syncs == 3:
            raise OSError("kill-after-replace")
        real_fsync(descriptor)

    monkeypatch.setattr(globals_["os"], "fsync", fail_after_replace)
    with pytest.raises(namespace["BootstrapError"], match="replacement failed"):
        replace_wal(transaction_path, staging_path, second_raw)
    monkeypatch.setattr(globals_["os"], "fsync", real_fsync)
    assert not staging_path.exists()
    assert namespace["_read_corrective_transaction_readonly"](marker) == second


def test_standard_wal_hash_bound_staging_retries_enrollment_resume_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    transaction = new_transaction(namespace, monkeypatch)
    globals_ = namespace["_set_transaction"].__globals__
    state_root = tmp_path / "standard-wal"
    state_root.mkdir(mode=0o700)
    transaction_path = state_root / "transaction.json"
    monkeypatch.setitem(globals_, "STATE_ROOT", state_root)
    monkeypatch.setitem(globals_, "TRANSACTION_PATH", transaction_path)
    monkeypatch.setitem(globals_, "_validate_root_directory", lambda *_args: None)
    monkeypatch.setitem(
        globals_,
        "_read_exact_root_file",
        lambda path, *_args, **_kwargs: Path(path).read_bytes(),
    )
    monkeypatch.setattr(globals_["os"], "fchown", lambda *_args: None)
    transaction_path.write_bytes(globals_["_canonical"](transaction) + b"\n")
    transaction_path.chmod(0o600)
    initial = dict(transaction)
    real_replace = os.replace

    def fail_after_staging(source: object, destination: object) -> None:
        if Path(destination) == transaction_path:
            raise OSError("kill-at-enrollment-resuming")
        real_replace(source, destination)

    monkeypatch.setattr(globals_["os"], "replace", fail_after_staging)
    with pytest.raises(namespace["BootstrapError"], match="replacement failed"):
        namespace["_set_transaction"](
            transaction, phase="enrollment-resuming"
        )
    assert transaction == initial
    candidates = namespace["_standard_transaction_staging_candidates"]()
    assert len(candidates) == 1
    assert re.fullmatch(
        r"\.transaction\.json\.next-[0-9a-f]{64}-[0-9a-f]{64}",
        candidates[0].name,
    )
    staged = namespace["_read_standard_transaction_frontier"](
        promote_staging=False
    )
    assert staged is not None and staged["phase"] == "enrollment-resuming"

    monkeypatch.setattr(globals_["os"], "replace", real_replace)
    namespace["_set_transaction"](staged, phase="enrollment-resuming")
    assert namespace["_standard_transaction_staging_candidates"]() == []
    assert namespace["_read_standard_transaction_frontier"](
        promote_staging=False
    ) == staged
    review_source = __import__("inspect").getsource(
        namespace["_review_enrollment_resume"]
    )
    assert "_read_standard_transaction_frontier(promote_staging=False)" in review_source


def test_corrective_archive_never_accepts_duplicate_marker_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    archive = namespace["_archive_predecessor_file"]
    globals_ = archive.__globals__
    source = tmp_path / "live.json"
    destination = tmp_path / "archive.json"
    payload = b'{"old":true}\n'
    source.write_bytes(payload)
    destination.write_bytes(payload)
    source.chmod(0o600)
    destination.chmod(0o600)
    monkeypatch.setitem(
        globals_, "_read_exact_root_file", lambda path, *_args: Path(path).read_bytes()
    )
    with pytest.raises(namespace["BootstrapError"], match="Duplicate"):
        archive(
            source,
            destination,
            hashlib.sha256(payload).hexdigest(),
            b'{"new":true}\n',
        )
    assert source.read_bytes() == payload
    assert destination.read_bytes() == payload


def test_corrective_terminal_history_inventory_rejects_sibling_and_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    validate = namespace["_validate_completed_corrective_history_inventory"]
    globals_ = validate.__globals__
    history_root = tmp_path / "history"
    attempt = history_root / "corrective-0123456789ab"
    (attempt / "state").mkdir(parents=True)
    (attempt / "consumed.json").write_text("{}\n", encoding="ascii")
    (attempt / "archive.json").write_text("{}\n", encoding="ascii")
    monkeypatch.setitem(globals_, "CORRECTIVE_HISTORY_ROOT", history_root)
    monkeypatch.setitem(globals_, "_validate_root_directory", lambda *_args: None)
    validate(attempt)

    sibling = history_root / "foreign-attempt"
    sibling.mkdir()
    with pytest.raises(namespace["BootstrapError"], match="history inventory"):
        validate(attempt)
    sibling.rmdir()
    (attempt / "foreign").write_text("evidence", encoding="ascii")
    with pytest.raises(namespace["BootstrapError"], match="archive inventory"):
        validate(attempt)


def test_create_once_approver_and_proof_have_no_named_temp_frontier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    publish = namespace["_publish_exact_once"]
    globals_ = publish.__globals__
    target = tmp_path / "proof.json"
    payload = b'{"proof":true}\n'
    monkeypatch.setitem(
        globals_,
        "_read_exact_root_file",
        lambda path, *_args, **_kwargs: Path(path).read_bytes(),
    )
    real_lstat = os.lstat
    real_write = os.write

    def root_lstat(path: object) -> os.stat_result:
        observed = real_lstat(path)
        return _as_root_owned(observed) if Path(path) == tmp_path else observed

    monkeypatch.setattr(globals_["os"], "lstat", root_lstat)
    monkeypatch.setattr(globals_["os"], "fchown", lambda *_args: None)

    def interrupted(_descriptor: int, _data: object) -> int:
        raise OSError("kill-before-create-once-link")

    monkeypatch.setattr(globals_["os"], "write", interrupted)
    with pytest.raises(OSError, match="kill-before-create-once-link"):
        publish(target, payload, 0o600)
    assert list(tmp_path.iterdir()) == []

    monkeypatch.setattr(globals_["os"], "write", real_write)
    publish(target, payload, 0o600)
    publish(target, payload, 0o600)
    assert {entry.name for entry in tmp_path.iterdir()} == {"proof.json"}
    with pytest.raises(namespace["BootstrapError"], match="conflicts"):
        publish(target, b'{"proof":false}\n', 0o600)

    approver_source = __import__("inspect").getsource(
        namespace["_write_approver_state"]
    )
    cleanup_source = __import__("inspect").getsource(
        namespace["_finish_committed_cleanup"]
    )
    assert "_publish_exact_once(" in approver_source
    assert "_atomic_json(" not in approver_source
    assert "_publish_exact_once(" in cleanup_source
    assert "_atomic_json(PROOF_PATH" not in cleanup_source


def test_corrective_terminal_accepts_only_valid_named_escrow_frontiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    validate = namespace["_validate_escrow_staging_frontier_readonly"]
    globals_ = validate.__globals__
    state_root = tmp_path / "state"
    state_root.mkdir()
    root = state_root / "root-token.cred"
    undo = state_root / "openbao-undo.cred"
    userpass = state_root / "userpass-password.cred"
    monkeypatch.setitem(globals_, "STATE_ROOT", state_root)
    monkeypatch.setitem(globals_, "ROOT_TOKEN_ESCROW", root)
    monkeypatch.setitem(globals_, "OPENBAO_UNDO_ESCROW", undo)
    monkeypatch.setitem(globals_, "USERPASS_PASSWORD_ESCROW", userpass)
    staging = [globals_["_escrow_staging_path"](path) for path in (root, undo, userpass)]
    for path in staging:
        path.write_bytes(b"ciphertext")
    root_document = {
        "nonce": "n" * 16,
        "otp": "o" * 16,
        "started_at": 1,
        "progress": 0,
        "encoded_token": None,
    }
    monkeypatch.setitem(
        globals_, "_root_escrow_document_raw", lambda _path: root_document
    )
    monkeypatch.setitem(
        globals_,
        "_read_encrypted_document",
        lambda path, _name: {"kind": Path(path).name},
    )
    monkeypatch.setitem(
        globals_, "_validated_userpass_document", lambda *_args: bytearray(b"secret")
    )
    monkeypatch.setitem(
        globals_, "_undo_document_shape_is_valid", lambda *_args: True
    )
    transaction = {
        "root_generation_pending": True,
        "password_recovery_started": False,
        "userpass_password_escrowed": False,
        "openbao_undo_write_started": False,
        "openbao_undo_escrowed": False,
        "openbao_provisioning_started": False,
    }
    assert set(validate(transaction)) == set(staging)

    (state_root / ".root-token.cred.foreign").write_bytes(b"foreign")
    with pytest.raises(namespace["BootstrapError"], match="unexpected secret"):
        validate(transaction)


def test_successor_audit_partial_append_is_readonly_valid_then_truncated_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    globals_ = namespace["_read_audit_partial_frontier"].__globals__
    audit = tmp_path / "events.jsonl"
    record = namespace["_audit_record"](
        "bootstrap_consumed", "release-1", 1, "0" * 64, {"mode": "test"}
    )
    complete = globals_["_canonical"](record) + b"\n"
    partial = b'{"actor":"codex-supervised","event"'
    audit.write_bytes(complete + partial)
    audit.chmod(0o600)
    monkeypatch.setitem(globals_, "AUDIT_PATH", audit)
    monkeypatch.setitem(
        globals_,
        "_read_exact_root_file",
        lambda path, *_args, **_kwargs: Path(path).read_bytes(),
    )
    monkeypatch.setitem(globals_, "_fsync_directory", lambda *_args: None)
    real_fstat = os.fstat
    monkeypatch.setattr(
        globals_["os"], "fstat", lambda fd: _as_root_owned(real_fstat(fd))
    )

    raw, observed_complete, records, tail, observed_partial = namespace[
        "_read_audit_partial_frontier"
    ](audit)
    assert raw == complete + partial
    assert observed_complete == complete
    assert records == [record]
    assert tail == record["record_sha256"]
    assert observed_partial == partial
    namespace["_reconcile_audit_partial_frontier"]()
    assert audit.read_bytes() == complete

    next_record = namespace["_audit_record"](
        "bootstrap_failure_detected",
        "release-1",
        2,
        record["record_sha256"],
        {"message": "échec borné"},
    )
    next_raw = globals_["_canonical"](next_record) + b"\n"
    assert next_raw.isascii()
    for boundary in range(1, len(next_raw)):
        audit.write_bytes(complete + next_raw[:boundary])
        assert namespace["_read_audit_partial_frontier"](audit)[-1] \
            == next_raw[:boundary]

    audit.write_bytes(complete + b'{"actor":"codex-supervised","x":"\xc3')
    with pytest.raises(namespace["BootstrapError"], match="append frontier"):
        namespace["_read_audit_partial_frontier"](audit)

    audit.write_bytes(complete + b"foreign")
    with pytest.raises(namespace["BootstrapError"], match="append frontier"):
        namespace["_read_audit_partial_frontier"](audit)

    audit.write_bytes(complete + next_raw[: len(next_raw) // 2])
    assert namespace["_tail_hash"]() == record["record_sha256"]
    assert audit.read_bytes() == complete
    for corrupt in (b"foreign", b'{"actor":"codex-supervised","x":"\xc3'):
        audit.write_bytes(complete + corrupt)
        with pytest.raises(namespace["BootstrapError"], match="append frontier"):
            namespace["_tail_hash"]()
        assert audit.read_bytes() == complete + corrupt

    # The very first append may die before its newline, leaving no committed
    # record.  A canonical prefix is the sole valid empty-chain repair case.
    audit.write_bytes(complete[: len(complete) // 2])
    raw, observed_complete, records, tail, observed_partial = namespace[
        "_read_audit_partial_frontier"
    ](audit)
    assert raw == observed_partial
    assert observed_complete == b""
    assert records == []
    assert tail == "0" * 64
    assert namespace["_tail_hash"]() == "0" * 64
    assert audit.read_bytes() == b""


def test_completed_handoff_authenticates_before_audit_truncation_and_revalidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    reconcile = namespace["_reconcile_corrective_handoff"]
    globals_ = reconcile.__globals__
    marker: dict[str, object] = {}
    transaction = {"status": "completed"}
    events: list[str] = []
    monkeypatch.setitem(
        globals_, "_validate_corrective_marker_document", lambda _marker: None
    )
    monkeypatch.setitem(
        globals_, "_materialize_corrective_state", lambda _marker: transaction
    )
    monkeypatch.setitem(
        globals_,
        "_validate_completed_corrective_handoff",
        lambda *_args: (_ for _ in ()).throw(
            namespace["BootstrapError"](
                "corrective_state_invalid", "untrusted handoff"
            )
        ),
    )
    monkeypatch.setitem(
        globals_,
        "_reconcile_audit_partial_frontier",
        lambda: events.append("truncate"),
    )
    with pytest.raises(namespace["BootstrapError"], match="untrusted handoff"):
        reconcile(marker)
    assert events == []

    monkeypatch.setitem(
        globals_,
        "_validate_completed_corrective_handoff",
        lambda *_args: events.append("validate") or {"status": "running"},
    )
    monkeypatch.setitem(
        globals_, "_read_audit_partial_frontier", lambda _path: (b"", b"", [], "", b"")
    )
    monkeypatch.setitem(globals_, "AUDIT_PATH", Path("/unused/events.jsonl"))
    result = reconcile(marker)
    assert result == {"status": "running"}
    assert events == ["validate", "truncate", "validate"]


def test_corrective_recovery_releases_lock_before_standard_recovery_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    entry = namespace["corrective_recovery_entry"]
    globals_ = entry.__globals__
    marker_path = tmp_path / "corrective-consumed.json"
    marker_path.write_text("consumed\n", encoding="ascii")
    marker = {"bootstrap_helper_sha256": "a" * 64}
    successor = {"status": "running", "phase": "broker-quiesced"}
    events: list[tuple[str, object]] = []
    monkeypatch.setitem(globals_, "CORRECTIVE_CONSUMED_PATH", marker_path)
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    monkeypatch.setitem(globals_, "_read_corrective_marker", lambda: marker)
    monkeypatch.setitem(
        globals_, "_independent_regular_sha256", lambda *_args, **_kwargs: "a" * 64
    )
    monkeypatch.setitem(
        globals_, "_acquire_corrective_recovery_lock", lambda **_kwargs: 91
    )
    monkeypatch.setitem(
        globals_, "_reconcile_corrective_handoff", lambda _marker: successor
    )
    monkeypatch.setitem(
        globals_,
        "_standard_recovery_handoff_unit",
        lambda _marker, observed: (
            events.append(("select", observed["phase"]))
            or "ops-control-plane-bootstrap-recovery.service"
        ),
    )
    monkeypatch.setitem(
        globals_,
        "_start_standard_recovery_handoff",
        lambda unit: events.append(("start", unit)),
    )
    monkeypatch.setattr(
        globals_["os"], "close", lambda descriptor: events.append(("close", descriptor))
    )
    assert entry() == 0
    assert events == [
        ("select", "broker-quiesced"),
        ("close", 91),
        ("start", "ops-control-plane-bootstrap-recovery.service"),
    ]
    assert globals_["ACTIVE_DEPLOYMENT_LOCK_FD"] is None


def test_corrective_recovery_finalizes_preproduction_pending_under_its_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    entry = namespace["corrective_recovery_entry"]
    globals_ = entry.__globals__
    marker_path = tmp_path / "corrective-consumed.json"
    marker_path.write_text("consumed\n", encoding="ascii")
    marker = {"bootstrap_helper_sha256": "a" * 64}
    successor = {
        "status": "rollback-failed",
        "phase": globals_["PREPRODUCTION_ROLLBACK_AUDIT_PHASE"],
    }
    events: list[tuple[str, object]] = []
    monkeypatch.setitem(globals_, "CORRECTIVE_CONSUMED_PATH", marker_path)
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    monkeypatch.setitem(globals_, "_read_corrective_marker", lambda: marker)
    monkeypatch.setitem(
        globals_, "_independent_regular_sha256", lambda *_args, **_kwargs: "a" * 64
    )
    monkeypatch.setitem(
        globals_, "_acquire_corrective_recovery_lock", lambda **_kwargs: 93
    )
    monkeypatch.setitem(
        globals_, "_reconcile_corrective_handoff", lambda _marker: successor
    )
    monkeypatch.setitem(
        globals_,
        "_preproduction_corrector_frontier_is_unchanged",
        lambda observed: events.append(("unchanged", observed["phase"])) or True,
    )
    monkeypatch.setitem(
        globals_,
        "_validate_preproduction_recovery_scaffold_readonly",
        lambda observed: events.append(("scaffold", observed["phase"])),
    )
    monkeypatch.setitem(
        globals_,
        "_finalize_rollback_transaction",
        lambda observed, **kwargs: events.append(
            ("finalize", (observed["phase"], kwargs.get("preproduction")))
        ),
    )
    monkeypatch.setitem(
        globals_,
        "_cleanup_recovery_guard",
        lambda observed: events.append(("cleanup", observed["phase"])),
    )
    monkeypatch.setitem(
        globals_,
        "_standard_recovery_handoff_unit",
        lambda *_args: (_ for _ in ()).throw(AssertionError("standard handoff")),
    )
    monkeypatch.setitem(
        globals_,
        "_start_standard_recovery_handoff",
        lambda *_args: (_ for _ in ()).throw(AssertionError("standard start")),
    )
    monkeypatch.setattr(
        globals_["os"], "close", lambda descriptor: events.append(("close", descriptor))
    )

    assert entry() == 0
    assert events == [
        ("unchanged", globals_["PREPRODUCTION_ROLLBACK_AUDIT_PHASE"]),
        ("scaffold", globals_["PREPRODUCTION_ROLLBACK_AUDIT_PHASE"]),
        (
            "finalize",
            (globals_["PREPRODUCTION_ROLLBACK_AUDIT_PHASE"], True),
        ),
        ("cleanup", globals_["PREPRODUCTION_ROLLBACK_AUDIT_PHASE"]),
        ("close", 93),
    ]
    assert b"ReadWritePaths=/var/lib /run/lock /etc/systemd/system" in globals_[
        "CORRECTIVE_RECOVERY_UNIT_BYTES"
    ]
    assert globals_["ACTIVE_DEPLOYMENT_LOCK_FD"] is None


def test_corrective_recovery_replays_cleanup_after_terminal_wal_without_trigger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    entry = namespace["corrective_recovery_entry"]
    globals_ = entry.__globals__
    marker_path = tmp_path / "corrective-consumed.json"
    marker_path.write_text("consumed\n", encoding="ascii")
    marker = {"bootstrap_helper_sha256": "a" * 64}
    successor = {"status": "rolled-back", "phase": "rolled-back"}
    events: list[tuple[str, object]] = []
    monkeypatch.setitem(globals_, "CORRECTIVE_CONSUMED_PATH", marker_path)
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    monkeypatch.setitem(globals_, "_read_corrective_marker", lambda: marker)
    monkeypatch.setitem(
        globals_, "_independent_regular_sha256", lambda *_args, **_kwargs: "a" * 64
    )
    monkeypatch.setitem(
        globals_, "_acquire_corrective_recovery_lock", lambda **_kwargs: 94
    )
    monkeypatch.setitem(
        globals_, "_reconcile_corrective_handoff", lambda _marker: successor
    )
    monkeypatch.setitem(
        globals_,
        "_validate_preproduction_recovery_scaffold_readonly",
        lambda observed: events.append(("scaffold", observed["status"])),
    )
    monkeypatch.setitem(
        globals_,
        "_append_rollback_audit_exact_once",
        lambda observed: events.append(("audit", observed["phase"])) or "a" * 64,
    )
    monkeypatch.setitem(
        globals_,
        "_cleanup_recovery_guard",
        lambda observed: events.append(("cleanup", observed["status"])),
    )
    monkeypatch.setitem(
        globals_,
        "_standard_recovery_handoff_unit",
        lambda *_args: (_ for _ in ()).throw(AssertionError("standard handoff")),
    )
    monkeypatch.setitem(
        globals_,
        "_start_standard_recovery_handoff",
        lambda *_args: (_ for _ in ()).throw(AssertionError("standard start")),
    )
    monkeypatch.setattr(
        globals_["os"], "close", lambda descriptor: events.append(("close", descriptor))
    )

    assert entry() == 0
    assert events == [
        ("scaffold", "rolled-back"),
        ("audit", "rolled-back"),
        ("cleanup", "rolled-back"),
        ("close", 94),
    ]
    assert globals_["ACTIVE_DEPLOYMENT_LOCK_FD"] is None


def test_corrective_handoff_leaves_only_exact_safe_enrollment_to_gui(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    select = namespace["_standard_recovery_handoff_unit"]
    globals_ = select.__globals__
    marker = {
        "release_id": "release-1",
        "source_tree_sha256": "1" * 64,
        "atlas_rpm_sha256": "2" * 64,
        "hermes_source_archive_sha256": "7" * 64,
    }
    transaction = {"status": "running", "phase": "consumed"}
    monkeypatch.setitem(globals_, "_validate_transaction_document", lambda _value: None)
    monkeypatch.setitem(
        globals_,
        "_transaction_matches_safe_enrollment_frontier",
        lambda observed, **_identity: observed["phase"] in {
            "consumed", globals_["PREPRODUCTION_ROLLBACK_AUDIT_PHASE"]
        },
    )
    assert select(marker, transaction) is None

    transaction.update({
        "phase": globals_["PREPRODUCTION_ROLLBACK_AUDIT_PHASE"],
        "status": "rollback-failed",
        "recovery_guard_installing": True,
        "recovery_guard_installed": False,
    })
    main = globals_["RECOVERY_UNITS"][1]
    main_link, main_target = globals_["_standard_recovery_enablement"](main)
    monkeypatch.setattr(
        globals_["os"].path,
        "lexists",
        lambda path: path in {main_link, main_target},
    )
    validated: list[str] = []
    monkeypatch.setitem(
        globals_,
        "_validate_standard_recovery_handoff_anchor",
        lambda _marker, _transaction, unit: validated.append(unit),
    )
    assert select(marker, transaction) == main
    assert validated == [main]

    transaction.update({
        "phase": "broker-quiesced",
        "recovery_guard_installing": False,
        "recovery_guard_installed": True,
    })
    validated.clear()
    assert select(marker, transaction) == main
    assert validated == [main]


def test_broker_quiesce_intent_is_durable_before_mutation_and_requires_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    quiesce = namespace["_quiesce_broker_with_recovery_intent"]
    globals_ = quiesce.__globals__
    transaction = new_transaction(namespace, monkeypatch)
    transaction.update({
        "phase": "recovery-armed",
        "recovery_guard_installing": False,
        "recovery_guard_installed": True,
    })
    events: list[tuple[str, str]] = []

    def set_transaction(target: dict[str, object], **changes: object) -> None:
        target.update(changes)
        events.append(("wal", str(changes.get("phase", changes.get("status")))))

    def die_after_first_mutation(_worker: dict[str, object]) -> None:
        events.append(("mutation", "broker-stop"))
        raise SystemExit(137)

    monkeypatch.setitem(globals_, "_set_transaction", set_transaction)
    monkeypatch.setitem(globals_, "_quiesce_broker", die_after_first_mutation)
    with pytest.raises(SystemExit):
        quiesce({}, transaction)
    assert events == [
        ("wal", "broker-quiescing"),
        ("mutation", "broker-stop"),
    ]
    assert transaction["phase"] == "broker-quiescing"

    marker = {
        "release_id": transaction["release_id"],
        "source_tree_sha256": transaction["source_tree_sha256"],
        "atlas_rpm_sha256": transaction["atlas_rpm_sha256"],
        "hermes_source_archive_sha256": transaction[
            "hermes_source_archive_sha256"
        ],
    }
    main = globals_["RECOVERY_UNITS"][1]
    main_link, main_target = globals_["_standard_recovery_enablement"](main)
    monkeypatch.setattr(
        globals_["os"].path,
        "lexists",
        lambda path: path in {main_link, main_target},
    )
    monkeypatch.setitem(
        globals_, "_validate_standard_recovery_handoff_anchor", lambda *_args: None
    )
    assert globals_["_standard_recovery_handoff_unit"](marker, transaction) == main

    failed = namespace["_mark_rollback_failed_preserving_frontier"]
    failed_globals = failed.__globals__
    monkeypatch.setitem(failed_globals, "_set_transaction", set_transaction)
    failed(transaction)
    assert transaction["status"] == "rollback-failed"
    assert transaction["phase"] == "broker-quiescing"
    assert globals_["_standard_recovery_handoff_unit"](marker, transaction) == main

    rollback_source = __import__("inspect").getsource(namespace["rollback_bootstrap"])
    assert "_quiesce_broker_with_recovery_intent(worker, transaction)" in rollback_source


def test_standard_recovery_handoff_is_blocking_and_propagates_each_stage_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    start = namespace["_start_standard_recovery_handoff"]
    globals_ = start.__globals__
    commands: list[tuple[tuple[str, ...], int]] = []

    def fail_started_unit(
        command: list[str], *, timeout: int, **_kwargs: object
    ) -> subprocess.CompletedProcess[bytes]:
        commands.append((tuple(command), timeout))
        return subprocess.CompletedProcess(
            command, 1 if command[1] == "start" else 0, stdout=b"", stderr=b""
        )

    monkeypatch.setattr(globals_["subprocess"], "run", fail_started_unit)
    for unit in (globals_["RECOVERY_UNITS"][1], globals_["RECOVERY_JANITOR_UNIT"]):
        commands.clear()
        with pytest.raises(namespace["BootstrapError"], match="could not queue"):
            start(unit)
        assert commands == [
            (("/usr/bin/systemctl", "reset-failed", unit), 60),
            (("/usr/bin/systemctl", "start", unit), 6 * 60 * 60 + 10 * 60),
        ]
        assert all("--no-block" not in command for command, _timeout in commands)


def test_preproduction_rollback_never_stops_a_managed_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    rollback = namespace["rollback_bootstrap"]
    globals_ = rollback.__globals__
    transaction: dict[str, object] = {"release_id": "release-1"}
    events: list[str] = []
    monkeypatch.setitem(
        globals_, "_remove_public_installed_proof", lambda _transaction: events.append("proof")
    )
    monkeypatch.setitem(
        globals_, "_preproduction_rollback_is_unchanged", lambda _transaction: True
    )
    monkeypatch.setitem(
        globals_, "_finalize_rollback_transaction",
        lambda _transaction, **_kwargs: events.append("terminal")
    )
    monkeypatch.setitem(
        globals_, "_cleanup_recovery_guard", lambda _transaction: events.append("cleanup")
    )
    monkeypatch.setitem(
        globals_,
        "_quiesce_broker_with_recovery_intent",
        lambda *_args: (_ for _ in ()).throw(AssertionError("service stop attempted")),
    )
    rollback({}, transaction)
    assert events == ["proof", "terminal", "cleanup"]


def test_preproduction_rollback_requires_the_exact_initial_service_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    review = namespace["_preproduction_rollback_is_unchanged"]
    globals_ = review.__globals__
    expected = {"unit.service": {"active": False, "enabled": True}}
    transaction = {
        "release_id": "release-1",
        "source_tree_sha256": "1" * 64,
        "atlas_rpm_sha256": "2" * 64,
        "hermes_source_archive_sha256": "7" * 64,
        "service_state_before": expected,
    }
    monkeypatch.setitem(globals_, "RECOVERY_DEPENDENT_UNITS", ("unit.service",))
    monkeypatch.setitem(
        globals_, "_transaction_matches_safe_enrollment_frontier", lambda *_args, **_kwargs: True
    )
    observed = {"active": False, "enabled": True}
    monkeypatch.setitem(
        globals_, "_strict_corrective_service_state", lambda _unit: dict(observed)
    )
    assert review(transaction) is True
    observed["active"] = True
    with pytest.raises(namespace["BootstrapError"], match="changed before"):
        review(transaction)


def test_rollback_audit_append_is_exact_once_across_partial_and_committed_frontiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    append_once = namespace["_append_rollback_audit_exact_once"]
    globals_ = append_once.__globals__
    transaction = {"release_id": "release-1"}
    records: list[dict[str, object]] = []
    partial = [b'{"actor":"codex-supervised"']
    events: list[str] = []
    monkeypatch.setitem(globals_, "AUDIT_PATH", Path("/unused/events.jsonl"))
    monkeypatch.setitem(
        globals_,
        "_read_audit_partial_frontier",
        lambda _path: (b"", b"", records, "0" * 64, partial[0]),
    )
    monkeypatch.setitem(
        globals_,
        "_reconcile_audit_partial_frontier",
        lambda: (events.append("truncate"), partial.__setitem__(0, b"")),
    )
    monkeypatch.setitem(
        globals_, "_read_exact_audit", lambda _path: (b"", records, "0" * 64)
    )
    monkeypatch.setitem(
        globals_, "_read_stable_audit_file", lambda _path: b""
    )

    def append(event: str, *, release_id: str, details: dict[str, object]) -> str:
        events.append("append")
        assert event == "bootstrap_rolled_back"
        assert release_id == "release-1"
        assert details == {"password_recovery_persists": True}
        return "a" * 64

    monkeypatch.setitem(globals_, "append_audit", append)
    assert append_once(transaction) == "a" * 64
    assert events == ["truncate", "append"]

    records.append({
        "event": "bootstrap_rolled_back",
        "release_id": "release-1",
        "details": {"password_recovery_persists": True},
        "record_sha256": "b" * 64,
    })
    events.clear()
    assert append_once(transaction) == "b" * 64
    assert events == []
    records[0]["details"] = {"password_recovery_persists": False}
    with pytest.raises(namespace["BootstrapError"], match="frontier conflicts"):
        append_once(transaction)


def test_rollback_terminal_wal_follows_idempotent_audit_and_retries_after_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    finalize = namespace["_finalize_rollback_transaction"]
    globals_ = finalize.__globals__
    transaction: dict[str, object] = {
        "status": "running",
        "phase": "broker-quiesced",
    }
    events: list[str] = []
    fail_terminal = [True]

    def set_transaction(target: dict[str, object], **changes: object) -> None:
        phase = str(changes.get("phase"))
        events.append(f"wal:{phase}")
        if phase == "rolled-back" and fail_terminal[0]:
            fail_terminal[0] = False
            raise OSError("simulated kill before terminal WAL")
        target.update(changes)

    monkeypatch.setitem(globals_, "_set_transaction", set_transaction)
    monkeypatch.setitem(
        globals_,
        "_append_rollback_audit_exact_once",
        lambda _transaction: events.append("audit") or "c" * 64,
    )
    with pytest.raises(OSError, match="simulated kill"):
        finalize(transaction)
    assert transaction["phase"] == "rollback-audit-pending"
    assert events == ["wal:rollback-audit-pending", "audit", "wal:rolled-back"]

    events.clear()
    finalize(transaction)
    assert transaction["status"] == "rolled-back"
    assert transaction["phase"] == "rolled-back"
    assert events == ["wal:rollback-audit-pending", "audit", "wal:rolled-back"]


def test_terminal_preproduction_rollback_remains_secretless_cleanup_resumable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    transaction = new_transaction(namespace, monkeypatch)
    transaction.update({
        "status": "rolled-back",
        "phase": "rolled-back",
        "rolled_back_at": max(1, int(transaction["updated_at"])),
    })
    classify = namespace["_preproduction_terminal_rollback_candidate"]
    globals_ = classify.__globals__
    monkeypatch.setattr(globals_["os"].path, "lexists", lambda _path: False)
    candidate = classify(
        transaction,
        release_id=transaction["release_id"],
        source_tree_sha256=transaction["source_tree_sha256"],
        atlas_rpm_sha256=transaction["atlas_rpm_sha256"],
        hermes_source_archive_sha256=transaction[
            "hermes_source_archive_sha256"
        ],
    )
    assert candidate is not None
    assert candidate["status"] == "rollback-failed"
    assert candidate["phase"] == globals_["PREPRODUCTION_ROLLBACK_AUDIT_PHASE"]
    assert candidate["rolled_back_at"] is None

    transaction["deployment_backup"] = "/var/backups/foreign"
    assert classify(
        transaction,
        release_id=transaction["release_id"],
        source_tree_sha256=transaction["source_tree_sha256"],
        atlas_rpm_sha256=transaction["atlas_rpm_sha256"],
        hermes_source_archive_sha256=transaction[
            "hermes_source_archive_sha256"
        ],
    ) is None

    apply_source = __import__("inspect").getsource(namespace["apply_bootstrap"])
    terminal_review = apply_source.index(
        "_preproduction_terminal_rollback_candidate("
    )
    pending_branch = apply_source.index("if pending:", terminal_review)
    exact_audit = apply_source.index(
        "_append_rollback_audit_exact_once(resume_transaction)", pending_branch
    )
    cleanup = apply_source.index("_cleanup_recovery_guard(resume_transaction)")
    assert terminal_review < pending_branch < exact_audit < cleanup


def test_terminal_corrective_handoff_uses_exact_remaining_janitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    select = namespace["_standard_recovery_handoff_unit"]
    globals_ = select.__globals__
    marker = {
        "release_id": "release-1",
        "source_tree_sha256": "1" * 64,
        "atlas_rpm_sha256": "2" * 64,
        "hermes_source_archive_sha256": "7" * 64,
    }
    transaction = {"status": "completed", "phase": "cleanup-pending"}
    monkeypatch.setitem(globals_, "_validate_transaction_document", lambda _value: None)
    monkeypatch.setitem(
        globals_, "_transaction_matches_safe_enrollment_frontier", lambda *_args, **_kwargs: False
    )
    monkeypatch.setitem(
        globals_, "_terminal_standard_recovery_is_clean", lambda _transaction: False
    )
    main = globals_["RECOVERY_UNITS"][1]
    janitor = globals_["RECOVERY_JANITOR_UNIT"]
    main_link, main_target = globals_["_standard_recovery_enablement"](main)
    janitor_link, janitor_target = globals_["_standard_recovery_enablement"](janitor)
    monkeypatch.setattr(
        globals_["os"].path,
        "lexists",
        lambda path: path in {main_target, janitor_link, janitor_target}
        and path != main_link,
    )
    validated: list[str] = []
    monkeypatch.setitem(
        globals_,
        "_validate_standard_recovery_unit_bytes",
        lambda _marker, _transaction, unit: validated.append(f"residual:{unit}"),
    )
    monkeypatch.setitem(
        globals_,
        "_validate_standard_recovery_handoff_anchor",
        lambda _marker, _transaction, unit: validated.append(unit),
    )
    assert select(marker, transaction) == janitor
    assert validated == [f"residual:{main}", janitor]


def test_standard_supervisor_is_dependency_free_and_attested_before_quiesce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    start = namespace["_start_standard_recovery_supervisor"]
    globals_ = start.__globals__
    events: list[tuple[str, object]] = []
    transaction: dict[str, object] = {"phase": "recovery-armed"}
    monkeypatch.setitem(
        globals_, "_read_consumed_identity", lambda _transaction: {"marker": True}
    )
    monkeypatch.setitem(
        globals_,
        "_validate_standard_recovery_handoff_anchor",
        lambda *_args: events.append(("anchor", "validated")),
    )
    monkeypatch.setitem(
        globals_, "_standard_recovery_supervisor_state", lambda: "absent"
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        events.append(("command", tuple(command)))
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(globals_["subprocess"], "run", run)
    start(transaction)
    assert events[0] == ("anchor", "validated")
    command = events[1][1]
    assert isinstance(command, tuple)
    assert command[:4] == (
        "/usr/bin/systemd-run", "--quiet", "--no-block", "--collect"
    )
    assert f"--unit={globals_['STANDARD_RECOVERY_SUPERVISOR_UNIT']}" in command
    assert "--property=Restart=on-failure" in command
    assert "--property=RuntimeMaxSec=13h" in command
    assert "--recover-supervise" == command[-1]
    assert not any("Before=" in value for value in command)

    events.clear()
    monkeypatch.setitem(
        globals_, "_standard_recovery_supervisor_state", lambda: "active"
    )
    start(transaction)
    assert events == [("anchor", "validated")]

    install_source = __import__("inspect").getsource(
        namespace["_install_recovery_guard"]
    )
    assert install_source.index("_start_standard_recovery_supervisor(") \
        < install_source.index("_wait_standard_recovery_supervisor()") \
        < install_source.index('phase="recovery-armed"')
    apply_source = __import__("inspect").getsource(namespace["apply_bootstrap"])
    assert apply_source.index("_install_recovery_guard(") \
        < apply_source.index("_quiesce_broker_with_recovery_intent(")


def test_standard_supervisor_waits_past_five_minutes_and_revalidates_frontier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    acquire = namespace["_acquire_corrective_recovery_lock"]
    globals_ = acquire.__globals__
    now = [0.0]
    attempts = [0]
    validations = [0]

    monkeypatch.setattr(globals_["os"], "open", lambda *_args, **_kwargs: 73)
    monkeypatch.setattr(globals_["os"], "close", lambda _fd: None)
    monkeypatch.setattr(globals_["time"], "monotonic", lambda: now[0])
    monkeypatch.setattr(
        globals_["time"], "sleep", lambda seconds: now.__setitem__(0, now[0] + max(seconds, 30.0))
    )

    def flock(_fd: int, _operation: int) -> None:
        attempts[0] += 1
        if attempts[0] <= 12:
            raise BlockingIOError(11, "busy")

    monkeypatch.setattr(globals_["fcntl"], "flock", flock)
    descriptor = acquire(
        timeout=600.0,
        wait_validator=lambda: validations.__setitem__(0, validations[0] + 1),
    )
    assert descriptor == 73
    assert now[0] > 300.0
    assert attempts[0] == 13
    assert validations[0] == 12


def test_standard_supervisor_pid1_identity_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    state = namespace["_standard_recovery_supervisor_state"]
    globals_ = state.__globals__
    unit = globals_["STANDARD_RECOVERY_SUPERVISOR_UNIT"]
    description = globals_["STANDARD_RECOVERY_SUPERVISOR_DESCRIPTION"]
    properties = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "UnitFileState": "transient",
        "FragmentPath": f"/run/systemd/transient/{unit}",
        "DropInPaths": str(globals_["SYSTEMD_GLOBAL_SERVICE_DROPIN"]),
        "Transient": "yes",
        "Type": "exec",
        "Description": description,
        "User": "root",
        "Group": "root",
        "SubState": "running",
        "MainPID": "1234",
    }

    def run(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raw = "".join(f"{key}={value}\n" for key, value in properties.items()).encode()
        return subprocess.CompletedProcess([], 0, stdout=raw, stderr=b"")

    monkeypatch.setattr(globals_["subprocess"], "run", run)
    monkeypatch.setitem(
        globals_, "_standard_recovery_supervisor_process_is_exact", lambda pid: pid == 1234
    )
    assert state() == "active"
    properties["FragmentPath"] = "/run/systemd/transient/foreign.service"
    with pytest.raises(namespace["BootstrapError"], match="identity differs"):
        state()

    properties["FragmentPath"] = f"/run/systemd/transient/{unit}"
    properties["ActiveState"] = "activating"
    properties["SubState"] = "auto-restart"
    properties["MainPID"] = "0"
    assert state() == "starting"
    monkeypatch.setitem(
        globals_, "_standard_recovery_supervisor_state", lambda: "starting"
    )
    with pytest.raises(namespace["BootstrapError"], match="did not become active"):
        namespace["_wait_standard_recovery_supervisor"](timeout=0.0)


def test_supervisor_treats_pre_wal_guard_frontier_as_gui_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    select = namespace["_standard_recovery_handoff_unit"]
    globals_ = select.__globals__
    transaction = new_transaction(namespace, monkeypatch)
    transaction.update({
        "phase": "recovery-guard-installing",
        "recovery_guard_installing": True,
        "recovery_guard_installed": False,
    })
    marker = {
        "release_id": transaction["release_id"],
        "source_tree_sha256": transaction["source_tree_sha256"],
        "atlas_rpm_sha256": transaction["atlas_rpm_sha256"],
        "hermes_source_archive_sha256": transaction[
            "hermes_source_archive_sha256"
        ],
    }
    monkeypatch.setitem(globals_, "ROOT_TOKEN_ESCROW", Path("/definitely/absent/root"))
    monkeypatch.setitem(globals_, "OPENBAO_UNDO_ESCROW", Path("/definitely/absent/undo"))
    monkeypatch.setitem(
        globals_, "USERPASS_PASSWORD_ESCROW", Path("/definitely/absent/userpass")
    )
    assert select(marker, transaction) is None


def test_standard_supervisor_never_promotes_staged_wal_it_hands_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    source = __import__("inspect").getsource(
        namespace["standard_recovery_supervisor_entry"]
    )
    assert "promote_staging=True" not in source
    before_lock = source[:source.index("_acquire_corrective_recovery_lock(")]
    assert "_read_standard_transaction_frontier" not in before_lock
    assert source.count("promote_staging=False") == 1


def test_standard_supervisor_hands_staged_successor_off_after_unlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    entry = namespace["standard_recovery_supervisor_entry"]
    globals_ = entry.__globals__
    real_lstat = os.lstat
    helper_info = list(real_lstat(BOOTSTRAP))
    helper_info[0] = stat.S_IFREG | 0o700
    helper_info[3] = 1
    helper_info[4] = 0
    helper_info[5] = 0
    reviewed_stat = os.stat_result(helper_info)
    transaction = {"status": "running", "phase": "broker-quiesced"}
    consumed = {
        "release_id": "release-1",
        "source_tree_sha256": "1" * 64,
        "atlas_rpm_sha256": "2" * 64,
        "hermes_source_archive_sha256": "7" * 64,
        "bootstrap_helper_sha256": "3" * 64,
    }
    reads: list[bool] = []
    events: list[tuple[str, object]] = []
    monkeypatch.setitem(globals_, "INSTALLED_HELPER", BOOTSTRAP)
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    monkeypatch.setattr(globals_["os"], "lstat", lambda _path: reviewed_stat)
    monkeypatch.setattr(globals_["os"], "stat", lambda _path: reviewed_stat)
    monkeypatch.setitem(
        globals_,
        "_read_standard_transaction_frontier",
        lambda *, promote_staging: reads.append(promote_staging) or transaction,
    )
    consumed_raw = globals_["_canonical"](consumed) + b"\n"
    monkeypatch.setitem(
        globals_, "_read_exact_root_file", lambda *_args, **_kwargs: consumed_raw
    )
    monkeypatch.setitem(globals_, "_read_consumed_identity", lambda _value: consumed)

    def acquire(**kwargs: object) -> int:
        validator = kwargs["wait_validator"]
        assert callable(validator)
        validator()
        return 92

    monkeypatch.setitem(globals_, "_acquire_corrective_recovery_lock", acquire)
    monkeypatch.setitem(
        globals_,
        "_standard_recovery_handoff_unit",
        lambda _marker, observed: events.append(("select", observed["phase"]))
        or globals_["RECOVERY_UNITS"][1],
    )
    monkeypatch.setitem(
        globals_,
        "_start_standard_recovery_handoff",
        lambda unit: events.append(("start", unit)),
    )
    monkeypatch.setattr(
        globals_["os"], "close", lambda descriptor: events.append(("close", descriptor))
    )
    assert entry() == 0
    assert reads and all(value is False for value in reads)
    assert events == [
        ("select", "broker-quiesced"),
        ("close", 92),
        ("start", globals_["RECOVERY_UNITS"][1]),
    ]


def test_completed_wal_precedes_loss_of_last_recovery_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    cleanup = namespace["_cleanup_recovery_guard"]
    globals_ = cleanup.__globals__
    installed = tmp_path / "helper"
    guard = tmp_path / "guard"
    primary_one = tmp_path / "guard.service"
    primary_two = tmp_path / "recovery.service"
    janitor = tmp_path / "recovery-activate.service"
    janitor.write_text("unit\n", encoding="ascii")
    primary_links = [tmp_path / "primary-one.link", tmp_path / "primary-two.link"]
    janitor_link = tmp_path / "janitor.link"
    janitor_link.symlink_to(janitor)
    targets = [primary_one, primary_two, janitor]
    monkeypatch.setitem(
        globals_,
        "RECOVERY_ASSETS",
        (installed, guard, primary_one, primary_two, janitor),
    )
    monkeypatch.setitem(globals_, "RECOVERY_BARRIER_ASSETS", ())
    monkeypatch.setitem(
        globals_,
        "RECOVERY_ENABLEMENT_LINKS",
        dict(zip((*primary_links, janitor_link), targets, strict=True)),
    )
    monkeypatch.setitem(globals_, "_remove_recovery_barriers", lambda: None)
    monkeypatch.setitem(globals_, "_validate_enablement_link", lambda *_args: None)
    monkeypatch.setitem(globals_, "_fsync_directory", lambda *_args: None)
    monkeypatch.setitem(globals_, "_systemctl_state", lambda *_args: False)
    events: list[str] = []
    transaction: dict[str, object] = {
        "status": "completed",
        "phase": "cleanup-pending",
        "recovery_guard_installing": False,
        "recovery_guard_installed": True,
    }
    monkeypatch.setitem(
        globals_,
        "_set_transaction",
        lambda target, **changes: (target.update(changes), events.append(
            f"wal:{changes.get('phase', 'flags')}"
        ))[0],
    )
    monkeypatch.setitem(
        globals_, "_publish_public_installed_proof", lambda _transaction: events.append("proof")
    )

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if command[:2] == ["/usr/bin/systemctl", "disable"]:
            assert transaction["phase"] == "completed"
            events.append("last-anchor-disable")
            raise RuntimeError("simulated power loss")
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(globals_["subprocess"], "run", run)
    with pytest.raises(RuntimeError, match="power loss"):
        cleanup(transaction)
    assert events.index("proof") < events.index("wal:completed") \
        < events.index("last-anchor-disable")
    assert transaction["phase"] == "completed"

    finish_source = __import__("inspect").getsource(
        namespace["_finish_committed_cleanup"]
    )
    assert finish_source.rstrip().endswith("_cleanup_recovery_guard(transaction)")


def test_corrective_successor_cannot_resume_through_fresh_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    review = namespace["_review_enrollment_resume"]
    globals_ = review.__globals__
    corrective = tmp_path / "corrective-consumed.json"
    corrective.write_text("consumed", encoding="utf-8")
    monkeypatch.setitem(globals_, "CORRECTIVE_CONSUMED_PATH", corrective)
    monkeypatch.setitem(globals_, "CONSUMED_PATH", tmp_path / "successor.json")
    monkeypatch.setitem(globals_, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setitem(globals_, "TRANSACTION_PATH", tmp_path / "state/transaction.json")
    manifest = {
        "artifacts": {
            "atlas_rpm": {"sha256": "2" * 64},
            "hermes_source_archive": {"sha256": "7" * 64},
        }
    }
    with pytest.raises(namespace["BootstrapError"], match="corrective operation"):
        review(manifest, "1" * 64, "3" * 64, "4" * 64)
    assert review(
        manifest,
        "1" * 64,
        "3" * 64,
        "4" * 64,
        allow_corrective_successor=True,
    ) is None


def test_corrective_predecessor_systemd_query_fails_closed_on_query_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    strict = namespace["_strict_systemctl_state"]
    globals_ = strict.__globals__
    monkeypatch.setattr(
        globals_["subprocess"],
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, stdout=b""),
    )
    with pytest.raises(namespace["BootstrapError"], match="query failed"):
        strict("ops-broker.service", "is-active")


def test_corrective_final_snapshot_never_collapses_systemd_query_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    new_transaction = namespace["_new_transaction"]
    globals_ = new_transaction.__globals__
    monkeypatch.setitem(globals_, "_systemctl_state", lambda *_args: False)

    def strict_error(_unit: str) -> dict[str, bool]:
        raise namespace["BootstrapError"](
            "corrective_predecessor_invalid", "strict query failed"
        )

    monkeypatch.setitem(globals_, "_strict_corrective_service_state", strict_error)
    manifest = {
        "release_id": globals_["CORRECTIVE_TARGET_RELEASE_ID"],
        "source": {"tree_sha256": "1" * 64},
        "artifacts": {
            "atlas_rpm": {"sha256": "2" * 64},
            "hermes_source_archive": {"sha256": "7" * 64},
        },
    }
    preflight = {
        "credential_groups_present": {
            name: False for name in globals_["CREDENTIAL_GROUPS"]
        }
    }
    assert new_transaction(manifest, preflight)["service_state_before"]
    with pytest.raises(namespace["BootstrapError"], match="strict query failed"):
        new_transaction(manifest, preflight, strict_service_state=True)

    source = __import__("inspect").getsource(namespace["apply_bootstrap"])
    new_branch = source[source.index('if second_corrective_context["kind"] == "new"') :]
    assert new_branch.index("_install_corrective_scaffold(") < new_branch.index(
        "strict_service_state=True"
    ) < new_branch.index("_new_corrective_marker(")


def test_corrective_service_snapshot_distinguishes_expected_absence_from_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    read_state = namespace["_strict_corrective_service_state"]
    globals_ = read_state.__globals__
    absent = next(iter(globals_["CORRECTIVE_PREDECESSOR_ABSENT_DEPENDENT_UNITS"]))
    loaded = next(
        unit for unit in globals_["RECOVERY_DEPENDENT_UNITS"]
        if unit not in globals_["CORRECTIVE_PREDECESSOR_ABSENT_DEPENDENT_UNITS"]
    )
    properties = {
        absent: {
            "LoadState": "not-found",
            "ActiveState": "inactive",
            "UnitFileState": "",
            "FragmentPath": "",
            "DropInPaths": "",
        },
        loaded: {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "UnitFileState": "disabled",
            "FragmentPath": f"/usr/lib/systemd/system/{loaded}",
            "DropInPaths": "",
        },
    }
    monkeypatch.setitem(
        globals_, "_strict_systemctl_properties", lambda unit: dict(properties[unit])
    )
    assert read_state(absent) == {"active": False, "enabled": False}
    assert read_state(loaded) == {"active": False, "enabled": False}

    properties[absent]["LoadState"] = "error"
    with pytest.raises(namespace["BootstrapError"], match="expected-absent"):
        read_state(absent)
    properties[absent]["LoadState"] = "not-found"
    properties[loaded]["LoadState"] = "not-found"
    with pytest.raises(namespace["BootstrapError"], match="loaded predecessor"):
        read_state(loaded)


def test_corrective_predecessor_recovery_unit_query_error_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    validate = namespace["_validate_corrective_predecessor_recovery_units"]
    globals_ = validate.__globals__
    monkeypatch.setitem(
        globals_,
        "_strict_systemctl_properties",
        lambda _unit: (_ for _ in ()).throw(
            namespace["BootstrapError"](
                "corrective_scaffold_conflict", "PID 1 query failed"
            )
        ),
    )
    with pytest.raises(namespace["BootstrapError"], match="query failed"):
        validate()


def test_corrective_recovery_activation_wait_is_bounded_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    wait = namespace["_wait_corrective_recovery_job"]
    globals_ = wait.__globals__
    states = iter(("starting", "starting", "live"))
    monkeypatch.setitem(
        globals_, "_corrective_recovery_job_state", lambda: next(states)
    )
    monkeypatch.setattr(globals_["time"], "sleep", lambda _seconds: None)
    wait(timeout=1.0)

    monkeypatch.setitem(
        globals_, "_corrective_recovery_job_state", lambda: "starting"
    )
    with pytest.raises(namespace["BootstrapError"], match="did not become active"):
        wait(timeout=0.0)


def test_corrective_recovery_activation_wait_tolerates_pid1_restart_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = resume_bootstrap_namespace()
    wait = namespace["_wait_corrective_recovery_job"]
    globals_ = wait.__globals__
    error = namespace["BootstrapError"](
        "corrective_scaffold_conflict",
        "The corrective recovery service has no exact live waiter.",
    )
    states = iter((error, error, "starting", "live"))

    def recovery_state() -> str:
        observed = next(states)
        if isinstance(observed, BaseException):
            raise observed
        return observed

    monkeypatch.setitem(globals_, "_corrective_recovery_job_state", recovery_state)
    monkeypatch.setattr(globals_["time"], "sleep", lambda _seconds: None)
    wait(timeout=1.0)


def test_corrective_recovery_activation_wait_preserves_persistent_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = resume_bootstrap_namespace()
    wait = namespace["_wait_corrective_recovery_job"]
    globals_ = wait.__globals__

    def recovery_state() -> str:
        raise namespace["BootstrapError"](
            "corrective_scaffold_conflict", "PID 1 query failed"
        )

    monkeypatch.setitem(globals_, "_corrective_recovery_job_state", recovery_state)
    with pytest.raises(namespace["BootstrapError"], match="PID 1 query failed"):
        wait(timeout=0.0)


def test_resume_patch_manifest_projects_to_exact_consumed_deployment() -> None:
    namespace = resume_bootstrap_namespace()
    manifest = json.loads(RESUME_MANIFEST.read_text(encoding="utf-8"))

    projected = namespace["_resume_patch_deployment_manifest"](
        manifest,
        namespace["RESUME_PATCH_DEPLOYMENT"]["source_tree_sha256"],
    )

    assert projected == json.loads((ROOT / "artifacts/recovery-patch/release-manifest.d.v1.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(
        namespace["_canonical"](projected) + b"\n"
    ).hexdigest() == namespace[
        "RESUME_PATCH_DEPLOYMENT_MANIFEST_CANONICAL_SHA256"
    ]


def test_resume_patch_reviewed_manifest_accepts_the_real_e_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = resume_bootstrap_namespace()
    review = namespace["_reviewed_manifest"]
    globals_ = review.__globals__
    manifest_raw = RESUME_MANIFEST.read_bytes()
    mapping = {
        globals_["SOURCE_MANIFEST"]: manifest_raw,
        globals_["TRUST_ANCHOR_BOOTSTRAP_HELPER"]: RESUME_BOOTSTRAP.read_bytes(),
        globals_["TRUST_ANCHOR_LAUNCHER"]: (
            ROOT / "artifacts/recovery-patch/launch-atlas-reviewed-rpm.e"
        ).read_bytes(),
    }

    monkeypatch.setitem(globals_, "_validate_root_directory", lambda *_args: None)
    monkeypatch.setitem(
        globals_,
        "_read_exact_root_file",
        lambda path, _maximum, _mode: mapping[path],
    )

    observed, digest = review()
    assert observed == json.loads(manifest_raw)
    assert digest == hashlib.sha256(manifest_raw).hexdigest()


def test_resume_patch_manifest_rejects_incomplete_interface_delta() -> None:
    namespace = resume_bootstrap_namespace()
    manifest = json.loads((ROOT / "artifacts/recovery-patch/release-manifest.d.v1.json").read_text(encoding="utf-8"))
    manifest["bootstrap"]["helper_sha256"] = "a" * 64

    with pytest.raises(
        namespace["BootstrapError"], match="exact reviewed E interface"
    ):
        namespace["_resume_patch_deployment_manifest"](
            manifest,
            namespace["RESUME_PATCH_DEPLOYMENT"]["source_tree_sha256"],
        )


def test_resume_patch_rollback_preflight_uses_deployment_d_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = resume_bootstrap_namespace()
    transaction = new_transaction(namespace, monkeypatch)
    transaction.update(
        status="rollback-failed",
        phase=namespace["PREPRODUCTION_ROLLBACK_AUDIT_PHASE"],
    )
    manifest_e = json.loads(RESUME_MANIFEST.read_text(encoding="utf-8"))
    manifest_d = namespace["_resume_patch_deployment_manifest"](
        manifest_e,
        namespace["RESUME_PATCH_DEPLOYMENT"]["source_tree_sha256"],
    )
    captured: dict[str, object] = {}

    def rollback_preflight(
        manifest: dict[str, object],
        source_digest: str,
        manifest_digest: str,
        observed: dict[str, object],
        corrective_context: dict[str, object],
    ) -> dict[str, object]:
        captured.update(
            manifest=manifest,
            source_digest=source_digest,
            manifest_digest=manifest_digest,
            observed=observed,
            corrective_context=corrective_context,
        )
        return {"ok": True}

    preflight = namespace["bootstrap_preflight"]
    globals_ = preflight.__globals__
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    monkeypatch.setattr(globals_["os"].path, "lexists", lambda _path: False)
    monkeypatch.setitem(globals_, "_rollback_finalize_preflight", rollback_preflight)
    context = {
        "kind": "resume",
        "enrollment_resume": transaction,
        "deployment_manifest": manifest_d,
    }

    result = preflight(
        manifest_e,
        namespace["RESUME_PATCH_DEPLOYMENT"]["source_tree_sha256"],
        "e" * 64,
        enrollment_resume=transaction,
        corrective_context=context,
    )
    assert result == {
        "ok": True,
        "manifest_sha256": "e" * 64,
        "atlas_rpm_sha256": manifest_e["artifacts"]["atlas_rpm"]["sha256"],
    }
    assert captured["manifest"] == manifest_d
    assert captured["manifest_digest"] == namespace["RESUME_PATCH_DEPLOYMENT"][
        "manifest_sha256"
    ]


def test_resume_patch_wait_retries_transient_strict_identity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = resume_bootstrap_namespace()
    wait = namespace["_wait_corrective_recovery_job"]
    globals_ = wait.__globals__
    error = namespace["BootstrapError"](
        "corrective_scaffold_conflict",
        "The corrective recovery service has no exact live waiter.",
    )
    states = iter((error, error, "starting", "live"))

    def recovery_state() -> str:
        observed = next(states)
        if isinstance(observed, BaseException):
            raise observed
        return observed

    monkeypatch.setitem(globals_, "_corrective_recovery_job_state", recovery_state)
    monkeypatch.setattr(globals_["time"], "sleep", lambda _seconds: None)
    wait(timeout=1.0)


def test_resume_patch_wait_rethrows_a_persistent_strict_identity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = resume_bootstrap_namespace()
    wait = namespace["_wait_corrective_recovery_job"]
    globals_ = wait.__globals__

    def recovery_state() -> str:
        raise namespace["BootstrapError"](
            "corrective_scaffold_conflict", "persistent PID 1 mismatch"
        )

    monkeypatch.setitem(globals_, "_corrective_recovery_job_state", recovery_state)
    with pytest.raises(
        namespace["BootstrapError"], match="persistent PID 1 mismatch"
    ):
        wait(timeout=0.0)


def test_resume_patch_reviews_e_but_resumes_the_exact_d_wal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = resume_bootstrap_namespace()
    review = namespace["_review_corrective_retry"]
    globals_ = review.__globals__
    manifest_raw = RESUME_MANIFEST.read_bytes()
    manifest = json.loads(manifest_raw)
    helper_sha256 = hashlib.sha256(RESUME_BOOTSTRAP.read_bytes()).hexdigest()
    marker = dict(namespace["RESUME_PATCH_DEPLOYMENT"])
    successor = {"status": "running", "phase": "consumed"}
    captured: dict[str, object] = {}

    monkeypatch.setitem(globals_, "_validate_corrective_authority", lambda: None)
    monkeypatch.setattr(globals_["os"].path, "lexists", lambda _path: True)
    monkeypatch.setitem(globals_, "_read_corrective_marker", lambda: marker)
    monkeypatch.setitem(
        globals_, "_read_corrective_transaction_readonly", lambda _marker: {"status": "completed"}
    )
    monkeypatch.setitem(
        globals_, "_validate_completed_corrective_handoff", lambda *_args: successor
    )

    def enrollment_resume(*args: object, **kwargs: object) -> dict[str, object]:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return successor

    monkeypatch.setitem(globals_, "_review_enrollment_resume", enrollment_resume)
    result = review(
        manifest,
        namespace["RESUME_PATCH_DEPLOYMENT"]["source_tree_sha256"],
        hashlib.sha256(manifest_raw).hexdigest(),
        helper_sha256,
    )

    args = captured["args"]
    assert args[0] == json.loads((ROOT / "artifacts/recovery-patch/release-manifest.d.v1.json").read_text(encoding="utf-8"))
    assert args[1:] == (
        namespace["RESUME_PATCH_DEPLOYMENT"]["source_tree_sha256"],
        namespace["RESUME_PATCH_DEPLOYMENT"]["manifest_sha256"],
        namespace["RESUME_PATCH_DEPLOYMENT"]["bootstrap_helper_sha256"],
    )
    assert captured["kwargs"] == {"allow_corrective_successor": True}
    assert result["kind"] == "resume"
    assert result["enrollment_resume"] is successor
    assert result["deployment_manifest"] == args[0]


def test_resume_patch_replaces_only_the_exact_d_corrective_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = resume_bootstrap_namespace()
    publish = namespace["_publish_corrective_resume_helper"]
    globals_ = publish.__globals__
    old_helper = (ROOT / "artifacts/recovery-patch/control-plane-bootstrap.d").read_bytes()
    new_helper = RESUME_BOOTSTRAP.read_bytes()
    new_sha256 = hashlib.sha256(new_helper).hexdigest()
    writes: list[tuple[Path, bytes, int]] = []

    monkeypatch.setattr(globals_["os"].path, "lexists", lambda _path: True)
    monkeypatch.setitem(
        globals_, "_read_corrective_marker", lambda: dict(namespace["RESUME_PATCH_DEPLOYMENT"])
    )
    monkeypatch.setitem(
        globals_, "_read_exact_root_file", lambda *_args, **_kwargs: old_helper
    )
    monkeypatch.setitem(
        globals_, "_atomic_bytes", lambda path, payload, mode: writes.append((path, payload, mode))
    )
    monkeypatch.setitem(
        globals_, "_validate_corrective_scaffold_file", lambda *_args: None
    )

    publish(new_helper, new_sha256)

    assert writes == [(globals_["CORRECTIVE_RECOVERY_HELPER"], new_helper, 0o700)]


def test_corrective_recovery_job_requires_a_live_exact_pid_not_active_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    state = namespace["_corrective_recovery_job_state"]
    globals_ = state.__globals__
    properties = {
        "LoadState": "loaded",
        "ActiveState": "active",
        "UnitFileState": "enabled",
        "FragmentPath": str(globals_["CORRECTIVE_RECOVERY_UNIT"]),
        "DropInPaths": str(globals_["SYSTEMD_GLOBAL_SERVICE_DROPIN"]),
        "SubState": "exited",
        "MainPID": "0",
    }

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raw = "".join(f"{key}={value}\n" for key, value in properties.items()).encode()
        return subprocess.CompletedProcess(command, 0, stdout=raw, stderr=b"")

    monkeypatch.setattr(globals_["subprocess"], "run", run)
    monkeypatch.setitem(
        globals_, "_corrective_recovery_process_is_exact", lambda pid: pid == 4321
    )
    with pytest.raises(namespace["BootstrapError"], match="no exact live waiter"):
        state()

    properties.update({
        "ActiveState": "deactivating",
        "SubState": "stop-sigterm",
        "MainPID": "4321",
    })
    assert state() == "starting"
    properties["MainPID"] = "9999"
    with pytest.raises(namespace["BootstrapError"], match="no exact live waiter"):
        state()

    properties.update({
        "ActiveState": "inactive",
        "SubState": "dead",
        "MainPID": "0",
    })
    assert state() == "starting"

    properties.update({
        "ActiveState": "activating",
        "SubState": "start",
        "MainPID": "4321",
    })
    assert state() == "live"
    properties["MainPID"] = "9999"
    with pytest.raises(namespace["BootstrapError"], match="no exact live waiter"):
        state()


def test_corrective_scaffold_binds_exact_loaded_pid1_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    validate = namespace["_validate_corrective_scaffold"]
    globals_ = validate.__globals__
    helper = tmp_path / "corrective-helper"
    service_fragment = tmp_path / "corrective.service"
    path_fragment = tmp_path / "corrective.path"
    global_dropin = tmp_path / "10-timeout-abort.conf"
    marker = tmp_path / "corrective-consumed.json"
    helper_bytes = b"#!/usr/bin/python3 -I\n"
    dropin_bytes = b"[Service]\nTimeoutStopFailureMode=abort\n"
    helper.write_bytes(helper_bytes)
    service_fragment.write_bytes(globals_["CORRECTIVE_RECOVERY_UNIT_BYTES"])
    path_fragment.write_bytes(globals_["CORRECTIVE_RECOVERY_PATH_BYTES"])
    global_dropin.write_bytes(dropin_bytes)
    helper.chmod(0o700)
    service_fragment.chmod(0o644)
    path_fragment.chmod(0o644)
    global_dropin.chmod(0o644)

    monkeypatch.setitem(globals_, "CORRECTIVE_RECOVERY_HELPER", helper)
    monkeypatch.setitem(globals_, "CORRECTIVE_RECOVERY_UNIT", service_fragment)
    monkeypatch.setitem(globals_, "CORRECTIVE_RECOVERY_PATH", path_fragment)
    monkeypatch.setitem(
        globals_, "CORRECTIVE_RECOVERY_SERVICE_LINK", tmp_path / "service-link"
    )
    monkeypatch.setitem(
        globals_, "CORRECTIVE_RECOVERY_PATH_LINK", tmp_path / "path-link"
    )
    monkeypatch.setitem(globals_, "CORRECTIVE_CONSUMED_PATH", marker)
    monkeypatch.setitem(globals_, "SYSTEMD_GLOBAL_SERVICE_DROPIN", global_dropin)
    monkeypatch.setitem(
        globals_,
        "SYSTEMD_GLOBAL_SERVICE_DROPIN_SHA256",
        hashlib.sha256(dropin_bytes).hexdigest(),
    )
    monkeypatch.setitem(
        globals_,
        "_read_exact_root_file",
        lambda path, *_args, **_kwargs: Path(path).read_bytes(),
    )
    monkeypatch.setitem(globals_, "_validate_enablement_link", lambda *_args: None)
    monkeypatch.setitem(globals_, "_fsync_directory", lambda *_args: None)

    service_name = globals_["CORRECTIVE_RECOVERY_UNIT_NAME"]
    path_name = globals_["CORRECTIVE_RECOVERY_PATH_NAME"]
    properties = {
        service_name: {
            "LoadState": "loaded",
            "ActiveState": "inactive",
            "UnitFileState": "enabled",
            "FragmentPath": str(service_fragment),
            "DropInPaths": str(global_dropin),
        },
        path_name: {
            "LoadState": "loaded",
            "ActiveState": "active",
            "UnitFileState": "enabled",
            "FragmentPath": str(path_fragment),
            "DropInPaths": "",
        },
    }

    def systemctl_state(unit: str, operation: str) -> bool:
        if operation == "is-enabled":
            return unit in {service_name, path_name}
        if operation == "is-active":
            return unit == path_name
        raise AssertionError(operation)

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert command[:2] == ["/usr/bin/systemctl", "show"]
        unit = command[2]
        stdout = "".join(
            f"{key}={value}\n" for key, value in properties[unit].items()
        ).encode("utf-8")
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")

    monkeypatch.setitem(globals_, "_systemctl_state", systemctl_state)
    monkeypatch.setattr(globals_["subprocess"], "run", run)
    helper_sha256 = hashlib.sha256(helper_bytes).hexdigest()

    validate(helper_sha256, require_active_path=True)

    properties[service_name]["FragmentPath"] = "/run/systemd/system/shadow.service"
    with pytest.raises(namespace["BootstrapError"], match="PID 1"):
        validate(helper_sha256, require_active_path=True)
    properties[service_name]["FragmentPath"] = str(service_fragment)

    properties[service_name]["DropInPaths"] = "/run/systemd/system/service.d/evil.conf"
    with pytest.raises(namespace["BootstrapError"], match="PID 1"):
        validate(helper_sha256, require_active_path=True)
    properties[service_name]["DropInPaths"] = str(global_dropin)

    properties[service_name]["ActiveState"] = "failed"
    with pytest.raises(namespace["BootstrapError"], match="PID 1"):
        validate(helper_sha256, require_active_path=True)

    marker.write_text("consumed\n", encoding="ascii")
    properties[service_name]["ActiveState"] = "inactive"
    with pytest.raises(namespace["BootstrapError"], match="PID 1"):
        validate(helper_sha256, require_active_path=True)
    properties[service_name]["ActiveState"] = "activating"
    validate(helper_sha256, require_active_path=True)
    properties[service_name]["ActiveState"] = "active"
    with pytest.raises(namespace["BootstrapError"], match="PID 1"):
        validate(helper_sha256, require_active_path=True)


@pytest.mark.parametrize(
    "argv",
    [
        ["bootstrap", "gui-corrective-apply"],
        ["bootstrap", "--bootstrap-elevated-gui", "corrective-apply"],
        ["bootstrap", "--bootstrap-sealed-gui", "corrective-preflight"],
    ],
)
def test_corrective_gui_terminal_failure_keeps_mode_at_every_stage(
    argv: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    main = namespace["main"]
    globals_ = main.__globals__
    emitted: list[dict[str, object]] = []
    monkeypatch.setattr(globals_["sys"], "argv", argv)
    monkeypatch.setitem(
        globals_,
        "public_entry",
        lambda _args: (_ for _ in ()).throw(
            namespace["BootstrapError"]("enrollment_failed", "failed")
        ),
    )
    monkeypatch.setitem(
        globals_,
        "_emit_gui_event",
        lambda *_args, **kwargs: emitted.append(kwargs),
    )
    assert main() == 1
    assert emitted[-1]["operation_mode"] == "corrective"
    assert emitted[-1]["recovery_from_release_id"] == globals_[
        "CORRECTIVE_PREDECESSOR"
    ]["release_id"]


def test_gui_corrective_frame_binds_mode_predecessor_and_domain_separator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    read_request = namespace["_read_gui_apply_request"]
    globals_ = read_request.__globals__
    release = globals_["CORRECTIVE_TARGET_RELEASE_ID"]
    predecessor = globals_["CORRECTIVE_PREDECESSOR"]["release_id"]
    helper, manifest, source = "1" * 64, "2" * 64, "3" * 64
    hermes_archive = "4" * 64
    suffix = namespace["_corrective_suffix_for_identity"](
        release_id=release,
        helper_sha256=helper,
        manifest_sha256=manifest,
        source_sha256=source,
        hermes_source_archive_sha256=hermes_archive,
    )

    def frame(mode: str) -> bytearray:
        metadata = json.dumps(
            {
                "schema_version": 1,
                "confirmed_release_id": release,
                "confirmed_digest_suffix": suffix,
                "operation_mode": mode,
            },
            separators=(",", ":"),
        ).encode()
        one, two = b"OpenBao-password-123", b"Zulip-password-4567"
        return bytearray(
            b"ATLASBOOT1\n" + len(metadata).to_bytes(4, "big") + metadata
            + len(one).to_bytes(4, "big") + one
            + len(two).to_bytes(4, "big") + two
        )

    queued = frame("corrective")
    monkeypatch.setattr(globals_["select"], "select", lambda *_args: ([0], [], []))

    def read(_fd: int, size: int) -> bytes:
        chunk = bytes(queued[:size])
        del queued[:size]
        return chunk

    monkeypatch.setattr(globals_["os"], "read", read)
    openbao, zulip = read_request(
        release,
        helper,
        manifest,
        source,
        hermes_archive,
        operation_mode="corrective",
        recovery_from_release_id=predecessor,
    )
    assert bytes(openbao).startswith(b"OpenBao")
    assert bytes(zulip).startswith(b"Zulip")
    queued = frame("fresh")
    with pytest.raises(namespace["BootstrapError"], match="confirmation differs"):
        read_request(
            release,
            helper,
            manifest,
            source,
            hermes_archive,
            operation_mode="corrective",
            recovery_from_release_id=predecessor,
        )
    queued = frame("corrective")
    with pytest.raises(namespace["BootstrapError"], match="predecessor differs"):
        read_request(
            release,
            helper,
            manifest,
            source,
            hermes_archive,
            operation_mode="corrective",
            recovery_from_release_id="wrong-release",
        )

    public_source = __import__("inspect").getsource(namespace["public_entry"])
    sealed_gui = public_source[public_source.index('arguments[0] == "--bootstrap-sealed-gui"'):]
    assert sealed_gui.index("_harden_root_process()") < sealed_gui.index("_verify_sealed_self()")
    assert sealed_gui.index("_verify_sealed_self()") < sealed_gui.index("_validate_local_gui_session(")
    assert sealed_gui.index("_validate_local_gui_session(") < sealed_gui.index("load_reviewed_release(")
    assert sealed_gui.index("load_reviewed_release(") < sealed_gui.index("bootstrap_preflight(")
    assert sealed_gui.index("bootstrap_preflight(") < sealed_gui.index("_emit_gui_release(preflight)")
    assert sealed_gui.index("_emit_gui_release(preflight)") < sealed_gui.index("_emit_gui_secret_readiness(preflight)")
    assert sealed_gui.index("_emit_gui_secret_readiness(preflight)") < sealed_gui.index("_read_gui_apply_request(")


def test_consumed_audit_is_exact_once_across_every_first_append_frontier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    ensure = namespace["_ensure_bootstrap_consumed_audit_exact_once"]
    globals_ = ensure.__globals__
    transaction = new_transaction(namespace, monkeypatch)
    initial = json.loads(json.dumps(transaction))
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    transaction_path = state_root / "transaction.json"
    transaction_path.write_bytes(globals_["_canonical"](transaction) + b"\n")
    transaction_path.chmod(0o600)
    audit = state_root / "events.jsonl"
    consumed_path = tmp_path / "consumed.json"
    corrective_path = tmp_path / "corrective.json"
    consumed_at = int(transaction["updated_at"]) + 1
    consumed = {
        "schema_version": 1,
        "consumed": True,
        "consumed_at": consumed_at,
        "actor": "codex-supervised",
        "physical_uid": 1000,
        "physical_session": "3",
        "release_id": transaction["release_id"],
        "source_tree_sha256": transaction["source_tree_sha256"],
        "atlas_rpm_sha256": transaction["atlas_rpm_sha256"],
        "hermes_source_archive_sha256": transaction[
            "hermes_source_archive_sha256"
        ],
        "bootstrap_helper_sha256": "5" * 64,
        "manifest_sha256": "6" * 64,
        "initial_transaction": initial,
    }
    consumed_path.write_bytes(globals_["_canonical"](consumed) + b"\n")
    consumed_path.chmod(0o600)
    expected = namespace["_audit_record"](
        "bootstrap_consumed",
        str(transaction["release_id"]),
        consumed_at,
        "0" * 64,
        {"physical_uid": 1000, "physical_session": "3"},
    )
    expected_raw = globals_["_canonical"](expected) + b"\n"

    monkeypatch.setitem(globals_, "STATE_ROOT", state_root)
    monkeypatch.setitem(globals_, "TRANSACTION_PATH", transaction_path)
    monkeypatch.setitem(globals_, "AUDIT_PATH", audit)
    monkeypatch.setitem(globals_, "CONSUMED_PATH", consumed_path)
    monkeypatch.setitem(globals_, "CORRECTIVE_CONSUMED_PATH", corrective_path)
    monkeypatch.setattr(
        globals_["pwd"], "getpwnam", lambda _name: SimpleNamespace(pw_uid=1000)
    )
    real_lstat = os.lstat
    real_fstat = os.fstat
    monkeypatch.setattr(
        globals_["os"], "lstat", lambda path: _as_root_owned(real_lstat(path))
    )
    monkeypatch.setattr(
        globals_["os"], "fstat", lambda fd: _as_root_owned(real_fstat(fd))
    )

    # Absent (before open), empty (after O_CREAT), every mid-write byte, and
    # the fully fsynced record all converge to exactly one deterministic row.
    frontiers: list[bytes | None] = [None, b""]
    frontiers.extend(expected_raw[:boundary] for boundary in range(1, len(expected_raw)))
    frontiers.append(expected_raw)
    for frontier in frontiers:
        audit.unlink(missing_ok=True)
        if frontier is not None:
            audit.write_bytes(frontier)
            audit.chmod(0o600)
        assert ensure(transaction) == expected["record_sha256"]
        assert audit.read_bytes() == expected_raw
        # A second resume is a no-op, not a duplicate consumption event.
        assert ensure(transaction) == expected["record_sha256"]
        assert audit.read_bytes() == expected_raw

    duplicate = namespace["_audit_record"](
        "bootstrap_consumed",
        str(transaction["release_id"]),
        consumed_at,
        str(expected["record_sha256"]),
        {"physical_uid": 1000, "physical_session": "3"},
    )
    audit.write_bytes(expected_raw + globals_["_canonical"](duplicate) + b"\n")
    with pytest.raises(namespace["BootstrapError"], match="consumed audit"):
        ensure(transaction)
    audit.write_bytes(b'{"actor":"codex-supervised","event":"foreign')
    with pytest.raises(namespace["BootstrapError"], match="consumed-record prefix"):
        ensure(transaction)


def test_rollback_audit_exact_once_repairs_empty_and_first_record_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    append_once = namespace["_append_rollback_audit_exact_once"]
    globals_ = append_once.__globals__
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    audit = state_root / "events.jsonl"
    transaction = {"release_id": "release-1"}
    timestamp = 123
    expected = namespace["_audit_record"](
        "bootstrap_rolled_back",
        "release-1",
        timestamp,
        "0" * 64,
        {"password_recovery_persists": True},
    )
    expected_raw = globals_["_canonical"](expected) + b"\n"
    monkeypatch.setitem(globals_, "AUDIT_PATH", audit)
    monkeypatch.setattr(globals_["time"], "time", lambda: timestamp)
    real_lstat = os.lstat
    real_fstat = os.fstat
    monkeypatch.setattr(
        globals_["os"], "lstat", lambda path: _as_root_owned(real_lstat(path))
    )
    monkeypatch.setattr(
        globals_["os"], "fstat", lambda fd: _as_root_owned(real_fstat(fd))
    )

    for frontier in (b"", *(expected_raw[:i] for i in range(1, len(expected_raw)))):
        audit.write_bytes(frontier)
        audit.chmod(0o600)
        assert append_once(transaction) == expected["record_sha256"]
        assert audit.read_bytes() == expected_raw
        assert append_once(transaction) == expected["record_sha256"]
        assert audit.read_bytes() == expected_raw


def test_recovery_scaffold_transition_accepts_only_ordered_old_new_frontiers() -> None:
    namespace = bootstrap_namespace()
    validate = namespace["_validate_recovery_scaffold_transition"]
    globals_ = validate.__globals__
    assets = globals_["RECOVERY_ASSETS"]
    barriers = globals_["RECOVERY_BARRIER_ASSETS"]
    links = tuple(globals_["RECOVERY_ENABLEMENT_LINKS"])
    helper, guard_file, guard_unit, recovery_unit, janitor_unit = assets[:5]

    def blank() -> tuple[dict[Path, str], dict[Path, bool]]:
        return ({path: "missing" for path in assets}, {path: False for path in links})

    states, link_states = blank()
    states[helper] = states[janitor_unit] = "old"
    validate(
        states, link_states, predecessor_declared=True, successor_declared=False
    )

    # Exact .10 residue, helper replacement, each .11 core prefix, and the
    # retained .11 cleanup janitor are all recoverable frontiers.
    for prefix in range(0, 4):
        states, link_states = blank()
        states[janitor_unit] = "old"
        states[helper] = "old" if prefix == 0 else "new"
        for path in (guard_unit, recovery_unit)[: max(prefix - 1, 0)]:
            states[path] = "new"
        validate(
            states, link_states,
            predecessor_declared=True,
            successor_declared=True,
        )
    states, link_states = blank()
    states[helper] = states[janitor_unit] = "new"
    validate(
        states, link_states, predecessor_declared=True, successor_declared=True
    )
    states[guard_unit] = "new"
    validate(
        states, link_states, predecessor_declared=True, successor_declared=True
    )

    states, link_states = blank()
    for path in (helper, guard_unit, recovery_unit, janitor_unit):
        states[path] = "new"
    link_states[links[1]] = True
    link_states[links[2]] = True
    states[guard_file] = "new"
    for path in barriers:
        states[path] = "new"
    link_states[links[0]] = True
    validate(
        states, link_states, predecessor_declared=True, successor_declared=True
    )

    states, link_states = blank()
    states[recovery_unit] = "new"
    with pytest.raises(namespace["BootstrapError"], match="out of order"):
        validate(
            states, link_states,
            predecessor_declared=False,
            successor_declared=True,
        )
    states, link_states = blank()
    states[helper] = "old"
    states[guard_unit] = "new"
    with pytest.raises(namespace["BootstrapError"], match="out of order"):
        validate(
            states, link_states,
            predecessor_declared=True,
            successor_declared=True,
        )
    states, link_states = blank()
    states[helper] = states[guard_unit] = states[recovery_unit] = "new"
    link_states[links[1]] = True
    with pytest.raises(namespace["BootstrapError"], match="core was complete"):
        validate(
            states, link_states,
            predecessor_declared=False,
            successor_declared=True,
        )


def test_terminal_recovery_scaffold_cleanup_accepts_every_ordered_frontier() -> None:
    namespace = bootstrap_namespace()
    validate = namespace["_validate_recovery_scaffold_transition"]
    globals_ = validate.__globals__
    assets = tuple(globals_["RECOVERY_ASSETS"])
    barriers = tuple(globals_["RECOVERY_BARRIER_ASSETS"])
    links = tuple(globals_["RECOVERY_ENABLEMENT_LINKS"])
    helper, guard_file, guard_unit, recovery_unit, janitor_unit = assets[:5]
    guard_link, recovery_link, janitor_link = links

    def check(
        states: dict[Path, str],
        link_states: dict[Path, bool],
        flags: tuple[bool, bool],
    ) -> None:
        validate(
            states,
            link_states,
            predecessor_declared=False,
            successor_declared=True,
            cleanup_flags=flags,
        )

    # Fully armed cleanup: every barrier unlink is individually durable,
    # followed by guard/recovery disable, the three removable assets, the WAL
    # flag transition, proof reconciliation (same filesystem fingerprint),
    # and finally janitor disable.
    states = {path: "new" for path in assets}
    link_states = {path: True for path in links}
    check(states, link_states, (False, True))
    for barrier in barriers:
        states[barrier] = "missing"
        check(states, link_states, (False, True))
    link_states[guard_link] = False
    check(states, link_states, (False, True))
    link_states[recovery_link] = False
    check(states, link_states, (False, True))
    for path in (guard_file, guard_unit, recovery_unit):
        states[path] = "missing"
        check(states, link_states, (False, True))
    # The exact same files are valid on both sides of the durable flag/proof
    # boundary, but only the post-flag language permits janitor removal.
    check(states, link_states, (False, False))
    check(states, link_states, (False, False))
    link_states[janitor_link] = False
    check(states, link_states, (False, False))
    assert states[helper] == states[janitor_unit] == "new"

    # Cleanup can start after any prefix of barrier installation while the WAL
    # still says guard-installing.  Removing an installed prefix produces the
    # exact missing-prefix/new-suffix frontier used by the recovery replay.
    for installed_count in range(len(barriers) + 1):
        partial_states = {path: "missing" for path in assets}
        partial_links = {path: False for path in links}
        for path in (helper, guard_file, guard_unit, recovery_unit, janitor_unit):
            partial_states[path] = "new"
        partial_links[recovery_link] = True
        partial_links[janitor_link] = True
        for barrier in barriers[:installed_count]:
            partial_states[barrier] = "new"
        check(partial_states, partial_links, (True, False))
        for barrier in barriers[:installed_count]:
            partial_states[barrier] = "missing"
            check(partial_states, partial_links, (True, False))


def test_terminal_recovery_scaffold_cleanup_rejects_out_of_order_subsets() -> None:
    namespace = bootstrap_namespace()
    validate = namespace["_validate_recovery_scaffold_transition"]
    globals_ = validate.__globals__
    assets = tuple(globals_["RECOVERY_ASSETS"])
    barriers = tuple(globals_["RECOVERY_BARRIER_ASSETS"])
    links = tuple(globals_["RECOVERY_ENABLEMENT_LINKS"])
    guard_link, recovery_link, janitor_link = links

    def rejects(
        states: dict[Path, str],
        link_states: dict[Path, bool],
        flags: tuple[bool, bool],
    ) -> None:
        with pytest.raises(namespace["BootstrapError"], match="cleanup is out of order"):
            validate(
                states,
                link_states,
                predecessor_declared=False,
                successor_declared=True,
                cleanup_flags=flags,
            )

    states = {path: "new" for path in assets}
    link_states = {path: True for path in links}
    # Barrier cleanup is a missing prefix, never a hole or suffix deletion.
    states[barriers[1]] = "missing"
    rejects(states, link_states, (False, True))

    states = {path: "new" for path in assets}
    link_states = {path: True for path in links}
    for barrier in barriers:
        states[barrier] = "missing"
    # Primary links are disabled guard first, then main recovery.
    link_states[recovery_link] = False
    rejects(states, link_states, (False, True))

    states = {path: "new" for path in assets}
    link_states = {path: True for path in links}
    for barrier in barriers:
        states[barrier] = "missing"
    link_states[guard_link] = link_states[recovery_link] = False
    states[assets[2]] = "missing"
    # The guard drop-in must be removed before the guard unit.
    rejects(states, link_states, (False, True))

    states = {path: "new" for path in assets}
    link_states = {path: True for path in links}
    # Flags cannot become inert while primary authority remains.
    rejects(states, link_states, (False, False))

    for barrier in barriers:
        states[barrier] = "missing"
    link_states[guard_link] = link_states[recovery_link] = False
    for path in assets[1:4]:
        states[path] = "missing"
    # Conversely, janitor removal is after the WAL flag boundary, never while
    # the terminal transaction still declares an armed guard.
    link_states[janitor_link] = False
    rejects(states, link_states, (False, True))


def test_neutral_gui_recovery_classifies_exactly_one_mode_and_freezes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    classify = namespace["_classify_gui_recovery_mode"]
    globals_ = classify.__globals__
    manifest = {"release_id": "release-1"}
    corrective = {"enrollment_resume": None, "kind": "new"}

    monkeypatch.setitem(globals_, "_review_corrective_retry", lambda *_args: corrective)
    monkeypatch.setitem(
        globals_, "_review_enrollment_resume",
        lambda *_args: (_ for _ in ()).throw(
            namespace["BootstrapError"]("not_fresh", "not fresh")
        ),
    )
    assert classify(manifest, "1" * 64, "2" * 64, "3" * 64) == (
        "corrective", corrective, None
    )

    monkeypatch.setitem(
        globals_, "_review_corrective_retry",
        lambda *_args: (_ for _ in ()).throw(
            namespace["BootstrapError"]("not_corrective", "not corrective")
        ),
    )
    fresh = {"phase": "consumed"}
    monkeypatch.setitem(globals_, "_review_enrollment_resume", lambda *_args: fresh)
    assert classify(manifest, "1" * 64, "2" * 64, "3" * 64) == (
        "fresh", None, fresh
    )

    monkeypatch.setitem(globals_, "_review_corrective_retry", lambda *_args: corrective)
    with pytest.raises(namespace["BootstrapError"], match="no unique"):
        classify(manifest, "1" * 64, "2" * 64, "3" * 64)
    monkeypatch.setitem(globals_, "_review_enrollment_resume", lambda *_args: None)
    monkeypatch.setitem(
        globals_, "_review_corrective_retry",
        lambda *_args: (_ for _ in ()).throw(
            namespace["BootstrapError"]("none", "none")
        ),
    )
    with pytest.raises(namespace["BootstrapError"], match="no unique"):
        classify(manifest, "1" * 64, "2" * 64, "3" * 64)

    namespace["_set_gui_operation_context"]("gui-recovery-preflight")
    assert globals_["GUI_RESULT_IDENTITY"] == {
        "operation_mode": "recovery", "recovery_from_release_id": None
    }
    namespace["_freeze_gui_recovery_classification"]("fresh")
    assert globals_["GUI_RESULT_IDENTITY"] == {"operation_mode": "fresh"}
    with pytest.raises(namespace["BootstrapError"], match="classification changed"):
        namespace["_freeze_gui_recovery_classification"]("corrective")


def test_neutral_gui_recovery_keeps_minimal_context_on_every_early_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    entry = namespace["public_entry"]
    globals_ = entry.__globals__
    expected_context = {
        "operation_mode": "recovery",
        "recovery_from_release_id": None,
    }

    class StopStage(RuntimeError):
        pass

    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []
    monkeypatch.setitem(
        globals_, "_emit_gui_event", lambda *args, **kwargs: emitted.append((args, kwargs))
    )
    monkeypatch.setattr(globals_["os"], "getuid", lambda: 1000)
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 1000)
    monkeypatch.setitem(globals_, "_gui_channel_digest", lambda _uid: "b" * 64)
    monkeypatch.setitem(globals_, "_new_gui_atlas_binding", lambda _uid: "binding")
    monkeypatch.setitem(globals_, "_read_gui_launcher_memfd", lambda _uid: b"helper")
    monkeypatch.setitem(globals_, "_command_available", lambda _path: True)
    monkeypatch.setattr(
        globals_["subprocess"],
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        globals_["os"],
        "execve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(StopStage()),
    )
    with pytest.raises(StopStage):
        entry(["gui-recovery-preflight"])
    assert [event[0][1] for event in emitted] == ["authentication", "enrollment"]
    assert all(event[1] == expected_context for event in emitted)

    emitted.clear()
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    monkeypatch.setitem(globals_, "_harden_root_process", lambda: None)
    monkeypatch.setitem(globals_, "_verify_enrolled_self", lambda _sha: b"helper")
    monkeypatch.setitem(
        globals_, "_validate_local_gui_session", lambda *_args, **_kwargs: "session-1"
    )
    monkeypatch.setitem(globals_, "_cleanup_stale_enrolled_helpers", lambda _sha: None)
    monkeypatch.setitem(
        globals_,
        "_remove_enrolled_helper_exact",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setitem(
        globals_,
        "_seal_and_exec",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(StopStage()),
    )
    monkeypatch.setenv("PKEXEC_UID", "1000")
    with pytest.raises(StopStage):
        entry(
            [
                "--bootstrap-elevated-gui",
                "recovery-preflight",
                "a" * 64,
                "binding",
                "b" * 64,
            ]
        )
    assert emitted[0][0][1] == "authorization"
    assert emitted[0][1] == expected_context

    emitted.clear()
    monkeypatch.setitem(globals_, "_verify_sealed_self", lambda: None)
    monkeypatch.setitem(
        globals_,
        "load_reviewed_release",
        lambda _sha: (_ for _ in ()).throw(StopStage()),
    )
    with pytest.raises(StopStage):
        entry(
            [
                "--bootstrap-sealed-gui",
                "recovery-preflight",
                str(globals_["EXPECTED_SOURCE_ROOT"]),
                "1000",
                "session-1",
                "a" * 64,
                "binding",
                "b" * 64,
            ]
        )
    assert emitted[0][0][1] == "preflight"
    assert emitted[0][1] == expected_context


def test_neutral_gui_recovery_is_classified_before_identity_and_rechecked_under_lock() -> None:
    namespace = bootstrap_namespace()
    public = __import__("inspect").getsource(namespace["public_entry"])
    sealed = public[public.index('arguments[0] == "--bootstrap-sealed-gui"'):]
    classify = sealed.index("_classify_gui_recovery_mode(")
    preflight = sealed.index("preflight = bootstrap_preflight(")
    freeze = sealed.index("_freeze_gui_recovery_classification(classified_mode)")
    release = sealed.index("_emit_gui_release(preflight)")
    assert classify < preflight < freeze < release
    assert '"recovery-preflight", "recovery-apply"' in public
    apply_source = __import__("inspect").getsource(namespace["apply_bootstrap"])
    under_lock_review = apply_source.index("_classify_gui_recovery_mode(")
    repeated = apply_source.index("repeated = bootstrap_preflight(")
    boundary = apply_source.index("mutation_boundary()")
    assert under_lock_review < repeated < boundary


def test_neutral_gui_preflight_error_keeps_preidentity_recovery_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    main = namespace["main"]
    globals_ = main.__globals__
    emitted: list[tuple[tuple[object, ...], dict[str, object]]] = []
    root = globals_["EXPECTED_SOURCE_ROOT"]
    helper_sha256 = "a" * 64
    channel_digest = "b" * 64
    manifest = {
        "bootstrap": {
            "gui_protocol": {"atlas_payload_sha256": "c" * 64}
        }
    }
    transaction = {"phase": "consumed"}
    monkeypatch.setattr(
        globals_["sys"],
        "argv",
        [
            "bootstrap",
            "--bootstrap-sealed-gui",
            "recovery-preflight",
            str(root),
            "1000",
            "session-1",
            helper_sha256,
            "atlas-binding",
            channel_digest,
        ],
    )
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    monkeypatch.setitem(globals_, "_harden_root_process", lambda: None)
    monkeypatch.setitem(globals_, "_verify_sealed_self", lambda: None)
    monkeypatch.setitem(
        globals_, "_remove_enrolled_helper_exact", lambda *_args, **_kwargs: True
    )
    monkeypatch.setitem(
        globals_, "_validate_local_gui_session", lambda *_args, **_kwargs: "session-1"
    )
    monkeypatch.setitem(
        globals_,
        "_emit_gui_event",
        lambda *args, **kwargs: emitted.append((args, kwargs)),
    )
    monkeypatch.setitem(
        globals_,
        "load_reviewed_release",
        lambda _sha: (manifest, "d" * 64, "e" * 64, b"worker"),
    )
    monkeypatch.setitem(globals_, "_validate_gui_atlas_binding", lambda *_args: None)
    monkeypatch.setitem(
        globals_,
        "_classify_gui_recovery_mode",
        lambda *_args: ("fresh", None, transaction),
    )
    monkeypatch.setitem(
        globals_,
        "bootstrap_preflight",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            namespace["BootstrapError"](
                "network_unavailable", "preflight deliberately failed"
            )
        ),
    )

    assert main() == 1
    assert emitted[-1][0][0] == "result"
    assert emitted[-1][1] == {
        "error_code": "network_unavailable",
        "operation_mode": "recovery",
        "recovery_from_release_id": None,
    }


def test_recovery_entry_finalizes_preproduction_pending_without_services_or_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    namespace = bootstrap_namespace()
    entry = namespace["recovery_entry"]
    globals_ = entry.__globals__
    helper = tmp_path / "helper"
    helper.write_text("helper", encoding="utf-8")
    helper.chmod(0o700)
    state_root = tmp_path / "state"
    state_root.mkdir(mode=0o700)
    (state_root / "transaction.json").write_text("{}", encoding="utf-8")
    corrective = tmp_path / "corrective.json"
    lock_fd = os.open(tmp_path / "lock", os.O_WRONLY | os.O_CREAT, 0o600)
    transaction = {
        "phase": globals_["PREPRODUCTION_ROLLBACK_AUDIT_PHASE"],
        "status": "rollback-failed",
    }
    events: list[str] = []
    real_lstat = os.lstat
    real_stat = os.stat
    monkeypatch.setitem(globals_, "INSTALLED_HELPER", helper)
    monkeypatch.setitem(globals_, "STATE_ROOT", state_root)
    monkeypatch.setitem(globals_, "CORRECTIVE_CONSUMED_PATH", corrective)
    monkeypatch.setitem(globals_, "__file__", str(helper))
    monkeypatch.setattr(globals_["os"], "geteuid", lambda: 0)
    monkeypatch.setattr(
        globals_["os"], "lstat",
        lambda path: _as_root_owned(real_lstat(helper if Path(path) == helper else path)),
    )
    monkeypatch.setattr(
        globals_["os"], "stat",
        lambda path, *a, **k: real_stat(helper if Path(path) == helper else path, *a, **k),
    )
    monkeypatch.setitem(
        globals_, "_read_standard_transaction_frontier", lambda **_kwargs: transaction
    )
    monkeypatch.setitem(globals_, "_acquire_lock", lambda **_kwargs: lock_fd)
    monkeypatch.setitem(
        globals_, "_ensure_bootstrap_consumed_audit_exact_once",
        lambda _tx: events.append("consumed-audit") or "0" * 64,
    )
    monkeypatch.setitem(
        globals_, "_preproduction_recovery_frontier_is_unchanged", lambda _tx: True
    )
    monkeypatch.setitem(
        globals_, "_validate_preproduction_recovery_scaffold_readonly",
        lambda _tx: events.append("scaffold"),
    )
    monkeypatch.setitem(
        globals_, "_finalize_rollback_transaction",
        lambda _tx, **_kwargs: events.append("finalize"),
    )
    monkeypatch.setitem(
        globals_, "_cleanup_recovery_guard", lambda _tx: events.append("cleanup")
    )
    monkeypatch.setitem(
        globals_, "_verified_staged_worker",
        lambda _tx: (_ for _ in ()).throw(AssertionError("worker loaded")),
    )
    try:
        assert entry(openbao_only=False) == 0
    finally:
        try:
            os.close(lock_fd)
        except OSError:
            pass
    assert events == ["consumed-audit", "scaffold", "finalize", "cleanup"]


def test_recovery_timeout_budget_covers_all_bounded_restore_attempts() -> None:
    recovery = (
        ROOT / "deploy/control-plane/systemd/ops-control-plane-bootstrap-recovery.service"
    ).read_text(encoding="utf-8")
    activate = (
        ROOT
        / "deploy/control-plane/systemd/ops-control-plane-bootstrap-recovery-activate.service"
    ).read_text(encoding="utf-8")
    helper = BOOTSTRAP.read_text(encoding="utf-8")
    worker = WORKER.read_text(encoding="utf-8")
    assert "TimeoutStartSec=6h" in recovery
    assert "TimeoutStartSec=2h" in activate
    assert "TimeoutStartSec=13h" in helper
    assert '"--property=RuntimeMaxSec=13h"' in helper
    assert "6 * 60 * 60 + 10 * 60" in helper
    assert worker.count("timeout=1800") >= 3
    assert worker.count("timeout=600") >= 3
    assert 3 * 1800 + 3 * 600 + 3600 < 6 * 60 * 60
    assert 21_300 + 22_200 < 13 * 60 * 60


def test_bootstrap_isolated_shebang_blocks_neighbor_json_from_reading_stdin(
    tmp_path: Path,
) -> None:
    helper = tmp_path / "control-plane-bootstrap"
    helper.write_bytes(BOOTSTRAP.read_bytes())
    helper.chmod(0o700)
    captured = tmp_path / "intercepted-secret"
    (tmp_path / "json.py").write_text(
        "import sys\n"
        f"open({str(captured)!r}, 'wb').write(sys.stdin.buffer.read())\n"
        "raise RuntimeError('neighbor json.py was imported')\n",
        encoding="utf-8",
    )
    secret = b"MUST-NOT-BE-READ-BY-NEIGHBOR-JSON"
    environment = {
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONPATH": str(tmp_path),
    }

    isolated = subprocess.run(
        [str(helper), "invalid-operation"],
        input=secret,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env=environment,
        check=False,
        timeout=15,
    )
    assert isolated.returncode == 64
    assert json.loads(isolated.stderr) == {
        "error": "usage",
        "ok": False,
        "status": "failed",
    }
    assert not captured.exists()
    assert secret not in isolated.stdout + isolated.stderr

    nonisolated = subprocess.run(
        ["/usr/bin/python3", str(helper), "invalid-operation"],
        input=secret,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env=environment,
        check=False,
        timeout=15,
    )
    assert nonisolated.returncode == 78
    assert nonisolated.stdout == b""
    assert nonisolated.stderr == b""
    assert not captured.exists()


def test_bootstrap_executes_every_real_python_entry_in_isolated_mode() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")
    assert source.startswith("#!/usr/bin/python3 -I\n")
    assert source.index('if __name__ == "__main__" and not sys.flags.isolated:') < source.index(
        "import json"
    )
    namespace = bootstrap_namespace()
    seal_source = __import__("inspect").getsource(namespace["_seal_and_exec"])
    assert seal_source.count('"/usr/bin/python3", "-I", path') == 2
