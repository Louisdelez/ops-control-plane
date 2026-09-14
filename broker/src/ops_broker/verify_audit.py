"""Offline audit-chain verifier."""

from __future__ import annotations

import argparse
from pathlib import Path

from .database import Database


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify an ops-broker SQLite audit hash chain")
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error(f"database does not exist: {args.database}")
    result = Database.verify_file(args.database)
    if result.valid:
        print(f"audit chain valid ({result.event_count} events)")
        return
    print(f"audit chain INVALID ({result.reason}, sequence={result.failure_seq})")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
