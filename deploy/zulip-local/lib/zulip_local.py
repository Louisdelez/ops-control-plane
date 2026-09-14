"""Security boundaries shared by the local Zulip deployment tools.

The module deliberately uses only the Python standard library.  Exceptions are
constant, non-secret-bearing messages; response bodies and credentials are
never interpolated into logs or subprocess arguments.
"""

from __future__ import annotations

import base64
import fcntl
import json
import math
import os
from pathlib import Path
import re
import ssl
import stat
import subprocess
from typing import Any, Mapping
import urllib.error
import urllib.parse
import urllib.request
import uuid


OPENBAO_ORIGIN = "https://127.0.0.1:8200"
OPENBAO_CA = Path("/etc/openbao.d/tls/ca.crt")
SERVER_KV_API_PATH = "/v1/kv-infra-shared/data/zulip/server"
BOT_KV_API_PATH = "/v1/kv-infra-shared/data/zulip/bot"
ZULIP_ORIGIN = "https://zulip.ops.local:8443"
ZULIP_CA = Path("/run/zulip-local/tls/ca.crt")
RUN_DIR = Path("/run/zulip-local")
LOCK_PATH = Path("/run/lock/zulip-local/operation.lock")
MAX_RESPONSE_BYTES = 256 * 1024
MAX_CREDENTIAL_BYTES = 4096
HTTP_TIMEOUT_SECONDS = 5.0

SERVER_FIELDS = frozenset(
    {
        "avatar_salt",
        "camo_key",
        "postgres_password",
        "memcached_password",
        "rabbitmq_password",
        "redis_password",
        "secret_key",
        "shared_secret",
        "email_password",
        "zulip_org_id",
        "zulip_org_key",
        "tls_ca_certificate",
        "tls_certificate",
        "tls_private_key",
    }
)

ZULIP_SECRET_FIELDS = (
    "avatar_salt",
    "camo_key",
    "postgres_password",
    "memcached_password",
    "rabbitmq_password",
    "redis_password",
    "secret_key",
    "shared_secret",
    "email_password",
    "zulip_org_id",
    "zulip_org_key",
)

BOT_FIELDS = frozenset(
    {
        "realm_url",
        "bot_email",
        "api_key",
        "bot_user_id",
        "approver_user_ids",
        "approval_stream",
        "approval_stream_id",
        "approval_topic",
        "alert_stream",
        "alert_stream_id",
        "alert_topic",
        "daily_stream",
        "daily_stream_id",
        "daily_topic",
        "ca_bundle",
    }
)

BOT_TUNING_FIELDS = frozenset(
    {
        "poll_timeout_seconds",
        "retry_max_seconds",
        "producer_poll_seconds",
        "pending_page_size",
    }
)

DEFAULT_KEYS = frozenset(
    {
        "ZULIP_LOCAL_ORIGIN",
        "ZULIP_LOCAL_HOSTNAME",
        "ZULIP_LOCAL_REALM_NAME",
        "ZULIP_LOCAL_ADMIN_EMAIL",
        "ZULIP_LOCAL_ADMIN_NAME",
        "ZULIP_LOCAL_BOT_SHORT_NAME",
        "ZULIP_LOCAL_BOT_NAME",
        "ZULIP_LOCAL_APPROVAL_STREAM",
        "ZULIP_LOCAL_APPROVAL_TOPIC",
        "ZULIP_LOCAL_ALERT_STREAM",
        "ZULIP_LOCAL_ALERT_TOPIC",
        "ZULIP_LOCAL_DAILY_STREAM",
        "ZULIP_LOCAL_DAILY_TOPIC",
    }
)

_TOKEN_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._~+/=-]{15,4095}\Z")
_IDENTIFIER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{15,511}\Z")
_EMAIL_RE = re.compile(r"\A[^\s@]+@[^\s@]+\Z")
_POSITIVE_INT_RE = re.compile(r"\A[1-9][0-9]{0,18}\Z")
_DECIMAL_RE = re.compile(r"\A(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)\Z")


class ZulipLocalError(RuntimeError):
    """An intentionally non-secret-bearing boundary error."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise ZulipLocalError("A redirected security-boundary request was rejected.")


def _ssl_opener(ca_file: Path) -> urllib.request.OpenerDirector:
    context = ssl.create_default_context(cafile=str(ca_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RejectRedirects(),
        urllib.request.HTTPSHandler(context=context),
    )


def _decode_json_response(response: Any) -> dict[str, Any]:
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise ZulipLocalError("A remote endpoint returned an oversized response.")
    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ZulipLocalError("A remote endpoint returned invalid JSON.") from exc
    if not isinstance(document, dict):
        raise ZulipLocalError("A remote endpoint returned an invalid document.")
    return document


def _bounded_text(value: Any, *, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise ZulipLocalError("A configuration value has an invalid type.")
    if not value or len(value) > maximum or value != value.strip():
        raise ZulipLocalError("A configuration value has an invalid length.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ZulipLocalError("A configuration value contains a control character.")
    return value


def _secret_text(value: Any) -> str:
    result = _bounded_text(value)
    if (
        len(result) < 24
        or result != result.strip()
        or "\n" in result
        or "\r" in result
        or not _TOKEN_RE.fullmatch(result)
    ):
        raise ZulipLocalError("A server secret does not match the reviewed schema.")
    return result


def _pem(value: Any, label: str, *, private: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 64 * 1024 or "\x00" in value:
        raise ZulipLocalError("A TLS value does not match the reviewed schema.")
    begin = f"-----BEGIN {label}-----"
    end = f"-----END {label}-----"
    if not value.startswith(begin + "\n") or not value.rstrip().endswith(end):
        raise ZulipLocalError("A TLS value does not match the reviewed schema.")
    if private and "PRIVATE KEY" not in label:
        raise ZulipLocalError("A TLS private key does not match the reviewed schema.")
    return value.rstrip("\n") + "\n"


def validate_server_secret(data: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(data, dict) or frozenset(data) != SERVER_FIELDS:
        raise ZulipLocalError("The Zulip server KV object has an unexpected schema.")
    result = {name: _secret_text(data[name]) for name in SERVER_FIELDS if not name.startswith("tls_")}
    result["tls_ca_certificate"] = _pem(data["tls_ca_certificate"], "CERTIFICATE")
    result["tls_certificate"] = _pem(data["tls_certificate"], "CERTIFICATE")
    private_value = data["tls_private_key"]
    if isinstance(private_value, str) and private_value.startswith("-----BEGIN EC PRIVATE KEY-----"):
        private_label = "EC PRIVATE KEY"
    else:
        private_label = "PRIVATE KEY"
    result["tls_private_key"] = _pem(private_value, private_label, private=True)
    try:
        organization_id = uuid.UUID(result["zulip_org_id"])
    except ValueError as exc:
        raise ZulipLocalError("The Zulip organization identifier is invalid.") from exc
    if organization_id.version != 4 or str(organization_id) != result["zulip_org_id"]:
        raise ZulipLocalError("The Zulip organization identifier is invalid.")
    return result


def validate_bot_secret(data: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(data, dict) or frozenset(data) != BOT_FIELDS:
        raise ZulipLocalError("The Zulip bot KV object has an unexpected schema.")
    result: dict[str, str] = {}
    for key, value in data.items():
        result[key] = _bounded_text(value, maximum=2048)
    if result["realm_url"] != ZULIP_ORIGIN:
        raise ZulipLocalError("The bot realm origin is not the reviewed local origin.")
    if not _EMAIL_RE.fullmatch(result["bot_email"]):
        raise ZulipLocalError("The bot email is invalid.")
    if not _TOKEN_RE.fullmatch(result["api_key"]):
        raise ZulipLocalError("The bot API key has an invalid shape.")
    for field in ("bot_user_id", "approval_stream_id", "alert_stream_id", "daily_stream_id"):
        if not _POSITIVE_INT_RE.fullmatch(result[field]):
            raise ZulipLocalError("A Zulip numeric identifier is invalid.")
    approvers = result["approver_user_ids"].split(",")
    if not approvers or any(not _POSITIVE_INT_RE.fullmatch(item) for item in approvers):
        raise ZulipLocalError("The approver identifier list is invalid.")
    if result["bot_user_id"] in approvers or len(set(approvers)) != len(approvers):
        raise ZulipLocalError("The approver identifier list is unsafe.")
    if result["ca_bundle"] != "/etc/pki/ca-trust/source/anchors/zulip-ops-local-ca.crt":
        raise ZulipLocalError("The bridge CA path is not the reviewed trust anchor.")
    return result


def merge_bot_secret(
    existing: Mapping[str, Any] | None, desired: Mapping[str, Any]
) -> dict[str, str]:
    """Reconcile generated identity while retaining reviewed bridge tuning."""

    result = validate_bot_secret(desired)
    if existing is None:
        return result
    required_existing = BOT_FIELDS - {"ca_bundle"}
    keys = frozenset(existing)
    if not required_existing.issubset(keys) or not keys.issubset(
        BOT_FIELDS | BOT_TUNING_FIELDS
    ):
        raise ZulipLocalError("The existing Zulip bot KV object has an unexpected schema.")

    # Validate the previous required contract before replacing its generated
    # identity. The local CA path is authoritative and intentionally replaced.
    previous_core = {key: existing[key] for key in required_existing}
    previous_core["ca_bundle"] = result["ca_bundle"]
    validate_bot_secret(previous_core)

    decimal_bounds = {
        "poll_timeout_seconds": (30.0, 300.0),
        "retry_max_seconds": (5.0, 300.0),
        "producer_poll_seconds": (5.0, 300.0),
    }
    for key, (minimum, maximum) in decimal_bounds.items():
        if key not in existing:
            continue
        raw = _bounded_text(existing[key], maximum=64)
        if not raw.isascii() or not _DECIMAL_RE.fullmatch(raw):
            raise ZulipLocalError("An existing Zulip bridge tuning value is invalid.")
        parsed = float(raw)
        if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
            raise ZulipLocalError("An existing Zulip bridge tuning value is invalid.")
        result[key] = format(parsed, "g")
    if "pending_page_size" in existing:
        raw = _bounded_text(existing["pending_page_size"], maximum=16)
        if not _POSITIVE_INT_RE.fullmatch(raw) or not 1 <= int(raw) <= 50:
            raise ZulipLocalError("An existing Zulip bridge page size is invalid.")
        result["pending_page_size"] = str(int(raw))
    return result


class OpenBaoClient:
    def __init__(self, opener: urllib.request.OpenerDirector | None = None) -> None:
        self.opener = opener or _ssl_opener(OPENBAO_CA)

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        token: str | None = None,
        allowed_statuses: frozenset[int] = frozenset({200}),
    ) -> tuple[int, dict[str, Any]]:
        if method not in {"GET", "POST", "PUT"} or not path.startswith("/v1/"):
            raise ZulipLocalError("An internal OpenBao request was rejected.")
        headers = {"Accept": "application/json", "User-Agent": "zulip-local/1"}
        body = None
        if payload is not None or method in {"POST", "PUT"}:
            body = json.dumps(payload or {}, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token is not None:
            if not _TOKEN_RE.fullmatch(token):
                raise ZulipLocalError("OpenBao returned an invalid token.")
            headers["X-Vault-Token"] = token
        request = urllib.request.Request(
            OPENBAO_ORIGIN + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                status = int(response.status)
                if status not in allowed_statuses:
                    raise ZulipLocalError("OpenBao rejected a scoped request.")
                if status == 204:
                    return status, {}
                return status, _decode_json_response(response)
        except urllib.error.HTTPError as exc:
            if exc.code in allowed_statuses:
                if exc.code == 204:
                    return exc.code, {}
                return exc.code, _decode_json_response(exc)
            raise ZulipLocalError("OpenBao rejected a scoped request.") from None
        except (OSError, ssl.SSLError, urllib.error.URLError) as exc:
            raise ZulipLocalError("The pinned local OpenBao endpoint is unavailable.") from exc

    def login_approle(self, role_id: str, secret_id: str) -> str:
        for value in (role_id, secret_id):
            if not _IDENTIFIER_RE.fullmatch(value):
                raise ZulipLocalError("An AppRole credential has an invalid shape.")
        _, document = self.request(
            "POST",
            "/v1/auth/approle/login",
            payload={"role_id": role_id, "secret_id": secret_id},
        )
        auth = document.get("auth")
        token = auth.get("client_token") if isinstance(auth, dict) else None
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise ZulipLocalError("OpenBao did not issue a valid scoped token.")
        return token

    def login_userpass(self, username: str, password: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", username) or not password:
            raise ZulipLocalError("The OpenBao operator identity is invalid.")
        _, document = self.request(
            "POST",
            "/v1/auth/userpass/login/" + urllib.parse.quote(username, safe=""),
            payload={"password": password},
        )
        auth = document.get("auth")
        token = auth.get("client_token") if isinstance(auth, dict) else None
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise ZulipLocalError("OpenBao did not issue a valid human token.")
        return token

    def read_kv(self, path: str, token: str) -> tuple[dict[str, Any], int] | None:
        status, document = self.request(
            "GET", path, token=token, allowed_statuses=frozenset({200, 404})
        )
        if status == 404:
            return None
        outer = document.get("data")
        data = outer.get("data") if isinstance(outer, dict) else None
        metadata = outer.get("metadata") if isinstance(outer, dict) else None
        version = metadata.get("version") if isinstance(metadata, dict) else None
        if not isinstance(data, dict) or not isinstance(version, int) or version < 1:
            raise ZulipLocalError("OpenBao returned an invalid KV v2 document.")
        return data, version

    def write_kv(self, path: str, data: Mapping[str, Any], token: str, *, cas: int) -> None:
        if cas < 0:
            raise ZulipLocalError("An invalid KV compare-and-set version was rejected.")
        self.request(
            "POST",
            path,
            token=token,
            payload={"data": dict(data), "options": {"cas": cas}},
            allowed_statuses=frozenset({200, 204}),
        )

    def revoke(self, token: str) -> None:
        self.request(
            "POST",
            "/v1/auth/token/revoke-self",
            token=token,
            allowed_statuses=frozenset({200, 204}),
        )


class ZulipClient:
    def __init__(self, opener: urllib.request.OpenerDirector | None = None) -> None:
        self.opener = opener or _ssl_opener(ZULIP_CA)

    def request(
        self,
        method: str,
        path: str,
        *,
        fields: Mapping[str, str] | None = None,
        auth: tuple[str, str] | None = None,
        allowed_statuses: frozenset[int] = frozenset({200}),
    ) -> tuple[int, dict[str, Any]]:
        if method not in {"GET", "POST"} or not path.startswith("/api/v1/"):
            raise ZulipLocalError("An internal Zulip request was rejected.")
        headers = {"Accept": "application/json", "User-Agent": "zulip-local-provision/1"}
        body = None
        url = ZULIP_ORIGIN + path
        if fields:
            encoded = urllib.parse.urlencode(fields).encode("utf-8")
            if method == "GET":
                url += "?" + encoded.decode("ascii")
            else:
                body = encoded
                headers["Content-Type"] = "application/x-www-form-urlencoded"
        if auth is not None:
            email, api_key = auth
            if not _EMAIL_RE.fullmatch(email) or not _TOKEN_RE.fullmatch(api_key):
                raise ZulipLocalError("A Zulip API credential has an invalid shape.")
            basic = base64.b64encode(f"{email}:{api_key}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = "Basic " + basic
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                status = int(response.status)
                if status not in allowed_statuses:
                    raise ZulipLocalError("Zulip rejected a scoped API request.")
                document = _decode_json_response(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in allowed_statuses:
                raise ZulipLocalError("Zulip rejected a scoped API request.") from None
            status = exc.code
            document = _decode_json_response(exc)
        except (OSError, ssl.SSLError, urllib.error.URLError) as exc:
            raise ZulipLocalError("The pinned local Zulip endpoint is unavailable.") from exc
        if status == 200 and document.get("result") != "success":
            raise ZulipLocalError("Zulip returned an unsuccessful API document.")
        return status, document

    def fetch_api_key(self, email: str, password: str) -> str:
        _, document = self.request(
            "POST",
            "/api/v1/fetch_api_key",
            fields={"username": email, "password": password},
        )
        api_key = document.get("api_key")
        if not isinstance(api_key, str) or not _TOKEN_RE.fullmatch(api_key):
            raise ZulipLocalError("Zulip did not return a valid API key.")
        return api_key


def load_defaults(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ZulipLocalError("The reviewed non-secret defaults are unavailable.") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator != "=" or key not in DEFAULT_KEYS or key in result:
            raise ZulipLocalError("The non-secret defaults file has an unexpected schema.")
        result[key] = _bounded_text(value, maximum=512)
    if frozenset(result) != DEFAULT_KEYS:
        raise ZulipLocalError("The non-secret defaults file is incomplete.")
    if result["ZULIP_LOCAL_ORIGIN"] != ZULIP_ORIGIN:
        raise ZulipLocalError("The configured Zulip origin is not reviewed.")
    return result


def read_credential(path: Path, *, pattern: re.Pattern[str] = _IDENTIFIER_RE) -> str:
    try:
        path_stat = os.lstat(path)
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or path_stat.st_uid != 0
            or stat.S_IMODE(path_stat.st_mode) & 0o077
            or not 1 <= path_stat.st_size <= MAX_CREDENTIAL_BYTES
        ):
            raise ZulipLocalError("A runtime credential has unsafe metadata.")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            raw = os.read(descriptor, MAX_CREDENTIAL_BYTES + 1)
            if len(raw) > MAX_CREDENTIAL_BYTES or os.read(descriptor, 1):
                raise ZulipLocalError("A runtime credential is oversized.")
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ZulipLocalError("A runtime credential is unavailable.") from exc
    try:
        value = raw.rstrip(b"\r\n").decode("ascii", errors="strict")
    except UnicodeError as exc:
        raise ZulipLocalError("A runtime credential has an invalid encoding.") from exc
    if not pattern.fullmatch(value):
        raise ZulipLocalError("A runtime credential has an invalid shape.")
    return value


def decrypt_systemd_credential(path: Path, name: str) -> str:
    if not re.fullmatch(r"[a-z0-9-]{8,80}", name):
        raise ZulipLocalError("An encrypted credential name is invalid.")
    try:
        path_stat = os.lstat(path)
    except OSError as exc:
        raise ZulipLocalError("An encrypted AppRole credential is unavailable.") from exc
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_uid != 0
        or stat.S_IMODE(path_stat.st_mode) & 0o077
        or not 32 <= path_stat.st_size <= 64 * 1024
    ):
        raise ZulipLocalError("An encrypted AppRole credential has unsafe metadata.")
    completed = subprocess.run(
        ["/usr/bin/systemd-creds", "decrypt", f"--name={name}", str(path), "-"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0 or len(completed.stdout) > MAX_CREDENTIAL_BYTES:
        raise ZulipLocalError("An encrypted AppRole credential cannot be decrypted.")
    try:
        value = completed.stdout.rstrip(b"\r\n").decode("ascii", errors="strict")
    except UnicodeError as exc:
        raise ZulipLocalError("An encrypted AppRole credential is invalid.") from exc
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ZulipLocalError("An encrypted AppRole credential has an invalid shape.")
    return value


def _safe_directory(path: Path, mode: int, expected_uid: int) -> None:
    try:
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        path_stat = os.lstat(path)
    except OSError as exc:
        raise ZulipLocalError("A runtime directory cannot be prepared safely.") from exc
    if (
        not stat.S_ISDIR(path_stat.st_mode)
        or path_stat.st_uid != expected_uid
        or stat.S_IMODE(path_stat.st_mode) & 0o077
    ):
        raise ZulipLocalError("A runtime directory has unsafe metadata.")
    os.chmod(path, mode)


def _atomic_secret(
    path: Path,
    value: str,
    mode: int,
    expected_uid: int,
    expected_gid: int | None = None,
) -> None:
    if expected_gid is None:
        expected_gid = expected_uid
    if path.parent != path.parent.resolve() or path.is_symlink():
        raise ZulipLocalError("A secret output path is unsafe.")
    temporary = path.with_name("." + path.name + f".{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, mode)
        try:
            encoded = value.encode("utf-8")
            offset = 0
            while offset < len(encoded):
                offset += os.write(descriptor, encoded[offset:])
            opened_stat = os.fstat(descriptor)
            if opened_stat.st_uid != expected_uid or opened_stat.st_gid != expected_gid:
                os.fchown(descriptor, expected_uid, expected_gid)
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        temporary_stat = os.lstat(temporary)
        if temporary_stat.st_uid != expected_uid or temporary_stat.st_gid != expected_gid:
            raise ZulipLocalError("A secret output file has unsafe ownership.")
        os.replace(temporary, path)
    except OSError as exc:
        raise ZulipLocalError("A secret output file cannot be written safely.") from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _label_container_runtime(paths: list[Path]) -> None:
    """Give bind-mounted runtime files the SELinux container file type."""

    if not Path("/sys/fs/selinux/enforce").is_file():
        return
    chcon = Path("/usr/bin/chcon")
    if not chcon.is_file():
        raise ZulipLocalError("SELinux is active but chcon is unavailable.")
    completed = subprocess.run(
        [str(chcon), "--type=container_file_t", "--", *(str(path) for path in paths)],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0:
        raise ZulipLocalError("The Zulip runtime files could not be labeled for containers.")


def render_runtime_files(
    data: Mapping[str, Any],
    run_dir: Path = RUN_DIR,
    *,
    expected_uid: int = 0,
    zulip_gid: int = 1000,
    label_for_containers: bool = True,
) -> None:
    values = validate_server_secret(data)
    _safe_directory(run_dir, 0o700, expected_uid)
    secret_dir = run_dir / "secrets"
    tls_dir = run_dir / "tls"
    admin_dir = run_dir / "admin"
    for directory in (secret_dir, tls_dir, admin_dir):
        _safe_directory(directory, 0o700, expected_uid)

    rendered_paths: list[Path] = [run_dir, secret_dir, tls_dir, admin_dir]
    for key in (
        "postgres_password",
        "memcached_password",
        "rabbitmq_password",
        "redis_password",
    ):
        # The pinned memcached image runs its command as the unprivileged
        # `memcache` user. Compose file secrets preserve source permissions,
        # so that one file must use Compose's normal 0444 in-container mode.
        # Its host parents remain root-only and non-traversable.
        mode = 0o444 if key == "memcached_password" else 0o400
        path = secret_dir / key
        _atomic_secret(path, values[key], mode, expected_uid)
        rendered_paths.append(path)
    ca_path = tls_dir / "ca.crt"
    _atomic_secret(ca_path, values["tls_ca_certificate"], 0o444, expected_uid)
    rendered_paths.append(ca_path)
    certificate_path = tls_dir / "zulip.combined-chain.crt"
    _atomic_secret(
        certificate_path, values["tls_certificate"], 0o444, expected_uid
    )
    rendered_paths.append(certificate_path)
    key_path = tls_dir / "zulip.key"
    _atomic_secret(key_path, values["tls_private_key"], 0o400, expected_uid)
    rendered_paths.append(key_path)
    # Compose implements file-backed secrets as bind mounts and ignores uid/gid
    # overrides. This empty placeholder must therefore be readable by the
    # unprivileged `zulip` user inside the container. Its host parents are 0700;
    # it is populated in-place for only the create_realm call, then truncated.
    admin_path = admin_dir / "admin-password"
    _atomic_secret(admin_path, "", 0o444, expected_uid)
    rendered_paths.append(admin_path)

    # The official image insists on finding this file under /data. Compose bind
    # mounts this complete, tmpfs-backed file at that exact location. No
    # zulip__* secret is mounted in the application service: the upstream
    # crudini loop would otherwise replace this exact-file bind mount and fail
    # with EBUSY. The four dependency services receive their own file secrets.
    conf = "[secrets]\n" + "\n".join(
        f"{key} = {values[key]}" for key in ZULIP_SECRET_FIELDS
    ) + "\n"
    # The upstream entrypoint enforces 0640 on this exact file. The containing
    # host directory is root-only (0700), so host GID 1000 cannot traverse it.
    # Inside the pinned image, GID 1000 is the `zulip` group and must be able to
    # read the bind-mounted file after the entrypoint switches users.
    conf_path = run_dir / "zulip-secrets.conf"
    _atomic_secret(conf_path, conf, 0o640, expected_uid, zulip_gid)
    rendered_paths.append(conf_path)
    if label_for_containers:
        _label_container_runtime(rendered_paths)


def api_integer(document: Mapping[str, Any], key: str) -> int:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ZulipLocalError("Zulip returned an invalid numeric identifier.")
    return value


def acquire_deployment_lock(path: Path = LOCK_PATH) -> int:
    """Acquire the shared non-blocking lifecycle lock until the fd is closed."""

    _safe_directory(path.parent, 0o700, os.geteuid())
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_uid != os.geteuid():
            raise ZulipLocalError("The Zulip deployment lock has unsafe metadata.")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise ZulipLocalError("Another Zulip deployment operation holds the lock.") from exc
    except OSError as exc:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise ZulipLocalError("The Zulip deployment lock is unavailable.") from exc
    return descriptor
