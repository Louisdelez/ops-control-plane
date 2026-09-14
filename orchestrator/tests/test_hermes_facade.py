from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime as RealDateTime
import http.client
import io
import json
from pathlib import Path
import socket
import sqlite3
import threading
import time
from typing import Callable, Iterator
import urllib.request

import pytest

import ops_orchestrator.hermes_facade as facade_module
from ops_orchestrator.budget import BudgetLedger, estimated_cost_microusd
from ops_orchestrator.database import Database
from ops_orchestrator.errors import ConfigurationError, ValidationError
from ops_orchestrator.hermes_facade import (
    CLIENT_MODEL_ALIAS,
    DEFAULT_BIND_PORT,
    HEALTH_PATH,
    DirectUpstreamTransport,
    FacadeResponse,
    HermesFacade,
    HermesFacadeHandler,
    HermesFacadeLimits,
    MultiProviderHermesFacade,
    QWEN37_FLASH_MODEL,
    UpstreamFailure,
    UpstreamResponse,
    build_loopback_server,
    qwen37_flash_prices,
)
from ops_orchestrator.models import parse_route_request
from ops_orchestrator.providers import conservative_input_tokens
from ops_orchestrator.service import OrchestratorService

from conftest import FakeProvider, make_config


CLIENT_TOKEN = "hermes-facade-client-token-123456"
PROVIDER_TOKEN = "qwen-provider-token-123456789"
PROVIDER_BASE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


def _private_file(path: Path, value: str) -> Path:
    # Mirrors systemd/OpenBao resolvers, which publish a conventional final LF.
    path.write_text(value + "\n", encoding="ascii")
    path.chmod(0o600)
    return path


class FakeTransport:
    def __init__(
        self,
        body: bytes = b"",
        *,
        content_type: str = "application/json",
        status: int = 200,
        before_yield: Callable[[], None] | None = None,
        error: BaseException | None = None,
    ):
        self.body = body
        self.content_type = content_type
        self.status = status
        self.before_yield = before_yield
        self.error = error
        self.calls: list[dict[str, object]] = []
        self.closed = False

    @contextmanager
    def open(self, *, url, headers, body, timeout) -> Iterator[UpstreamResponse]:
        self.calls.append(
            {"url": url, "headers": dict(headers), "body": body, "timeout": timeout}
        )
        if self.before_yield is not None:
            self.before_yield()
        if self.error is not None:
            raise self.error
        try:
            yield UpstreamResponse(
                status=self.status,
                headers={"Content-Type": self.content_type},
                body=io.BytesIO(self.body),
            )
        finally:
            self.closed = True


def _rows(database: Database):
    with database.connect_readonly() as connection:
        return connection.execute(
            "SELECT * FROM usage_reservations ORDER BY created_at"
        ).fetchall()


def _request(*, stream: bool = False) -> bytes:
    return json.dumps(
        {
            "model": CLIENT_MODEL_ALIAS,
            "messages": [
                {"role": "system", "content": "Coordinate safely."},
                {"role": "user", "content": "Inspect the bounded alert."},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "route_model_task",
                        "description": "Ask the budgeted orchestrator.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": "auto",
            "stream": stream,
            "n": 1,
            "max_tokens": 999_999,
            "enable_thinking": True,
        }
    ).encode()


def _pinned_hermes_custom_profile_request(*, reasoning_effort: object = "none") -> bytes:
    """Mirror the wire kwargs emitted by Hermes 0.21's pinned CustomProfile."""

    return json.dumps(
        {
            "model": CLIENT_MODEL_ALIAS,
            "messages": [
                {"role": "system", "content": "Coordinate safely."},
                {"role": "user", "content": "Inspect the bounded alert."},
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "route_model_task",
                        "description": "Ask the budgeted orchestrator.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            # The pinned custom provider advertises this default.  The facade
            # remains the authoritative, lower output cap.
            "max_tokens": 65_536,
            "reasoning_effort": reasoning_effort,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
    ).encode()


def _facade(tmp_path: Path, transport: FakeTransport):
    config = make_config(tmp_path, ["qwen-utility-api"])
    provider = replace(
        config.provider("qwen-utility-api"),
        model=QWEN37_FLASH_MODEL,
        model_env=None,
    )
    client_file = _private_file(tmp_path / "hermes-token", CLIENT_TOKEN)
    provider_file = _private_file(tmp_path / "qwen-token", PROVIDER_TOKEN)
    database = Database(config.database_path)
    database.initialize()
    limits = replace(
        HermesFacadeLimits.from_app_config(config),
        max_output_tokens=512,
        max_response_bytes=262_144,
    )
    service = HermesFacade(
        provider=provider,
        ledger=BudgetLedger.from_config(database, config),
        client_token_path=client_file,
        environment={
            "QWEN_API_BASE_URL": "https://attacker.invalid/v1",
            "QWEN_API_KEY_FILE": str(provider_file),
        },
        limits=limits,
        transport=transport,
        identity_factory=lambda: ("hermes-route", "hermes-mission"),
    )
    return service, database


def _auth() -> str:
    return f"Bearer {CLIENT_TOKEN}"


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true before timeout")


def _assert_peer_closes(client):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            data = client.recv(4096)
        except (ConnectionResetError, BrokenPipeError):
            return
        except socket.timeout:
            continue
        if not data:
            return
    raise AssertionError("server did not close the client socket")


def _completion(*, usage: dict[str, int] | None = None) -> bytes:
    document: dict[str, object] = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": QWEN37_FLASH_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_route_1",
                            "type": "function",
                            "function": {
                                "name": "route_model_task",
                                "arguments": "{\"task_type\":\"ANALYZE\"}",
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    if usage is not None:
        document["usage"] = usage
    return json.dumps(document).encode()


@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        (1, (28_000, 110_000)),
        (32_000, (28_000, 110_000)),
        (32_001, (83_000, 330_000)),
        (256_000, (83_000, 330_000)),
        (256_001, (165_000, 660_000)),
        (1_000_000, (165_000, 660_000)),
    ],
)
def test_qwen37_flash_prices_are_selected_by_prompt_tier(tokens, expected):
    assert qwen37_flash_prices(tokens) == expected


@pytest.mark.parametrize("tokens", [0, -1, True, 1_000_001])
def test_qwen37_flash_prices_reject_out_of_contract_usage(tokens):
    with pytest.raises(ValueError):
        qwen37_flash_prices(tokens)


def test_non_streaming_request_is_reserved_before_egress_and_forced_safe(tmp_path):
    database_holder: list[Database] = []

    def assert_reserved() -> None:
        rows = _rows(database_holder[0])
        assert len(rows) == 1
        assert rows[0]["status"] == "reserved"

    transport = FakeTransport(
        _completion(usage={"prompt_tokens": 100, "completion_tokens": 20}),
        before_yield=assert_reserved,
    )
    service, database = _facade(tmp_path, transport)
    database_holder.append(database)

    response = service.dispatch(_auth(), _request())

    assert response.status == 200
    assert isinstance(response.body, bytes)
    result = json.loads(response.body)
    assert result["model"] == CLIENT_MODEL_ALIAS
    assert result["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "route_model_task"
    assert len(transport.calls) == 1
    call = transport.calls[0]
    outbound = json.loads(call["body"])
    assert call["url"] == PROVIDER_BASE + "/chat/completions"
    assert call["headers"]["Authorization"] == f"Bearer {PROVIDER_TOKEN}"
    assert outbound["model"] == QWEN37_FLASH_MODEL
    assert outbound["enable_thinking"] is False
    assert outbound["n"] == 1
    assert outbound["max_tokens"] == 512
    assert "max_completion_tokens" not in outbound
    row = _rows(database)[0]
    assert row["status"] == "completed"
    assert row["actual_input_tokens"] == 100
    assert row["actual_output_tokens"] == 20


def test_deepseek_compatible_provider_uses_its_own_dialect_and_prices(tmp_path):
    config = make_config(tmp_path, ["deepseek-ops-api"])
    provider = config.provider("deepseek-ops-api")
    token_file = _private_file(tmp_path / "hermes-token", CLIENT_TOKEN)
    provider_file = _private_file(tmp_path / "deepseek-token", "D" * 48)
    database = Database(config.database_path)
    database.initialize()
    transport = FakeTransport(
        json.dumps(
            {
                "id": "deepseek-test",
                "model": provider.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }
        ).encode()
    )
    service = HermesFacade(
        provider=provider,
        ledger=BudgetLedger.from_config(database, config),
        client_token_path=token_file,
        environment={
            "OPS_ORCHESTRATOR_ENABLE_DEEPSEEK": "1",
            "DEEPSEEK_API_KEY_FILE": str(provider_file),
        },
        limits=replace(HermesFacadeLimits.from_app_config(config), max_output_tokens=512),
        transport=transport,
    )

    response = service.dispatch(_auth(), _request())

    assert response.status == 200
    outbound = json.loads(transport.calls[0]["body"])
    assert outbound["model"] == provider.model
    assert outbound["thinking"] == {"type": "disabled"}
    assert "enable_thinking" not in outbound
    row = _rows(database)[0]
    assert row["provider_id"] == "deepseek-ops-api"
    assert row["status"] == "completed"


def test_pool_falls_back_only_when_cheapest_budget_rejects_before_egress(tmp_path):
    config = make_config(tmp_path, ["qwen-utility-api", "deepseek-ops-api"])
    token_file = _private_file(tmp_path / "hermes-token", CLIENT_TOKEN)
    qwen_file = _private_file(tmp_path / "qwen-token", "Q" * 48)
    deepseek_file = _private_file(tmp_path / "deepseek-token", "D" * 48)
    database = Database(config.database_path)
    database.initialize()
    ledger = BudgetLedger.from_config(database, config)
    environment = {
        "OPS_ORCHESTRATOR_ENABLE_QWEN": "1",
        "OPS_ORCHESTRATOR_ENABLE_DEEPSEEK": "1",
        "QWEN_API_BASE_URL": "https://attacker.invalid/v1",
        "QWEN_API_KEY_FILE": str(qwen_file),
        "DEEPSEEK_API_KEY_FILE": str(deepseek_file),
    }
    qwen_provider = config.provider("qwen-utility-api")
    qwen_provider = replace(
        qwen_provider,
        budget=replace(qwen_provider.budget, daily_calls=0),
    )
    deepseek_provider = config.provider("deepseek-ops-api")
    qwen_transport = FakeTransport(error=AssertionError("Qwen egress is forbidden"))
    deepseek_transport = FakeTransport(
        json.dumps(
            {
                "id": "fallback-test",
                "model": deepseek_provider.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            }
        ).encode()
    )
    limits = replace(HermesFacadeLimits.from_app_config(config), max_output_tokens=512)
    qwen = HermesFacade(
        provider=qwen_provider,
        ledger=ledger,
        client_token_path=token_file,
        environment=environment,
        limits=limits,
        transport=qwen_transport,
    )
    deepseek = HermesFacade(
        provider=deepseek_provider,
        ledger=ledger,
        client_token_path=token_file,
        environment=environment,
        limits=limits,
        transport=deepseek_transport,
    )
    pool = MultiProviderHermesFacade(
        (deepseek, qwen),
        client_token_path=token_file,
    )

    response = pool.dispatch(_auth(), _request())

    assert response.status == 200
    assert qwen_transport.calls == []
    assert len(deepseek_transport.calls) == 1
    rows = _rows(database)
    assert len(rows) == 1
    assert rows[0]["provider_id"] == "deepseek-ops-api"


@pytest.mark.parametrize(
    "reasoning_effort",
    ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
)
def test_hermes_reasoning_effort_is_bounded_and_never_forwarded(
    tmp_path, reasoning_effort
):
    usage = {
        "id": "chatcmpl-hermes",
        "model": QWEN37_FLASH_MODEL,
        "choices": [],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }
    transport = FakeTransport(
        b"data: "
        + json.dumps(usage).encode()
        + b"\n\ndata: [DONE]\n\n",
        content_type="text/event-stream",
    )
    service, database = _facade(tmp_path, transport)

    response = service.dispatch(
        _auth(),
        _pinned_hermes_custom_profile_request(reasoning_effort=reasoning_effort),
    )

    assert response.status == 200
    assert not isinstance(response.body, bytes)
    assert b"".join(response.body).endswith(b"data: [DONE]\n\n")
    assert len(transport.calls) == 1
    outbound = json.loads(transport.calls[0]["body"])
    assert "reasoning_effort" not in outbound
    assert outbound["enable_thinking"] is False
    assert outbound["max_tokens"] == 512
    assert outbound["stream"] is True
    assert outbound["stream_options"] == {"include_usage": True}
    assert _rows(database)[0]["status"] == "completed"


@pytest.mark.parametrize(
    "reasoning_effort",
    [None, True, 1, "", "ultra", "HIGH", [], {}],
)
def test_unknown_hermes_reasoning_effort_fails_before_budget_or_egress(
    tmp_path, reasoning_effort
):
    transport = FakeTransport(
        _completion(usage={"prompt_tokens": 100, "completion_tokens": 20})
    )
    service, database = _facade(tmp_path, transport)

    response = service.dispatch(
        _auth(),
        _pinned_hermes_custom_profile_request(reasoning_effort=reasoning_effort),
    )

    assert response.status == 400
    assert json.loads(response.body)["error"]["code"] == "invalid_request"
    assert transport.calls == []
    assert _rows(database) == []


def test_missing_usage_is_passed_through_but_keeps_conservative_reservation(tmp_path):
    transport = FakeTransport(_completion())
    service, database = _facade(tmp_path, transport)

    response = service.dispatch(_auth(), _request())

    assert response.status == 200
    assert json.loads(response.body)["model"] == CLIENT_MODEL_ALIAS
    row = _rows(database)[0]
    assert row["status"] == "uncertain"
    assert row["error_code"] == "usage_missing"
    assert row["reserved_input_tokens"] > 0
    assert row["reserved_output_tokens"] == 512


def test_usage_above_reserved_output_fails_closed_and_is_uncertain(tmp_path):
    transport = FakeTransport(
        _completion(usage={"prompt_tokens": 100, "completion_tokens": 513})
    )
    service, database = _facade(tmp_path, transport)

    response = service.dispatch(_auth(), _request())

    assert response.status == 502
    row = _rows(database)[0]
    assert row["status"] == "uncertain"
    assert row["error_code"] == "usage_exceeded_reservation"
    assert row["reserved_output_tokens"] == 513


def test_transport_failure_after_reservation_is_uncertain(tmp_path):
    transport = FakeTransport(error=OSError("offline"))
    service, database = _facade(tmp_path, transport)

    response = service.dispatch(_auth(), _request())

    assert response.status == 502
    assert _rows(database)[0]["status"] == "uncertain"


@pytest.mark.parametrize('status', [402, 429, 503])
def test_provider_http_refusal_is_not_retried_or_disclosed(tmp_path, monkeypatch, status):
    import urllib.error
    calls = []
    class Refusal:
        def open(self, request, **kwargs):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, status, 'synthetic refusal', {},
                                         io.BytesIO(b'private upstream details'))
    monkeypatch.setattr(facade_module, '_DIRECT_OPENER', Refusal())
    service, database = _facade(tmp_path, DirectUpstreamTransport())
    pool = MultiProviderHermesFacade((service,), client_token_path=service.client_token_path)
    response = pool.dispatch(_auth(), _request())
    assert response.status == 502
    assert len(calls) == 1
    assert b'private upstream details' not in response.body
    assert _rows(database)[0]['status'] == 'uncertain'


def test_unauthorized_or_invalid_requests_never_reserve_or_egress(tmp_path):
    transport = FakeTransport(_completion(usage={"prompt_tokens": 1, "completion_tokens": 1}))
    service, database = _facade(tmp_path, transport)

    unauthorized = service.dispatch("Bearer wrong-token-that-is-long-enough", _request())
    invalid = service.dispatch(
        _auth(),
        json.dumps(
            {"model": CLIENT_MODEL_ALIAS, "messages": [{"role": "user", "content": "x"}], "n": 2}
        ).encode(),
    )
    unknown = service.dispatch(
        _auth(),
        json.dumps(
            {
                "model": CLIENT_MODEL_ALIAS,
                "messages": [{"role": "user", "content": "x"}],
                "base_url": "https://attacker.invalid/v1",
            }
        ).encode(),
    )

    assert unauthorized.status == 401
    assert invalid.status == 400
    assert unknown.status == 400
    assert transport.calls == []
    assert _rows(database) == []


def test_streaming_preserves_tool_call_deltas_and_completes_from_final_usage(tmp_path):
    chunks = [
        {
            "id": "chatcmpl-stream",
            "model": QWEN37_FLASH_MODEL,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_route_1",
                                "type": "function",
                                "function": {"name": "route_model_task", "arguments": "{\"task"},
                            }
                        ],
                    },
                }
            ],
        },
        {
            "id": "chatcmpl-stream",
            "model": QWEN37_FLASH_MODEL,
            "choices": [],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        },
    ]
    wire = b"".join(
        b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks
    ) + b"data: [DONE]\n\n"
    transport = FakeTransport(wire, content_type="text/event-stream")
    service, database = _facade(tmp_path, transport)

    response = service.dispatch(_auth(), _request(stream=True))
    assert response.status == 200
    assert not isinstance(response.body, bytes)
    output = b"".join(response.body)

    assert b'"model":"qwen-coordinator"' in output
    assert b'"tool_calls"' in output
    assert output.endswith(b"data: [DONE]\n\n")
    outbound = json.loads(transport.calls[0]["body"])
    assert outbound["stream_options"] == {"include_usage": True}
    assert outbound["enable_thinking"] is False
    assert transport.closed is True
    assert _rows(database)[0]["status"] == "completed"


def test_sse_accepts_comments_metadata_and_multiline_data_but_normalizes_them(tmp_path):
    content = {
        "id": "chatcmpl-stream",
        "model": QWEN37_FLASH_MODEL,
        "choices": [{"index": 0, "delta": {"content": "bounded"}}],
    }
    usage = {
        "id": "chatcmpl-stream",
        "model": QWEN37_FLASH_MODEL,
        "choices": [],
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
    }

    def event(document: dict[str, object]) -> bytes:
        lines = json.dumps(document, indent=2).encode().splitlines()
        return (
            b": qwen keep-alive\n"
            b"event: message\n"
            b"id: qwen-event-1\n"
            b"retry: 1000\n"
            + b"".join(b"data: " + line + b"\n" for line in lines)
            + b"\n"
        )

    wire = event(content) + event(usage) + b"event: done\ndata: [DONE]\n\n"
    transport = FakeTransport(wire, content_type="text/event-stream")
    service, database = _facade(tmp_path, transport)

    response = service.dispatch(_auth(), _request(stream=True))
    assert not isinstance(response.body, bytes)
    output = b"".join(response.body)

    assert b"event:" not in output
    assert b"id:" not in output
    assert b"retry:" not in output
    assert b'"content":"bounded"' in output
    assert output.endswith(b"data: [DONE]\n\n")
    assert _rows(database)[0]["status"] == "completed"


@pytest.mark.parametrize(
    "choice",
    [
        {"index": 1, "delta": {"content": "wrong choice"}},
        {"index": 0, "delta": {"role": "user", "content": "wrong role"}},
        {
            "index": 0,
            "delta": {
                "tool_calls": [
                    {
                        "index": 0,
                        "type": "function",
                        "function": {"arguments": {"not": "a string"}},
                    }
                ]
            },
        },
    ],
)
def test_stream_rejects_invalid_choice_delta_and_tool_call_shapes(tmp_path, choice):
    chunk = {
        "id": "chatcmpl-stream",
        "model": QWEN37_FLASH_MODEL,
        "choices": [choice],
    }
    wire = b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n"
    transport = FakeTransport(wire, content_type="text/event-stream")
    service, database = _facade(tmp_path, transport)
    response = service.dispatch(_auth(), _request(stream=True))

    with pytest.raises(UpstreamFailure):
        b"".join(response.body)
    assert transport.closed is True
    assert _rows(database)[0]["status"] == "uncertain"


def test_closing_partial_stream_marks_reservation_uncertain(tmp_path):
    chunk = {
        "id": "chatcmpl-stream",
        "model": QWEN37_FLASH_MODEL,
        "choices": [{"index": 0, "delta": {"content": "partial"}}],
    }
    transport = FakeTransport(
        b"data: " + json.dumps(chunk).encode() + b"\n\n",
        content_type="text/event-stream",
    )
    service, database = _facade(tmp_path, transport)

    response = service.dispatch(_auth(), _request(stream=True))
    assert not isinstance(response.body, bytes)
    assert next(response.body).startswith(b"data: ")
    response.body.close()

    row = _rows(database)[0]
    assert row["status"] == "uncertain"
    assert row["error_code"] == "client_disconnect"
    assert transport.closed is True


def test_disconnect_during_end_headers_closes_and_accounts_stream(tmp_path):
    chunk = {
        "id": "chatcmpl-stream",
        "model": QWEN37_FLASH_MODEL,
        "choices": [{"index": 0, "delta": {"content": "partial"}}],
    }
    transport = FakeTransport(
        b"data: " + json.dumps(chunk).encode() + b"\n\n",
        content_type="text/event-stream",
    )
    service, database = _facade(tmp_path, transport)
    response = service.dispatch(_auth(), _request(stream=True))

    handler = object.__new__(HermesFacadeHandler)
    handler.send_response = lambda _status: None
    handler.send_header = lambda _name, _value: None

    def disconnected() -> None:
        raise BrokenPipeError("client disconnected before headers completed")

    handler.end_headers = disconnected
    handler.wfile = io.BytesIO()
    handler.close_connection = False
    handler._send(response)

    row = _rows(database)[0]
    assert row["status"] == "uncertain"
    assert row["error_code"] == "client_disconnect"
    assert transport.closed is True
    assert handler.close_connection is True


def test_stream_without_done_raises_and_marks_reservation_uncertain(tmp_path):
    chunk = {
        "id": "chatcmpl-stream",
        "model": QWEN37_FLASH_MODEL,
        "choices": [{"index": 0, "delta": {"content": "partial"}}],
    }
    transport = FakeTransport(
        b"data: " + json.dumps(chunk).encode() + b"\n\n",
        content_type="text/event-stream",
    )
    service, database = _facade(tmp_path, transport)
    response = service.dispatch(_auth(), _request(stream=True))

    with pytest.raises(UpstreamFailure):
        b"".join(response.body)
    assert _rows(database)[0]["status"] == "uncertain"


def test_malformed_stream_json_is_an_upstream_failure_and_is_uncertain(tmp_path):
    transport = FakeTransport(b"data: {not-json}\n\n", content_type="text/event-stream")
    service, database = _facade(tmp_path, transport)
    response = service.dispatch(_auth(), _request(stream=True))

    with pytest.raises(UpstreamFailure, match="invalid_upstream_json"):
        b"".join(response.body)
    assert _rows(database)[0]["status"] == "uncertain"


def test_default_budget_identity_is_stable_for_utc_day_and_ignores_user_rotation(
    tmp_path, monkeypatch
):
    class FrozenDateTime:
        @classmethod
        def now(cls, tz):
            return RealDateTime(2026, 9, 5, 23, 59, tzinfo=tz)

    monkeypatch.setattr(facade_module, "datetime", FrozenDateTime)
    transport = FakeTransport(_completion())
    service, database = _facade(tmp_path, transport)
    service.identity_factory = service._identity

    first_body = json.loads(_request())
    first_body["user"] = "hermes-session-one"
    first = service.dispatch(_auth(), json.dumps(first_body).encode())
    assert first.status == 200
    first_row = _rows(database)[0]
    assert first_row["mission_id"] == "hermes/coordinator/utc-day/2026-09-05"

    # Retain exactly the already uncertain first reservation.  A client-picked
    # OpenAI `user` value must not create a fresh mission budget bucket.
    service.provider = replace(
        service.provider,
        budget=replace(
            service.provider.budget,
            mission_cost_microusd=first_row["reserved_cost_microusd"],
        ),
    )
    second_body = dict(first_body)
    second_body["user"] = "rotated-client-controlled-value"
    second = service.dispatch(_auth(), json.dumps(second_body).encode())

    assert second.status == 429
    assert json.loads(second.body)["error"]["code"] == "budget_exceeded"
    assert len(transport.calls) == 1
    assert len(_rows(database)) == 1


def test_ordinary_route_cannot_collide_with_or_exhaust_hermes_mission_bucket(
    tmp_path, payload, monkeypatch
):
    class FrozenDateTime:
        @classmethod
        def now(cls, tz):
            return RealDateTime(2026, 9, 5, 12, 0, tzinfo=tz)

    monkeypatch.setattr(facade_module, "datetime", FrozenDateTime)
    internal_mission_id = "hermes/coordinator/utc-day/2026-09-05"
    rejected_payload = dict(payload, mission_id=internal_mission_id)
    config = make_config(tmp_path, ["qwen-utility-api"])
    with pytest.raises(ValidationError, match="safe identifiers"):
        parse_route_request(rejected_payload, config.limits)

    # Saturate the formerly injectable bucket with one conservative failed
    # ordinary route.  Its large, valid context keeps this cap above the much
    # smaller facade request reservation.
    ordinary_payload = dict(payload)
    ordinary_payload["mission_id"] = "hermes-coordinator-utc-day-2026-09-05"
    ordinary_payload["context"] = dict(
        payload["context"],
        mission="🚨" * 6_000,
        current_state="",
        relevant_memories=[],
        tool_results=[],
    )
    ordinary_request = parse_route_request(ordinary_payload, config.limits)
    provider = config.provider("qwen-utility-api")
    ordinary_input = conservative_input_tokens(ordinary_request, provider.kind)
    ordinary_cost = estimated_cost_microusd(
        ordinary_input,
        config.limits.max_output_tokens,
        *provider.price_microusd_per_million({}, input_tokens=ordinary_input),
    )
    assert ordinary_cost < 1_000_000
    config = make_config(
        tmp_path,
        ["qwen-utility-api"],
        budgets={
            "qwen-utility-api": {
                "mission_cost_usd": f"0.{ordinary_cost:06d}",
            }
        },
    )
    ordinary_provider = FakeProvider(
        config.provider("qwen-utility-api"),
        [RuntimeError("retain the full reservation")],
    )
    orchestrator = OrchestratorService(
        config,
        provider_overrides={"qwen-utility-api": ordinary_provider},
    )
    ordinary_decision = orchestrator.route(ordinary_payload)
    assert ordinary_decision["disposition"] == "human_escalation_required"

    transport = FakeTransport(
        _completion(usage={"prompt_tokens": 100, "completion_tokens": 20})
    )
    facade, database = _facade(tmp_path, transport)
    facade.provider = replace(
        facade.provider,
        budget=replace(
            facade.provider.budget,
            mission_cost_microusd=ordinary_cost,
        ),
    )
    facade.identity_factory = lambda: (
        "would-collide-route",
        "hermes-coordinator-utc-day-2026-09-05",
    )
    collision = facade.dispatch(_auth(), _request())
    assert collision.status == 429
    assert transport.calls == []

    facade.identity_factory = facade._identity

    response = facade.dispatch(_auth(), _request())

    assert response.status == 200
    rows = _rows(database)
    assert [row["mission_id"] for row in rows] == [
        "hermes-coordinator-utc-day-2026-09-05",
        internal_mission_id,
    ]
    assert rows[0]["status"] == "uncertain"
    assert rows[0]["reserved_cost_microusd"] == ordinary_cost
    assert rows[1]["status"] == "completed"
    assert len(transport.calls) == 1


def test_sqlite_reservation_failure_blocks_egress_and_returns_service_error(
    tmp_path, monkeypatch
):
    transport = FakeTransport(
        _completion(usage={"prompt_tokens": 100, "completion_tokens": 20})
    )
    service, database = _facade(tmp_path, transport)

    def fail_reserve(**_kwargs):
        raise sqlite3.OperationalError("ledger unavailable")

    monkeypatch.setattr(service.ledger, "reserve", fail_reserve)
    response = service.dispatch(_auth(), _request())

    assert response.status == 503
    assert json.loads(response.body)["error"]["code"] == "budget_ledger_unavailable"
    assert transport.calls == []
    assert _rows(database) == []


def test_sqlite_completion_failure_is_contained_and_retains_reservation(
    tmp_path, monkeypatch
):
    transport = FakeTransport(
        _completion(usage={"prompt_tokens": 100, "completion_tokens": 20})
    )
    service, database = _facade(tmp_path, transport)

    def fail_complete(*_args, **_kwargs):
        raise sqlite3.OperationalError("ledger unavailable")

    monkeypatch.setattr(service.ledger, "complete", fail_complete)
    response = service.dispatch(_auth(), _request())

    assert response.status == 503
    assert json.loads(response.body)["error"]["code"] == "budget_ledger_unavailable"
    row = _rows(database)[0]
    assert row["status"] == "uncertain"
    assert row["error_code"] == "budget_ledger_failure"
    assert transport.closed is True


def test_sqlite_uncertain_failure_after_egress_is_contained_as_reserved(
    tmp_path, monkeypatch
):
    transport = FakeTransport(error=OSError("provider disconnected"))
    service, database = _facade(tmp_path, transport)

    def fail_uncertain(*_args, **_kwargs):
        raise sqlite3.OperationalError("ledger unavailable")

    monkeypatch.setattr(service.ledger, "mark_uncertain", fail_uncertain)
    response = service.dispatch(_auth(), _request())

    assert response.status == 503
    assert json.loads(response.body)["error"]["code"] == "budget_ledger_unavailable"
    assert len(transport.calls) == 1
    assert _rows(database)[0]["status"] == "reserved"


def test_sqlite_stream_completion_failure_closes_without_done_and_is_conservative(
    tmp_path, monkeypatch
):
    chunks = [
        {
            "id": "chatcmpl-stream",
            "model": QWEN37_FLASH_MODEL,
            "choices": [{"index": 0, "delta": {"content": "partial"}}],
        },
        {
            "id": "chatcmpl-stream",
            "model": QWEN37_FLASH_MODEL,
            "choices": [],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        },
    ]
    wire = b"".join(
        b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks
    ) + b"data: [DONE]\n\n"
    transport = FakeTransport(wire, content_type="text/event-stream")
    service, database = _facade(tmp_path, transport)

    def fail_complete(*_args, **_kwargs):
        raise sqlite3.OperationalError("ledger unavailable")

    monkeypatch.setattr(service.ledger, "complete", fail_complete)
    response = service.dispatch(_auth(), _request(stream=True))

    with pytest.raises(UpstreamFailure, match="budget_ledger_unavailable"):
        b"".join(response.body)
    row = _rows(database)[0]
    assert row["status"] == "uncertain"
    assert row["error_code"] == "budget_ledger_failure"
    assert transport.closed is True


@pytest.mark.parametrize("terminator", [b"", b"\n", b"\r\n"])
def test_private_tokens_accept_only_conventional_single_line_terminators(tmp_path, terminator):
    path = tmp_path / "token"
    path.write_bytes(CLIENT_TOKEN.encode() + terminator)
    path.chmod(0o600)
    assert facade_module._private_token(path) == CLIENT_TOKEN

    path.write_bytes(CLIENT_TOKEN.encode() + b"\nsecond-line")
    with pytest.raises(ConfigurationError, match="malformed"):
        facade_module._private_token(path)


def test_health_checks_files_and_config_without_network_or_budget(tmp_path):
    transport = FakeTransport()
    service, database = _facade(tmp_path, transport)

    response = service.health()

    assert response.status == 200
    assert json.loads(response.body) == {
        "status": "ok",
        "model": CLIENT_MODEL_ALIAS,
        "provider_network_probe": False,
    }
    assert HEALTH_PATH == "/healthz"
    assert DEFAULT_BIND_PORT == 8643
    assert transport.calls == []
    assert _rows(database) == []


def test_health_fails_when_private_client_token_becomes_group_readable(tmp_path):
    transport = FakeTransport()
    service, database = _facade(tmp_path, transport)
    service.client_token_path.chmod(0o640)

    response = service.health()

    assert response.status == 503
    assert json.loads(response.body)["status"] == "unavailable"
    assert transport.calls == []
    assert _rows(database) == []


def test_partial_facade_request_expires_while_health_remains_available(tmp_path):
    transport = FakeTransport()
    service, _ = _facade(tmp_path, transport)
    server = build_loopback_server(
        "127.0.0.1",
        0,
        service,
        client_timeout_seconds=0.5,
        max_request_workers=3,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    slow_client = socket.create_connection(server.server_address, timeout=2)
    slow_client.settimeout(2)
    try:
        slow_client.sendall(
            b"POST /v1/chat/completions HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\n"
            + f"Authorization: {_auth()}\r\n".encode("ascii")
            + b"Content-Length: 4096\r\n"
            b"Connection: close\r\n\r\n"
            b"{"
        )
        _wait_until(lambda: server.active_request_count == 1)

        connection = http.client.HTTPConnection(*server.server_address, timeout=2)
        connection.request("GET", HEALTH_PATH, headers={"Connection": "close"})
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()

        assert response.status == 200
        assert body["status"] == "ok"
        assert transport.calls == []
        _assert_peer_closes(slow_client)
        _wait_until(lambda: server.active_request_count == 0)
    finally:
        slow_client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_socket_deadline_preserves_openai_sse_streaming(tmp_path):
    chunks = [
        {
            "id": "chatcmpl-stream",
            "model": QWEN37_FLASH_MODEL,
            "choices": [{"index": 0, "delta": {"content": "bounded"}}],
        },
        {
            "id": "chatcmpl-stream",
            "model": QWEN37_FLASH_MODEL,
            "choices": [],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        },
    ]
    wire = b"".join(
        b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks
    ) + b"data: [DONE]\n\n"
    transport = FakeTransport(wire, content_type="text/event-stream")
    service, database = _facade(tmp_path, transport)
    server = build_loopback_server(
        "127.0.0.1",
        0,
        service,
        client_timeout_seconds=0.5,
        max_request_workers=2,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)
    try:
        request_body = _request(stream=True)
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=request_body,
            headers={
                "Authorization": _auth(),
                "Content-Type": "application/json",
                "Content-Length": str(len(request_body)),
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        output = response.read()

        assert response.status == 200
        assert response.getheader("Content-Type") == "text/event-stream; charset=utf-8"
        assert b'"model":"qwen-coordinator"' in output
        assert output.endswith(b"data: [DONE]\n\n")
        assert _rows(database)[0]["status"] == "completed"
        _wait_until(lambda: server.active_request_count == 0)
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_from_app_config_has_a_one_mib_request_envelope_for_64k_context(tmp_path):
    transport = FakeTransport()
    service, _ = _facade(tmp_path, transport)
    assert service.limits.max_request_bytes >= 1_048_576

    large_request = json.dumps(
        {
            "model": CLIENT_MODEL_ALIAS,
            "messages": [{"role": "user", "content": "a" * 300_000}],
            "max_tokens": 64,
        }
    ).encode()
    prepared = service.prepare(large_request)
    assert prepared.estimated_input_tokens >= len(prepared.body)
    assert prepared.maximum_output_tokens == 64


def test_provider_model_and_region_must_match_hard_coded_pricing(tmp_path):
    transport = FakeTransport()
    service, database = _facade(tmp_path, transport)
    service.provider = replace(service.provider, model="some-other-qwen-model")

    response = service.dispatch(_auth(), _request())

    assert response.status == 503
    assert transport.calls == []
    assert _rows(database) == []


@pytest.mark.parametrize(
    "drift",
    [
        {"chat_family": "deepseek"},
        {"chat_dialect": "deepseek"},
        {"thinking_mode": "enabled"},
        {"thinking_mode": None},
    ],
)
def test_facade_provider_dialect_must_match_reviewed_qwen_contract(
    tmp_path, drift
):
    transport = FakeTransport()
    service, database = _facade(tmp_path, transport)
    service.provider = replace(service.provider, **drift)

    health = service.health()
    response = service.dispatch(_auth(), _request())

    assert health.status == 503
    assert response.status == 503
    assert transport.calls == []
    assert _rows(database) == []


def test_provider_price_tiers_must_exactly_match_facade_contract(tmp_path):
    transport = FakeTransport()
    service, database = _facade(tmp_path, transport)
    first_tier = service.provider.price_tiers[0]
    service.provider = replace(
        service.provider,
        price_tiers=(
            replace(
                first_tier,
                output_price_microusd_per_million=(
                    first_tier.output_price_microusd_per_million - 1
                ),
            ),
            *service.provider.price_tiers[1:],
        ),
    )

    health = service.health()
    response = service.dispatch(_auth(), _request())

    assert health.status == 503
    assert response.status == 503
    assert transport.calls == []
    assert _rows(database) == []


def test_facade_construction_rejects_price_tier_drift(tmp_path):
    transport = FakeTransport()
    service, _ = _facade(tmp_path, transport)
    first_tier = service.provider.price_tiers[0]
    drifted_provider = replace(
        service.provider,
        price_tiers=(
            replace(
                first_tier,
                input_price_microusd_per_million=(
                    first_tier.input_price_microusd_per_million + 1
                ),
            ),
            *service.provider.price_tiers[1:],
        ),
    )

    with pytest.raises(ConfigurationError, match="price tiers"):
        HermesFacade(
            provider=drifted_provider,
            ledger=service.ledger,
            client_token_path=service.client_token_path,
            environment=service.environment,
            limits=service.limits,
            transport=transport,
        )


def test_default_transport_has_no_environment_proxy_and_rejects_redirects():
    assert isinstance(DirectUpstreamTransport(), DirectUpstreamTransport)
    proxy_handlers = [
        handler
        for handler in facade_module._DIRECT_OPENER.handlers
        if isinstance(handler, urllib.request.ProxyHandler)
    ]
    redirect_handlers = [
        handler
        for handler in facade_module._DIRECT_OPENER.handlers
        if isinstance(handler, facade_module._RejectRedirects)
    ]
    # Supplying ProxyHandler({}) also suppresses build_opener's environment-
    # aware default; urllib omits the empty handler from the final handler list.
    assert proxy_handlers == []
    assert len(redirect_handlers) == 1
    assert redirect_handlers[0].redirect_request(None, None, 302, "", {}, "https://elsewhere") is None


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "192.0.2.1", "::1"])
def test_server_rejects_every_non_ipv4_loopback_bind_before_opening_socket(host):
    with pytest.raises(ValueError):
        build_loopback_server(host, 8643, None)


def test_cli_builds_shared_ledger_facade_with_documented_defaults(tmp_path, monkeypatch):
    make_config(tmp_path, ["qwen-utility-api"])
    token_file = _private_file(tmp_path / "facade-token", CLIENT_TOKEN)
    observed: dict[str, object] = {}

    def fake_serve(facade, *, host, port):
        observed.update(facade=facade, host=host, port=port)

    monkeypatch.setattr(facade_module, "serve_loopback", fake_serve)
    result = facade_module.main(
        ["--config", str(tmp_path / "config.json"), "--token-file", str(token_file)]
    )

    assert result == 0
    assert isinstance(observed["facade"], MultiProviderHermesFacade)
    assert [
        item.provider.provider_id for item in observed["facade"].facades
    ] == [
        "qwen-utility-api",
        "alibaba-deepseek-ops-api",
        "deepseek-ops-api",
    ]
    assert observed["host"] == "127.0.0.1"
    assert observed["port"] == 8643


def test_pool_accepts_http_ingress_and_enforces_body_limit(tmp_path):
    service, database = _facade(tmp_path, FakeTransport())
    pool = MultiProviderHermesFacade([service], client_token_path=service.client_token_path)
    server = build_loopback_server('127.0.0.1', 0, pool)
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    connection = http.client.HTTPConnection(*server.server_address, timeout=2)
    try:
        connection.request('POST','/v1/chat/completions',body=b'{}',headers={'Authorization':_auth(),'Content-Type':'application/json'})
        response=connection.getresponse()
        assert response.status==400
        assert json.loads(response.read())['error']['code']=='model_not_allowed'
    finally:
        connection.close();server.shutdown();server.server_close();thread.join(timeout=2)
