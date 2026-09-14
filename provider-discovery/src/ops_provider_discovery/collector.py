"""Fail-closed, bounded and non-mutating provider model discovery."""

from __future__ import annotations

import copy
import datetime as dt
import errno
import ipaddress
import json
import os
import queue
import re
import selectors
import socket
import ssl
import stat
import urllib.parse
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


REGISTRY_KEYS = {
    "schema_version",
    "revision",
    "verified_on",
    "finance_capability_names",
    "finance_access_modes",
    "provider_accounts",
    "deployment_mappings",
}
PROVIDER_KEYS = {
    "id",
    "display_name",
    "inference",
    "credentials",
    "financial_capabilities",
    "finance_endpoints",
    "official_sources",
    "limitations",
}
DISCOVERY_KEYS = {"mode", "method", "endpoint", "operation", "credential_scope"}
CREDENTIAL_KEYS = {"credential_ref", "auth_scheme", "header_name", "value_prefix"}
DEPLOYMENT_KEYS = {
    "deployment_id",
    "card_id",
    "developer_id",
    "inference_provider_account_id",
    "exact_model_id",
    "activation_state",
    "source",
}
FINANCE_CAPABILITIES = (
    "cash_balance",
    "plan_quota",
    "usage",
    "cost",
    "rate_limits",
    "spending_limit",
)
FINANCE_ACCESS_MODES = (
    "direct_api",
    "admin_api",
    "cloud_billing_api",
    "console_only",
    "not_applicable",
    "unknown",
)
PROTOCOLS = {
    "openai_native",
    "openai_compatible",
    "openai_chat_partial",
    "dashscope_native",
    "anthropic_compatible",
    "anthropic_messages",
    "gemini_native",
    "cohere_v2",
    "bedrock_native",
    "preview_openai_compatible",
}
AUTH_SCHEMES = {
    "none",
    "unknown",
    "bearer",
    "x_api_key",
    "x_api_key_and_version",
    "google_api_key",
    "google_oauth2",
    "api_key_or_bearer",
    "aws_sigv4",
    "aws_sigv4_or_bearer",
    "azure_api_key_or_entra",
    "azure_entra",
    "alibaba_rpc_signature",
    "tencent_tc3_hmac",
    "volcengine_hmac",
}
SUPPORTED_AUTH_SCHEMES = {
    "bearer",
    "x_api_key",
    "x_api_key_and_version",
    "google_api_key",
    "api_key_or_bearer",
}
IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
REVISION = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,255}$")
HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")
CREDENTIAL_REF = re.compile(r"^kv-infra-shared/data/llm(?:-admin)?/[a-z0-9/-]+$")
FORBIDDEN_HEADERS = {"host", "content-length", "connection", "transfer-encoding", "proxy-authorization"}
MAX_DNS_ADDRESSES = 32
MAX_RESPONSE_HEADER_BYTES = 64 * 1024
MAX_CHUNK_LINE_BYTES = 256
MAX_TRAILER_BYTES = 16 * 1024
MAX_TRUST_BUNDLE_BYTES = 4 * 1024 * 1024
FEDORA_TRUST_BUNDLE = Path("/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem")
_DNS_SLOTS = threading.BoundedSemaphore(4)


class DiscoveryFailure(Exception):
    """A safe error whose code can be emitted without leaking response data."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message


@dataclass(frozen=True)
class CollectorLimits:
    registry_bytes: int = 2 * 1024 * 1024
    credential_map_bytes: int = 64 * 1024
    credential_bytes: int = 8 * 1024
    response_bytes: int = 1024 * 1024
    max_candidates: int = 5_000
    max_json_nodes: int = 25_000
    max_json_depth: int = 32
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class TransportResponse:
    status: int
    final_url: str
    content_type: str
    body: bytes


class Transport(Protocol):
    def get(
        self,
        endpoint: str,
        headers: Mapping[str, str],
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse: ...


class UrlLibTransport:
    """Direct HTTPS transport pinned to a previously validated global address.

    The historical class name is retained as part of the small public API.  It
    no longer delegates connection setup to urllib: doing so would resolve the
    hostname a second time and reopen DNS-rebinding and environment-proxy paths.
    """

    def __init__(
        self,
        *,
        resolver: Callable[..., list[tuple[Any, ...]]] | None = None,
        clock: Callable[[], float] | None = None,
        connection_factory: Callable[
            [str, tuple[int, int, int, tuple[Any, ...]], float, Callable[[], float]],
            Any,
        ]
        | None = None,
    ) -> None:
        self._resolver = resolver or socket.getaddrinfo
        self._clock = clock or time.monotonic
        self._connection_factory = connection_factory or _open_pinned_tls

    def get(
        self,
        endpoint: str,
        headers: Mapping[str, str],
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        started = self._clock()
        deadline = started + timeout_seconds
        endpoint = _static_https_endpoint(endpoint)
        parsed = urllib.parse.urlsplit(endpoint)
        hostname = parsed.hostname
        if hostname is None:
            raise DiscoveryFailure("non_static_endpoint", "model discovery endpoint has no hostname")
        addresses = _resolve_global_addresses(
            hostname,
            443,
            deadline=deadline,
            resolver=self._resolver,
            clock=self._clock,
        )
        connection = None
        try:
            last_error: Exception | None = None
            for address in addresses:
                try:
                    connection = self._connection_factory(
                        hostname,
                        address,
                        deadline,
                        self._clock,
                    )
                    break
                except DiscoveryFailure:
                    raise
                except (OSError, ssl.SSLError) as exc:
                    last_error = exc
            if connection is None:
                raise DiscoveryFailure(
                    "provider_unavailable",
                    "provider could not be reached",
                ) from last_error
            _send_http_get(
                connection,
                hostname=hostname,
                target=parsed.path or "/",
                headers=headers,
                deadline=deadline,
                clock=self._clock,
            )
            status, content_type, body = _read_http_response(
                connection,
                deadline=deadline,
                clock=self._clock,
                max_response_bytes=max_response_bytes,
            )
        except DiscoveryFailure:
            raise
        except (OSError, ssl.SSLError, ValueError):
            raise DiscoveryFailure("provider_unavailable", "provider could not be reached") from None
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
        return TransportResponse(status=status, final_url=endpoint, content_type=content_type, body=body)


def _remaining(deadline: float, clock: Callable[[], float]) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise DiscoveryFailure("provider_timeout", "provider request exceeded the total time limit")
    return remaining


def _resolve_global_addresses(
    hostname: str,
    port: int,
    *,
    deadline: float,
    resolver: Callable[..., list[tuple[Any, ...]]],
    clock: Callable[[], float],
) -> tuple[tuple[int, int, int, tuple[Any, ...]], ...]:
    """Resolve once, reject mixed/private answers, and return pinned socket targets."""

    if not _DNS_SLOTS.acquire(blocking=False):
        raise DiscoveryFailure("provider_unavailable", "DNS resolver capacity is unavailable")
    completed: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            completed.put(
                (
                    True,
                    resolver(
                        hostname,
                        port,
                        socket.AF_UNSPEC,
                        socket.SOCK_STREAM,
                        socket.IPPROTO_TCP,
                    ),
                ),
                block=False,
            )
        except BaseException as exc:  # normalized on the caller thread
            completed.put((False, exc), block=False)
        finally:
            _DNS_SLOTS.release()

    threading.Thread(
        target=resolve,
        name="provider-discovery-dns",
        daemon=True,
    ).start()
    try:
        succeeded, raw_result = completed.get(timeout=_remaining(deadline, clock))
    except queue.Empty:
        raise DiscoveryFailure("provider_timeout", "provider DNS resolution exceeded the total time limit") from None
    if not succeeded:
        raise DiscoveryFailure("provider_unavailable", "provider hostname could not be resolved") from None
    if not isinstance(raw_result, list) or not 1 <= len(raw_result) <= MAX_DNS_ADDRESSES:
        raise DiscoveryFailure("provider_unavailable", "provider DNS answer is outside safe bounds")

    addresses: list[tuple[int, int, int, tuple[Any, ...]]] = []
    seen: set[tuple[int, tuple[Any, ...]]] = set()
    for item in raw_result:
        if not isinstance(item, tuple) or len(item) != 5:
            raise DiscoveryFailure("provider_unavailable", "provider DNS answer is malformed")
        family, socktype, protocol, _canonical_name, sockaddr = item
        if (
            family not in {socket.AF_INET, socket.AF_INET6}
            or socktype != socket.SOCK_STREAM
            or protocol not in {0, socket.IPPROTO_TCP}
            or not isinstance(sockaddr, tuple)
            or len(sockaddr) < 2
            or not isinstance(sockaddr[0], str)
            or sockaddr[1] != port
        ):
            raise DiscoveryFailure("provider_unavailable", "provider DNS answer is malformed")
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            raise DiscoveryFailure("provider_unavailable", "provider DNS answer is malformed") from None
        if not _is_public_unicast(address):
            raise DiscoveryFailure(
                "non_global_endpoint",
                "provider hostname resolved to a non-global address",
            )
        normalized_sockaddr: tuple[Any, ...]
        if family == socket.AF_INET:
            normalized_sockaddr = (str(address), port)
        else:
            flowinfo = sockaddr[2] if len(sockaddr) >= 3 and isinstance(sockaddr[2], int) else 0
            scope_id = sockaddr[3] if len(sockaddr) >= 4 and isinstance(sockaddr[3], int) else 0
            if flowinfo != 0 or scope_id != 0:
                raise DiscoveryFailure("non_global_endpoint", "scoped provider addresses are forbidden")
            normalized_sockaddr = (str(address), port, 0, 0)
        key = (family, normalized_sockaddr)
        if key in seen:
            continue
        seen.add(key)
        addresses.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, normalized_sockaddr))
    if not addresses:
        raise DiscoveryFailure("provider_unavailable", "provider hostname has no usable global address")
    return tuple(addresses)


def _is_public_unicast(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or address.is_loopback
        or address.is_link_local
        or address.is_private
    ):
        return False
    if isinstance(address, ipaddress.IPv6Address):
        embedded = [address.ipv4_mapped, address.sixtofour]
        if address.teredo is not None:
            embedded.extend(address.teredo)
        if any(
            value is not None and not _is_public_unicast(value)
            for value in embedded
        ):
            return False
    return True


def _wait_ready(
    connection: Any,
    event: int,
    *,
    deadline: float,
    clock: Callable[[], float],
) -> None:
    with selectors.DefaultSelector() as selector:
        selector.register(connection, event)
        if not selector.select(_remaining(deadline, clock)):
            raise DiscoveryFailure("provider_timeout", "provider request exceeded the total time limit")


def _open_pinned_tls(
    hostname: str,
    address: tuple[int, int, int, tuple[Any, ...]],
    deadline: float,
    clock: Callable[[], float],
) -> ssl.SSLSocket:
    family, socktype, protocol, sockaddr = address
    context = _tls_context()
    raw = socket.socket(family, socktype, protocol)
    raw.setblocking(False)
    wrapped: ssl.SSLSocket | None = None
    try:
        error = raw.connect_ex(sockaddr)
        if error not in {0, errno.EISCONN}:
            if error not in {
                errno.EINPROGRESS,
                errno.EALREADY,
                errno.EWOULDBLOCK,
                errno.EINTR,
            }:
                raise OSError(error, os.strerror(error))
            _wait_ready(
                raw,
                selectors.EVENT_WRITE,
                deadline=deadline,
                clock=clock,
            )
            error = raw.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if error:
                raise OSError(error, os.strerror(error))
        wrapped = context.wrap_socket(
            raw,
            server_hostname=hostname,
            do_handshake_on_connect=False,
        )
        while True:
            _remaining(deadline, clock)
            try:
                wrapped.do_handshake()
                break
            except ssl.SSLWantReadError:
                _wait_ready(
                    wrapped,
                    selectors.EVENT_READ,
                    deadline=deadline,
                    clock=clock,
                )
            except ssl.SSLWantWriteError:
                _wait_ready(
                    wrapped,
                    selectors.EVENT_WRITE,
                    deadline=deadline,
                    clock=clock,
                )
        if wrapped.selected_alpn_protocol() not in {None, "http/1.1"}:
            raise ssl.SSLError("provider negotiated an unsupported HTTP protocol")
        return wrapped
    except BaseException:
        if wrapped is not None:
            wrapped.close()
        else:
            raw.close()
        raise


def _tls_context() -> ssl.SSLContext:
    """Build a client context from Fedora's fixed root-owned trust bundle.

    Constructing the context explicitly avoids OpenSSL's SSL_CERT_FILE,
    SSL_CERT_DIR and Python's SSLKEYLOGFILE environment hooks.
    """

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    try:
        descriptor = os.open(FEDORA_TRUST_BUNDLE, flags)
    except OSError:
        raise DiscoveryFailure("trust_store_unavailable", "system TLS trust store is unavailable") from None
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & 0o022
            or not 1 <= info.st_size <= MAX_TRUST_BUNDLE_BYTES
        ):
            raise DiscoveryFailure("trust_store_unavailable", "system TLS trust store is unsafe")
        raw = os.read(descriptor, MAX_TRUST_BUNDLE_BYTES + 1)
        if len(raw) != info.st_size or len(raw) > MAX_TRUST_BUNDLE_BYTES or os.read(descriptor, 1):
            raise DiscoveryFailure("trust_store_unavailable", "system TLS trust store changed while reading")
    finally:
        os.close(descriptor)
    try:
        certificate_blocks = re.findall(
            rb"-----BEGIN CERTIFICATE-----[\r\n]+[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----",
            raw,
        )
        if (
            not 1 <= len(certificate_blocks) <= 1024
            or raw.count(b"-----BEGIN CERTIFICATE-----") != len(certificate_blocks)
            or raw.count(b"-----END CERTIFICATE-----") != len(certificate_blocks)
        ):
            raise ValueError
        certificate_data = b"\n".join(certificate_blocks).decode("ascii", errors="strict")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_verify_locations(cadata=certificate_data)
        context.set_alpn_protocols(["http/1.1"])
    except (UnicodeError, ValueError, ssl.SSLError):
        raise DiscoveryFailure("trust_store_unavailable", "system TLS trust store is invalid") from None
    return context


def _send_bytes(
    connection: Any,
    payload: bytes,
    *,
    deadline: float,
    clock: Callable[[], float],
) -> None:
    pending = memoryview(payload)
    while pending:
        _remaining(deadline, clock)
        try:
            sent = connection.send(pending)
            if not isinstance(sent, int) or sent <= 0:
                raise OSError("provider connection closed while sending")
            pending = pending[sent:]
        except (ssl.SSLWantWriteError, BlockingIOError):
            _wait_ready(
                connection,
                selectors.EVENT_WRITE,
                deadline=deadline,
                clock=clock,
            )
        except ssl.SSLWantReadError:
            _wait_ready(
                connection,
                selectors.EVENT_READ,
                deadline=deadline,
                clock=clock,
            )


def _send_http_get(
    connection: Any,
    *,
    hostname: str,
    target: str,
    headers: Mapping[str, str],
    deadline: float,
    clock: Callable[[], float],
) -> None:
    normalized: dict[str, tuple[str, str]] = {}
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or not HEADER_NAME.fullmatch(name)
            or name.lower() in FORBIDDEN_HEADERS
            or not isinstance(value, str)
            or len(value.encode("utf-8")) > 16_384
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise DiscoveryFailure("invalid_request", "provider request headers are unsafe")
        lowered = name.lower()
        if lowered in normalized:
            raise DiscoveryFailure("invalid_request", "provider request contains duplicate headers")
        normalized[lowered] = (name, value)
    lines = [f"GET {target} HTTP/1.1", f"Host: {hostname}", "Connection: close"]
    lines.extend(
        f"{name}: {value}"
        for name, value in sorted(normalized.values(), key=lambda item: item[0].lower())
    )
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
    if len(request) > 32_768:
        raise DiscoveryFailure("invalid_request", "provider request headers exceed the byte limit")
    _send_bytes(connection, request, deadline=deadline, clock=clock)


def _recv_bytes(
    connection: Any,
    maximum: int,
    *,
    deadline: float,
    clock: Callable[[], float],
) -> bytes:
    while True:
        _remaining(deadline, clock)
        try:
            return connection.recv(maximum)
        except (ssl.SSLWantReadError, BlockingIOError):
            _wait_ready(
                connection,
                selectors.EVENT_READ,
                deadline=deadline,
                clock=clock,
            )
        except ssl.SSLWantWriteError:
            _wait_ready(
                connection,
                selectors.EVENT_WRITE,
                deadline=deadline,
                clock=clock,
            )


class _ResponseReader:
    def __init__(
        self,
        connection: Any,
        initial: bytes,
        *,
        deadline: float,
        clock: Callable[[], float],
    ) -> None:
        self.connection = connection
        self.buffer = bytearray(initial)
        self.deadline = deadline
        self.clock = clock

    def _fill(self) -> bool:
        chunk = _recv_bytes(
            self.connection,
            16_384,
            deadline=self.deadline,
            clock=self.clock,
        )
        if not chunk:
            return False
        self.buffer.extend(chunk)
        return True

    def line(self, maximum: int) -> bytes:
        while True:
            marker = self.buffer.find(b"\r\n")
            if marker >= 0:
                if marker > maximum:
                    raise DiscoveryFailure("invalid_response", "provider response line exceeds the byte limit")
                value = bytes(self.buffer[:marker])
                del self.buffer[: marker + 2]
                return value
            if len(self.buffer) > maximum or not self._fill():
                raise DiscoveryFailure("invalid_response", "provider response line is incomplete")

    def exact(self, size: int) -> bytes:
        while len(self.buffer) < size:
            if not self._fill():
                raise DiscoveryFailure("invalid_response", "provider response body is incomplete")
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value

    def to_eof(self, maximum: int) -> bytes:
        if len(self.buffer) > maximum:
            raise DiscoveryFailure("response_too_large", "provider response exceeds the byte limit")
        while self._fill():
            if len(self.buffer) > maximum:
                raise DiscoveryFailure("response_too_large", "provider response exceeds the byte limit")
        return bytes(self.buffer)


def _read_http_response(
    connection: Any,
    *,
    deadline: float,
    clock: Callable[[], float],
    max_response_bytes: int,
) -> tuple[int, str, bytes]:
    reader = _ResponseReader(connection, b"", deadline=deadline, clock=clock)
    status_line = reader.line(MAX_RESPONSE_HEADER_BYTES)
    try:
        decoded_status = status_line.decode("ascii")
    except UnicodeError:
        raise DiscoveryFailure("invalid_response", "provider returned an invalid HTTP status") from None
    matched = re.fullmatch(r"HTTP/1\.[01] ([0-9]{3})(?: [\x20-\x7e]*)?", decoded_status)
    if matched is None:
        raise DiscoveryFailure("invalid_response", "provider returned an invalid HTTP status")
    status = int(matched.group(1))
    response_headers: dict[str, list[str]] = {}
    header_bytes = len(status_line) + 2
    while True:
        line = reader.line(MAX_RESPONSE_HEADER_BYTES)
        header_bytes += len(line) + 2
        if header_bytes > MAX_RESPONSE_HEADER_BYTES:
            raise DiscoveryFailure("invalid_response", "provider response headers exceed the byte limit")
        if not line:
            break
        if line[:1] in {b" ", b"\t"} or b":" not in line:
            raise DiscoveryFailure("invalid_response", "provider returned malformed HTTP headers")
        raw_name, raw_value = line.split(b":", 1)
        try:
            name = raw_name.decode("ascii")
            value = raw_value.decode("iso-8859-1").strip()
        except UnicodeError:
            raise DiscoveryFailure("invalid_response", "provider returned malformed HTTP headers") from None
        if not HEADER_NAME.fullmatch(name) or any(
            ord(character) < 32 and character != "\t" for character in value
        ) or any(ord(character) == 127 for character in value):
            raise DiscoveryFailure("invalid_response", "provider returned malformed HTTP headers")
        response_headers.setdefault(name.lower(), []).append(value)

    if 300 <= status < 400:
        raise DiscoveryFailure("redirect_refused", "provider redirect was refused")
    if status != 200:
        raise DiscoveryFailure("provider_http_error", "provider returned a non-success HTTP status")

    content_lengths = response_headers.get("content-length", [])
    parsed_length: int | None = None
    if content_lengths:
        if len(set(content_lengths)) != 1 or not content_lengths[0].isascii() or not content_lengths[0].isdigit():
            raise DiscoveryFailure("invalid_response", "provider returned an invalid content length")
        parsed_length = int(content_lengths[0])
        if parsed_length > max_response_bytes:
            raise DiscoveryFailure("response_too_large", "provider response exceeds the byte limit")
    transfer_values = response_headers.get("transfer-encoding", [])
    if transfer_values and parsed_length is not None:
        raise DiscoveryFailure("invalid_response", "provider response framing is ambiguous")
    if transfer_values:
        transfer = ",".join(transfer_values).lower().replace(" ", "")
        if transfer != "chunked":
            raise DiscoveryFailure("invalid_response", "provider response transfer encoding is unsupported")
        body = _read_chunked_body(reader, max_response_bytes)
    elif parsed_length is not None:
        body = reader.exact(parsed_length)
    else:
        body = reader.to_eof(max_response_bytes)
    content_type = response_headers.get("content-type", [""])[-1]
    return status, content_type, body


def _read_chunked_body(reader: _ResponseReader, maximum: int) -> bytes:
    body = bytearray()
    while True:
        line = reader.line(MAX_CHUNK_LINE_BYTES)
        size_text = line.split(b";", 1)[0]
        if not re.fullmatch(rb"[0-9A-Fa-f]{1,16}", size_text):
            raise DiscoveryFailure("invalid_response", "provider returned invalid chunk framing")
        size = int(size_text, 16)
        if size == 0:
            trailer_bytes = 0
            while True:
                trailer = reader.line(MAX_TRAILER_BYTES)
                trailer_bytes += len(trailer) + 2
                if trailer_bytes > MAX_TRAILER_BYTES:
                    raise DiscoveryFailure("invalid_response", "provider response trailers exceed the byte limit")
                if not trailer:
                    return bytes(body)
                if trailer[:1] in {b" ", b"\t"} or b":" not in trailer:
                    raise DiscoveryFailure("invalid_response", "provider response trailers are malformed")
        if len(body) + size > maximum:
            raise DiscoveryFailure("response_too_large", "provider response exceeds the byte limit")
        body.extend(reader.exact(size))
        if reader.exact(2) != b"\r\n":
            raise DiscoveryFailure("invalid_response", "provider returned invalid chunk framing")


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise DiscoveryFailure("invalid_registry", f"{label} has an invalid field set")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DiscoveryFailure("invalid_registry", f"{label} must be an object")
    return value


def _list(value: Any, label: str, minimum: int, maximum: int) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise DiscoveryFailure("invalid_registry", f"{label} has an invalid list length")
    return value


def _text(value: Any, label: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum or any(ord(char) < 32 for char in value):
        raise DiscoveryFailure("invalid_registry", f"{label} has invalid text")
    return value


def _identifier(value: Any, label: str) -> str:
    text = _text(value, label, 1, 128)
    if not IDENTIFIER.fullmatch(text):
        raise DiscoveryFailure("invalid_registry", f"{label} has an invalid identifier")
    return text


def _date(value: Any, label: str) -> None:
    text = _text(value, label, 10, 10)
    try:
        if dt.date.fromisoformat(text).isoformat() != text:
            raise ValueError
    except ValueError as exc:
        raise DiscoveryFailure("invalid_registry", f"{label} has an invalid date") from exc


def _registry_https_url(value: Any, label: str, *, nullable: bool = False, maximum: int = 512) -> None:
    if value is None and nullable:
        return
    text = _text(value, label, 8, maximum)
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        raise DiscoveryFailure("invalid_registry", f"{label} has an invalid HTTPS URL")


def _load_file(path: Path, *, max_bytes: int, private: bool, error_code: str) -> bytes:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise DiscoveryFailure(error_code, "input path is not a safe regular file")
        if info.st_uid not in {0, os.geteuid()}:
            raise DiscoveryFailure(error_code, "input file owner is not trusted")
        forbidden_mode = 0o077 if private else 0o022
        if info.st_mode & forbidden_mode:
            raise DiscoveryFailure(error_code, "input file permissions are unsafe")
        if info.st_size > max_bytes:
            raise DiscoveryFailure(error_code, "input file exceeds the byte limit")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            opened = os.fstat(descriptor)
            if opened.st_dev != info.st_dev or opened.st_ino != info.st_ino or not stat.S_ISREG(opened.st_mode):
                raise DiscoveryFailure(error_code, "input file changed while opening")
            raw = os.read(descriptor, max_bytes + 1)
            if len(raw) > max_bytes or os.read(descriptor, 1):
                raise DiscoveryFailure(error_code, "input file exceeds the byte limit")
            return raw
        finally:
            os.close(descriptor)
    except DiscoveryFailure:
        raise
    except OSError:
        raise DiscoveryFailure(error_code, "input file is unavailable") from None


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DiscoveryFailure("invalid_json", "JSON contains a duplicate field")
        result[key] = value
    return result


def _decode_json(raw: bytes, *, code: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_object)
    except DiscoveryFailure:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise DiscoveryFailure(code, "input is not valid bounded JSON") from None


def load_registry(path: Path, limits: CollectorLimits = CollectorLimits()) -> dict[str, Any]:
    raw = _load_file(path, max_bytes=limits.registry_bytes, private=False, error_code="invalid_registry")
    document = _decode_json(raw, code="invalid_registry")
    registry = _object(document, "registry")
    _validate_registry(registry)
    return registry


def _validate_registry(registry: dict[str, Any]) -> None:
    _exact_keys(registry, REGISTRY_KEYS, "registry")
    if registry["schema_version"] != 1 or isinstance(registry["schema_version"], bool):
        raise DiscoveryFailure("invalid_registry", "registry schema_version must be 1")
    revision = _text(registry["revision"], "registry.revision", 1, 128)
    if not REVISION.fullmatch(revision):
        raise DiscoveryFailure("invalid_registry", "registry.revision is invalid")
    _date(registry["verified_on"], "registry.verified_on")
    if registry["finance_capability_names"] != list(FINANCE_CAPABILITIES):
        raise DiscoveryFailure("invalid_registry", "registry finance capability names are invalid")
    if registry["finance_access_modes"] != list(FINANCE_ACCESS_MODES):
        raise DiscoveryFailure("invalid_registry", "registry finance access modes are invalid")

    providers = _list(registry["provider_accounts"], "registry.provider_accounts", 21, 21)
    provider_ids: set[str] = set()
    for index, raw_provider in enumerate(providers):
        provider = _object(raw_provider, f"provider_accounts[{index}]")
        _exact_keys(provider, PROVIDER_KEYS, f"provider_accounts[{index}]")
        provider_id = _identifier(provider["id"], f"provider_accounts[{index}].id")
        if provider_id in provider_ids:
            raise DiscoveryFailure("invalid_registry", "registry contains duplicate provider ids")
        provider_ids.add(provider_id)
        _text(provider["display_name"], f"{provider_id}.display_name", 1, 160)
        _validate_inference(provider_id, _object(provider["inference"], f"{provider_id}.inference"))
        _validate_credentials(provider_id, _object(provider["credentials"], f"{provider_id}.credentials"))
        _validate_finance(provider_id, provider)

    mappings = _list(registry["deployment_mappings"], "registry.deployment_mappings", 0, 1024)
    deployment_ids: set[str] = set()
    for index, raw_mapping in enumerate(mappings):
        mapping = _object(raw_mapping, f"deployment_mappings[{index}]")
        _exact_keys(mapping, DEPLOYMENT_KEYS, f"deployment_mappings[{index}]")
        deployment_id = _identifier(mapping["deployment_id"], f"deployment_mappings[{index}].deployment_id")
        if deployment_id in deployment_ids:
            raise DiscoveryFailure("invalid_registry", "registry contains duplicate deployment ids")
        deployment_ids.add(deployment_id)
        _identifier(mapping["card_id"], f"{deployment_id}.card_id")
        _identifier(mapping["developer_id"], f"{deployment_id}.developer_id")
        provider_id = _identifier(mapping["inference_provider_account_id"], f"{deployment_id}.provider")
        if provider_id not in provider_ids:
            raise DiscoveryFailure("invalid_registry", "deployment references an unknown provider")
        model_id = _text(mapping["exact_model_id"], f"{deployment_id}.exact_model_id", 1, 200)
        if not MODEL_ID.fullmatch(model_id):
            raise DiscoveryFailure("invalid_registry", "deployment has an invalid exact model id")
        if mapping["activation_state"] not in {"configured_not_network_verified", "canary_verified", "disabled"}:
            raise DiscoveryFailure("invalid_registry", "deployment has an invalid activation state")
        if mapping["source"] != "local_orchestrator_configuration":
            raise DiscoveryFailure("invalid_registry", "deployment has an invalid source")


def _validate_inference(provider_id: str, inference: dict[str, Any]) -> None:
    _exact_keys(inference, {"protocols", "sites", "model_discovery"}, f"{provider_id}.inference")
    protocols = _list(inference["protocols"], f"{provider_id}.protocols", 1, 8)
    if len(set(protocols)) != len(protocols) or any(protocol not in PROTOCOLS for protocol in protocols):
        raise DiscoveryFailure("invalid_registry", f"{provider_id}.protocols is invalid")
    for index, raw_site in enumerate(_list(inference["sites"], f"{provider_id}.sites", 1, 16)):
        site = _object(raw_site, f"{provider_id}.sites[{index}]")
        _exact_keys(site, {"id", "region", "base_url", "status"}, f"{provider_id}.sites[{index}]")
        _identifier(site["id"], f"{provider_id}.sites[{index}].id")
        _text(site["region"], f"{provider_id}.sites[{index}].region", 1, 64)
        _registry_https_url(site["base_url"], f"{provider_id}.sites[{index}].base_url", nullable=True)
        if site["status"] not in {"documented", "template", "unverified"}:
            raise DiscoveryFailure("invalid_registry", f"{provider_id}.sites[{index}].status is invalid")
    discovery = _object(inference["model_discovery"], f"{provider_id}.model_discovery")
    _exact_keys(discovery, DISCOVERY_KEYS, f"{provider_id}.model_discovery")
    if discovery["mode"] not in {"api", "control_plane_api", "static_documentation", "unverified"}:
        raise DiscoveryFailure("invalid_registry", f"{provider_id}.model_discovery.mode is invalid")
    if discovery["method"] not in {"GET", "POST", None}:
        raise DiscoveryFailure("invalid_registry", f"{provider_id}.model_discovery.method is invalid")
    _registry_https_url(discovery["endpoint"], f"{provider_id}.model_discovery.endpoint", nullable=True)
    if discovery["operation"] is not None:
        _text(discovery["operation"], f"{provider_id}.model_discovery.operation", 1, 128)
    if discovery["credential_scope"] not in {"inference", "admin_finance", "none"}:
        raise DiscoveryFailure("invalid_registry", f"{provider_id}.model_discovery.credential_scope is invalid")
    if discovery["mode"] == "api" and (discovery["method"] is None or discovery["endpoint"] is None):
        raise DiscoveryFailure("invalid_registry", f"{provider_id}.model_discovery API is incomplete")


def _validate_credentials(provider_id: str, credentials: dict[str, Any]) -> None:
    _exact_keys(credentials, {"inference", "admin_finance"}, f"{provider_id}.credentials")
    for scope in ("inference", "admin_finance"):
        credential = _object(credentials[scope], f"{provider_id}.credentials.{scope}")
        _exact_keys(credential, CREDENTIAL_KEYS, f"{provider_id}.credentials.{scope}")
        ref = credential["credential_ref"]
        if ref is not None and (not isinstance(ref, str) or len(ref) > 256 or not CREDENTIAL_REF.fullmatch(ref)):
            raise DiscoveryFailure("invalid_registry", f"{provider_id}.{scope}.credential_ref is invalid")
        if credential["auth_scheme"] not in AUTH_SCHEMES:
            raise DiscoveryFailure("invalid_registry", f"{provider_id}.{scope}.auth_scheme is invalid")
        header = credential["header_name"]
        if header is not None and (not isinstance(header, str) or not HEADER_NAME.fullmatch(header)):
            raise DiscoveryFailure("invalid_registry", f"{provider_id}.{scope}.header_name is invalid")
        prefix = credential["value_prefix"]
        if prefix is not None and (not isinstance(prefix, str) or len(prefix) > 32 or any(ord(c) < 32 for c in prefix)):
            raise DiscoveryFailure("invalid_registry", f"{provider_id}.{scope}.value_prefix is invalid")


def _validate_finance(provider_id: str, provider: dict[str, Any]) -> None:
    capabilities = _object(provider["financial_capabilities"], f"{provider_id}.financial_capabilities")
    _exact_keys(capabilities, set(FINANCE_CAPABILITIES), f"{provider_id}.financial_capabilities")
    if any(value not in FINANCE_ACCESS_MODES for value in capabilities.values()):
        raise DiscoveryFailure("invalid_registry", f"{provider_id}.financial_capabilities is invalid")
    for index, raw_endpoint in enumerate(_list(provider["finance_endpoints"], f"{provider_id}.finance_endpoints", 0, 32)):
        endpoint = _object(raw_endpoint, f"{provider_id}.finance_endpoints[{index}]")
        _exact_keys(endpoint, {"capabilities", "access", "method", "endpoint", "operation", "credential_scope"}, f"{provider_id}.finance_endpoints[{index}]")
        listed = _list(endpoint["capabilities"], f"{provider_id}.finance_endpoints[{index}].capabilities", 1, 6)
        if len(set(listed)) != len(listed) or any(item not in FINANCE_CAPABILITIES for item in listed):
            raise DiscoveryFailure("invalid_registry", f"{provider_id}.finance endpoint capabilities are invalid")
        if endpoint["access"] not in {"direct_api", "admin_api", "cloud_billing_api"} or endpoint["method"] not in {"GET", "POST"}:
            raise DiscoveryFailure("invalid_registry", f"{provider_id}.finance endpoint contract is invalid")
        _registry_https_url(endpoint["endpoint"], f"{provider_id}.finance_endpoints[{index}].endpoint")
        if endpoint["operation"] is not None:
            _text(endpoint["operation"], f"{provider_id}.finance_endpoints[{index}].operation", 1, 160)
        if endpoint["credential_scope"] not in {"inference", "admin_finance"}:
            raise DiscoveryFailure("invalid_registry", f"{provider_id}.finance endpoint scope is invalid")
    for index, raw_source in enumerate(_list(provider["official_sources"], f"{provider_id}.official_sources", 1, 16)):
        source = _object(raw_source, f"{provider_id}.official_sources[{index}]")
        _exact_keys(source, {"title", "url", "verified_on"}, f"{provider_id}.official_sources[{index}]")
        _text(source["title"], f"{provider_id}.official_sources[{index}].title", 1, 160)
        _registry_https_url(source["url"], f"{provider_id}.official_sources[{index}].url", maximum=1024)
        _date(source["verified_on"], f"{provider_id}.official_sources[{index}].verified_on")
    limitations = _list(provider["limitations"], f"{provider_id}.limitations", 1, 16)
    if len(set(limitations)) != len(limitations):
        raise DiscoveryFailure("invalid_registry", f"{provider_id}.limitations contains duplicates")
    for index, limitation in enumerate(limitations):
        _text(limitation, f"{provider_id}.limitations[{index}]", 1, 512)


def load_credential_map(
    path: Path,
    registry: Mapping[str, Any],
    limits: CollectorLimits = CollectorLimits(),
) -> dict[str, Path]:
    raw = _load_file(path, max_bytes=limits.credential_map_bytes, private=True, error_code="invalid_credential_map")
    document = _decode_json(raw, code="invalid_credential_map")
    mapping = _object(document, "credential_map")
    _exact_keys(mapping, {"schema_version", "credentials"}, "credential_map")
    if mapping["schema_version"] != 1 or isinstance(mapping["schema_version"], bool):
        raise DiscoveryFailure("invalid_credential_map", "credential map schema_version must be 1")
    entries = _object(mapping["credentials"], "credential_map.credentials")
    if len(entries) > 128:
        raise DiscoveryFailure("invalid_credential_map", "credential map contains too many entries")
    providers = {provider["id"]: provider for provider in registry["provider_accounts"]}
    result: dict[str, Path] = {}
    for provider_id, raw_entry in entries.items():
        if provider_id not in providers:
            raise DiscoveryFailure("invalid_credential_map", "credential map references an unknown provider")
        entry = _object(raw_entry, f"credential_map.credentials.{provider_id}")
        _exact_keys(entry, {"credential_ref", "path"}, f"credential_map.credentials.{provider_id}")
        expected_ref = providers[provider_id]["credentials"]["inference"]["credential_ref"]
        if entry["credential_ref"] != expected_ref or expected_ref is None:
            raise DiscoveryFailure("invalid_credential_map", "credential map reference does not match the registry")
        path_text = _text(entry["path"], f"credential_map.credentials.{provider_id}.path", 1, 4096)
        credential_path = Path(path_text)
        if not credential_path.is_absolute():
            raise DiscoveryFailure("invalid_credential_map", "credential paths must be absolute")
        result[provider_id] = credential_path
    return result


def _read_credential(path: Path, limits: CollectorLimits) -> str:
    raw = bytearray(_load_file(path, max_bytes=limits.credential_bytes, private=True, error_code="credential_unavailable"))
    try:
        try:
            encoded = bytes(raw)
            if encoded.endswith(b"\r\n"):
                encoded = encoded[:-2]
            elif encoded.endswith(b"\n"):
                encoded = encoded[:-1]
            if b"\r" in encoded or b"\n" in encoded:
                raise UnicodeError
            value = encoded.decode("utf-8", errors="strict")
        except UnicodeError:
            raise DiscoveryFailure("credential_unavailable", "credential file has an invalid encoding") from None
        if (
            not value
            or value != value.strip()
            or any(ord(char) < 33 or ord(char) == 127 for char in value)
            or len(value.encode("utf-8")) > limits.credential_bytes
        ):
            raise DiscoveryFailure("credential_unavailable", "credential file has an invalid value")
        return value
    finally:
        for index in range(len(raw)):
            raw[index] = 0


def _static_https_endpoint(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 512
        or not value.isascii()
        or any(ord(char) < 33 or ord(char) > 126 for char in value)
        or any(char in value for char in "{}\\")
    ):
        raise DiscoveryFailure("non_static_endpoint", "model discovery endpoint is not a static URL")
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise DiscoveryFailure("non_static_endpoint", "model discovery endpoint is not a static URL") from None
    hostname = parsed.hostname
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port not in {None, 443}
        or not parsed.path.startswith("/")
        or hostname.lower() in {"localhost", "localhost.localdomain"}
        or hostname.lower().endswith((".local", ".internal", ".localhost"))
    ):
        raise DiscoveryFailure("non_static_endpoint", "model discovery endpoint is not a static HTTPS URL")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise DiscoveryFailure("non_static_endpoint", "literal IP discovery endpoints are forbidden")
    if not all(label and re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in hostname.split(".")):
        raise DiscoveryFailure("non_static_endpoint", "model discovery hostname is invalid")
    return value


def _build_headers(credential: Mapping[str, Any], secret: str) -> dict[str, str]:
    scheme = credential["auth_scheme"]
    header = credential["header_name"]
    prefix = credential["value_prefix"]
    if scheme not in SUPPORTED_AUTH_SCHEMES or not isinstance(header, str) or not HEADER_NAME.fullmatch(header):
        raise DiscoveryFailure("unsupported_auth_scheme", "registry authentication scheme is not safely supported")
    if header.lower() in FORBIDDEN_HEADERS:
        raise DiscoveryFailure("unsupported_auth_scheme", "registry authentication header is forbidden")
    if scheme == "bearer" and (header.lower() != "authorization" or prefix != "Bearer "):
        raise DiscoveryFailure("unsupported_auth_scheme", "bearer authentication contract is inconsistent")
    if scheme == "google_api_key" and (header.lower() != "x-goog-api-key" or prefix is not None):
        raise DiscoveryFailure("unsupported_auth_scheme", "Google API key contract is inconsistent")
    if scheme == "x_api_key" and prefix is not None:
        raise DiscoveryFailure("unsupported_auth_scheme", "API key authentication contract is inconsistent")
    if scheme == "x_api_key_and_version" and (header.lower() != "x-api-key" or prefix is not None):
        raise DiscoveryFailure("unsupported_auth_scheme", "versioned API key contract is inconsistent")
    if scheme == "api_key_or_bearer" and (header.lower() != "authorization" or prefix != "Bearer "):
        raise DiscoveryFailure("unsupported_auth_scheme", "API key or bearer contract is ambiguous")
    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": "ops-provider-discovery/0.1",
        header: f"{prefix or ''}{secret}",
    }
    if scheme == "x_api_key_and_version":
        headers["anthropic-version"] = "2023-06-01"
    return headers


def _bounded_json_tree(value: Any, limits: CollectorLimits) -> None:
    stack: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_json_nodes or depth > limits.max_json_depth:
            raise DiscoveryFailure("invalid_response", "provider JSON exceeds structural limits")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)


def _extract_models(document: Any, limits: CollectorLimits, secret: str) -> tuple[list[str], bool, int]:
    root = _object_response(document)
    locations: list[list[Any]] = []
    for key in ("data", "models"):
        if isinstance(root.get(key), list):
            locations.append(root[key])
    output = root.get("output")
    if isinstance(output, dict) and isinstance(output.get("models"), list):
        locations.append(output["models"])
    if len(locations) != 1:
        raise DiscoveryFailure("invalid_response", "provider JSON has no single recognized model list")
    items = locations[0]
    if len(items) > limits.max_candidates:
        raise DiscoveryFailure("too_many_models", "provider returned too many model entries")
    model_ids: list[str] = []
    for item in items:
        if isinstance(item, str):
            model_id = item
        elif isinstance(item, dict):
            values = [item[key] for key in ("id", "name", "model", "model_id", "modelId") if isinstance(item.get(key), str)]
            unique = list(dict.fromkeys(values))
            if len(unique) != 1:
                raise DiscoveryFailure("invalid_response", "provider model entry has no unambiguous id")
            model_id = unique[0]
        else:
            raise DiscoveryFailure("invalid_response", "provider model entry has an invalid type")
        if not MODEL_ID.fullmatch(model_id) or secret in model_id:
            raise DiscoveryFailure("invalid_response", "provider model id is invalid")
        model_ids.append(model_id)
    unique_ids = sorted(set(model_ids), key=lambda item: (item.casefold(), item))
    pagination_keys = ("has_more", "next", "next_page", "next_page_token", "nextPageToken")
    possibly_truncated = any(root.get(key) not in {None, False, ""} for key in pagination_keys)
    return unique_ids, possibly_truncated, len(model_ids) - len(unique_ids)


def _object_response(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DiscoveryFailure("invalid_response", "provider JSON root must be an object")
    return value


def _validate_limits(limits: CollectorLimits) -> None:
    defaults = CollectorLimits()
    integer_limits = (
        (limits.registry_bytes, defaults.registry_bytes),
        (limits.credential_map_bytes, defaults.credential_map_bytes),
        (limits.credential_bytes, defaults.credential_bytes),
        (limits.response_bytes, defaults.response_bytes),
        (limits.max_candidates, defaults.max_candidates),
        (limits.max_json_nodes, defaults.max_json_nodes),
        (limits.max_json_depth, defaults.max_json_depth),
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum for value, maximum in integer_limits):
        raise DiscoveryFailure("invalid_limits", "collector limits must be positive and cannot exceed hard limits")
    if (
        isinstance(limits.timeout_seconds, bool)
        or not isinstance(limits.timeout_seconds, (int, float))
        or not 0 < limits.timeout_seconds <= defaults.timeout_seconds
    ):
        raise DiscoveryFailure("invalid_limits", "collector timeout must be positive and cannot exceed the hard limit")


class Collector:
    def __init__(
        self,
        registry: Mapping[str, Any],
        credential_paths: Mapping[str, Path],
        *,
        transport: Transport | None = None,
        limits: CollectorLimits = CollectorLimits(),
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        _validate_limits(limits)
        copied_registry = copy.deepcopy(dict(registry))
        _validate_registry(copied_registry)
        provider_ids = {provider["id"] for provider in copied_registry["provider_accounts"]}
        copied_paths: dict[str, Path] = {}
        for provider_id, path in credential_paths.items():
            if provider_id not in provider_ids or not isinstance(path, Path) or not path.is_absolute():
                raise DiscoveryFailure("invalid_credential_map", "collector credential path mapping is invalid")
            copied_paths[provider_id] = path
        self._registry = copied_registry
        self._credential_paths = copied_paths
        self._transport = transport or UrlLibTransport()
        self._limits = limits
        self._clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))

    def collect(self) -> dict[str, Any]:
        mappings_by_provider: dict[str, dict[str, list[str]]] = {}
        for mapping in self._registry["deployment_mappings"]:
            provider_models = mappings_by_provider.setdefault(mapping["inference_provider_account_id"], {})
            provider_models.setdefault(mapping["exact_model_id"], []).append(mapping["deployment_id"])

        provider_results: list[dict[str, Any]] = []
        for provider in self._registry["provider_accounts"]:
            provider_results.append(self._collect_provider(provider, mappings_by_provider.get(provider["id"], {})))

        attempted = [item for item in provider_results if item["status"] != "excluded"]
        succeeded = [item for item in provider_results if item["status"] == "succeeded"]
        return {
            "schema_version": 1,
            "generated_at": self._clock().astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "registry_revision": self._registry["revision"],
            "mode": "observation_only",
            "catalogue_mutated": False,
            "activation_performed": False,
            "limits": {
                "response_bytes": self._limits.response_bytes,
                "max_candidates_per_provider": self._limits.max_candidates,
                "timeout_seconds": self._limits.timeout_seconds,
                "redirects": "refused",
                "proxies": "disabled",
                "pagination": "not_followed",
            },
            "summary": {
                "providers_total": len(provider_results),
                "providers_eligible": len(attempted),
                "providers_succeeded": len(succeeded),
                "providers_failed": sum(item["status"] == "failed" for item in provider_results),
                "providers_excluded": sum(item["status"] == "excluded" for item in provider_results),
                "models_discovered": sum(len(item.get("candidates", [])) for item in succeeded),
                "unregistered_candidates": sum(
                    candidate["classification"] == "unregistered_candidate"
                    for item in succeeded
                    for candidate in item.get("candidates", [])
                ),
                "registered_models_missing": sum(len(item.get("registered_models_missing", [])) for item in succeeded),
            },
            "providers": provider_results,
        }

    def _collect_provider(self, provider: Mapping[str, Any], registered: Mapping[str, list[str]]) -> dict[str, Any]:
        provider_id = provider["id"]
        discovery = provider["inference"]["model_discovery"]
        base = {"provider_account_id": provider_id, "display_name": provider["display_name"]}
        if discovery["mode"] != "api" or discovery["method"] != "GET":
            return {**base, "status": "excluded", "reason_code": "not_api_get"}
        if discovery["credential_scope"] != "inference" or discovery["operation"] is not None:
            return {**base, "status": "excluded", "reason_code": "unsupported_discovery_contract"}
        try:
            endpoint = _static_https_endpoint(discovery["endpoint"])
        except DiscoveryFailure as exc:
            return {**base, "status": "excluded", "reason_code": exc.code}
        credential = provider["credentials"]["inference"]
        if credential["auth_scheme"] not in SUPPORTED_AUTH_SCHEMES:
            return {**base, "status": "excluded", "reason_code": "unsupported_auth_scheme"}
        path = self._credential_paths.get(provider_id)
        if path is None:
            return {**base, "status": "failed", "error_code": "credential_unavailable"}
        secret = ""
        try:
            secret = _read_credential(path, self._limits)
            headers = _build_headers(credential, secret)
            response = self._transport.get(
                endpoint,
                headers,
                timeout_seconds=self._limits.timeout_seconds,
                max_response_bytes=self._limits.response_bytes,
            )
            if response.status != 200:
                raise DiscoveryFailure("provider_http_error", "provider returned a non-success HTTP status")
            if response.final_url != endpoint:
                raise DiscoveryFailure("redirect_refused", "provider final URL differs from the registry endpoint")
            content_type = response.content_type.lower().split(";", 1)[0].strip()
            if content_type != "application/json" and not content_type.endswith("+json"):
                raise DiscoveryFailure("invalid_response", "provider response is not JSON")
            if len(response.body) > self._limits.response_bytes:
                raise DiscoveryFailure("response_too_large", "provider response exceeds the byte limit")
            document = _decode_json(response.body, code="invalid_response")
            _bounded_json_tree(document, self._limits)
            model_ids, possibly_truncated, duplicates = _extract_models(document, self._limits, secret)
            candidates = [
                {
                    "model_id": model_id,
                    "classification": "registered_mapping" if model_id in registered else "unregistered_candidate",
                    "registered_deployment_ids": sorted(registered.get(model_id, [])),
                }
                for model_id in model_ids
            ]
            return {
                **base,
                "status": "succeeded",
                "endpoint": endpoint,
                "possibly_truncated": possibly_truncated,
                "duplicate_entries_discarded": duplicates,
                "candidates": candidates,
                "registered_models_missing": sorted(set(registered) - set(model_ids)),
            }
        except DiscoveryFailure as exc:
            return {**base, "status": "failed", "endpoint": endpoint, "error_code": exc.code}
        except Exception:
            return {**base, "status": "failed", "endpoint": endpoint, "error_code": "internal_collection_error"}
        finally:
            secret = ""


def collect_inventory(
    registry_path: Path,
    credential_map_path: Path,
    *,
    transport: Transport | None = None,
    limits: CollectorLimits = CollectorLimits(),
    clock: Callable[[], dt.datetime] | None = None,
) -> dict[str, Any]:
    registry = load_registry(registry_path, limits)
    credential_paths = load_credential_map(credential_map_path, registry, limits)
    return Collector(registry, credential_paths, transport=transport, limits=limits, clock=clock).collect()
