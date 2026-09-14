#!/usr/bin/python3
"""Publish non-sensitive ops-memory backup job health for node_exporter.

This helper is deliberately independent of the backup implementation: it only
receives the fixed job name and systemd outcome, and writes an atomically
replaced Prometheus textfile.  A failed attempt never destroys the timestamp
of the most recent successful attempt.
"""

from __future__ import annotations

import grp
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time


TEXTFILE_DIRECTORY = Path("/var/lib/node-exporter/textfile")
JOBS = {
    "backup": {
        "destination": TEXTFILE_DIRECTORY / "ops-memory-backup.prom",
        "timestamp": "ops_memory_backup_last_success_timestamp_seconds",
        "result": "ops_memory_backup_last_result_ok",
        "description": "ops-memory backup",
    },
    "restore-test": {
        "destination": TEXTFILE_DIRECTORY / "ops-memory-restore-test.prom",
        "timestamp": "ops_memory_restore_test_last_success_timestamp_seconds",
        "result": "ops_memory_restore_test_last_result_ok",
        "description": "ops-memory isolated restore test",
    },
}


def _safe_textfile_directory() -> Path:
    """Return the controlled node_exporter directory or reject unsafe state."""
    try:
        directory_stat = TEXTFILE_DIRECTORY.lstat()
    except OSError as exc:
        raise OSError("node_exporter textfile directory is unavailable") from exc
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
        raise OSError("node_exporter textfile directory is unsafe")
    try:
        expected_gid = grp.getgrnam("node-exporter").gr_gid
    except KeyError as exc:
        raise OSError("node-exporter group is unavailable") from exc
    if directory_stat.st_uid != 0 or directory_stat.st_gid != expected_gid:
        raise OSError("node_exporter textfile directory ownership is unsafe")
    if directory_stat.st_mode & 0o022:
        raise OSError("node_exporter textfile directory is writable by others")
    return TEXTFILE_DIRECTORY


def _previous_success_timestamp(destination: Path, metric_name: str) -> str:
    """Read only a syntactically valid prior timestamp from our own textfile."""
    try:
        existing_stat = destination.lstat()
    except FileNotFoundError:
        return "0"
    if stat.S_ISLNK(existing_stat.st_mode) or not stat.S_ISREG(existing_stat.st_mode):
        raise OSError("existing metric destination is unsafe")
    try:
        existing = destination.read_text(encoding="ascii")
    except OSError as exc:
        raise OSError("existing metric destination is unreadable") from exc
    expression = re.compile(
        rf"^{re.escape(metric_name)} ([0-9]+(?:\.[0-9]+)?)$", re.MULTILINE
    )
    match = expression.search(existing)
    return match.group(1) if match else "0"


def _content(job: dict[str, str], outcome: str) -> str:
    destination = Path(job["destination"])
    timestamp = (
        f"{time.time():.6f}"
        if outcome == "success"
        else _previous_success_timestamp(destination, job["timestamp"])
    )
    result = "1" if outcome == "success" else "0"
    description = job["description"]
    return (
        f"# HELP {job['timestamp']} Unix time of the last successful {description}.\n"
        f"# TYPE {job['timestamp']} gauge\n"
        f"{job['timestamp']} {timestamp}\n"
        f"# HELP {job['result']} Whether the latest {description} completed successfully.\n"
        f"# TYPE {job['result']} gauge\n"
        f"{job['result']} {result}\n"
    )


def _publish(destination: Path, content: str) -> None:
    directory = _safe_textfile_directory()
    if destination.parent != directory:
        raise OSError("metric destination escapes textfile directory")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".ops-memory-job.", suffix=".tmp", dir=directory, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o640)
        os.replace(temporary, destination)
        directory_descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def main(arguments: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) != 2:
        return 64
    job_name, outcome = arguments
    if job_name not in JOBS or outcome not in {"success", "failure"}:
        return 64
    job = JOBS[job_name]
    # Validate the parent before reading a prior result on the failure path.
    _safe_textfile_directory()
    _publish(Path(job["destination"]), _content(job, outcome))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError:
        raise SystemExit(1) from None
