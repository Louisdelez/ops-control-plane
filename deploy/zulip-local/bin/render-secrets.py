#!/usr/bin/python3
"""Render the exact Zulip server KV object into root-only /run files."""

from __future__ import annotations

import os
from pathlib import Path
import sys

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent / "lib"))

from zulip_local import (  # noqa: E402
    OpenBaoClient,
    RUN_DIR,
    SERVER_KV_API_PATH,
    ZulipLocalError,
    acquire_deployment_lock,
    read_credential,
    render_runtime_files,
)


SERVICE_NAME = "zulip-local-secrets.service"
CREDENTIAL_DIRECTORY = Path(f"/run/credentials/{SERVICE_NAME}")


def main() -> int:
    if os.geteuid() != 0:
        raise ZulipLocalError("The Zulip secret renderer must run as root.")
    _lock_descriptor = acquire_deployment_lock()
    if os.environ.get("CREDENTIALS_DIRECTORY") != str(CREDENTIAL_DIRECTORY):
        raise ZulipLocalError("The private systemd credential directory is unavailable.")
    role_id = read_credential(CREDENTIAL_DIRECTORY / "zulip-local-runtime-role-id")
    secret_id = read_credential(CREDENTIAL_DIRECTORY / "zulip-local-runtime-secret-id")
    client = OpenBaoClient()
    token: str | None = None
    try:
        token = client.login_approle(role_id, secret_id)
        result = client.read_kv(SERVER_KV_API_PATH, token)
        if result is None:
            raise ZulipLocalError("The required Zulip server KV object is absent.")
        data, _version = result
        render_runtime_files(data, RUN_DIR)
    finally:
        if token is not None:
            client.revoke(token)
        role_id = ""
        secret_id = ""
        token = None
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ZulipLocalError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
