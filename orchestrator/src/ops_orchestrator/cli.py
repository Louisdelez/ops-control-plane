from __future__ import annotations

import argparse
import http.client
import json
from pathlib import Path
import socket
import sys
from urllib.parse import quote

from .catalogue import ModelCatalogue
from .config import expand_catalogue_runtime, load_config
from .metrics import write_metrics
from .provider_integrations import load_provider_integrations


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 30):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


def _request(
    socket_path: str,
    method: str,
    path: str,
    payload: object | None = None,
    *,
    timeout: float = 240,
) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(data))
    connection = UnixHTTPConnection(socket_path, timeout=timeout)
    try:
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        body = response.read(4_194_305)
    finally:
        connection.close()
    if len(body) > 4_194_304:
        raise RuntimeError("server response exceeds limit")
    value = json.loads(body.decode("utf-8"))
    if response.status < 200 or response.status >= 300:
        raise RuntimeError(f"orchestrator HTTP {response.status}: {value.get('error', 'error')}")
    if not isinstance(value, dict):
        raise RuntimeError("server returned a non-object response")
    return value


def _read_json(path: str, maximum: int = 1_048_576) -> object:
    if path == "-":
        raw = sys.stdin.buffer.read(maximum + 1)
    else:
        source = Path(path)
        if source.is_symlink() or not source.is_file():
            raise RuntimeError("request source must be a regular non-symlink file")
        if source.stat().st_size > maximum:
            raise RuntimeError("request source exceeds limit")
        raw = source.read_bytes()
    if len(raw) > maximum:
        raise RuntimeError("request source exceeds limit")
    value = json.loads(raw.decode("utf-8"))
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Client for the ops orchestrator Unix API")
    parser.add_argument("--socket", default="/run/ops-orchestrator/api.sock")
    subparsers = parser.add_subparsers(dest="command", required=True)
    route = subparsers.add_parser("route", help="submit a strict routing request")
    route.add_argument("--file", required=True, help="JSON file, or - for stdin")
    subparsers.add_parser("health")
    subparsers.add_parser("budgets")
    subparsers.add_parser("performance")
    subparsers.add_parser(
        "provider-integrations",
        help="show provider integration capabilities and cached balances",
    )
    refresh_finance = subparsers.add_parser(
        "refresh-finance",
        help="refresh one provider account or every reviewed account",
    )
    refresh_finance.add_argument("--account", required=True)
    refresh_finance.add_argument("--project-id", default="infra-shared")
    catalogue = subparsers.add_parser("catalogue")
    catalogue.add_argument("--offset", type=int, default=0)
    catalogue.add_argument("--limit", type=int, default=100)
    preview = subparsers.add_parser("preview-candidates")
    preview.add_argument("--file", required=True, help="JSON file, or - for stdin")
    trace = subparsers.add_parser("trace")
    trace.add_argument("mission_id")
    signal_parser = subparsers.add_parser("signal")
    signal_parser.add_argument(
        "name",
        choices=["mission_succeeded", "mission_failed", "tool_error", "memory_search"],
    )
    signal_parser.add_argument("--project-id", required=True)
    outcome = subparsers.add_parser(
        "record-outcome", help="record externally validated model outcome JSON"
    )
    outcome.add_argument("--file", required=True, help="JSON file, or - for stdin")
    check = subparsers.add_parser("check-config")
    check.add_argument("--config", default="/etc/ops-orchestrator/config.json")
    check.add_argument(
        "--catalogue",
        help="absolute catalogue path used only for pre-install validation",
    )
    check.add_argument(
        "--provider-integrations",
        help="absolute provider registry path used only for pre-install validation",
    )
    export = subparsers.add_parser("export-metrics")
    export.add_argument("--config", default="/etc/ops-orchestrator/config.json")
    export.add_argument("--target")
    return parser


def run(arguments: argparse.Namespace) -> dict:
    if arguments.command == "route":
        return _request(arguments.socket, "POST", "/v1/route", _read_json(arguments.file))
    if arguments.command == "health":
        return _request(arguments.socket, "GET", "/v1/health")
    if arguments.command == "budgets":
        return _request(arguments.socket, "GET", "/v1/budgets")
    if arguments.command == "performance":
        return _request(arguments.socket, "GET", "/v1/model-performance", timeout=10)
    if arguments.command == "provider-integrations":
        return _request(arguments.socket, "GET", "/v1/provider-integrations", timeout=10)
    if arguments.command == "refresh-finance":
        return _request(
            arguments.socket,
            "POST",
            "/v1/provider-finance/refresh",
            {
                "project_id": arguments.project_id,
                "provider_account_id": arguments.account,
            },
            timeout=240,
        )
    if arguments.command == "catalogue":
        if not 0 <= arguments.offset <= 999999 or not 1 <= arguments.limit <= 200:
            raise RuntimeError("catalogue pagination is outside the accepted range")
        return _request(
            arguments.socket,
            "GET",
            f"/v1/catalogue?offset={arguments.offset}&limit={arguments.limit}",
            timeout=10,
        )
    if arguments.command == "preview-candidates":
        return _request(
            arguments.socket,
            "POST",
            "/v1/catalogue/route-preview",
            _read_json(arguments.file),
            timeout=10,
        )
    if arguments.command == "trace":
        return _request(arguments.socket, "GET", "/v1/traces/" + quote(arguments.mission_id, safe=""))
    if arguments.command == "signal":
        return _request(
            arguments.socket,
            "POST",
            "/v1/signals",
            {"signal": arguments.name, "project_id": arguments.project_id},
        )
    if arguments.command == "record-outcome":
        return _request(
            arguments.socket,
            "POST",
            "/v1/model-outcomes",
            _read_json(arguments.file),
            timeout=10,
        )
    if arguments.command == "check-config":
        config = load_config(arguments.config)
        catalogue_path = (
            Path(arguments.catalogue) if arguments.catalogue else config.catalogue_path
        )
        if not catalogue_path.is_absolute():
            raise RuntimeError("catalogue override must be an absolute path")
        if arguments.provider_integrations:
            integrations_path = Path(arguments.provider_integrations)
        elif arguments.catalogue:
            # A pre-install validation override is a coherent catalogue bundle;
            # derive the sibling registry instead of consulting an old/missing
            # installed path.
            integrations_path = Path(arguments.catalogue).with_name(
                "provider-integrations.v1.json"
            )
        else:
            integrations_path = config.provider_integrations_path
        if not integrations_path.is_absolute():
            raise RuntimeError("provider integrations override must be an absolute path")
        config = expand_catalogue_runtime(
            config,
            catalogue_path=catalogue_path,
            provider_integrations_path=integrations_path,
        )
        catalogue = ModelCatalogue.load(
            catalogue_path,
            configured_deployment_ids=(provider.provider_id for provider in config.providers),
            auxiliary_deployment_ids=config.catalogue_auxiliary_deployment_ids,
            runtime_bindings=config.catalogue_runtime_bindings,
            runtime_reasons=config.catalogue_runtime_reasons,
        )
        integrations = load_provider_integrations(integrations_path)
        return {
            "status": "valid",
            "providers": len(config.providers),
            "roles": len(config.providers_by_role),
            "catalogue_cards": len(catalogue.cards),
            "catalogue_linked_deployments": sum(
                len(card["deployment_ids"]) for card in catalogue.cards
            ),
            "catalogue_auxiliary_deployments": sorted(
                config.catalogue_auxiliary_deployment_ids
            ),
            "catalogue_revision": catalogue.revision,
            "provider_integration_accounts": len(integrations.accounts),
            "provider_integration_revision": integrations.revision,
        }
    if arguments.command == "export-metrics":
        config = load_config(arguments.config)
        target = Path(arguments.target) if arguments.target else config.metrics_path
        if not target.is_absolute():
            raise RuntimeError("metrics target must be absolute")
        snapshot = _request(arguments.socket, "GET", "/v1/metrics", timeout=10)
        if set(snapshot) != {"metrics"} or not isinstance(snapshot["metrics"], str):
            raise RuntimeError("server returned an invalid metrics snapshot")
        if len(snapshot["metrics"].encode("utf-8")) > config.limits.max_response_bytes:
            raise RuntimeError("server returned an oversized metrics snapshot")
        write_metrics(target, snapshot["metrics"])
        return {"status": "written", "target": str(target)}
    raise RuntimeError("unsupported command")


def main() -> None:
    parser = build_parser()
    try:
        result = run(parser.parse_args())
    except Exception as exc:
        print(f"ops-orchestrator: {exc}", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
