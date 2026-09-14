"""Read optional provider credentials without accepting unsafe persistent files."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from .config import APIProviderConfig
from .errors import BackendUnavailableError


MAX_CREDENTIAL_BYTES = 65_536


def provider_credential(config: APIProviderConfig, provider: str) -> str:
    if config.credential_credential:
        directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
        if not directory or not os.path.isabs(directory):
            raise BackendUnavailableError(f"{provider} systemd credential is unavailable")
        path = Path(directory) / config.credential_credential
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise BackendUnavailableError(f"{provider} credential path is unsafe")
            # systemd uses 0440 for credentials mapped into non-root units;
            # the containing mount is private to that service.
            if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o037:
                raise BackendUnavailableError(f"{provider} credential permissions are unsafe")
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            try:
                opened = os.fstat(descriptor)
                if opened.st_dev != info.st_dev or opened.st_ino != info.st_ino:
                    raise BackendUnavailableError(f"{provider} credential changed while opening")
                raw = os.read(descriptor, MAX_CREDENTIAL_BYTES + 1)
                if len(raw) > MAX_CREDENTIAL_BYTES or os.read(descriptor, 1):
                    raise BackendUnavailableError(f"{provider} credential is oversized")
            finally:
                os.close(descriptor)
            value = raw.rstrip(b"\r\n").decode("utf-8", errors="strict")
        except OSError as exc:
            raise BackendUnavailableError(f"{provider} systemd credential is unavailable") from exc
        except UnicodeError as exc:
            raise BackendUnavailableError(f"{provider} systemd credential is invalid") from exc
    elif config.credential_env:
        value = os.environ.get(config.credential_env, "")
    else:
        value = ""
    if not value or "\x00" in value or len(value.encode("utf-8")) > MAX_CREDENTIAL_BYTES:
        raise BackendUnavailableError(f"{provider} credential is unavailable")
    return value
