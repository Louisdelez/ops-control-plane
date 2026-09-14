from __future__ import annotations

import grp
import json
import os
import socket
import socketserver
import stat
import struct
import time
from pathlib import Path
from typing import Any

from .errors import MemoryErrorBase, ProtocolError, ValidationError
from .service import MemoryService


PROTOCOL_VERSION = 1


def error_response(request_id: object, error: Exception) -> dict[str, Any]:
    if isinstance(error, MemoryErrorBase):
        code = error.code
        message = str(error)
    else:
        code = "internal_error"
        message = "internal memory service error"
    return {
        "version": PROTOCOL_VERSION,
        "request_id": request_id if isinstance(request_id, str) else None,
        "ok": False,
        "error": {"code": code, "message": message},
    }


class Dispatcher:
    def __init__(self, service: MemoryService) -> None:
        self.service = service

    def dispatch(self, request: object, peer_uid: int) -> dict[str, Any]:
        request_id: object = None
        try:
            if not isinstance(request, dict):
                raise ProtocolError("request must be an object")
            unknown = set(request) - {"version", "request_id", "operation", "actor", "payload"}
            if unknown:
                raise ProtocolError(f"unknown envelope fields: {sorted(unknown)}")
            version = request.get("version")
            if isinstance(version, bool) or version != PROTOCOL_VERSION:
                raise ProtocolError("unsupported protocol version")
            request_id = request.get("request_id")
            if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
                raise ProtocolError("request_id must be a short non-empty string")
            operation = request.get("operation")
            actor = request.get("actor")
            payload = request.get("payload", {})
            if not isinstance(operation, str) or not isinstance(actor, str):
                raise ProtocolError("operation and actor are required strings")
            if not isinstance(payload, dict):
                raise ProtocolError("payload must be an object")
            body = {**payload, "actor": actor}

            if operation == "health":
                if payload:
                    raise ValidationError("health payload must be empty")
                result = self.service.health(actor, peer_uid)
            elif operation == "stats":
                if payload:
                    raise ValidationError("stats payload must be empty")
                result = self.service.stats(actor, peer_uid)
            elif operation == "ingest":
                result = self.service.ingest(body, peer_uid)
            elif operation == "reindex_api":
                if set(payload) != {"ids"}:
                    raise ValidationError("reindex_api requires only ids")
                result = self.service.reindex_api(actor, peer_uid, payload["ids"])
            elif operation == "search":
                result = self.service.search(body, peer_uid)
            elif operation == "get":
                if set(payload) != {"id"}:
                    raise ValidationError("get payload must contain only id")
                record_id = payload.get("id")
                if not isinstance(record_id, str):
                    raise ValidationError("payload.id is required")
                result = self.service.get(actor, peer_uid, record_id)
            elif operation == "invalidate":
                result = self.service.invalidate(body, peer_uid)
            elif operation == "summarize":
                result = self.service.summarize(body, peer_uid)
            elif operation == "maintain":
                if payload:
                    raise ValidationError("maintain payload must be empty")
                result = self.service.maintain(actor, peer_uid)
            else:
                raise ProtocolError("unknown operation")
            return {
                "version": PROTOCOL_VERSION,
                "request_id": request_id,
                "ok": True,
                "result": result,
            }
        except Exception as exc:  # Stable boundary; never leak a traceback or secret.
            return error_response(request_id, exc)


class _ThreadingUnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False

    def service_actions(self) -> None:
        self.last_heartbeat = time.monotonic()  # type: ignore[attr-defined]


class MemorySocketHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        credentials = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", credentials)
        self.request.settimeout(15.0)
        maximum = self.server.maximum_request_bytes  # type: ignore[attr-defined]
        dispatcher = self.server.dispatcher  # type: ignore[attr-defined]
        while True:
            line = self.rfile.readline(maximum + 1)
            if not line:
                return
            if len(line) > maximum or not line.endswith(b"\n"):
                response = error_response(None, ProtocolError("request exceeds the byte limit"))
                self.wfile.write(json.dumps(response, separators=(",", ":")).encode("utf-8") + b"\n")
                self.wfile.flush()
                return
            try:
                request = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                response = error_response(None, ProtocolError("request is not valid UTF-8 JSON"))
            else:
                response = dispatcher.dispatch(request, uid)
            self.wfile.write(json.dumps(response, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n")
            self.wfile.flush()


class MemorySocketServer:
    def __init__(self, service: MemoryService) -> None:
        self.service = service
        self.path = service.config.socket_path
        self.server: _ThreadingUnixServer | None = None

    def __enter__(self) -> "MemorySocketServer":
        parent = self.path.parent
        if parent.exists() and parent.is_symlink():
            raise OSError("socket directory must not be a symlink")
        parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise OSError("refusing to replace an unsafe socket path")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.2)
                probe.connect(str(self.path))
            except OSError:
                self.path.unlink()
            else:
                raise OSError("memory service socket is already active")
            finally:
                probe.close()
        server = _ThreadingUnixServer(str(self.path), MemorySocketHandler)
        server.dispatcher = Dispatcher(self.service)  # type: ignore[attr-defined]
        server.maximum_request_bytes = self.service.config.max_content_bytes + 65_536  # type: ignore[attr-defined]
        server.last_heartbeat = time.monotonic()  # type: ignore[attr-defined]
        try:
            group_id = grp.getgrnam(self.service.config.socket_group).gr_gid
            os.chown(self.path, -1, group_id)
            os.chmod(self.path, self.service.config.socket_mode)
        except Exception:
            server.server_close()
            if self.path.exists():
                self.path.unlink()
            raise
        self.server = server
        return self

    def serve_forever(self) -> None:
        if self.server is None:
            raise RuntimeError("server is not entered")
        self.server.serve_forever(poll_interval=0.5)

    def healthy(self) -> bool:
        if self.server is None:
            return False
        heartbeat = self.server.last_heartbeat  # type: ignore[attr-defined]
        return time.monotonic() - heartbeat < 3.0

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if self.server is not None:
            self.server.server_close()
        if self.path.exists() and stat.S_ISSOCK(self.path.lstat().st_mode):
            self.path.unlink()


def socket_request(path: Path, request: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
    encoded = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    if len(encoded) > 2_000_000:
        raise ProtocolError("request is too large")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(path))
        client.sendall(encoded)
        chunks = bytearray()
        while not chunks.endswith(b"\n"):
            chunk = client.recv(65_536)
            if not chunk:
                raise ProtocolError("memory service closed the connection")
            chunks.extend(chunk)
            if len(chunks) > 2_000_000:
                raise ProtocolError("response is too large")
    except OSError as exc:
        raise ProtocolError(f"cannot reach memory service: {type(exc).__name__}") from exc
    finally:
        client.close()
    try:
        response = json.loads(chunks)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("memory service returned invalid JSON") from exc
    if not isinstance(response, dict):
        raise ProtocolError("memory service response is not an object")
    return response
