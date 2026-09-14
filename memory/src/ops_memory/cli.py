from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from .config import MemoryConfig
from .errors import ConfigurationError, MemoryErrorBase, ProtocolError
from .notify import Watchdog, notify
from .protocol import Dispatcher, MemorySocketServer, PROTOCOL_VERSION, socket_request
from .service import MemoryService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ops-memory")
    subcommands = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "stdio", "check-config"):
        command = subcommands.add_parser(name)
        command.add_argument("--config", required=True, type=Path)
    maintain = subcommands.add_parser("maintain")
    maintain.add_argument("--config", required=True, type=Path)
    maintain.add_argument("--actor", required=True)
    request = subcommands.add_parser("request")
    request.add_argument("--socket", required=True, type=Path)
    request.add_argument("--input", default="-", help="JSON file, or - for stdin")
    request.add_argument("--timeout", type=float, default=15.0)
    probe = subcommands.add_parser("probe")
    probe.add_argument("--socket", required=True, type=Path)
    probe.add_argument("--actor", required=True)
    probe.add_argument("--timeout", type=float, default=5.0)
    return parser


def _load_request(path: str) -> dict[str, Any]:
    try:
        if path == "-":
            value = json.load(sys.stdin)
        else:
            with Path(path).open("r", encoding="utf-8") as handle:
                value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"cannot read request JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ProtocolError("request must be a JSON object")
    return value


def _print(value: object) -> None:
    json.dump(value, sys.stdout, sort_keys=True, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "request":
            _print(socket_request(args.socket, _load_request(args.input), args.timeout))
            return 0
        if args.command == "probe":
            request = {
                "version": PROTOCOL_VERSION,
                "request_id": str(uuid.uuid4()),
                "operation": "health",
                "actor": args.actor,
                "payload": {},
            }
            response = socket_request(args.socket, request, args.timeout)
            _print(response)
            return 0 if response.get("ok") and response.get("result", {}).get("status") == "ok" else 1
        config = MemoryConfig.from_toml(args.config)
        if args.command == "check-config":
            _print({"ok": True, "actors": sorted(config.actors), "database": str(config.database_path)})
            return 0
        service = MemoryService(config)
        if args.command == "serve":
            with MemorySocketServer(service) as server:
                watchdog = Watchdog(server.healthy)
                notify("READY=1\nSTATUS=Ops memory accepting local requests")
                watchdog.start()
                try:
                    server.serve_forever()
                finally:
                    watchdog.stop()
                    notify("STOPPING=1")
            return 0
        if args.command == "stdio":
            dispatcher = Dispatcher(service)
            uid = os.getuid()
            for line in sys.stdin:
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    request = {"version": PROTOCOL_VERSION, "request_id": None}
                _print(dispatcher.dispatch(request, uid))
            return 0
        if args.command == "maintain":
            request = {
                "version": PROTOCOL_VERSION,
                "request_id": str(uuid.uuid4()),
                "operation": "maintain",
                "actor": args.actor,
                "payload": {},
            }
            response = Dispatcher(service).dispatch(request, os.getuid())
            _print(response)
            return 0 if response["ok"] else 1
    except (MemoryErrorBase, ConfigurationError, OSError) as exc:
        _print({"ok": False, "error": {"code": getattr(exc, "code", "startup_error"), "message": str(exc)}})
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
