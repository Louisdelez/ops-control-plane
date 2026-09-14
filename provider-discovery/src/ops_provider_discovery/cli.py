"""Command line entry point for observation-only provider model discovery."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .collector import DiscoveryFailure, collect_inventory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ops-provider-discovery",
        description="Collect bounded provider model inventories without changing the catalogue.",
    )
    parser.add_argument("--registry", type=Path, required=True, help="provider-integrations.v1 JSON path")
    parser.add_argument(
        "--credential-map",
        type=Path,
        required=True,
        help="private JSON mapping provider ids to private credential file paths",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = collect_inventory(arguments.registry, arguments.credential_map)
    except DiscoveryFailure as exc:
        print(f"ops-provider-discovery: {exc.code}: {exc.safe_message}", file=sys.stderr)
        return 2
    except Exception:
        print("ops-provider-discovery: internal_error: collection could not start", file=sys.stderr)
        return 2
    json.dump(report, sys.stdout, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
