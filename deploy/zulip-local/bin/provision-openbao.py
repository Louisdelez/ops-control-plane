#!/usr/bin/python3
"""Provision narrowly-scoped Zulip AppRoles and the server secret object.

This is an explicit, interactive host-administration operation.  It neither
starts Docker nor enables a unit.  Secret values only exist in process memory,
OpenBao, an ephemeral directory under /run, or host+TPM2 encrypted credentials.
"""

from __future__ import annotations

import argparse
import fcntl
import getpass
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any
import uuid

SCRIPT_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PACKAGE_DIR / "lib"))

from zulip_local import (  # noqa: E402
    OPENBAO_CA,
    LOCK_PATH,
    OpenBaoClient,
    SERVER_KV_API_PATH,
    ZulipLocalError,
    acquire_deployment_lock,
    decrypt_systemd_credential,
    validate_server_secret,
)


CREDSTORE = Path("/etc/credstore.encrypted")
POLICY_DIR = PACKAGE_DIR / "openbao"
ROLE_SPECS = {
    "zulip-local-runtime": {
        "policy": "zulip-local-runtime",
        "token_num_uses": 2,
        "probe_path": SERVER_KV_API_PATH,
    },
    "zulip-local-bootstrap": {
        "policy": "zulip-local-bootstrap",
        "token_num_uses": 4,
        "probe_path": "/v1/kv-infra-shared/data/zulip/bot",
    },
}
_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{15,511}\Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Provision OpenBao for the local Zulip deployment without starting it."
    )
    parser.add_argument("--username", default="ops-user", help="OpenBao userpass account name")
    parser.add_argument(
        "--initialize-server-secrets",
        action="store_true",
        help="Create the server KV object and local CA if it is absent",
    )
    parser.add_argument(
        "--rotate-tls",
        action="store_true",
        help="Replace only the local CA/certificate/key in the existing server object",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Perform offline/read-only checks and do not authenticate to OpenBao",
    )
    parser.add_argument(
        "--password-fd",
        type=int,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--deployment-lock-fd", type=int, help=argparse.SUPPRESS)
    return parser.parse_args()


def inherit_deployment_lock(descriptor: int) -> int:
    if descriptor < 3 or descriptor > 1024:
        raise ZulipLocalError("The inherited Zulip deployment lock is invalid.")
    try:
        opened = os.fstat(descriptor)
        target = os.lstat(LOCK_PATH)
        if (
            not stat.S_ISREG(opened.st_mode) or opened.st_uid != 0
            or stat.S_IMODE(opened.st_mode) & 0o077
            or opened.st_dev != target.st_dev or opened.st_ino != target.st_ino
        ):
            raise ZulipLocalError("The inherited Zulip deployment lock is unsafe.")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as exc:
        raise ZulipLocalError("The inherited Zulip deployment lock is unavailable.") from exc
    return descriptor


def password_from_pipe(descriptor: int) -> str:
    """Read one bootstrap password from an inherited anonymous pipe.

    The descriptor number is not sensitive.  Requiring a FIFO prevents the
    bootstrap-only interface from being repurposed to read a plaintext file.
    """

    if descriptor < 3 or descriptor > 1024:
        raise ZulipLocalError("The inherited password descriptor is invalid.")
    try:
        info = os.fstat(descriptor)
        target = os.readlink(f"/proc/self/fd/{descriptor}")
        if not stat.S_ISFIFO(info.st_mode) or not re.fullmatch(r"pipe:\[\d+\]", target):
            raise ZulipLocalError("The inherited password descriptor is not a pipe.")
        chunks: list[bytes] = []
        remaining = 4097
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError as exc:
        raise ZulipLocalError("The inherited password pipe is unavailable.") from exc
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    if not raw or len(raw) > 4096:
        raise ZulipLocalError("The inherited password is invalid.")
    try:
        password = raw.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise ZulipLocalError("The inherited password is invalid.") from exc
    raw = b""
    if (
        len(password) < 10
        or len(password) > 256
        or password != password.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in password)
    ):
        password = ""
        raise ZulipLocalError("The inherited password is invalid.")
    return password


def require_root_file(path: Path) -> None:
    try:
        path_stat = os.lstat(path)
    except OSError as exc:
        raise ZulipLocalError("A required provisioning file is unavailable.") from exc
    if (
        not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_uid != 0
        or stat.S_IMODE(path_stat.st_mode) & 0o022
    ):
        raise ZulipLocalError("A required provisioning file has unsafe metadata.")


def check_sources() -> None:
    require_root_file(OPENBAO_CA)
    for name in ROLE_SPECS:
        policy = POLICY_DIR / f"{name}.hcl"
        if not policy.is_file() or policy.is_symlink():
            raise ZulipLocalError("A required Zulip OpenBao policy is unavailable.")
        text = policy.read_text(encoding="utf-8")
        if "kv-infra-shared" not in text or "auth/token/revoke-self" not in text:
            raise ZulipLocalError("A Zulip OpenBao policy is incomplete.")
    credentials_program = shutil.which("systemd-creds")
    if credentials_program is None or Path(credentials_program).resolve() != Path("/usr/bin/systemd-creds"):
        raise ZulipLocalError("The reviewed systemd-creds executable is unavailable.")
    require_root_file(Path("/usr/bin/systemd-creds"))
    if shutil.which("openssl") is None:
        raise ZulipLocalError("OpenSSL is required for local TLS provisioning.")


def generate_tls() -> tuple[str, str, str]:
    transient = Path(tempfile.mkdtemp(prefix="zulip-local-pki.", dir="/run"))
    os.chmod(transient, 0o700)
    commands = (
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "EC",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-out",
            str(transient / "ca.key"),
        ],
        [
            "openssl",
            "req",
            "-x509",
            "-new",
            "-sha256",
            "-days",
            "3650",
            "-key",
            str(transient / "ca.key"),
            "-subj",
            "/CN=Zulip Ops Local CA",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
            "-addext",
            "keyUsage=critical,keyCertSign,cRLSign",
            "-out",
            str(transient / "ca.crt"),
        ],
        [
            "openssl",
            "genpkey",
            "-algorithm",
            "EC",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-out",
            str(transient / "zulip.key"),
        ],
        [
            "openssl",
            "req",
            "-new",
            "-sha256",
            "-key",
            str(transient / "zulip.key"),
            "-subj",
            "/CN=zulip.ops.local",
            "-addext",
            "subjectAltName=DNS:zulip.ops.local",
            "-addext",
            "basicConstraints=critical,CA:FALSE",
            "-addext",
            "keyUsage=critical,digitalSignature,keyEncipherment",
            "-addext",
            "extendedKeyUsage=serverAuth",
            "-out",
            str(transient / "zulip.csr"),
        ],
        [
            "openssl",
            "x509",
            "-req",
            "-sha256",
            "-days",
            "397",
            "-in",
            str(transient / "zulip.csr"),
            "-CA",
            str(transient / "ca.crt"),
            "-CAkey",
            str(transient / "ca.key"),
            "-CAcreateserial",
            "-copy_extensions",
            "copy",
            "-out",
            str(transient / "zulip.crt"),
        ],
    )
    try:
        for command in commands:
            completed = subprocess.run(
                command,
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if completed.returncode != 0:
                raise ZulipLocalError("OpenSSL could not generate the local Zulip certificate.")
        return (
            (transient / "ca.crt").read_text(encoding="ascii"),
            (transient / "zulip.crt").read_text(encoding="ascii"),
            (transient / "zulip.key").read_text(encoding="ascii"),
        )
    finally:
        shutil.rmtree(transient)


def new_server_document() -> dict[str, str]:
    ca_certificate, certificate, private_key = generate_tls()
    data = {
        # These five values are generated by Zulip's 12.2
        # generate_secrets.py when absent. Keeping them in OpenBao prevents
        # identity, avatar and inter-process keys from changing whenever the
        # host tmpfs is recreated.
        "avatar_salt": secrets.token_hex(32),
        "camo_key": "A" + secrets.token_urlsafe(48),
        "postgres_password": "A" + secrets.token_urlsafe(48),
        "memcached_password": "A" + secrets.token_urlsafe(48),
        "rabbitmq_password": "A" + secrets.token_urlsafe(48),
        "redis_password": "A" + secrets.token_urlsafe(48),
        "secret_key": "A" + secrets.token_urlsafe(64),
        "shared_secret": secrets.token_hex(32),
        # Kept random even though outbound email is disabled, so enabling a
        # reviewed SMTP configuration later does not start with a blank value.
        "email_password": "A" + secrets.token_urlsafe(48),
        "zulip_org_id": str(uuid.uuid4()),
        "zulip_org_key": "A" + secrets.token_urlsafe(48),
        "tls_ca_certificate": ca_certificate,
        "tls_certificate": certificate,
        "tls_private_key": private_key,
    }
    return validate_server_secret(data)


def install_policy(client: OpenBaoClient, token: str, name: str) -> None:
    policy_text = (POLICY_DIR / f"{name}.hcl").read_text(encoding="utf-8")
    client.request(
        "PUT",
        f"/v1/sys/policies/acl/{name}",
        token=token,
        payload={"policy": policy_text},
        allowed_statuses=frozenset({200, 204}),
    )
    _, document = client.request("GET", f"/v1/sys/policies/acl/{name}", token=token)
    returned = document.get("data")
    returned_policy = None
    if isinstance(returned, dict):
        returned_policy = returned.get("policy") or returned.get("rules")
    if returned_policy is None:
        returned_policy = document.get("policy") or document.get("rules")
    if not isinstance(returned_policy, str) or returned_policy.strip() != policy_text.strip():
        raise ZulipLocalError("OpenBao did not retain the reviewed Zulip policy.")


def role_payload(policy: str, token_num_uses: int) -> dict[str, Any]:
    return {
        "bind_secret_id": True,
        "secret_id_num_uses": 1024,
        "secret_id_ttl": "720h",
        "secret_id_bound_cidrs": ["127.0.0.1/32"],
        "token_bound_cidrs": ["127.0.0.1/32"],
        "token_no_default_policy": True,
        "token_policies": [policy],
        "token_num_uses": token_num_uses,
        "token_period": "0s",
        "token_ttl": "120s",
        "token_max_ttl": "120s",
        "token_explicit_max_ttl": "120s",
        "token_type": "service",
    }


def validate_role_document(document: dict[str, Any], policy: str, token_num_uses: int) -> None:
    data = document.get("data")
    if not isinstance(data, dict):
        raise ZulipLocalError("OpenBao returned an invalid AppRole document.")
    exact = {
        "bind_secret_id": True,
        "secret_id_num_uses": 1024,
        "token_num_uses": token_num_uses,
        "token_no_default_policy": True,
        "token_period": 0,
        "token_type": "service",
    }
    if any(data.get(key) != value for key, value in exact.items()):
        raise ZulipLocalError("OpenBao retained an unsafe AppRole setting.")
    if data.get("token_policies") != [policy]:
        raise ZulipLocalError("OpenBao retained an unsafe AppRole policy set.")
    # OpenBao canonicalizes a token /32 to its host address. Accept only
    # those two equivalent spellings; broader networks remain forbidden.
    for field in ("secret_id_bound_cidrs", "token_bound_cidrs"):
        if data.get(field) not in (["127.0.0.1/32"], ["127.0.0.1"]):
            raise ZulipLocalError("OpenBao retained an unsafe AppRole network scope.")
    durations = {
        "secret_id_ttl": 2_592_000,
        "token_ttl": 120,
        "token_max_ttl": 120,
        "token_explicit_max_ttl": 120,
    }
    if any(data.get(key) != value for key, value in durations.items()):
        raise ZulipLocalError("OpenBao retained an unsafe AppRole lifetime.")


def encrypt_credential(name: str, value: str) -> bytes:
    completed = subprocess.run(
        [
            "/usr/bin/systemd-creds",
            "encrypt",
            "--with-key=host+tpm2",
            f"--name={name}",
            "-",
            "-",
        ],
        check=False,
        input=(value + "\n").encode("ascii"),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if completed.returncode != 0 or not 32 <= len(completed.stdout) <= 64 * 1024:
        raise ZulipLocalError("systemd-creds could not encrypt a Zulip AppRole credential.")
    return completed.stdout


def atomic_credential(path: Path, encrypted: bytes) -> None:
    temporary = path.with_name("." + path.name + f".{os.getpid()}.tmp")
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o400,
        )
        offset = 0
        while offset < len(encrypted):
            offset += os.write(descriptor, encrypted[offset:])
        os.fchmod(descriptor, 0o400)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
    except OSError as exc:
        raise ZulipLocalError("An encrypted Zulip credential cannot be installed safely.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def decrypt_old_accessor(role_name: str) -> str | None:
    name = f"{role_name}-secret-id-accessor"
    path = CREDSTORE / name
    if not path.exists():
        return None
    try:
        return decrypt_systemd_credential(path, name)
    except ZulipLocalError:
        raise ZulipLocalError("The previous Zulip SecretID accessor cannot be decrypted.") from None


def issue_role(
    client: OpenBaoClient, human_token: str, role_name: str, spec: dict[str, Any]
) -> tuple[dict[str, bytes], str | None]:
    previous_accessor = decrypt_old_accessor(role_name)
    client.request(
        "POST",
        f"/v1/auth/approle/role/{role_name}",
        token=human_token,
        payload=role_payload(str(spec["policy"]), int(spec["token_num_uses"])),
        allowed_statuses=frozenset({200, 204}),
    )
    _, configured_role = client.request(
        "GET", f"/v1/auth/approle/role/{role_name}", token=human_token
    )
    validate_role_document(
        configured_role, str(spec["policy"]), int(spec["token_num_uses"])
    )
    _, role_document = client.request(
        "GET", f"/v1/auth/approle/role/{role_name}/role-id", token=human_token
    )
    role_data = role_document.get("data")
    role_id = role_data.get("role_id") if isinstance(role_data, dict) else None
    _, secret_document = client.request(
        "POST", f"/v1/auth/approle/role/{role_name}/secret-id", token=human_token
    )
    secret_data = secret_document.get("data")
    secret_id = secret_data.get("secret_id") if isinstance(secret_data, dict) else None
    accessor = secret_data.get("secret_id_accessor") if isinstance(secret_data, dict) else None
    if not all(isinstance(value, str) and _ID_RE.fullmatch(value) for value in (role_id, secret_id, accessor)):
        raise ZulipLocalError("OpenBao returned an invalid Zulip AppRole credential set.")
    assert isinstance(role_id, str) and isinstance(secret_id, str) and isinstance(accessor, str)

    probe_token = client.login_approle(role_id, secret_id)
    try:
        client.request(
            "GET",
            str(spec["probe_path"]),
            token=probe_token,
            allowed_statuses=frozenset({200, 404}),
        )
    finally:
        if probe_token:
            client.revoke(probe_token)
        probe_token = ""

    values = {
        f"{role_name}-role-id": role_id,
        f"{role_name}-secret-id": secret_id,
        f"{role_name}-secret-id-accessor": accessor,
    }
    encrypted = {name: encrypt_credential(name, value) for name, value in values.items()}
    values.clear()
    return encrypted, previous_accessor


def destroy_previous_accessor(
    client: OpenBaoClient, token: str, role_name: str, accessor: str | None
) -> None:
    if accessor is None:
        return
    client.request(
        "POST",
        f"/v1/auth/approle/role/{role_name}/secret-id-accessor/destroy",
        token=token,
        payload={"secret_id_accessor": accessor},
        allowed_statuses=frozenset({200, 204}),
    )


def main() -> int:
    args = parse_args()
    if os.geteuid() != 0:
        raise ZulipLocalError("OpenBao provisioning must run as root.")
    check_sources()
    if args.check:
        if args.password_fd is not None or args.deployment_lock_fd is not None:
            raise ZulipLocalError("Offline checks cannot consume a password pipe.")
        print("Zulip OpenBao provisioning sources passed offline validation.")
        return 0
    if args.deployment_lock_fd is None:
        _lock_descriptor = acquire_deployment_lock()
    else:
        _lock_descriptor = inherit_deployment_lock(args.deployment_lock_fd)
    if args.rotate_tls and args.initialize_server_secrets:
        raise ZulipLocalError("Choose initialization or TLS rotation, not both.")
    tpm = subprocess.run(
        ["/usr/bin/systemd-analyze", "has-tpm2", "--quiet"],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if tpm.returncode != 0:
        raise ZulipLocalError("A working TPM2 is required for Zulip AppRole persistence.")
    CREDSTORE.mkdir(mode=0o700, parents=True, exist_ok=True)
    store_stat = os.lstat(CREDSTORE)
    if not stat.S_ISDIR(store_stat.st_mode) or store_stat.st_uid != 0 or stat.S_IMODE(store_stat.st_mode) & 0o077:
        raise ZulipLocalError("The encrypted credential store has unsafe metadata.")
    subprocess.run(
        ["/usr/bin/systemd-creds", "setup"],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    if args.password_fd is None:
        confirmation = input("Type PROVISION-ZULIP-LOCAL to continue: ")
        if confirmation != "PROVISION-ZULIP-LOCAL":
            raise ZulipLocalError("OpenBao provisioning was cancelled.")
        password = getpass.getpass("OpenBao password (never stored): ")
    else:
        password = password_from_pipe(args.password_fd)
    if not password:
        raise ZulipLocalError("An OpenBao password is required.")

    client = OpenBaoClient()
    human_token: str | None = None
    staged: dict[str, bytes] = {}
    old_accessors: dict[str, str | None] = {}
    try:
        human_token = client.login_userpass(args.username, password)
        password = ""
        for name in ROLE_SPECS:
            install_policy(client, human_token, name)

        existing = client.read_kv(SERVER_KV_API_PATH, human_token)
        if existing is None:
            if not args.initialize_server_secrets:
                raise ZulipLocalError(
                    "The server KV object is absent; rerun with --initialize-server-secrets."
                )
            server_data = new_server_document()
            client.write_kv(SERVER_KV_API_PATH, server_data, human_token, cas=0)
        else:
            raw_server_data, server_version = existing
            server_data = validate_server_secret(raw_server_data)
            if args.rotate_tls:
                ca_certificate, certificate, private_key = generate_tls()
                server_data.update(
                    {
                        "tls_ca_certificate": ca_certificate,
                        "tls_certificate": certificate,
                        "tls_private_key": private_key,
                    }
                )
                server_data = validate_server_secret(server_data)
                client.write_kv(
                    SERVER_KV_API_PATH, server_data, human_token, cas=server_version
                )

        for role_name, spec in ROLE_SPECS.items():
            encrypted, old_accessor = issue_role(client, human_token, role_name, spec)
            staged.update(encrypted)
            old_accessors[role_name] = old_accessor
        for name, encrypted in staged.items():
            atomic_credential(CREDSTORE / name, encrypted)
        for role_name, old_accessor in old_accessors.items():
            destroy_previous_accessor(client, human_token, role_name, old_accessor)
        client.revoke(human_token)
        human_token = None
    finally:
        password = ""
        staged.clear()
        if human_token is not None:
            try:
                client.revoke(human_token)
            except ZulipLocalError:
                pass
        human_token = None

    print("Zulip server secrets and host+TPM2 AppRoles were provisioned; no value was displayed.")
    print("No container or systemd unit was started or enabled.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ZulipLocalError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, ZulipLocalError) else "A provisioning command failed."
        print(message, file=sys.stderr)
        raise SystemExit(1) from None
