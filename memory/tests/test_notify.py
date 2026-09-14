from __future__ import annotations

import socket

from ops_memory.notify import notify


def test_systemd_notify_sends_unix_datagram(tmp_path, monkeypatch) -> None:
    path = tmp_path / "notify.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    listener.bind(str(path))
    listener.settimeout(1.0)
    monkeypatch.setenv("NOTIFY_SOCKET", str(path))
    try:
        assert notify("READY=1") is True
        assert listener.recv(64) == b"READY=1"
    finally:
        listener.close()
