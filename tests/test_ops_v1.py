from __future__ import annotations

import datetime as dt
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tarfile
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ops-v1"
loader = importlib.machinery.SourceFileLoader("ops_v1_script", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None
ops = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = ops
loader.exec_module(ops)

daily_loader = importlib.machinery.SourceFileLoader("ops_daily_report_script", str(ROOT / "scripts/ops-daily-report.py"))
daily_spec = importlib.util.spec_from_loader(daily_loader.name, daily_loader)
assert daily_spec is not None
daily = importlib.util.module_from_spec(daily_spec)
sys.modules[daily_loader.name] = daily
daily_loader.exec_module(daily)


def write_json(path: Path, value: object, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(mode)


def create_tar(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w") as archive:
        for name, contents in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            info.mode = 0o640
            archive.addfile(info, io.BytesIO(contents))


def release_fixture(tmp_path: Path, *, health_digest: str | None = None) -> tuple[Path, Path, Path, dict[str, Path]]:
    roots = {name: tmp_path / name for name in ("incoming", "releases", "persistent", "approvals", "audit", "locks")}
    for path in roots.values():
        path.mkdir()
    roots["approvals"].chmod(0o700)
    (roots["persistent"] / "data").mkdir()
    (roots["persistent"] / "data" / "keep.txt").write_text("persistent", encoding="utf-8")
    old = roots["releases"] / "old-1"
    old.mkdir()
    (old / "health.txt").write_text("healthy", encoding="utf-8")
    write_json(
        old / ".ops-release.json",
        {"schema_version": 1, "release_id": "old-1", "plan_hash": "a" * 64},
    )
    current = tmp_path / "current"
    current.symlink_to(old, target_is_directory=True)
    artifact = roots["incoming"] / "new.tar"
    create_tar(artifact, {"health.txt": b"healthy", "bin/app": b"release"})
    backup_evidence = tmp_path / "backup-evidence.json"
    write_json(
        backup_evidence,
        {
            "schema_version": 1,
            "resource_id": "gamebox",
            "target_id": "app-test",
            "completed_at": ops.isoformat(),
            "status": "success",
            "integrity_verified": True,
            "snapshot_id": "snapshot-before-new-2",
        },
    )
    expected_health = health_digest or ops.sha256_bytes(b"healthy")
    target = {
        "id": "app-test",
        "enabled": True,
        "project": "minecraft",
        "resource_id": "gamebox",
        "environment": "test",
        "incoming_root": str(roots["incoming"]),
        "releases_root": str(roots["releases"]),
        "persistent_root": str(roots["persistent"]),
        "current_link": str(current),
        "persistent_paths": ["data"],
        "service_units": [],
        "health_checks": [
            {
                "id": "release-sentinel",
                "type": "file",
                "enabled": True,
                "project": "minecraft",
                "resource_id": "gamebox",
                "timeout_seconds": 3,
                "path": str(current / "health.txt"),
                "sha256": expected_health,
            }
        ],
        "min_free_bytes": 0,
        "max_unpacked_bytes": 1048576,
        "backup": {
            "required": True,
            "evidence_path": str(backup_evidence),
            "maximum_age_seconds": 86400,
            "require_integrity": True,
        },
        "rollback_automatic": True,
        "approvers": ["test-reviewer"],
        "audit_events_path": str(roots["audit"] / "events.jsonl"),
        "lock_root": str(roots["locks"]),
    }
    targets = tmp_path / "targets.json"
    write_json(targets, {"schema_version": 1, "targets": [target]})
    manifest_data = {
        "schema_version": 1,
        "release_id": "new-2",
        "target_id": "app-test",
        "project": "minecraft",
        "version": "2.0.0",
        "commit": "0123456789abcdef",
        "build": "build-2",
        "change_ticket": "mission-123",
        "artifact": {"path": str(artifact), "format": "tar", "sha256": ops.sha256_file(artifact)},
        "migration": "none",
        "persistent_paths": ["data"],
    }
    manifest = tmp_path / "manifest.json"
    write_json(manifest, manifest_data)
    plan, _, _ = ops.build_release_plan(manifest, targets)
    now = ops.utcnow()
    approval = {
        "schema_version": 1,
        "action_class": "C",
        "action": "ops.deploy_release.v1",
        "resource_id": "gamebox",
        "target_id": "app-test",
        "plan_hash": plan["plan_hash"],
        "approved_by": "test-reviewer",
        "approved_at": ops.isoformat(now - dt.timedelta(seconds=1)),
        "expires_at": ops.isoformat(now + dt.timedelta(minutes=10)),
        "approval_id": "approval-test-1",
    }
    write_json(roots["approvals"] / f"{plan['plan_hash']}.json", approval)
    return manifest, targets, current, roots


def minimal_inventory(checks: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_from": ["test"],
        "resources": [
            {
                "id": "node-1",
                "kind": "host",
                "provider": "local",
                "os_family": "linux",
                "project": "infra-shared",
                "environment": "test",
                "criticality": "high",
                "connection_alias": "local",
                "management_enabled": True,
                "discovery_enabled": True,
                "expected_services": ["app.service"],
                "shared_by": ["infra-shared"],
            }
        ],
        "checks": checks,
    }


def test_repository_contracts_inventory_and_skills_validate() -> None:
    contracts = ops.validate_contract_catalog(ROOT / "runbooks/contracts/catalog.json")
    inventory = ops.validate_inventory(ROOT / "inventory/ops-v1.json")
    skills = ops.validate_skills(ROOT / "config/hermes/skill-registry.json", ROOT / "config/hermes/skills", ROOT)
    assert contracts["contracts"] == 10
    assert len(inventory["resources"]) == 5
    assert all(not item["management_enabled"] for item in inventory["resources"] if item["connection_alias"] != "local")
    assert skills["count"] == 5


def test_disabled_check_never_probes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    check = {
        "id": "remote-port",
        "type": "tcp",
        "enabled": False,
        "project": "infra-shared",
        "resource_id": "node-1",
        "timeout_seconds": 1,
        "hostname": "example.invalid",
        "port": 443,
    }
    inventory = tmp_path / "inventory.json"
    write_json(inventory, minimal_inventory([check]))
    monkeypatch.setitem(ops.PROBES, "tcp", lambda _: pytest.fail("disabled check was contacted"))
    result = ops.execute_checks(inventory, probe=True)
    assert result["results"][0]["status"] == "skipped"
    assert result["results"][0]["reason"] == "disabled"


def test_backup_evidence_is_bounded_and_scoped(tmp_path: Path) -> None:
    evidence = tmp_path / "backup.json"
    write_json(
        evidence,
        {
            "schema_version": 1,
            "resource_id": "node-1",
            "target_id": "backup-main",
            "completed_at": ops.isoformat(),
            "status": "success",
            "integrity_verified": True,
            "snapshot_id": "snapshot-1",
        },
    )
    check = {
        "id": "backup-check",
        "type": "backup",
        "enabled": True,
        "project": "infra-shared",
        "resource_id": "node-1",
        "timeout_seconds": 2,
        "evidence_path": str(evidence),
        "maximum_age_seconds": 3600,
        "require_integrity": True,
    }
    inventory = tmp_path / "inventory.json"
    write_json(inventory, minimal_inventory([check]))
    result = ops.execute_checks(inventory, probe=True)
    assert result["summary"] == {"ok": 1}
    check["resource_id"] = "other"
    with pytest.raises(ops.OpsError, match="another resource"):
        ops._probe_backup(check)


def test_discovery_reports_drift_without_mutating_inventory(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inventory.json"
    source = minimal_inventory([])
    write_json(inventory_path, source)
    before = inventory_path.read_bytes()
    observation = tmp_path / "observation.json"
    write_json(
        observation,
        {
            "schema_version": 1,
            "generated_at": ops.isoformat(),
            "collector": "test-collector",
            "inventory_sha256": ops.sha256_file(inventory_path),
            "resources": [{"id": "node-1", "reachable": True, "services": []}],
        },
    )
    result = ops.compare_discovery(inventory_path, observation)
    assert result["inventory_changed"] is False
    assert result["anomalies"][0]["kind"] == "missing_service"
    assert inventory_path.read_bytes() == before


def test_release_pipeline_atomic_switch_and_persistence(tmp_path: Path) -> None:
    manifest, targets, current, roots = release_fixture(tmp_path)
    result = ops.apply_release(manifest, targets, roots["approvals"], execute=True)
    assert result["ok"] is True
    assert current.resolve().name == "new-2"
    assert (current / "health.txt").read_text(encoding="utf-8") == "healthy"
    assert (current / "data").is_symlink()
    assert (current / "data" / "keep.txt").read_text(encoding="utf-8") == "persistent"
    events = ops.read_events(roots["audit"] / "events.jsonl")
    assert [event["event"] for event in events] == ["release_apply_started", "release_apply_succeeded"]


def test_release_health_failure_rolls_back_once(tmp_path: Path) -> None:
    manifest, targets, current, roots = release_fixture(tmp_path, health_digest="f" * 64)
    with pytest.raises(ops.OpsError) as raised:
        ops.apply_release(manifest, targets, roots["approvals"], execute=True)
    assert raised.value.code == "health_failed"
    assert raised.value.details["rollback"] == "completed"
    assert current.resolve().name == "old-1"
    events = ops.read_events(roots["audit"] / "events.jsonl")
    assert events[-1]["event"] == "release_apply_failed"
    assert events[-1]["rollback"] == "completed"


def test_explicit_rollback_is_separately_planned_and_approved(tmp_path: Path) -> None:
    manifest, targets, current, roots = release_fixture(tmp_path)
    ops.apply_release(manifest, targets, roots["approvals"], execute=True)
    plan, _target, _destination = ops.build_rollback_plan(targets, "app-test", "old-1")
    now = ops.utcnow()
    approval = {
        "schema_version": 1,
        "action_class": "C",
        "action": "ops.rollback_release.v1",
        "resource_id": "gamebox",
        "target_id": "app-test",
        "plan_hash": plan["plan_hash"],
        "approved_by": "test-reviewer",
        "approved_at": ops.isoformat(now - dt.timedelta(seconds=1)),
        "expires_at": ops.isoformat(now + dt.timedelta(minutes=10)),
        "approval_id": "approval-rollback-1",
    }
    write_json(roots["approvals"] / f"{plan['plan_hash']}.json", approval)
    result = ops.apply_rollback(targets, "app-test", "old-1", roots["approvals"], execute=True)
    assert result["ok"] is True
    assert current.resolve().name == "old-1"
    assert ops.read_events(roots["audit"] / "events.jsonl")[-1]["event"] == "release_rollback_succeeded"


def test_release_requires_execute_and_exact_checksum(tmp_path: Path) -> None:
    manifest, targets, _current, roots = release_fixture(tmp_path)
    with pytest.raises(ops.OpsError) as raised:
        ops.apply_release(manifest, targets, roots["approvals"], execute=False)
    assert raised.value.code == "execute_required"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["artifact"]["sha256"] = "0" * 64
    write_json(manifest, payload)
    with pytest.raises(ops.OpsError) as mismatch:
        ops.build_release_plan(manifest, targets)
    assert mismatch.value.code == "checksum_mismatch"


def test_archive_traversal_and_persistent_collision_are_rejected(tmp_path: Path) -> None:
    manifest, targets, _current, _roots = release_fixture(tmp_path)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    artifact = Path(payload["artifact"]["path"])
    create_tar(artifact, {"../escape": b"bad"})
    payload["artifact"]["sha256"] = ops.sha256_file(artifact)
    write_json(manifest, payload)
    with pytest.raises(ops.OpsError) as traversal:
        ops.build_release_plan(manifest, targets)
    assert traversal.value.code == "unsafe_archive"
    create_tar(artifact, {"data/user.db": b"overwrite"})
    payload["artifact"]["sha256"] = ops.sha256_file(artifact)
    write_json(manifest, payload)
    with pytest.raises(ops.OpsError) as collision:
        ops.build_release_plan(manifest, targets)
    assert collision.value.code == "persistent_collision"


def test_config_diff_validates_and_redacts(tmp_path: Path) -> None:
    current = tmp_path / "current.conf"
    current.write_text("workers=2\npassword=old\n", encoding="utf-8")
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    candidate = candidates / "next.conf"
    candidate.write_text("workers=4\npassword=new\n", encoding="utf-8")
    registry = tmp_path / "configs.json"
    write_json(
        registry,
        {
            "schema_version": 1,
            "targets": [
                {
                    "id": "app-config",
                    "enabled": True,
                    "project": "infra-shared",
                    "resource_id": "node-1",
                    "current_path": str(current),
                    "candidate_root": str(candidates),
                    "validator": "text",
                    "max_bytes": 4096,
                    "lock_root": str(tmp_path / "locks"),
                }
            ],
        },
    )
    result = ops.config_diff(registry, "app-config", candidate)
    assert result["changed"] is True
    assert "workers=4" in result["diff"]
    assert "password=new" not in result["diff"]
    assert result["redacted_lines"] == 2
    assert result["applied"] is False


def test_retention_only_plans_disabled_policy() -> None:
    result = ops.retention_plan(ROOT / "monitoring/ops-v1-retention.json")
    assert result["candidate_count"] == 0
    assert result["applied"] is False


def test_skill_promotion_requires_evidence_and_writes_candidate(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.json"
    write_json(
        evidence,
        {
            "schema_version": 1,
            "skill": "ops-deterministic-workflows",
            "version": 1,
            "tests_passed": True,
            "inspection_approved": True,
            "permissions_reviewed": True,
            "reviewed_by": "reviewer-1",
            "git_commit": "0123456789abcdef",
            "approval_id": "approval-skill-1",
            "executions": {"successful": 3, "failed": 0},
            "created_at": ops.isoformat(),
        },
    )
    candidate = tmp_path / "candidate.json"
    result = ops.promote_skill_candidate(
        ROOT / "config/hermes/skill-registry.json",
        ROOT / "config/hermes/skills",
        ROOT,
        "ops-deterministic-workflows",
        "semi-automatic",
        evidence,
        candidate,
    )
    assert result["installed"] is False
    validated, _ = ops.load_skill_registry(candidate, ROOT / "config/hermes/skills", ROOT)
    promoted = next(item for item in validated["skills"] if item["name"] == "ops-deterministic-workflows")
    assert promoted["automation"] == "semi-automatic"


def test_report_is_short_and_emits_metrics(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    ops.append_event(events, {"event": "release_apply_succeeded", "phase": "action", "project": "test", "resource_id": "node-1"})
    checks = tmp_path / "checks.json"
    write_json(checks, {"schema_version": 1, "results": [{"status": "ok", "id": "one"}]})
    short, detail, metrics = ops.build_report(events, checks)
    assert len(short) < 240
    assert detail["actions"]["counts"]["release_apply_succeeded"] == 1
    assert "ops_v1_check_results" in metrics


def test_resource_lock_refuses_concurrent_writer(tmp_path: Path) -> None:
    with ops.resource_lock("resource-1", tmp_path):
        with pytest.raises(ops.OpsError) as raised:
            with ops.resource_lock("resource-1", tmp_path):
                pass
    assert raised.value.code == "resource_locked"


def test_queue_orders_priority_then_fifo_and_never_executes(tmp_path: Path) -> None:
    queue = tmp_path / "queue.json"
    write_json(
        queue,
        {
            "schema_version": 1,
            "missions": [
                {"id": "normal-old", "kind": "maintenance", "priority": "normal", "created_at": "2026-09-04T10:00:00Z", "status": "queued", "resource_ids": ["node-1"]},
                {"id": "critical-new", "kind": "total-outage", "priority": "critical", "created_at": "2026-09-04T11:00:00Z", "status": "queued", "resource_ids": ["node-2"]},
                {"id": "critical-old", "kind": "security-incident", "priority": "critical", "created_at": "2026-09-04T09:00:00Z", "status": "queued", "resource_ids": ["node-3"]},
                {"id": "blocked", "kind": "database-unavailable", "priority": "critical", "created_at": "2026-09-04T08:00:00Z", "status": "blocked", "resource_ids": ["node-4"]},
            ],
        },
    )
    result = ops.order_mission_queue(ROOT / "inventory/mission-priorities.json", queue)
    assert [item["id"] for item in result["ordered_missions"]] == ["critical-old", "critical-new", "normal-old", "blocked"]
    assert result["next_mission_id"] == "critical-old"
    assert result["max_concurrent_actions"] == 1
    assert result["executed"] is False


def test_install_assets_are_dormant_and_units_use_fixed_argv() -> None:
    installer = (ROOT / "scripts/install-ops-v1.sh").read_text(encoding="utf-8")
    observe = (ROOT / "systemd/ops-v1-observe.service").read_text(encoding="utf-8")
    report = (ROOT / "systemd/ops-v1-report.service").read_text(encoding="utf-8")
    assert "if [[ $enable_observation -eq 1 ]]" in installer
    assert "systemctl enable --now ops-v1-observe.timer ops-v1-report.timer" in installer
    assert "systemctl disable ops-v1-observe.timer ops-v1-report.timer" in installer
    assert "--probe --output /var/lib/ops-v1/checks/latest.json" in observe
    assert "NoNewPrivileges=yes" in observe
    assert "RestrictAddressFamilies=AF_UNIX" in report
    assert "bash -c" not in observe + report


def test_main_daily_report_includes_ops_v1_facts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backup_timestamp = time.time() - 60
    values = {
        "sum(ops_service_up)": 6,
        "count(ops_service_up)": 6,
        "ops_nvme_smart_healthy": 1,
        "ops_root_filesystem_encrypted": 1,
        "ops_openbao_initialized": 1,
        "ops_openbao_sealed": 0,
        "ops_v1_enabled_checks": 2,
        "ops_v1_check_failures": 0,
        "ops_v1_check_result_timestamp_seconds": time.time() - 60,
        "ops_aide_database_present": 1,
        "ops_aide_last_check_changes_detected": 0,
        "ops_aide_last_check_error": 0,
        "ops_broker_backup_last_verify_ok": 1,
        "ops_broker_backup_last_success_timestamp_seconds": backup_timestamp,
        "ops_memory_backup_last_result_ok": 1,
        "ops_memory_backup_last_success_timestamp_seconds": backup_timestamp,
        "ops_memory_restore_test_last_result_ok": 1,
        "ops_memory_restore_test_last_success_timestamp_seconds": backup_timestamp,
        "ops_openbao_backup_last_verify_ok": 1,
        "ops_openbao_backup_last_success_timestamp_seconds": backup_timestamp,
    }
    monkeypatch.setattr(daily, "scalar", lambda expression: values.get(expression))
    monkeypatch.setattr(daily, "get_json", lambda _path: {"data": {"alerts": []}})
    monkeypatch.setattr(daily, "OUTPUT", tmp_path)
    assert daily.main() == 0
    payload = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert payload["ops_v1"]["enabled_checks"] == 2
    assert payload["ops_v1"]["failures"] == 0
    assert all(item["status"] == "ok" for item in payload["backups"].values())
    assert "Sauvegardes locales vérifiées" in payload["summary"]
    assert "Sondes distantes non activees" not in payload["summary"]
    assert "2 sonde(s) Ops OK" in payload["summary"]


def test_daily_report_marks_absent_failed_and_stale_local_backup_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = time.time()
    values = {
        "sum(ops_service_up)": 4,
        "count(ops_service_up)": 4,
        "ops_nvme_smart_healthy": 1,
        "ops_root_filesystem_encrypted": 1,
        "ops_openbao_initialized": 1,
        "ops_openbao_sealed": 0,
        "ops_aide_database_present": 1,
        "ops_aide_last_check_changes_detected": 0,
        "ops_aide_last_check_error": 0,
        "ops_broker_backup_last_verify_ok": 0,
        "ops_broker_backup_last_success_timestamp_seconds": now - 10,
        "ops_memory_backup_last_result_ok": 1,
        "ops_memory_backup_last_success_timestamp_seconds": now - daily.BACKUP_FRESHNESS_SECONDS - 1,
        "ops_memory_restore_test_last_result_ok": 1,
        "ops_memory_restore_test_last_success_timestamp_seconds": now - 10,
        # Deliberately no OpenBao Raft metrics: a report must not imply success.
    }
    monkeypatch.setattr(daily, "scalar", lambda expression: values.get(expression))
    monkeypatch.setattr(daily, "get_json", lambda _path: {"data": {"alerts": []}})
    monkeypatch.setattr(daily, "OUTPUT", tmp_path)

    assert daily.main() == 0
    payload = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert payload["backups"]["broker"]["status"] == "failed"
    assert payload["backups"]["memory_backup"]["status"] == "stale"
    assert payload["backups"]["memory_restore_test"]["status"] == "ok"
    assert payload["backups"]["openbao_raft"]["status"] == "absent"
    assert "État local à vérifier" in payload["summary"]
    assert "broker en échec" in payload["summary"]
    assert "backup mémoire périmé" in payload["summary"]
    assert "Raft OpenBao absent" in payload["summary"]
    assert "Sondes distantes non activees" in payload["summary"]
