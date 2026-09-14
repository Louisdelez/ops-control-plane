#!/usr/bin/python3
"""Stream one OpenBao Raft snapshot directly into an age envelope.

The unencrypted snapshot exists only in the HTTPS and age pipes.  Publication
is atomic and is refused unless the two-use OpenBao token was revoked.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tomllib
from typing import Any, BinaryIO
import urllib.error
import urllib.request
import uuid


MAX_JSON_BYTES = 32 * 1024
MAX_CONFIG_BYTES = 32 * 1024
MAX_CREDENTIAL_BYTES = 768
MAX_AGE_HEADER_BYTES = 16 * 1024
CHUNK_BYTES = 1024 * 1024
BACKUP_ID = re.compile(r"\Aopenbao-raft-[0-9]{8}T[0-9]{6}\.[0-9]{6}Z\Z")
IDENTIFIER = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{15,127}\Z")
TOKEN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._~-]{15,511}\Z")
RECIPIENT = re.compile(r"\Aage1[023456789acdefghjklmnpqrstuvwxyz]{20,100}\Z")
SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
EXPECTED_ENDPOINT = "https://127.0.0.1:8200"
EXPECTED_POLICIES = {"openbao-backup-runtime"}


class BackupError(RuntimeError):
    pass


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise BackupError("OpenBao redirected a local request")


def _trusted_regular(path: Path, *, private: bool = False) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupError("a required file is unavailable") from exc
    forbidden = 0o077 if private else 0o022
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & forbidden
    ):
        raise BackupError("a required file has unsafe ownership or mode")
    return info


def _read_limited(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        value = os.read(descriptor, maximum + 1)
        if len(value) > maximum or os.read(descriptor, 1):
            raise BackupError("a required file is oversized")
        return value
    finally:
        os.close(descriptor)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BackupError(f"{name} is invalid")
    return value


def _exact_keys(value: dict[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise BackupError(f"{name} fields are invalid")


def _positive_int(value: Any, name: str, *, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise BackupError(f"{name} is invalid")
    return value


def _positive_number(value: Any, name: str, *, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BackupError(f"{name} is invalid")
    result = float(value)
    if not 1.0 <= result <= maximum:
        raise BackupError(f"{name} is invalid")
    return result


def _absolute(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise BackupError(f"{name} is invalid")
    return Path(value)


def load_config(path: Path) -> dict[str, Any]:
    _trusted_regular(path)
    try:
        raw = tomllib.loads(_read_limited(path, MAX_CONFIG_BYTES).decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise BackupError("backup configuration is invalid") from exc
    _exact_keys(raw, {"backup", "credentials"}, "configuration")
    backup = _mapping(raw["backup"], "backup configuration")
    credentials = _mapping(raw["credentials"], "credential configuration")
    _exact_keys(
        backup,
        {
            "endpoint",
            "ca_file",
            "recipient_file",
            "destination",
            "retention_count",
            "max_snapshot_bytes",
            "timeout_seconds",
        },
        "backup configuration",
    )
    _exact_keys(credentials, {"role_id", "secret_id"}, "credential configuration")
    if backup["endpoint"] != EXPECTED_ENDPOINT:
        raise BackupError("OpenBao endpoint must be the pinned loopback TLS origin")
    role_name = credentials["role_id"]
    secret_name = credentials["secret_id"]
    if role_name != "openbao-backup-role-id" or secret_name != "openbao-backup-secret-id":
        raise BackupError("credential names are not the reviewed pair")
    return {
        "endpoint": EXPECTED_ENDPOINT,
        "ca_file": _absolute(backup["ca_file"], "CA file"),
        "recipient_file": _absolute(backup["recipient_file"], "recipient file"),
        "destination": _absolute(backup["destination"], "destination"),
        "retention_count": _positive_int(backup["retention_count"], "retention", maximum=365),
        "max_snapshot_bytes": _positive_int(
            backup["max_snapshot_bytes"], "snapshot limit", maximum=8 * 1024**3
        ),
        "timeout": _positive_number(backup["timeout_seconds"], "timeout", maximum=1800.0),
        "role_name": role_name,
        "secret_name": secret_name,
    }


def _credential(name: str) -> str:
    directory_raw = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if not directory_raw.startswith("/") or "\x00" in directory_raw:
        raise BackupError("systemd credential directory is unavailable")
    directory = Path(directory_raw)
    try:
        directory_info = directory.lstat()
    except OSError as exc:
        raise BackupError("systemd credential directory is unavailable") from exc
    # systemd deliberately exposes service credentials as root-owned files in
    # a private, ACL-gated directory.  They are readable by the service user,
    # but ownership must remain with root and neither path may be writable by
    # group or others.
    if (
        stat.S_ISLNK(directory_info.st_mode)
        or not stat.S_ISDIR(directory_info.st_mode)
        or directory_info.st_uid != 0
        or stat.S_IMODE(directory_info.st_mode) & 0o022
    ):
        raise BackupError("systemd credential directory is unsafe")
    path = directory / name
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupError("a runtime credential is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != 0
        or stat.S_IMODE(info.st_mode) & 0o022
    ):
        raise BackupError("a runtime credential is unsafe")
    raw = _read_limited(path, MAX_CREDENTIAL_BYTES).rstrip(b"\r\n")
    try:
        value = raw.decode("ascii", errors="strict")
    except UnicodeError as exc:
        raise BackupError("a runtime credential is invalid") from exc
    if not IDENTIFIER.fullmatch(value):
        raise BackupError("a runtime credential is invalid")
    return value


def _opener(ca_file: Path) -> urllib.request.OpenerDirector:
    _trusted_regular(ca_file)
    context = ssl.create_default_context(cafile=str(ca_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RejectRedirects(),
        urllib.request.HTTPSHandler(context=context),
    )


def _json_request(
    opener: urllib.request.OpenerDirector,
    method: str,
    path: str,
    *,
    timeout: float,
    payload: dict[str, str] | None = None,
    token: str | None = None,
    no_content: bool = False,
) -> dict[str, Any] | None:
    if method not in {"POST"} or not path.startswith("/v1/"):
        raise BackupError("internal OpenBao request was rejected")
    body = json.dumps(payload or {}, separators=(",", ":")).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "openbao-raft-backup/1",
    }
    if token is not None:
        if not TOKEN.fullmatch(token):
            raise BackupError("OpenBao token format is invalid")
        headers["X-Vault-Token"] = token
    request = urllib.request.Request(EXPECTED_ENDPOINT + path, data=body, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            allowed = {200, 204} if no_content else {200}
            if response.status not in allowed:
                raise BackupError("OpenBao rejected an operation")
            raw = response.read(MAX_JSON_BYTES + 1)
    except (OSError, ssl.SSLError, urllib.error.URLError) as exc:
        raise BackupError("local OpenBao request failed") from exc
    if len(raw) > MAX_JSON_BYTES:
        raise BackupError("OpenBao returned oversized JSON")
    if no_content:
        return None
    try:
        result = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError("OpenBao returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise BackupError("OpenBao returned an invalid document")
    return result


def _login(opener: urllib.request.OpenerDirector, role_id: str, secret_id: str, timeout: float) -> str:
    document = _json_request(
        opener,
        "POST",
        "/v1/auth/approle/login",
        timeout=timeout,
        payload={"role_id": role_id, "secret_id": secret_id},
    )
    auth = document.get("auth") if isinstance(document, dict) else None
    if not isinstance(auth, dict):
        raise BackupError("OpenBao AppRole login failed")
    token = auth.get("client_token")
    policies = auth.get("token_policies")
    uses = auth.get("num_uses")
    duration = auth.get("lease_duration")
    if (
        not isinstance(token, str)
        or not TOKEN.fullmatch(token)
        or not isinstance(policies, list)
        or set(policies) != EXPECTED_POLICIES
        or uses != 2
        or isinstance(duration, bool)
        or not isinstance(duration, int)
        or not 1 <= duration <= 300
        # OpenBao may mark a finite service token renewable even though this
        # policy deliberately grants no renew-self capability.  Its explicit
        # maximum TTL still caps it at five minutes.
        or type(auth.get("renewable")) is not bool
    ):
        raise BackupError("OpenBao issued an unsafe backup token")
    return token


def _recipient(path: Path) -> str:
    _trusted_regular(path)
    try:
        value = _read_limited(path, 256).decode("ascii", errors="strict").strip()
    except UnicodeError as exc:
        raise BackupError("age recipient is invalid") from exc
    if not RECIPIENT.fullmatch(value):
        raise BackupError("age recipient is invalid")
    return value


def _safe_destination(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise BackupError("backup destination is unavailable") from exc
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise BackupError("backup destination is unsafe")


def _stream_snapshot(
    opener: urllib.request.OpenerDirector,
    token: str,
    recipient: str,
    output: Path,
    maximum: int,
    timeout: float,
) -> int:
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    process: subprocess.Popen[bytes] | None = None
    count = 0
    try:
        process = subprocess.Popen(
            ["/usr/bin/age", "--recipient", recipient],
            stdin=subprocess.PIPE,
            stdout=descriptor,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            env={"PATH": "/usr/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        )
        request = urllib.request.Request(
            EXPECTED_ENDPOINT + "/v1/sys/storage/raft/snapshot",
            headers={"Accept": "application/octet-stream", "X-Vault-Token": token, "User-Agent": "openbao-raft-backup/1"},
            method="GET",
        )
        try:
            response = opener.open(request, timeout=timeout)
        except (OSError, ssl.SSLError, urllib.error.URLError) as exc:
            raise BackupError("OpenBao snapshot request failed") from exc
        with response:
            if response.status != 200:
                raise BackupError("OpenBao snapshot request was rejected")
            announced = response.headers.get("Content-Length")
            if announced is not None:
                try:
                    announced_size = int(announced)
                except ValueError as exc:
                    raise BackupError("OpenBao snapshot length is invalid") from exc
                if not 1 <= announced_size <= maximum:
                    raise BackupError("OpenBao snapshot length is outside the configured bound")
            assert process.stdin is not None
            while True:
                block = response.read(CHUNK_BYTES)
                if not block:
                    break
                count += len(block)
                if count > maximum:
                    raise BackupError("OpenBao snapshot exceeded the configured bound")
                try:
                    process.stdin.write(block)
                except BrokenPipeError as exc:
                    raise BackupError("age encryption stopped unexpectedly") from exc
        if count < 1:
            raise BackupError("OpenBao returned an empty snapshot")
        assert process.stdin is not None
        process.stdin.close()
        if process.wait(timeout=min(timeout, 60.0)) != 0:
            raise BackupError("age encryption failed")
        os.fsync(descriptor)
    except BaseException:
        if process is not None:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            if process.poll() is None:
                process.kill()
            process.wait()
        raise
    finally:
        os.close(descriptor)
    return count


def _inspect_age(path: Path) -> int:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise BackupError("age artifact is unsafe")
    if stat.S_IMODE(info.st_mode) & 0o077 or info.st_size < 128:
        raise BackupError("age artifact is invalid")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        header = os.read(descriptor, min(info.st_size, MAX_AGE_HEADER_BYTES))
    finally:
        os.close(descriptor)
    if not header.startswith(b"age-encryption.org/v1\n"):
        raise BackupError("age artifact header is invalid")
    if b"\n-> X25519 " not in header or b"\n--- " not in header:
        raise BackupError("age X25519 stanza is missing")
    return info.st_size


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        while True:
            block = os.read(descriptor, CHUNK_BYTES)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _write_file(path: Path, value: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(value)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_backup(path: Path) -> None:
    if path.parent.name == "" or not BACKUP_ID.fullmatch(path.name):
        raise BackupError("retention target is unsafe")
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        raise BackupError("retention target is unsafe")
    expected = {"snapshot.age", "snapshot.age.sha256", "manifest.json"}
    entries = list(path.iterdir())
    if {entry.name for entry in entries} != expected:
        raise BackupError("retention target contents are unexpected")
    for entry in entries:
        child = entry.lstat()
        if stat.S_ISLNK(child.st_mode) or not stat.S_ISREG(child.st_mode) or child.st_uid != os.geteuid():
            raise BackupError("retention target contents are unsafe")
    for name in sorted(expected):
        (path / name).unlink()
    path.rmdir()


def _retention(destination: Path, keep: int) -> int:
    backups: list[Path] = []
    for entry in destination.iterdir():
        if entry.name.startswith(".pending-"):
            continue
        if BACKUP_ID.fullmatch(entry.name):
            backups.append(entry)
    backups.sort(key=lambda item: item.name, reverse=True)
    removed = 0
    for old in backups[keep:]:
        _remove_backup(old)
        removed += 1
    if removed:
        _fsync_directory(destination)
    return removed


def create_backup(config: dict[str, Any]) -> dict[str, Any]:
    destination = config["destination"]
    _safe_destination(destination)
    recipient = _recipient(config["recipient_file"])
    opener = _opener(config["ca_file"])
    role_id = _credential(config["role_name"])
    secret_id = _credential(config["secret_name"])
    timestamp = datetime.now(timezone.utc)
    backup_id = "openbao-raft-" + timestamp.strftime("%Y%m%dT%H%M%S.%fZ")
    if not BACKUP_ID.fullmatch(backup_id):
        raise BackupError("generated backup identifier is invalid")
    pending = destination / (".pending-" + backup_id + "-" + uuid.uuid4().hex)
    final = destination / backup_id
    if os.path.lexists(pending) or os.path.lexists(final):
        raise BackupError("backup destination already exists")
    pending.mkdir(mode=0o700)
    token: str | None = None
    operation_error: BaseException | None = None
    revocation_error: BaseException | None = None
    raw_size = 0
    try:
        token = _login(opener, role_id, secret_id, config["timeout"])
        role_id = secret_id = ""
        try:
            raw_size = _stream_snapshot(
                opener,
                token,
                recipient,
                pending / "snapshot.age",
                config["max_snapshot_bytes"],
                config["timeout"],
            )
        except BaseException as exc:
            operation_error = exc
        finally:
            try:
                _json_request(
                    opener,
                    "POST",
                    "/v1/auth/token/revoke-self",
                    timeout=config["timeout"],
                    token=token,
                    no_content=True,
                )
            except BaseException as exc:
                revocation_error = exc
            token = None
        if operation_error is not None or revocation_error is not None:
            raise BackupError("snapshot acquisition or token revocation failed")
        age_size = _inspect_age(pending / "snapshot.age")
        digest = _sha256(pending / "snapshot.age")
        if not SHA256.fullmatch(digest):
            raise BackupError("encrypted snapshot digest is invalid")
        _write_file(pending / "snapshot.age.sha256", f"{digest}  snapshot.age\n".encode("ascii"))
        manifest = {
            "schema_version": 1,
            "backup_id": backup_id,
            "created_at": timestamp.isoformat().replace("+00:00", "Z"),
            "artifact": {
                "name": "snapshot.age",
                "encryption": "age-x25519",
                "sha256": digest,
                "encrypted_bytes": age_size,
                "snapshot_bytes": raw_size,
            },
            "verification": {
                "http_status": "ok",
                "stream_nonempty": True,
                "age_header": "ok",
                "sha256": "ok",
                "token_revoked": True,
                "restore_drill": "offline-required",
            },
        }
        _write_file(
            pending / "manifest.json",
            (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
        )
        _fsync_directory(pending)
        os.replace(pending, final)
        _fsync_directory(destination)
        removed = _retention(destination, config["retention_count"])
        return {
            "backup_id": backup_id,
            "encrypted_bytes": age_size,
            "retention_removed": removed,
            "status": "ok",
        }
    finally:
        role_id = secret_id = recipient = ""
        if pending.exists() and not pending.is_symlink():
            shutil.rmtree(pending, ignore_errors=True)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create an age-encrypted OpenBao Raft backup")
    parser.add_argument("--config", default="/etc/openbao-backup/backup.toml")
    options = parser.parse_args(arguments)
    try:
        result = create_backup(load_config(Path(options.config)))
    except (BackupError, OSError, subprocess.SubprocessError):
        print("openbao-raft-backup: backup failed", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
