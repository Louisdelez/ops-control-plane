"""Bounded argv execution. There is intentionally no shell entry point."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from typing import Protocol

from .util import redact_text


@dataclass(frozen=True)
class ExecutionResult:
    return_code: int
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool


class Executor(Protocol):
    def execute(self, argv: tuple[str, ...], timeout_seconds: int, max_bytes: int) -> ExecutionResult:
        ...


class CommandExecutor:
    """Execute a prevalidated argv with a minimal, secret-free environment."""

    _ENV = {
        "PATH": "/usr/sbin:/usr/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }

    def execute(self, argv: tuple[str, ...], timeout_seconds: int, max_bytes: int) -> ExecutionResult:
        if not argv:
            return ExecutionResult(0, "", "", False, False)

        process = subprocess.Popen(  # noqa: S603 - argv was validated by RunbookRegistry
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            env=self._ENV,
            start_new_session=True,
        )

        # Keep draining both pipes so a noisy helper cannot deadlock, but retain
        # at most max_bytes + one sentinel byte per stream.  Temporary files are
        # deliberately avoided: a broken helper must not be able to fill disk
        # before the broker gets a chance to truncate its response.
        captured = {"stdout": bytearray(), "stderr": bytearray()}

        def drain(name: str, pipe: object) -> None:
            buffer = captured[name]
            try:
                while True:
                    chunk = pipe.read(65_536)  # type: ignore[attr-defined]
                    if not chunk:
                        break
                    remaining = max_bytes + 1 - len(buffer)
                    if remaining > 0:
                        buffer.extend(chunk[:remaining])
            except (OSError, ValueError):
                # The main thread may close a pipe after killing a timed-out
                # process.  That is an expected shutdown path.
                return

        assert process.stdout is not None and process.stderr is not None
        readers = [
            threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return_code = process.wait()
        finally:
            process.stdout.close()
            process.stderr.close()
            for reader in readers:
                reader.join(timeout=1)

        stdout_bytes = bytes(captured["stdout"])
        stderr_bytes = bytes(captured["stderr"])
        truncated = len(stdout_bytes) > max_bytes or len(stderr_bytes) > max_bytes
        stdout = stdout_bytes[:max_bytes].decode("utf-8", errors="replace")
        stderr = stderr_bytes[:max_bytes].decode("utf-8", errors="replace")
        return ExecutionResult(
            return_code=return_code,
            stdout=redact_text(stdout),
            stderr=redact_text(stderr),
            timed_out=timed_out,
            truncated=truncated,
        )
