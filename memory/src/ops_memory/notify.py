from __future__ import annotations

import os
import socket
import threading
from collections.abc import Callable


def notify(message: str) -> bool:
    address = os.environ.get("NOTIFY_SOCKET", "")
    if not address:
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    client = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC)
    try:
        client.connect(address)
        client.sendall(message.encode("utf-8"))
    except OSError:
        return False
    finally:
        client.close()
    return True


class Watchdog:
    def __init__(self, healthy: Callable[[], bool] | None = None) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._healthy = healthy or (lambda: True)

    def start(self) -> None:
        usec_text = os.environ.get("WATCHDOG_USEC", "")
        pid_text = os.environ.get("WATCHDOG_PID", "")
        try:
            usec = int(usec_text)
            watched_pid = int(pid_text) if pid_text else os.getpid()
        except ValueError:
            return
        if usec <= 0 or watched_pid != os.getpid():
            return
        interval = max(0.5, usec / 2_000_000)

        def pulse() -> None:
            while not self._stop.wait(interval):
                if self._healthy():
                    notify("WATCHDOG=1")

        self._thread = threading.Thread(target=pulse, name="systemd-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
