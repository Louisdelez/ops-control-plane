from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import sqlite3

from ops_broker.backup import backup_sqlite
from ops_broker.database import Database

from conftest import Harness, request_id


def test_online_backup_is_coherent_private_and_retained(
    harness: Harness,
    tmp_path: Path,
) -> None:
    mission = harness.service.create_mission(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="backup consistency",
    )
    destination = tmp_path / "backups"
    instant = datetime(2026, 9, 4, 3, 0, tzinfo=UTC)

    first = backup_sqlite(
        harness.database.path,
        destination,
        retain=1,
        clock=lambda: instant,
    )
    assert first.is_file()
    assert os.stat(first).st_mode & 0o777 == 0o600
    assert Database.verify_file(first).valid
    with sqlite3.connect(first) as connection:
        assert connection.execute("SELECT id FROM missions").fetchone()[0] == mission["id"]

    second = backup_sqlite(
        harness.database.path,
        destination,
        retain=1,
        clock=lambda: instant + timedelta(seconds=1),
    )
    assert second.is_file()
    assert not first.exists()
    assert list(destination.glob("ops-broker-*.db")) == [second]
