#!/usr/bin/python3
"""Idempotently provision the local realm, owner, bot, channels and bridge KV."""

from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PACKAGE_DIR / "lib"))

from zulip_local import (  # noqa: E402
    BOT_KV_API_PATH,
    LOCK_PATH,
    OpenBaoClient,
    ZULIP_ORIGIN,
    ZulipClient,
    ZulipLocalError,
    acquire_deployment_lock,
    api_integer,
    decrypt_systemd_credential,
    load_defaults,
    merge_bot_secret,
    validate_bot_secret,
)


COMPOSE = PACKAGE_DIR / "compose.yaml"
DEFAULTS = PACKAGE_DIR / "config" / "defaults.env"
RUNTIME_PASSWORD = Path("/run/zulip-local/admin/admin-password")
CREDSTORE = Path("/etc/credstore.encrypted")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provision Zulip without putting passwords or API keys in argv/environment/logs."
    )
    parser.add_argument(
        "--admin-password-file",
        type=Path,
        help="Root-only password file; without this option a hidden native prompt is used",
    )
    parser.add_argument(
        "--admin-password-fd",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--reset-admin-password",
        action="store_true",
        help="Feed the password file to Zulip's native change_password prompt before reconciliation",
    )
    parser.add_argument(
        "--validate-password-file-only",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--deployment-lock-fd",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--approver-id-fd",
        type=int,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def inherit_deployment_lock(descriptor: int) -> int:
    if descriptor < 3 or descriptor > 1024:
        raise ZulipLocalError("The inherited Zulip deployment lock is invalid.")
    try:
        opened_stat = os.fstat(descriptor)
        path_stat = os.lstat(LOCK_PATH)
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or opened_stat.st_uid != 0
            or stat.S_IMODE(opened_stat.st_mode) & 0o077
            or opened_stat.st_dev != path_stat.st_dev
            or opened_stat.st_ino != path_stat.st_ino
        ):
            raise ZulipLocalError("The inherited Zulip deployment lock is unsafe.")
        # This succeeds only for this inherited locked open-file description,
        # or acquires the same lock itself if it was not already held.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as exc:
        raise ZulipLocalError("The inherited Zulip deployment lock is unavailable.") from exc
    return descriptor


def write_approver_id(descriptor: int, raw_identifier: str) -> None:
    """Return the reconciled non-secret owner ID over an anonymous pipe."""

    if descriptor < 3 or descriptor > 1024 or not re.fullmatch(r"[1-9][0-9]{0,18}", raw_identifier):
        raise ZulipLocalError("The inherited approver result descriptor is invalid.")
    try:
        info = os.fstat(descriptor)
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if not stat.S_ISFIFO(info.st_mode) or not re.fullmatch(r"pipe:\[\d+\]", target):
            raise ZulipLocalError("The inherited approver result descriptor is not a pipe.")
        payload = raw_identifier.encode("ascii")
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
    except OSError as exc:
        raise ZulipLocalError("The approver result pipe is unavailable.") from exc
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def compose_command(
    *arguments: str, capture: bool = False, stdin_data: bytes | None = None
) -> subprocess.CompletedProcess[bytes]:
    command = [
        "/usr/bin/docker",
        "compose",
        "--project-name",
        "zulip-local",
        "--file",
        str(COMPOSE),
        *arguments,
    ]
    options: dict[str, Any] = {
        "check": False,
        "stdout": subprocess.PIPE if capture else subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if stdin_data is None:
        options["stdin"] = subprocess.DEVNULL
    else:
        options["input"] = stdin_data
    return subprocess.run(command, **options)


def _read_anonymous_password_pipe(descriptor: int) -> bytes:
    if descriptor < 3 or descriptor > 1024:
        raise ZulipLocalError("The inherited password descriptor is invalid.")
    try:
        info = os.fstat(descriptor)
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if not stat.S_ISFIFO(info.st_mode) or not re.fullmatch(r"pipe:\[\d+\]", target):
            raise ZulipLocalError("The inherited password descriptor is not an anonymous pipe.")
        chunks: list[bytes] = []
        remaining = 4097
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    except OSError as exc:
        raise ZulipLocalError("The inherited password pipe is unavailable.") from exc
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def load_password(path: Path | None, descriptor: int | None = None) -> str:
    if path is not None and descriptor is not None:
        raise ZulipLocalError("Use exactly one administrator password input.")
    if descriptor is not None:
        raw = _read_anonymous_password_pipe(descriptor)
        if not raw or len(raw) > 4096:
            raise ZulipLocalError("The inherited administrator password is invalid.")
        try:
            password = raw.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ZulipLocalError("The inherited administrator password is not UTF-8.") from exc
        raw = b""
    elif path is None:
        first = getpass.getpass("Zulip administrator password (never stored): ")
        second = getpass.getpass("Confirm Zulip administrator password: ")
        if first != second:
            first = second = ""
            raise ZulipLocalError("The administrator password confirmation differs.")
        password = first
        first = second = ""
    else:
        try:
            path_stat = os.lstat(path)
            if (
                not stat.S_ISREG(path_stat.st_mode)
                or path_stat.st_uid != 0
                or stat.S_IMODE(path_stat.st_mode) & 0o077
                or path_stat.st_size > 4096
            ):
                raise ZulipLocalError("The administrator password file has unsafe metadata.")
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                opened_stat = os.fstat(descriptor)
                if (
                    opened_stat.st_dev != path_stat.st_dev
                    or opened_stat.st_ino != path_stat.st_ino
                    or not stat.S_ISREG(opened_stat.st_mode)
                ):
                    raise ZulipLocalError("The administrator password file changed during use.")
                raw = os.read(descriptor, 4097)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise ZulipLocalError("The administrator password file is unavailable.") from exc
        if len(raw) > 4096:
            raise ZulipLocalError("The administrator password file is oversized.")
        try:
            password = raw.rstrip(b"\r\n").decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ZulipLocalError("The administrator password file is not UTF-8.") from exc
    if (
        len(password) < 12
        or len(password) > 100
        or password != password.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in password)
    ):
        password = ""
        raise ZulipLocalError("The administrator password does not meet the local input policy.")
    return password


def write_runtime_password(password: str) -> None:
    parent_stat = os.lstat(RUNTIME_PASSWORD.parent)
    if (
        not stat.S_ISDIR(parent_stat.st_mode)
        or parent_stat.st_uid != 0
        or stat.S_IMODE(parent_stat.st_mode) & 0o077
    ):
        raise ZulipLocalError("The private administrator runtime directory is unsafe.")
    try:
        path_stat = os.lstat(RUNTIME_PASSWORD)
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_uid != 0
            or stat.S_IMODE(path_stat.st_mode) != 0o444
        ):
            raise ZulipLocalError("The transient administrator secret has unsafe metadata.")
        descriptor = os.open(
            RUNTIME_PASSWORD,
            os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
    except OSError as exc:
        raise ZulipLocalError("The transient administrator secret is unavailable.") from exc
    try:
        opened_stat = os.fstat(descriptor)
        if opened_stat.st_dev != path_stat.st_dev or opened_stat.st_ino != path_stat.st_ino:
            raise ZulipLocalError("The transient administrator secret changed during use.")
        encoded = password.encode("utf-8")
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fchmod(descriptor, 0o444)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def clear_runtime_password() -> None:
    try:
        descriptor = os.open(
            RUNTIME_PASSWORD,
            os.O_WRONLY | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW,
        )
        os.fsync(descriptor)
        os.close(descriptor)
    except OSError as exc:
        raise ZulipLocalError("The transient administrator secret could not be cleared.") from exc


def realm_rows() -> list[str]:
    result = compose_command(
        "exec",
        "-T",
        "-u",
        "zulip",
        "zulip",
        "/home/zulip/deployments/current/manage.py",
        "list_realms",
        capture=True,
    )
    if result.returncode != 0 or len(result.stdout) > 256 * 1024:
        raise ZulipLocalError("The official list_realms command failed.")
    text = ANSI_RE.sub("", result.stdout.decode("utf-8", errors="replace"))
    return [line for line in text.splitlines() if re.match(r"^\s*[1-9][0-9]*\s+", line)]


def create_realm(defaults: dict[str, str]) -> None:
    result = compose_command(
        "exec",
        "-T",
        "-u",
        "zulip",
        "zulip",
        "/home/zulip/deployments/current/manage.py",
        "create_realm",
        "--string-id=",
        "--password-file=/run/secrets/admin_bootstrap_password",
        defaults["ZULIP_LOCAL_REALM_NAME"],
        defaults["ZULIP_LOCAL_ADMIN_EMAIL"],
        defaults["ZULIP_LOCAL_ADMIN_NAME"],
    )
    if result.returncode != 0:
        raise ZulipLocalError("The official create_realm command failed.")


def reset_owner_password(defaults: dict[str, str], password: str) -> None:
    # change_password has no --password-file in Zulip 12.2. Its official native
    # prompt reads twice from stdin; the value therefore still never reaches
    # argv, the environment, or logs.
    prompt_input = (password + "\n" + password + "\n").encode("utf-8")
    result = compose_command(
        "exec",
        "-T",
        "-u",
        "zulip",
        "zulip",
        "/home/zulip/deployments/current/manage.py",
        "change_password",
        defaults["ZULIP_LOCAL_ADMIN_EMAIL"],
        "--realm=",
        stdin_data=prompt_input,
    )
    prompt_input = b""
    if result.returncode != 0:
        raise ZulipLocalError("The official change_password command failed.")


def api_array(document: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = document.get(key)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ZulipLocalError("Zulip returned an invalid collection.")
    return value


def provision_api(defaults: dict[str, str], password: str) -> dict[str, str]:
    client = ZulipClient()
    email = defaults["ZULIP_LOCAL_ADMIN_EMAIL"]
    try:
        admin_key = client.fetch_api_key(email, password)
    except ZulipLocalError:
        rows = realm_rows()
        if rows:
            raise ZulipLocalError(
                "A realm already exists, but the supplied owner credentials were rejected."
            ) from None
        create_realm(defaults)
        admin_key = client.fetch_api_key(email, password)
    admin_auth = (email, admin_key)
    clear_runtime_password()
    password = ""

    _, own_document = client.request("GET", "/api/v1/users/me", auth=admin_auth)
    if own_document.get("is_owner") is not True:
        raise ZulipLocalError("The configured administrator is not the realm owner.")
    admin_id = api_integer(own_document, "user_id")

    channels = [
        {
            "name": defaults["ZULIP_LOCAL_APPROVAL_STREAM"],
            "description": "Validated operations approvals.",
        },
        {
            "name": defaults["ZULIP_LOCAL_ALERT_STREAM"],
            "description": "Operational alerts from the control plane.",
        },
        {
            "name": defaults["ZULIP_LOCAL_DAILY_STREAM"],
            "description": "Daily operational reports.",
        },
    ]
    client.request(
        "POST",
        "/api/v1/users/me/subscriptions",
        fields={"subscriptions": json.dumps(channels, separators=(",", ":"))},
        auth=admin_auth,
    )

    _, bots_document = client.request("GET", "/api/v1/bots", auth=admin_auth)
    bots = api_array(bots_document, "bots")
    expected_bot_email = (
        defaults["ZULIP_LOCAL_BOT_SHORT_NAME"] + "-bot@" + defaults["ZULIP_LOCAL_HOSTNAME"]
    )
    matches = [bot for bot in bots if bot.get("username") == expected_bot_email]
    if len(matches) > 1:
        raise ZulipLocalError("Multiple bots match the reviewed bridge identity.")
    if matches:
        bot_key = matches[0].get("api_key")
        if not isinstance(bot_key, str):
            raise ZulipLocalError("The existing bridge bot has no retrievable API key.")
    else:
        _, created = client.request(
            "POST",
            "/api/v1/bots",
            fields={
                "full_name": defaults["ZULIP_LOCAL_BOT_NAME"],
                "short_name": defaults["ZULIP_LOCAL_BOT_SHORT_NAME"],
            },
            auth=admin_auth,
        )
        bot_key = created.get("api_key")
        if not isinstance(bot_key, str):
            raise ZulipLocalError("Zulip did not return the bridge bot API key.")

    _, members_document = client.request("GET", "/api/v1/users", auth=admin_auth)
    members = api_array(members_document, "members")
    bot_members = [
        member
        for member in members
        if member.get("email") == expected_bot_email and member.get("is_bot") is True
    ]
    if len(bot_members) != 1:
        raise ZulipLocalError("The bridge bot user identity is ambiguous.")
    bot_id = api_integer(bot_members[0], "user_id")
    bot_auth = (expected_bot_email, bot_key)
    client.request(
        "POST",
        "/api/v1/users/me/subscriptions",
        fields={"subscriptions": json.dumps(channels, separators=(",", ":"))},
        auth=bot_auth,
    )

    stream_ids: dict[str, int] = {}
    for field_name, stream_name in (
        ("approval", defaults["ZULIP_LOCAL_APPROVAL_STREAM"]),
        ("alert", defaults["ZULIP_LOCAL_ALERT_STREAM"]),
        ("daily", defaults["ZULIP_LOCAL_DAILY_STREAM"]),
    ):
        _, stream_document = client.request(
            "GET", "/api/v1/get_stream_id", fields={"stream": stream_name}, auth=bot_auth
        )
        stream_ids[field_name] = api_integer(stream_document, "stream_id")

    result = {
        "realm_url": ZULIP_ORIGIN,
        "bot_email": expected_bot_email,
        "api_key": bot_key,
        "bot_user_id": str(bot_id),
        "approver_user_ids": str(admin_id),
        "approval_stream": defaults["ZULIP_LOCAL_APPROVAL_STREAM"],
        "approval_stream_id": str(stream_ids["approval"]),
        "approval_topic": defaults["ZULIP_LOCAL_APPROVAL_TOPIC"],
        "alert_stream": defaults["ZULIP_LOCAL_ALERT_STREAM"],
        "alert_stream_id": str(stream_ids["alert"]),
        "alert_topic": defaults["ZULIP_LOCAL_ALERT_TOPIC"],
        "daily_stream": defaults["ZULIP_LOCAL_DAILY_STREAM"],
        "daily_stream_id": str(stream_ids["daily"]),
        "daily_topic": defaults["ZULIP_LOCAL_DAILY_TOPIC"],
        "ca_bundle": "/etc/pki/ca-trust/source/anchors/zulip-ops-local-ca.crt",
    }
    admin_key = ""
    bot_key = ""
    return validate_bot_secret(result)


def write_bridge_object(data: dict[str, str]) -> None:
    role_name = "zulip-local-bootstrap"
    role_id = decrypt_systemd_credential(
        CREDSTORE / f"{role_name}-role-id", f"{role_name}-role-id"
    )
    secret_id = decrypt_systemd_credential(
        CREDSTORE / f"{role_name}-secret-id", f"{role_name}-secret-id"
    )
    client = OpenBaoClient()
    token: str | None = None
    try:
        token = client.login_approle(role_id, secret_id)
        existing = client.read_kv(BOT_KV_API_PATH, token)
        version = 0 if existing is None else existing[1]
        reconciled = merge_bot_secret(None if existing is None else existing[0], data)
        if existing is None or existing[0] != reconciled:
            client.write_kv(BOT_KV_API_PATH, reconciled, token, cas=version)
        reconciled.clear()
    finally:
        if token is not None:
            client.revoke(token)
        role_id = secret_id = ""
        token = None


def main() -> int:
    args = parse_args()
    if os.geteuid() != 0:
        raise ZulipLocalError("Zulip provisioning must run as root.")
    if args.validate_password_file_only:
        if (
            args.admin_password_file is None
            or args.admin_password_fd is not None
            or args.reset_admin_password
            or args.deployment_lock_fd is not None
            or args.approver_id_fd is not None
        ):
            raise ZulipLocalError("Password-file validation requires one file and no action.")
        password = load_password(args.admin_password_file)
        password = ""
        return 0
    if args.deployment_lock_fd is None:
        _lock_descriptor = acquire_deployment_lock()
    else:
        _lock_descriptor = inherit_deployment_lock(args.deployment_lock_fd)
    defaults = load_defaults(DEFAULTS)
    health = subprocess.run(
        [str(SCRIPT_DIR / "health.sh"), "--quiet"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if health.returncode != 0:
        raise ZulipLocalError("The local Zulip stack is not healthy.")

    password = load_password(args.admin_password_file, args.admin_password_fd)
    bot_document: dict[str, str] = {}
    try:
        write_runtime_password(password)
        if args.reset_admin_password:
            reset_owner_password(defaults, password)
        bot_document = provision_api(defaults, password)
        password = ""
        write_bridge_object(bot_document)
        if args.approver_id_fd is not None:
            write_approver_id(args.approver_id_fd, bot_document["approver_user_ids"])
    finally:
        password = ""
        bot_document.clear()
        clear_runtime_password()
    print("Realm, owner, bot and channels are reconciled; the bridge object is in OpenBao.")
    print("No password, API key or numeric identity was displayed.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ZulipLocalError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
