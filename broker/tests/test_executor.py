from __future__ import annotations

import sys

from ops_broker.executor import CommandExecutor


def test_executor_drains_but_bounds_large_output() -> None:
    result = CommandExecutor().execute(
        (sys.executable, "-c", "import sys; sys.stdout.write('A' * 1000000)"),
        timeout_seconds=5,
        max_bytes=1024,
    )

    assert result.return_code == 0
    assert result.timed_out is False
    assert result.truncated is True
    assert result.stdout == "A" * 1024


def test_executor_kills_timed_out_process_group() -> None:
    result = CommandExecutor().execute(
        (sys.executable, "-c", "import time; time.sleep(10)"),
        timeout_seconds=1,
        max_bytes=1024,
    )

    assert result.return_code != 0
    assert result.timed_out is True
