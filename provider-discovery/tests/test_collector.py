from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
import socket
import threading
import time
from typing import Any, Mapping

import pytest

import ops_provider_discovery.collector as collector_module
from ops_provider_discovery.cli import main
from ops_provider_discovery.collector import (
    Collector,
    CollectorLimits,
    DiscoveryFailure,
    TransportResponse,
    UrlLibTransport,
    load_credential_map,
    load_registry,
)


ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = ROOT / "catalog" / "provider-integrations.v1.json"
FIXED_TIME = dt.datetime(2026, 9, 5, 12, 0, tzinfo=dt.timezone.utc)


class FakeTransport:
    def __init__(self, response: TransportResponse | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def get(
        self,
        endpoint: str,
        headers: Mapping[str, str],
        *,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> TransportResponse:
        self.calls.append(
            {
                "endpoint": endpoint,
                "headers": dict(headers),
                "timeout_seconds": timeout_seconds,
                "max_response_bytes": max_response_bytes,
            }
        )
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def registry_copy() -> dict[str, Any]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def private_json(tmp_path: Path, name: str, document: Any) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    return path


def private_secret(tmp_path: Path, name: str, value: str = "secret-test-value") -> Path:
    path = tmp_path / name
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def provider(registry: Mapping[str, Any], provider_id: str) -> dict[str, Any]:
    return next(item for item in registry["provider_accounts"] if item["id"] == provider_id)


def credential_paths(registry: Mapping[str, Any], tmp_path: Path, provider_id: str, secret: str = "secret-test-value") -> dict[str, Path]:
    del registry
    return {provider_id: private_secret(tmp_path, f"{provider_id}.credential", secret)}


def response(endpoint: str, document: Any, **overrides: Any) -> TransportResponse:
    values = {
        "status": 200,
        "final_url": endpoint,
        "content_type": "application/json; charset=utf-8",
        "body": json.dumps(document).encode("utf-8"),
    }
    values.update(overrides)
    return TransportResponse(**values)


def collect_one(
    registry: dict[str, Any],
    tmp_path: Path,
    provider_id: str,
    fake: FakeTransport,
    *,
    secret: str = "secret-test-value",
    limits: CollectorLimits = CollectorLimits(),
) -> dict[str, Any]:
    return Collector(
        registry,
        credential_paths(registry, tmp_path, provider_id, secret),
        transport=fake,
        limits=limits,
        clock=lambda: FIXED_TIME,
    ).collect()


def result_for(report: Mapping[str, Any], provider_id: str) -> dict[str, Any]:
    return next(item for item in report["providers"] if item["provider_account_id"] == provider_id)


def test_loads_the_real_registry_strictly() -> None:
    registry = load_registry(REGISTRY_PATH)
    assert registry["schema_version"] == 1
    assert len(registry["provider_accounts"]) == 21


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"unexpected": True}),
        lambda value: value["provider_accounts"][0].update({"unexpected": True}),
        lambda value: value.update({"schema_version": True}),
        lambda value: value["provider_accounts"].pop(),
    ],
)
def test_registry_rejects_unknown_fields_types_and_wrong_cardinality(tmp_path: Path, mutation: Any) -> None:
    document = registry_copy()
    mutation(document)
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(DiscoveryFailure) as caught:
        load_registry(path)
    assert caught.value.code == "invalid_registry"


def test_registry_rejects_duplicate_json_fields(tmp_path: Path) -> None:
    path = tmp_path / "registry.json"
    path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    path.chmod(0o644)
    with pytest.raises(DiscoveryFailure) as caught:
        load_registry(path)
    assert caught.value.code == "invalid_json"


def test_registry_rejects_symlink_and_group_writable_file(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text(REGISTRY_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o664)
    with pytest.raises(DiscoveryFailure) as caught:
        load_registry(target)
    assert caught.value.code == "invalid_registry"
    target.chmod(0o644)
    link = tmp_path / "registry.json"
    link.symlink_to(target)
    with pytest.raises(DiscoveryFailure) as caught:
        load_registry(link)
    assert caught.value.code == "invalid_registry"


def test_credential_map_requires_private_explicit_matching_paths(tmp_path: Path) -> None:
    registry = registry_copy()
    reference = provider(registry, "openai")["credentials"]["inference"]["credential_ref"]
    secret_path = private_secret(tmp_path, "openai.key")
    map_path = private_json(
        tmp_path,
        "map.json",
        {"schema_version": 1, "credentials": {"openai": {"credential_ref": reference, "path": str(secret_path)}}},
    )
    assert load_credential_map(map_path, registry) == {"openai": secret_path}

    map_path.chmod(0o640)
    with pytest.raises(DiscoveryFailure) as caught:
        load_credential_map(map_path, registry)
    assert caught.value.code == "invalid_credential_map"

    map_path.chmod(0o600)
    wrong = json.loads(map_path.read_text(encoding="utf-8"))
    wrong["credentials"]["openai"]["credential_ref"] = "kv-infra-shared/data/llm/providers/deepseek"
    map_path.write_text(json.dumps(wrong), encoding="utf-8")
    with pytest.raises(DiscoveryFailure) as caught:
        load_credential_map(map_path, registry)
    assert caught.value.code == "invalid_credential_map"


def test_collects_and_classifies_without_mutating_registry(tmp_path: Path) -> None:
    registry = registry_copy()
    registry["deployment_mappings"].append(
        {
            "deployment_id": "openai-test",
            "card_id": "gpt-5-4",
            "developer_id": "openai",
            "inference_provider_account_id": "openai",
            "exact_model_id": "gpt-known",
            "activation_state": "disabled",
            "source": "local_orchestrator_configuration",
        }
    )
    original = copy.deepcopy(registry)
    endpoint = provider(registry, "openai")["inference"]["model_discovery"]["endpoint"]
    fake = FakeTransport(response(endpoint, {"data": [{"id": "gpt-new"}, {"id": "gpt-known"}, {"id": "gpt-new"}]}))
    report = collect_one(registry, tmp_path, "openai", fake)
    openai = result_for(report, "openai")

    assert openai == {
        "provider_account_id": "openai",
        "display_name": "OpenAI",
        "status": "succeeded",
        "endpoint": endpoint,
        "possibly_truncated": False,
        "duplicate_entries_discarded": 1,
        "candidates": [
            {"model_id": "gpt-known", "classification": "registered_mapping", "registered_deployment_ids": ["openai-test"]},
            {"model_id": "gpt-new", "classification": "unregistered_candidate", "registered_deployment_ids": []},
        ],
        "registered_models_missing": [],
    }
    assert report["mode"] == "observation_only"
    assert report["catalogue_mutated"] is False
    assert report["activation_performed"] is False
    assert report["generated_at"] == "2026-09-05T12:00:00Z"
    assert registry == original
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer secret-test-value"


@pytest.mark.parametrize(
    ("provider_id", "header", "value", "extra_header"),
    [
        ("google", "Authorization", "Bearer google-secret", None),
        ("anthropic", "x-api-key", "anthropic-secret", ("anthropic-version", "2023-06-01")),
        ("xiaomi", "Authorization", "Bearer xiaomi-secret", None),
    ],
)
def test_authentication_follows_registry_scheme_and_header(
    tmp_path: Path,
    provider_id: str,
    header: str,
    value: str,
    extra_header: tuple[str, str] | None,
) -> None:
    registry = registry_copy()
    endpoint = provider(registry, provider_id)["inference"]["model_discovery"]["endpoint"]
    fake = FakeTransport(response(endpoint, {"models": [{"name": "model-one"}]}))
    report = collect_one(registry, tmp_path, provider_id, fake, secret=value.removeprefix("Bearer "))
    assert result_for(report, provider_id)["status"] == "succeeded"
    assert fake.calls[0]["headers"][header] == value
    if extra_header:
        assert fake.calls[0]["headers"][extra_header[0]] == extra_header[1]


def test_only_literal_api_get_endpoints_are_called(tmp_path: Path) -> None:
    registry = registry_copy()
    endpoint = provider(registry, "openai")["inference"]["model_discovery"]["endpoint"]
    fake = FakeTransport(response(endpoint, {"data": []}))
    report = collect_one(registry, tmp_path, "openai", fake)
    assert len(fake.calls) == 1
    assert fake.calls[0]["endpoint"] == endpoint
    assert result_for(report, "microsoft") == {
        "provider_account_id": "microsoft",
        "display_name": "Microsoft Azure Foundry",
        "status": "excluded",
        "reason_code": "non_static_endpoint",
    }
    assert result_for(report, "aws")["reason_code"] == "not_api_get"
    assert result_for(report, "bytedance")["reason_code"] == "not_api_get"


@pytest.mark.parametrize(
    ("fake_response", "error_code"),
    [
        (lambda endpoint: response(endpoint, {"data": []}, final_url="https://api.openai.com/v1/other"), "redirect_refused"),
        (lambda endpoint: response(endpoint, {"data": []}, status=500), "provider_http_error"),
        (lambda endpoint: response(endpoint, {"data": []}, content_type="text/html"), "invalid_response"),
        (lambda endpoint: TransportResponse(200, endpoint, "application/json", b"not-json"), "invalid_response"),
        (lambda endpoint: response(endpoint, {"data": [], "models": []}), "invalid_response"),
    ],
)
def test_response_errors_are_normalized_without_bodies(tmp_path: Path, fake_response: Any, error_code: str) -> None:
    registry = registry_copy()
    endpoint = provider(registry, "openai")["inference"]["model_discovery"]["endpoint"]
    fake = FakeTransport(fake_response(endpoint))
    report = collect_one(registry, tmp_path, "openai", fake)
    assert result_for(report, "openai")["error_code"] == error_code
    assert "not-json" not in json.dumps(report)


def test_response_byte_candidate_node_and_depth_limits(tmp_path: Path) -> None:
    registry = registry_copy()
    endpoint = provider(registry, "openai")["inference"]["model_discovery"]["endpoint"]

    limits = CollectorLimits(response_bytes=20, max_candidates=2, max_json_nodes=10, max_json_depth=3)
    oversized = FakeTransport(TransportResponse(200, endpoint, "application/json", b"{" + b"x" * 20))
    assert result_for(collect_one(registry, tmp_path, "openai", oversized, limits=limits), "openai")["error_code"] == "response_too_large"

    too_many = FakeTransport(response(endpoint, {"data": ["a", "b", "c"]}))
    spacious = CollectorLimits(response_bytes=1024, max_candidates=2)
    assert result_for(collect_one(registry, tmp_path, "openai", too_many, limits=spacious), "openai")["error_code"] == "too_many_models"

    too_deep = FakeTransport(response(endpoint, {"data": [{"id": "ok", "meta": {"a": {"b": 1}}}]}))
    shallow = CollectorLimits(response_bytes=1024, max_json_depth=3)
    assert result_for(collect_one(registry, tmp_path, "openai", too_deep, limits=shallow), "openai")["error_code"] == "invalid_response"

    too_many_nodes = FakeTransport(response(endpoint, {"data": [{"id": "one", "meta": [1, 2, 3, 4]}]}))
    few_nodes = CollectorLimits(response_bytes=1024, max_json_nodes=5)
    assert result_for(collect_one(registry, tmp_path, "openai", too_many_nodes, limits=few_nodes), "openai")["error_code"] == "invalid_response"


def test_transport_exceptions_and_malicious_echo_never_expose_secret(tmp_path: Path) -> None:
    registry = registry_copy()
    endpoint = provider(registry, "openai")["inference"]["model_discovery"]["endpoint"]
    secret = "super-private-provider-key"
    exploding = FakeTransport(error=RuntimeError(f"request failed with {secret}"))
    report = collect_one(registry, tmp_path, "openai", exploding, secret=secret)
    serialized = json.dumps(report)
    assert result_for(report, "openai")["error_code"] == "internal_collection_error"
    assert secret not in serialized

    echo = FakeTransport(response(endpoint, {"data": [{"id": secret}]}))
    report = collect_one(registry, tmp_path, "openai", echo, secret=secret)
    assert result_for(report, "openai")["error_code"] == "invalid_response"
    assert secret not in json.dumps(report)


def test_missing_or_unsafe_credential_never_calls_transport(tmp_path: Path) -> None:
    registry = registry_copy()
    fake = FakeTransport(error=AssertionError("transport must not be called"))
    report = Collector(registry, {}, transport=fake, clock=lambda: FIXED_TIME).collect()
    assert result_for(report, "openai")["error_code"] == "credential_unavailable"
    assert fake.calls == []

    secret_path = private_secret(tmp_path, "openai.key")
    secret_path.chmod(0o640)
    report = Collector(registry, {"openai": secret_path}, transport=fake, clock=lambda: FIXED_TIME).collect()
    assert result_for(report, "openai")["error_code"] == "credential_unavailable"
    assert fake.calls == []


def test_pagination_is_reported_but_never_followed(tmp_path: Path) -> None:
    registry = registry_copy()
    endpoint = provider(registry, "openai")["inference"]["model_discovery"]["endpoint"]
    fake = FakeTransport(response(endpoint, {"data": [{"id": "model-one"}], "has_more": True, "next": "secret-token"}))
    report = collect_one(registry, tmp_path, "openai", fake)
    assert result_for(report, "openai")["possibly_truncated"] is True
    assert len(fake.calls) == 1
    assert "secret-token" not in json.dumps(report)


def dns_answer(*addresses: str) -> list[tuple[Any, ...]]:
    result: list[tuple[Any, ...]] = []
    for address in addresses:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        sockaddr = (address, 443, 0, 0) if family == socket.AF_INET6 else (address, 443)
        result.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
    return result


@pytest.mark.parametrize(
    ("hostname", "address"),
    [
        ("127-0-0-1.nip.io", "127.0.0.1"),
        ("metadata.attacker.example", "169.254.169.254"),
        ("private.attacker.example", "198.51.100.1"),
        ("decimal.attacker.example", "127.0.0.1"),
        ("nat64.attacker.example", "64:ff9b::127.0.0.1"),
        ("mapped.attacker.example", "::ffff:127.0.0.1"),
        ("six-to-four.attacker.example", "2002:7f00:1::"),
        ("multicast.attacker.example", "ff0e::1"),
    ],
)
def test_direct_transport_rejects_dns_aliases_to_non_global_addresses_before_connect(
    hostname: str,
    address: str,
) -> None:
    connection_attempted = False

    def connection_factory(*_args: Any) -> Any:
        nonlocal connection_attempted
        connection_attempted = True
        raise AssertionError("connection must not be attempted")

    transport = UrlLibTransport(
        resolver=lambda *_args: dns_answer(address),
        connection_factory=connection_factory,
    )
    with pytest.raises(DiscoveryFailure) as caught:
        transport.get(
            f"https://{hostname}/models",
            {"Authorization": "Bearer must-not-leave-process"},
            timeout_seconds=1,
            max_response_bytes=1024,
        )
    assert caught.value.code == "non_global_endpoint"
    assert connection_attempted is False


def test_direct_transport_rejects_mixed_public_private_dns_answer() -> None:
    transport = UrlLibTransport(
        resolver=lambda *_args: dns_answer("8.8.8.8", "127.0.0.1"),
        connection_factory=lambda *_args: (_ for _ in ()).throw(
            AssertionError("connection must not be attempted")
        ),
    )
    with pytest.raises(DiscoveryFailure) as caught:
        transport.get(
            "https://models.attacker.example/models",
            {"Authorization": "Bearer must-not-leave-process"},
            timeout_seconds=1,
            max_response_bytes=1024,
        )
    assert caught.value.code == "non_global_endpoint"


def test_dns_resolution_obeys_the_total_deadline() -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocked_resolver(*_args: Any) -> list[tuple[Any, ...]]:
        entered.set()
        release.wait(1)
        return dns_answer("8.8.8.8")

    transport = UrlLibTransport(resolver=blocked_resolver)
    started = time.monotonic()
    try:
        with pytest.raises(DiscoveryFailure) as caught:
            transport.get(
                "https://models.example/models",
                {"Authorization": "Bearer test-only"},
                timeout_seconds=0.02,
                max_response_bytes=1024,
            )
        assert entered.wait(0.1)
        assert caught.value.code == "provider_timeout"
        assert time.monotonic() - started < 0.25
    finally:
        release.set()


def test_slow_drip_response_obeys_one_monotonic_deadline() -> None:
    class FakeClock:
        value = 0.0

        def __call__(self) -> float:
            return self.value

    class SlowConnection:
        def __init__(self, clock: FakeClock) -> None:
            self.clock = clock
            self.response = bytearray(
                b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                b'Content-Length: 11\r\n\r\n{"data":[]}'
            )
            self.sent = bytearray()
            self.closed = False

        def send(self, payload: Any) -> int:
            self.sent.extend(bytes(payload))
            return len(payload)

        def recv(self, _maximum: int) -> bytes:
            self.clock.value += 0.4
            if not self.response:
                return b""
            value = bytes(self.response[:1])
            del self.response[:1]
            return value

        def close(self) -> None:
            self.closed = True

    clock = FakeClock()
    connection = SlowConnection(clock)
    pinned: list[tuple[str, tuple[Any, ...]]] = []

    def connection_factory(
        hostname: str,
        address: tuple[int, int, int, tuple[Any, ...]],
        _deadline: float,
        _clock: Any,
    ) -> SlowConnection:
        pinned.append((hostname, address[3]))
        return connection

    transport = UrlLibTransport(
        resolver=lambda *_args: dns_answer("8.8.8.8"),
        clock=clock,
        connection_factory=connection_factory,
    )
    with pytest.raises(DiscoveryFailure) as caught:
        transport.get(
            "https://models.example/models",
            {"Authorization": "Bearer test-only"},
            timeout_seconds=1,
            max_response_bytes=1024,
        )
    assert caught.value.code == "provider_timeout"
    assert pinned == [("models.example", ("8.8.8.8", 443))]
    assert connection.closed is True
    assert b"GET /models HTTP/1.1\r\n" in connection.sent


@pytest.mark.parametrize(
    "wire_response",
    [
        (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n"
            b"Content-Length: 11\r\n\r\n{\"data\":[]}"
        ),
        (
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n\r\nb\r\n{\"data\":[]}\r\n0\r\n\r\n"
        ),
    ],
)
def test_direct_transport_reads_bounded_http11_without_redirect_or_proxy(
    wire_response: bytes,
) -> None:
    class StaticConnection:
        def __init__(self) -> None:
            self.response = wire_response
            self.sent = b""

        def send(self, payload: Any) -> int:
            self.sent += bytes(payload)
            return len(payload)

        def recv(self, _maximum: int) -> bytes:
            response, self.response = self.response, b""
            return response

        def close(self) -> None:
            return None

    connection = StaticConnection()
    transport = UrlLibTransport(
        resolver=lambda *_args: dns_answer("8.8.8.8"),
        connection_factory=lambda *_args: connection,
    )
    result = transport.get(
        "https://models.example/models",
        {"Authorization": "Bearer test-only"},
        timeout_seconds=1,
        max_response_bytes=1024,
    )
    assert result.status == 200
    assert result.final_url == "https://models.example/models"
    assert result.body == b'{"data":[]}'
    assert b"Host: models.example\r\n" in connection.sent
    assert b"Connection: close\r\n" in connection.sent


def test_pinned_tls_uses_literal_address_but_original_hostname_for_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: dict[str, Any] = {}

    class FakeRawSocket:
        def setblocking(self, value: bool) -> None:
            events["blocking"] = value

        def connect_ex(self, sockaddr: tuple[Any, ...]) -> int:
            events["sockaddr"] = sockaddr
            return 0

        def close(self) -> None:
            events["raw_closed"] = True

    class FakeTLSConnection:
        def do_handshake(self) -> None:
            events["handshake"] = True

        def selected_alpn_protocol(self) -> str:
            return "http/1.1"

        def close(self) -> None:
            events["tls_closed"] = True

    tls_connection = FakeTLSConnection()

    class FakeContext:
        def set_alpn_protocols(self, protocols: list[str]) -> None:
            events["alpn"] = protocols

        def wrap_socket(
            self,
            raw: FakeRawSocket,
            *,
            server_hostname: str,
            do_handshake_on_connect: bool,
        ) -> FakeTLSConnection:
            events["wrapped_raw"] = raw
            events["server_hostname"] = server_hostname
            events["automatic_handshake"] = do_handshake_on_connect
            return tls_connection

    raw = FakeRawSocket()
    monkeypatch.setattr(collector_module.socket, "socket", lambda *_args: raw)
    monkeypatch.setattr(collector_module, "_tls_context", lambda: FakeContext())
    result = collector_module._open_pinned_tls(
        "models.example",
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, ("8.8.8.8", 443)),
        deadline=10,
        clock=lambda: 0,
    )
    assert result is tls_connection
    assert events == {
        "blocking": False,
        "sockaddr": ("8.8.8.8", 443),
        "wrapped_raw": raw,
        "server_hostname": "models.example",
        "automatic_handshake": False,
        "handshake": True,
    }


def test_tls_context_ignores_environment_trust_and_keylog_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attacker_ca = tmp_path / "attacker.pem"
    attacker_ca.write_text("not a certificate\n", encoding="ascii")
    keylog = tmp_path / "tls.keys"
    monkeypatch.setenv("SSL_CERT_FILE", str(attacker_ca))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path))
    monkeypatch.setenv("SSLKEYLOGFILE", str(keylog))

    context = collector_module._tls_context()

    assert context.verify_mode == collector_module.ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= collector_module.ssl.TLSVersion.TLSv1_2
    assert context.keylog_filename is None
    assert keylog.exists() is False


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://models.example/a path",
        "https://models.example/a\\path",
        "https://models.example/a\x7fpath",
        "https://models.example/modèles",
    ],
)
def test_static_endpoint_rejects_unsafe_http_request_targets(endpoint: str) -> None:
    with pytest.raises(DiscoveryFailure) as caught:
        collector_module._static_https_endpoint(endpoint)
    assert caught.value.code == "non_static_endpoint"


def test_cli_has_no_secret_argument_and_emits_only_safe_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--registry", str(REGISTRY_PATH), "--credential-map", str(tmp_path / "missing")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "invalid_credential_map" in captured.err
    parser_actions = __import__("ops_provider_discovery.cli", fromlist=["_parser"])._parser()._actions
    option_strings = {option for action in parser_actions for option in action.option_strings}
    assert "--api-key" not in option_strings
    assert "--credential" not in option_strings
    assert "--secret" not in option_strings
    assert "--token" not in option_strings


def test_credential_map_and_key_size_limits(tmp_path: Path) -> None:
    registry = registry_copy()
    limits = CollectorLimits(credential_map_bytes=20, credential_bytes=8)
    map_path = private_json(tmp_path, "map.json", {"schema_version": 1, "credentials": {}})
    with pytest.raises(DiscoveryFailure) as caught:
        load_credential_map(map_path, registry, limits)
    assert caught.value.code == "invalid_credential_map"

    large_key = private_secret(tmp_path, "openai.key", "123456789")
    fake = FakeTransport(error=AssertionError("transport must not be called"))
    report = Collector(registry, {"openai": large_key}, transport=fake, limits=limits, clock=lambda: FIXED_TIME).collect()
    assert result_for(report, "openai")["error_code"] == "credential_unavailable"
    assert fake.calls == []


def test_limits_cannot_be_raised_above_hard_caps(tmp_path: Path) -> None:
    registry = registry_copy()
    with pytest.raises(DiscoveryFailure) as caught:
        Collector(
            registry,
            {"openai": private_secret(tmp_path, "openai.key")},
            transport=FakeTransport(error=AssertionError("transport must not be called")),
            limits=CollectorLimits(response_bytes=1024 * 1024 + 1),
        )
    assert caught.value.code == "invalid_limits"


def test_multiline_credential_is_rejected_before_transport(tmp_path: Path) -> None:
    registry = registry_copy()
    credential = tmp_path / "openai.key"
    credential.write_text("first-line\nsecond-line\n", encoding="utf-8")
    credential.chmod(0o600)
    fake = FakeTransport(error=AssertionError("transport must not be called"))
    report = Collector(registry, {"openai": credential}, transport=fake, clock=lambda: FIXED_TIME).collect()
    assert result_for(report, "openai")["error_code"] == "credential_unavailable"
    assert fake.calls == []
