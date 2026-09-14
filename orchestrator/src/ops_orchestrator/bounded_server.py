"""Bounded request-thread admission for the local HTTP servers.

``socketserver.ThreadingMixIn`` otherwise creates one thread for every
accepted connection.  A local client can therefore exhaust the systemd task
limit by opening requests very slowly.  This mix-in sets a deadline on each
accepted client socket and admits only a fixed number of request threads.
"""

from __future__ import annotations

import math
import socketserver
import threading
from typing import Any


DEFAULT_CLIENT_TIMEOUT_SECONDS = 5.0
DEFAULT_MAX_REQUEST_WORKERS = 16


class BoundedThreadingMixIn(socketserver.ThreadingMixIn):
    """Thread-per-request handling with non-blocking, bounded admission."""

    daemon_threads = True

    def __init__(
        self,
        *args: Any,
        client_timeout_seconds: float = DEFAULT_CLIENT_TIMEOUT_SECONDS,
        max_request_workers: int = DEFAULT_MAX_REQUEST_WORKERS,
        **kwargs: Any,
    ) -> None:
        if (
            isinstance(client_timeout_seconds, bool)
            or not isinstance(client_timeout_seconds, (int, float))
            or not math.isfinite(float(client_timeout_seconds))
            or not 0.05 <= float(client_timeout_seconds) <= 300
        ):
            raise ValueError("client_timeout_seconds is outside the accepted range")
        if (
            isinstance(max_request_workers, bool)
            or not isinstance(max_request_workers, int)
            or not 1 <= max_request_workers <= 256
        ):
            raise ValueError("max_request_workers is outside the accepted range")

        self.client_timeout_seconds = float(client_timeout_seconds)
        self.max_request_workers = max_request_workers
        self._request_slots = threading.BoundedSemaphore(max_request_workers)
        self._request_count_lock = threading.Lock()
        self._active_request_count = 0
        self._rejected_request_count = 0
        super().__init__(*args, **kwargs)

    @property
    def active_request_count(self) -> int:
        """Return the number of currently admitted request threads."""

        with self._request_count_lock:
            return self._active_request_count

    @property
    def rejected_request_count(self) -> int:
        """Return the number of connections rejected at worker saturation."""

        with self._request_count_lock:
            return self._rejected_request_count

    def get_request(self):
        request, client_address = super().get_request()
        try:
            # This bounds request-line/header/body reads and blocked response
            # writes.  It does not impose a total lifetime on a healthy SSE
            # stream because each successful socket operation resets the wait.
            request.settimeout(self.client_timeout_seconds)
        except BaseException:
            self.close_request(request)
            raise
        return request, client_address

    def process_request(self, request, client_address) -> None:
        if not self._request_slots.acquire(blocking=False):
            with self._request_count_lock:
                self._rejected_request_count += 1
            # Do not parse attacker-controlled bytes or allocate another
            # thread at saturation.  Closing is the fail-closed response.
            self.shutdown_request(request)
            return

        with self._request_count_lock:
            self._active_request_count += 1
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release_request_slot()
            self.shutdown_request(request)
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_request_slot()

    def _release_request_slot(self) -> None:
        with self._request_count_lock:
            self._active_request_count -= 1
        self._request_slots.release()
