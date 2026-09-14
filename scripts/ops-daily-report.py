#!/usr/bin/env python3
"""Create a short, deterministic daily control-plane report."""

from __future__ import annotations

import json
import os
import tempfile
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


PROMETHEUS = "http://127.0.0.1:9090"
OUTPUT = Path(os.environ.get("OPS_REPORT_DIR", "/var/lib/ops-reports/daily"))
BACKUP_FRESHNESS_SECONDS = 108_000
RESTORE_TEST_FRESHNESS_SECONDS = 777_600


def backup_health(
    *,
    now: datetime,
    latest_result_ok: float | None,
    last_success_timestamp: float | None,
    freshness_seconds: int,
) -> dict[str, Any]:
    """Classify one deterministic backup job without treating missing data as OK."""
    timestamp = (
        last_success_timestamp
        if last_success_timestamp is not None and last_success_timestamp > 0
        else None
    )
    age_seconds = max(0.0, now.timestamp() - timestamp) if timestamp else None
    if latest_result_ok is None or timestamp is None:
        status = "absent"
    elif latest_result_ok != 1:
        status = "failed"
    elif age_seconds is not None and age_seconds > freshness_seconds:
        status = "stale"
    else:
        status = "ok"
    return {
        "status": status,
        "latest_result_ok": latest_result_ok == 1 if latest_result_ok is not None else None,
        "last_success_timestamp_seconds": timestamp,
        "age_seconds": age_seconds,
        "freshness_seconds": freshness_seconds,
    }


def backup_summary(health: dict[str, dict[str, Any]]) -> str:
    """Return one mobile-sized, local-only backup sentence."""
    labels = {
        "broker": "broker",
        "memory_backup": "backup mémoire",
        "memory_restore_test": "restauration mémoire",
        "openbao_raft": "Raft OpenBao",
    }
    wording = {"absent": "absent", "failed": "en échec", "stale": "périmé"}
    problems = [
        f"{labels[name]} {wording[state['status']]}"
        for name, state in health.items()
        if state["status"] != "ok"
    ]
    if problems:
        return "Sauvegardes locales : " + "; ".join(problems) + "."
    return "Sauvegardes locales vérifiées ; restauration mémoire OK."


def get_json(path: str) -> dict[str, Any]:
    request = urllib.request.Request(PROMETHEUS + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=5) as response:
        if response.status != 200:
            raise RuntimeError("monitoring API unavailable")
        payload = response.read(1_048_577)
    if len(payload) > 1_048_576:
        raise RuntimeError("monitoring response exceeded limit")
    parsed = json.loads(payload)
    if parsed.get("status") != "success":
        raise RuntimeError("monitoring query failed")
    return parsed


def scalar(expression: str) -> float | None:
    query = urllib.parse.urlencode({"query": expression})
    result = get_json(f"/api/v1/query?{query}")["data"]["result"]
    if not result:
        return None
    return float(result[0]["value"][1])


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main() -> int:
    now = datetime.now(UTC)
    services_up = int(scalar("sum(ops_service_up)") or 0)
    services_total = int(scalar("count(ops_service_up)") or 0)
    disk_pct = scalar(
        '100 * (1 - node_filesystem_avail_bytes{job="dell-ops",mountpoint="/"} '
        '/ node_filesystem_size_bytes{job="dell-ops",mountpoint="/"})'
    )
    smart_ok = scalar("ops_nvme_smart_healthy")
    battery = scalar("ops_battery_health_ratio")
    root_encrypted = scalar("ops_root_filesystem_encrypted")
    hermes_credential_present = scalar("ops_hermes_openbao_credential_present")
    hermes_credential_age = scalar("ops_hermes_openbao_secret_id_age_seconds")
    zulip_credential_present = scalar("ops_zulip_openbao_credential_present")
    zulip_credential_age = scalar("ops_zulip_openbao_secret_id_age_seconds")
    aide_database = scalar("ops_aide_database_present")
    aide_changes = scalar("ops_aide_last_check_changes_detected")
    aide_error = scalar("ops_aide_last_check_error")
    bao_initialized = scalar("ops_openbao_initialized")
    bao_sealed = scalar("ops_openbao_sealed")
    broker_backup = backup_health(
        now=now,
        latest_result_ok=scalar("ops_broker_backup_last_verify_ok"),
        last_success_timestamp=scalar("ops_broker_backup_last_success_timestamp_seconds"),
        freshness_seconds=BACKUP_FRESHNESS_SECONDS,
    )
    memory_backup = backup_health(
        now=now,
        latest_result_ok=scalar("ops_memory_backup_last_result_ok"),
        last_success_timestamp=scalar("ops_memory_backup_last_success_timestamp_seconds"),
        freshness_seconds=BACKUP_FRESHNESS_SECONDS,
    )
    memory_restore_test = backup_health(
        now=now,
        latest_result_ok=scalar("ops_memory_restore_test_last_result_ok"),
        last_success_timestamp=scalar("ops_memory_restore_test_last_success_timestamp_seconds"),
        freshness_seconds=RESTORE_TEST_FRESHNESS_SECONDS,
    )
    openbao_raft = backup_health(
        now=now,
        latest_result_ok=scalar("ops_openbao_backup_last_verify_ok"),
        last_success_timestamp=scalar("ops_openbao_backup_last_success_timestamp_seconds"),
        freshness_seconds=BACKUP_FRESHNESS_SECONDS,
    )
    backups = {
        "broker": broker_backup,
        "memory_backup": memory_backup,
        "memory_restore_test": memory_restore_test,
        "openbao_raft": openbao_raft,
    }
    ops_v1_enabled_checks = scalar("ops_v1_enabled_checks")
    ops_v1_check_failures = scalar("ops_v1_check_failures")
    ops_v1_check_timestamp = scalar("ops_v1_check_result_timestamp_seconds")

    alerts_payload = get_json("/api/v1/alerts")
    firing = [item for item in alerts_payload["data"]["alerts"] if item.get("state") == "firing"]
    critical = [item for item in firing if item.get("labels", {}).get("severity") == "critical"]

    backup_problem = any(state["status"] != "ok" for state in backups.values())
    lead = "Incident critique local" if critical else ("État local à vérifier" if backup_problem else "État local stable")
    parts = [f"{lead}. {services_up}/{services_total} services disponibles."]
    if disk_pct is not None:
        parts.append(f"Disque au plus haut a {disk_pct:.0f} %.")
    parts.append("NVMe SMART OK." if smart_ok == 1 else "NVMe SMART a verifier.")
    parts.append(backup_summary(backups))
    if ops_v1_enabled_checks and ops_v1_enabled_checks > 0:
        failures = int(ops_v1_check_failures or 0)
        if failures:
            parts.append(f"Sondes Ops: {failures} echec(s).")
        elif ops_v1_check_timestamp:
            checks_age_minutes = max(0.0, (now.timestamp() - ops_v1_check_timestamp) / 60)
            parts.append(f"{int(ops_v1_enabled_checks)} sonde(s) Ops OK, age {checks_age_minutes:.0f} min.")
        else:
            parts.append("Sondes Ops actives sans resultat confirme.")
    else:
        parts.append("Sondes distantes non activees.")
    if bao_initialized != 1:
        parts.append("OpenBao attend son initialisation humaine.")
    elif bao_sealed == 1:
        parts.append("OpenBao est scelle.")
    if battery is not None and battery < 0.35:
        parts.append(f"Batterie usee ({battery * 100:.0f} % de capacite nominale).")
    if root_encrypted == 0:
        parts.append("Disque systeme non chiffre.")
    if hermes_credential_present == 1 and hermes_credential_age is not None:
        credential_days = hermes_credential_age / 86400
        if credential_days >= 25:
            parts.append(f"SecretID Hermes age de {credential_days:.0f} jours, rotation requise.")
    if zulip_credential_present == 1 and zulip_credential_age is not None:
        credential_days = zulip_credential_age / 86400
        if credential_days >= 25:
            parts.append(f"SecretID Zulip age de {credential_days:.0f} jours, rotation requise.")
    if aide_database != 1:
        parts.append("AIDE attend sa base de reference.")
    elif aide_changes == 1:
        parts.append("AIDE signale des changements.")
    elif aide_error == 1:
        parts.append("Controle AIDE en erreur.")
    if firing:
        parts.append(f"{len(firing)} alerte(s) active(s), dont {len(critical)} critique(s).")

    summary = " ".join(parts)
    if len(summary) > 500:
        summary = summary[:497] + "..."
    detail = {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "summary": summary,
        "services": {"up": services_up, "total": services_total},
        "disk_max_percent": disk_pct,
        "nvme_smart_healthy": smart_ok == 1,
        # Kept for consumers of the original daily-report schema.  New code
        # should use the per-job records below, including their result status.
        "backup_last_success_timestamp_seconds": broker_backup["last_success_timestamp_seconds"],
        "backups": backups,
        "ops_v1": {
            "enabled_checks": int(ops_v1_enabled_checks or 0),
            "failures": int(ops_v1_check_failures or 0),
            "last_result_timestamp_seconds": ops_v1_check_timestamp,
        },
        "openbao": {"initialized": bao_initialized == 1, "sealed": bao_sealed == 1},
        "battery_health_ratio": battery,
        "root_filesystem_encrypted": root_encrypted == 1,
        "hermes_openbao_credential": {
            "present": hermes_credential_present == 1,
            "age_seconds": hermes_credential_age,
        },
        "zulip_openbao_credential": {
            "present": zulip_credential_present == 1,
            "age_seconds": zulip_credential_age,
        },
        "aide": {
            "database_present": aide_database == 1,
            "changes_detected": aide_changes == 1,
            "error": aide_error == 1,
        },
        "alerts": [
            {
                "name": item.get("labels", {}).get("alertname"),
                "severity": item.get("labels", {}).get("severity"),
                "project": item.get("labels", {}).get("project"),
                "summary": item.get("annotations", {}).get("summary"),
            }
            for item in firing
        ],
    }

    day = now.date().isoformat()
    atomic_write(OUTPUT / f"{day}.txt", summary + "\n")
    atomic_write(OUTPUT / f"{day}.json", json.dumps(detail, ensure_ascii=False, indent=2) + "\n")
    atomic_write(OUTPUT / "latest.txt", summary + "\n")
    atomic_write(OUTPUT / "latest.json", json.dumps(detail, ensure_ascii=False, indent=2) + "\n")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
