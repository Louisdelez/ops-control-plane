from __future__ import annotations

import argparse
import grp
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import pwd
import signal
import socket
import socketserver
import stat
import struct
from urllib.parse import parse_qs, unquote, urlsplit

from .bounded_server import (
    DEFAULT_CLIENT_TIMEOUT_SECONDS,
    DEFAULT_MAX_REQUEST_WORKERS,
    BoundedThreadingMixIn,
)
from .config import load_config
from .errors import ConfigurationError, OrchestratorError, ValidationError
from .service import OrchestratorService


class AccessDenied(Exception):
    """A Unix peer has no root-configured grant for the requested project."""


class ThreadingUnixHTTPServer(BoundedThreadingMixIn, socketserver.UnixStreamServer):
    allow_reuse_address = False

    def __init__(
        self,
        address: str,
        handler,
        service: OrchestratorService,
        *,
        client_timeout_seconds: float = DEFAULT_CLIENT_TIMEOUT_SECONDS,
        max_request_workers: int = DEFAULT_MAX_REQUEST_WORKERS,
    ):
        self.service = service
        super().__init__(
            address,
            handler,
            client_timeout_seconds=client_timeout_seconds,
            max_request_workers=max_request_workers,
        )


class APIHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ops-orchestrator"
    sys_version = ""

    @property
    def service(self) -> OrchestratorService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, format: str, *args) -> None:
        # Do not log request bodies, mission identifiers, prompts or secret-adjacent headers.
        return

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            # Client disconnects and slow readers are expected transport
            # failures, not internal server errors worth a traceback.
            pass
        finally:
            self.close_connection = True

    def _body(self) -> object:
        if self.headers.get("Transfer-Encoding"):
            raise ValidationError("transfer encoding is not accepted")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise ValidationError("Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None or not raw_length.isdigit():
            raise ValidationError("valid Content-Length is required")
        length = int(raw_length)
        if length < 2 or length > self.service.config.limits.max_request_bytes:
            raise ValidationError("request body size is outside the accepted range")
        data = self.rfile.read(length)
        if len(data) != length:
            raise ValidationError("request body is incomplete")
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("request body is invalid JSON") from exc

    def _peer_username(self) -> str:
        try:
            credentials = self.connection.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            _pid, uid, _gid = struct.unpack("3i", credentials)
            return pwd.getpwuid(uid).pw_name
        except (AttributeError, KeyError, OSError, struct.error) as exc:
            raise AccessDenied from exc

    def _authorize_project(self, project_id: object):
        if not isinstance(project_id, str):
            raise ValidationError("project_id must be a string")
        grant = self.service.config.route_grant(self._peer_username(), project_id)
        if grant is None:
            raise AccessDenied
        return grant

    def _authorized_route_payload(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ValidationError("request must be an object")
        grant = self._authorize_project(payload.get("project_id"))
        requested_remote = payload.get("remote_allowed", False)
        if not isinstance(requested_remote, bool):
            raise ValidationError("remote_allowed must be boolean")
        authorized = dict(payload)
        # A caller may opt out of external processing, but can never elevate
        # itself beyond the root-owned grant derived from SO_PEERCRED.
        authorized["remote_allowed"] = requested_remote and grant.remote_allowed
        return authorized

    @staticmethod
    def _catalogue_pagination(query: str) -> tuple[int, int]:
        if not query:
            return (0, 100)
        try:
            parsed = parse_qs(
                query,
                keep_blank_values=True,
                strict_parsing=True,
                max_num_fields=2,
            )
        except ValueError as exc:
            raise ValidationError("catalogue query is invalid") from exc
        if set(parsed) - {"offset", "limit"} or any(len(values) != 1 for values in parsed.values()):
            raise ValidationError("catalogue query fields are invalid")
        values: dict[str, int] = {"offset": 0, "limit": 100}
        for field, raw_values in parsed.items():
            raw = raw_values[0]
            if not raw.isascii() or not raw.isdigit() or len(raw) > 6:
                raise ValidationError(f"catalogue {field} is invalid")
            values[field] = int(raw)
        return values["offset"], values["limit"]

    def do_GET(self) -> None:
        try:
            parsed = urlsplit(self.path)
            if parsed.path == "/v1/health" and not parsed.query:
                self._json(200, self.service.health())
                return
            if parsed.path == "/v1/budgets" and not parsed.query:
                self._json(200, self.service.budget_summary())
                return
            if parsed.path == "/v1/model-performance" and not parsed.query:
                self._json(200, self.service.model_performance_summary())
                return
            if parsed.path == "/v1/provider-integrations" and not parsed.query:
                self._json(200, self.service.provider_integrations())
                return
            if parsed.path == "/v1/metrics" and not parsed.query:
                self._json(200, self.service.metrics_snapshot())
                return
            if parsed.path == "/v1/catalogue":
                offset, limit = self._catalogue_pagination(parsed.query)
                self._json(
                    200,
                    self.service.catalogue_page(offset=offset, limit=limit),
                )
                return
            if parsed.path.startswith("/v1/traces/") and not parsed.query:
                mission_id = unquote(parsed.path[len("/v1/traces/"):])
                trace = self.service.mission_trace(mission_id)
                self._authorize_project(trace.get("project_id"))
                self._json(200, trace)
                return
            self._json(404, {"error": "not_found"})
        except (ValueError, OrchestratorError) as exc:
            self._json(400, {"error": type(exc).__name__, "message": str(exc)})
        except AccessDenied:
            self._json(403, {"error": "access_denied"})
        except TimeoutError:
            self.close_connection = True
        except Exception:
            self._json(500, {"error": "internal_error"})

    def do_POST(self) -> None:
        try:
            payload = self._body()
            parsed = urlsplit(self.path)
            if parsed.query:
                self._json(404, {"error": "not_found"})
                return
            if parsed.path == "/v1/route":
                self._json(200, self.service.route(self._authorized_route_payload(payload)))
                return
            if parsed.path == "/v1/catalogue/route-preview":
                if not isinstance(payload, dict):
                    raise ValidationError("catalogue preview must be an object")
                self._authorize_project(payload.get("project_id"))
                self._json(200, self.service.preview_catalogue_route(payload))
                return
            if parsed.path == "/v1/signals":
                if not isinstance(payload, dict) or set(payload) != {"signal", "project_id"}:
                    raise ValidationError("signal request fields are invalid")
                if not isinstance(payload["signal"], str):
                    raise ValidationError("signal value must be a string")
                self._authorize_project(payload["project_id"])
                self._json(
                    200,
                    self.service.record_signal(payload["signal"], payload["project_id"]),
                )
                return
            if parsed.path == "/v1/model-outcomes":
                if not isinstance(payload, dict):
                    raise ValidationError("model outcome request must be an object")
                self._authorize_project(payload.get("project_id"))
                self._json(200, self.service.record_model_outcome(payload))
                return
            if parsed.path == "/v1/provider-finance/refresh":
                if not isinstance(payload, dict):
                    raise ValidationError("provider finance refresh must be an object")
                self._authorize_project(payload.get("project_id"))
                self._json(200, self.service.refresh_provider_finance(payload))
                return
            self._json(404, {"error": "not_found"})
        except (ValueError, OrchestratorError) as exc:
            self._json(400, {"error": type(exc).__name__, "message": str(exc)})
        except AccessDenied:
            self._json(403, {"error": "access_denied"})
        except TimeoutError:
            self.close_connection = True
        except Exception:
            self._json(500, {"error": "internal_error"})


def serve(config_path: str) -> None:
    config = load_config(config_path)
    service = OrchestratorService(config)
    socket_path = config.socket_path
    socket_path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    parent_info = socket_path.parent.lstat()
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise ConfigurationError("socket parent must be a real directory")
    if socket_path.exists() or socket_path.is_symlink():
        info = socket_path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
            raise ConfigurationError("refusing to replace unsafe socket path")
        socket_path.unlink()
    old_mask = os.umask(0o077)
    try:
        server = ThreadingUnixHTTPServer(str(socket_path), APIHandler, service)
    finally:
        os.umask(old_mask)
    try:
        group_id = grp.getgrnam(config.socket_group).gr_gid
        os.chown(socket_path, -1, group_id)
        os.chmod(socket_path, 0o660)
        server.timeout = 0.5
        stop = False

        def request_stop(signum, frame):
            nonlocal stop
            stop = True

        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        while not stop:
            server.handle_request()
    finally:
        server.server_close()
        try:
            info = socket_path.lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid():
                socket_path.unlink()
        except FileNotFoundError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the fail-closed ops orchestrator Unix API")
    parser.add_argument("--config", default="/etc/ops-orchestrator/config.json")
    arguments = parser.parse_args()
    serve(arguments.config)


if __name__ == "__main__":
    main()
