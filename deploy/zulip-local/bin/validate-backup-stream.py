#!/usr/bin/python3
"""Fail-closed structural validation for a decrypted Zulip volume snapshot."""

from __future__ import annotations

from pathlib import PurePosixPath
import re
import sys
import tarfile


MAX_MEMBERS = 250_000
MAX_TOTAL_SIZE = 100 * 1024 * 1024 * 1024
DUMP_RE = re.compile(r"\A(?:\./)?backups/backup-[A-Za-z0-9_.:+-]+\.sql\Z")
SENSITIVE_PATHS = (
    "./zulip-secrets.conf",
    "./certs/manual",
    "./etc-zulip/zulip-secrets.conf",
)


def main() -> int:
    members = 0
    total_size = 0
    has_dump = False
    try:
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz") as archive:
            for member in archive:
                members += 1
                total_size += member.size
                if members > MAX_MEMBERS or total_size > MAX_TOTAL_SIZE:
                    raise ValueError("archive bounds exceeded")
                # GNU tar emits this directory member first for the exact
                # official snapshot command `tar ... -C /data .`.
                if member.name in {".", "./"}:
                    if not member.isdir() or member.size != 0:
                        raise ValueError("unsafe archive root entry")
                    continue
                if not member.name.startswith("./"):
                    raise ValueError("non-canonical archive path")
                relative_name = member.name[2:]
                raw_parts = relative_name.split("/")
                path = PurePosixPath(relative_name)
                if (
                    path.is_absolute()
                    or not path.parts
                    or any(part in {"", ".", ".."} for part in raw_parts)
                ):
                    raise ValueError("unsafe archive path")
                if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                    raise ValueError("unsafe archive entry type")
                if not (member.isfile() or member.isdir()):
                    raise ValueError("unsupported archive entry type")
                if any(
                    member.name == sensitive or member.name.startswith(sensitive + "/")
                    for sensitive in SENSITIVE_PATHS
                ):
                    raise ValueError("archive contains an excluded runtime secret")
                if DUMP_RE.fullmatch(member.name):
                    if member.size < 5:
                        raise ValueError("empty database dump")
                    dump_stream = archive.extractfile(member)
                    if dump_stream is None or dump_stream.read(5) != b"PGDMP":
                        raise ValueError("invalid PostgreSQL custom dump")
                    has_dump = True
    except (OSError, tarfile.TarError, ValueError):
        print("The encrypted Zulip backup failed structural validation.", file=sys.stderr)
        return 1
    if members == 0 or not has_dump:
        print("The encrypted Zulip backup contains no official database dump.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
