"""Budgeted OpenAI-compatible chat facade for the Hermes coordinator.

The deployment unit injects a dedicated remote ``ProviderConfig``, the shared
``BudgetLedger`` database, an environment containing the provider key-file
setting, and a separate private bearer-token file for Hermes.

Only ``qwen-coordinator`` is accepted from the client.  The real provider model
is selected from the injected configuration, and provider credentials are never
returned to or read from Hermes.  The default transport ignores environment
proxy variables and rejects redirects.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, BinaryIO, Callable, ContextManager, Iterator, Mapping, Protocol, Sequence
import urllib.error
import urllib.request
from urllib.parse import urlparse
import uuid

from .budget import BudgetLedger, Reservation, estimated_cost_microusd
from .bounded_server import (
    DEFAULT_CLIENT_TIMEOUT_SECONDS,
    DEFAULT_MAX_REQUEST_WORKERS,
    BoundedThreadingMixIn,
)
from .config import ALIBABA_SHARED_BASE_URL, AppConfig, ProviderConfig
from .config import load_config
from .database import Database
from .errors import BudgetExceeded, ConfigurationError, ProviderUnavailable


CLIENT_MODEL_ALIAS = "qwen-coordinator"
QWEN37_FLASH_MODEL = "qwen3.7-flash-2026-07-15"
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 8643
HEALTH_PATH = "/healthz"
_JSON_CONTENT_TYPE = "application/json; charset=utf-8"
_SSE_CONTENT_TYPE = "text/event-stream; charset=utf-8"


@dataclass(frozen=True)
class QwenPriceTier:
    """One all-tokens price tier selected by prompt tokens per request."""

    maximum_input_tokens: int
    input_microusd_per_million: int
    output_microusd_per_million: int


# Alibaba Model Studio Global list prices reviewed on 2026-09-05.
# Prices are micro-USD per one million tokens (USD 0.028 -> 28,000 micro-USD).
QWEN37_FLASH_GLOBAL_TIERS = (
    QwenPriceTier(32_000, 28_000, 110_000),
    QwenPriceTier(256_000, 83_000, 330_000),
    QwenPriceTier(1_000_000, 165_000, 660_000),
)


def qwen37_flash_prices(input_tokens: int) -> tuple[int, int]:
    """Return the price pair for a Qwen3.7 Flash Global request."""

    if isinstance(input_tokens, bool) or not isinstance(input_tokens, int) or input_tokens < 1:
        raise ValueError("input token count must be a positive integer")
    for tier in QWEN37_FLASH_GLOBAL_TIERS:
        if input_tokens <= tier.maximum_input_tokens:
            return (tier.input_microusd_per_million, tier.output_microusd_per_million)
    raise ValueError("input token count exceeds the reviewed Qwen3.7 Flash context")


@dataclass(frozen=True)
class HermesFacadeLimits:
    max_request_bytes: int
    max_response_bytes: int
    max_output_tokens: int
    provider_timeout_seconds: float
    max_input_tokens: int = 1_000_000
    max_messages: int = 128
    max_tools: int = 64
    max_json_depth: int = 24
    max_json_nodes: int = 20_000
    protocol_overhead_tokens: int = 4_096

    @classmethod
    def from_app_config(cls, config: AppConfig) -> "HermesFacadeLimits":
        return cls(
            # Hermes advertises a 64k-token context.  The ordinary routing API's
            # much smaller envelope limit is not an appropriate wire-byte cap
            # for that context, especially for escaped or multi-byte JSON.
            max_request_bytes=max(config.limits.max_request_bytes, 1_048_576),
            max_response_bytes=config.limits.max_response_bytes,
            max_output_tokens=config.limits.max_output_tokens,
            provider_timeout_seconds=config.limits.provider_timeout_seconds,
        )

    def validate(self) -> None:
        integer_bounds = (
            (self.max_request_bytes, 1_024, 4_194_304, "max_request_bytes"),
            (self.max_response_bytes, 1_024, 16_777_216, "max_response_bytes"),
            (self.max_output_tokens, 1, 65_536, "max_output_tokens"),
            (self.max_input_tokens, 1, 1_000_000, "max_input_tokens"),
            (self.max_messages, 1, 1_024, "max_messages"),
            (self.max_tools, 0, 256, "max_tools"),
            (self.max_json_depth, 4, 64, "max_json_depth"),
            (self.max_json_nodes, 100, 100_000, "max_json_nodes"),
            (self.protocol_overhead_tokens, 0, 65_536, "protocol_overhead_tokens"),
        )
        for value, minimum, maximum, label in integer_bounds:
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{label} is outside the accepted range")
        if (
            isinstance(self.provider_timeout_seconds, bool)
            or not isinstance(self.provider_timeout_seconds, (int, float))
            or not math.isfinite(float(self.provider_timeout_seconds))
            or not 1 <= float(self.provider_timeout_seconds) <= 300
        ):
            raise ValueError("provider_timeout_seconds is outside the accepted range")


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    headers: Mapping[str, str]
    body: BinaryIO


class UpstreamTransport(Protocol):
    def open(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> ContextManager[UpstreamResponse]: ...


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


_DIRECT_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _RejectRedirects(),
)


class DirectUpstreamTransport:
    """urllib transport with no environment proxy and no redirect following."""

    @contextmanager
    def open(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> Iterator[UpstreamResponse]:
        request = urllib.request.Request(
            url,
            data=body,
            headers=dict(headers),
            method="POST",
        )
        try:
            response = _DIRECT_OPENER.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise UpstreamFailure("upstream_http_error") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise UpstreamFailure("upstream_transport_error") from exc
        try:
            yield UpstreamResponse(
                status=int(response.status),
                headers={key: value for key, value in response.headers.items()},
                body=response,
            )
        finally:
            response.close()


class FacadeValidationError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_request", status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class UpstreamFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedRequest:
    body: bytes
    stream: bool
    estimated_input_tokens: int
    maximum_output_tokens: int
    prices: tuple[int, int]
    provider_url: str
    provider_key: str
    provider_model: str


def _uses_reviewed_qwen_contract(provider: ProviderConfig) -> bool:
    """Return whether this is the exact audited Qwen coordinator slot."""

    return provider.provider_id == "qwen-utility-api"


def _provider_prices(
    provider: ProviderConfig,
    environment: Mapping[str, str],
    input_tokens: int,
) -> tuple[int, int]:
    """Resolve request-tier prices without applying one model's price to another."""

    if _uses_reviewed_qwen_contract(provider):
        base_url = provider.resolved_base_url(environment)
        model = provider.resolved_model(environment)
        _validate_reviewed_provider_contract(provider, base_url, model)
        return qwen37_flash_prices(max(1, input_tokens))
    prices = provider.price_microusd_per_million(
        environment,
        input_tokens=max(1, input_tokens),
    )
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item < 0
        for item in prices
    ):
        raise ConfigurationError("Hermes facade provider prices are invalid")
    return prices


def _normalize_provider_payload(
    value: Mapping[str, Any],
    provider: ProviderConfig,
    *,
    provider_model: str,
    maximum_output: int,
    stream: bool,
) -> dict[str, Any]:
    """Translate one bounded request to a reviewed OpenAI-compatible dialect."""

    outbound = dict(value)
    outbound["model"] = provider_model
    outbound["n"] = 1
    outbound.pop("reasoning_effort", None)
    outbound.pop("max_completion_tokens", None)
    outbound.pop("enable_thinking", None)
    outbound.pop("thinking", None)
    outbound["max_tokens"] = maximum_output

    if provider.chat_dialect == "alibaba":
        if provider.thinking_mode not in {"enabled", "disabled"}:
            raise ConfigurationError("Alibaba facade provider thinking mode is invalid")
        outbound["enable_thinking"] = provider.thinking_mode == "enabled"
    elif provider.chat_dialect == "deepseek":
        if provider.thinking_mode not in {"enabled", "disabled"}:
            raise ConfigurationError("DeepSeek facade provider thinking mode is invalid")
        outbound["thinking"] = {"type": provider.thinking_mode}
    elif provider.chat_dialect not in {"openai", None}:
        raise ConfigurationError("Hermes facade provider dialect is unsupported")

    if stream:
        outbound["stream"] = True
        outbound["stream_options"] = {"include_usage": True}
    else:
        outbound["stream"] = False
        outbound.pop("stream_options", None)
    return outbound


@dataclass(frozen=True)
class FacadeResponse:
    status: int
    content_type: str
    body: bytes | Iterator[bytes]


def _private_token(path: Path) -> str:
    if not path.is_absolute():
        raise ConfigurationError("credential path must be absolute")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ConfigurationError("private credential is unavailable") from exc
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(info.st_mode) & 0o077
            or not 16 <= info.st_size <= 514
        ):
            raise ConfigurationError("private credential metadata is unsafe")
        value = os.read(descriptor, 515)
        if len(value) > 514 or os.read(descriptor, 1):
            raise ConfigurationError("private credential exceeds its bound")
    finally:
        os.close(descriptor)
    try:
        raw_token = value[:-2] if value.endswith(b"\r\n") else value[:-1] if value.endswith(b"\n") else value
        token = raw_token.decode("ascii", errors="strict")
    except UnicodeError as exc:
        raise ConfigurationError("private credential is not ASCII") from exc
    if (
        not 16 <= len(token) <= 512
        or token != token.strip()
        or any(character.isspace() or ord(character) < 0x21 or ord(character) > 0x7E for character in token)
    ):
        raise ConfigurationError("private credential is malformed")
    return token


def _chat_endpoint(base_url: str) -> str:
    return (
        base_url + "/chat/completions"
        if base_url.endswith("/v1")
        else base_url + "/v1/chat/completions"
    )


def _validate_reviewed_price_tiers(provider: ProviderConfig) -> None:
    """Reject config/code price drift before accepting any facade request."""

    configured_tiers = tuple(
        (
            tier.max_input_tokens,
            tier.input_price_microusd_per_million,
            tier.output_price_microusd_per_million,
        )
        for tier in provider.price_tiers
    )
    reviewed_tiers = tuple(
        (
            tier.maximum_input_tokens,
            tier.input_microusd_per_million,
            tier.output_microusd_per_million,
        )
        for tier in QWEN37_FLASH_GLOBAL_TIERS
    )
    if configured_tiers != reviewed_tiers:
        raise ConfigurationError("Hermes facade provider price tiers differ from the reviewed Qwen prices")


def _validate_reviewed_provider_contract(
    provider: ProviderConfig,
    base_url: str,
    model: str,
) -> None:
    """Keep hard-coded prices coupled to the exact reviewed config contract."""

    parsed = urlparse(base_url)
    if (
        base_url != ALIBABA_SHARED_BASE_URL
        or parsed.scheme != "https"
        or parsed.hostname != "dashscope-intl.aliyuncs.com"
        or parsed.path != "/compatible-mode/v1"
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ConfigurationError("Hermes facade provider endpoint is not the exact reviewed shared Qwen endpoint")
    if model != QWEN37_FLASH_MODEL:
        raise ConfigurationError("Hermes facade pricing is valid only for the reviewed Qwen3.7 Flash model")
    if (
        provider.chat_family != "qwen"
        or provider.chat_dialect != "alibaba"
        or provider.thinking_mode != "disabled"
    ):
        raise ConfigurationError(
            "Hermes facade provider dialect differs from the reviewed non-thinking Qwen contract"
        )
    _validate_reviewed_price_tiers(provider)


def _strict_json_loads(data: bytes) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite number {value}")

    try:
        return json.loads(data.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise FacadeValidationError("request body is invalid JSON") from exc


def _validate_json_tree(value: Any, limits: HermesFacadeLimits) -> None:
    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_json_nodes or depth > limits.max_json_depth:
            raise FacadeValidationError("request JSON structure exceeds its bound")
        if item is None or isinstance(item, (str, bool)):
            continue
        if isinstance(item, int):
            if abs(item) > 2**63 - 1:
                raise FacadeValidationError("request integer is outside its bound")
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise FacadeValidationError("request number must be finite")
            continue
        if isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
            continue
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 256:
                    raise FacadeValidationError("request object key is invalid")
                stack.append((child, depth + 1))
            continue
        raise FacadeValidationError("request contains an unsupported JSON value")


_REQUEST_FIELDS = {
    "model",
    "messages",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "stream",
    "stream_options",
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "stop",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "response_format",
    "user",
    "n",
    "enable_thinking",
    "reasoning_effort",
}
_HERMES_REASONING_EFFORTS = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
}
_MESSAGE_FIELDS = {
    "role",
    "content",
    "name",
    "tool_call_id",
    "tool_calls",
    "reasoning_content",
    "reasoning_details",
    "refusal",
}


def _number(value: Any, label: str, minimum: float, maximum: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise FacadeValidationError(f"{label} is outside the accepted range")


def _validate_tool_calls(value: Any) -> None:
    if not isinstance(value, list) or len(value) > 64:
        raise FacadeValidationError("assistant tool_calls is invalid")
    indexes: set[int] = set()
    for call in value:
        if not isinstance(call, dict) or set(call) - {"id", "type", "function", "index"}:
            raise FacadeValidationError("assistant tool call is invalid")
        identifier = call.get("id")
        if (
            call.get("type") != "function"
            or not isinstance(identifier, str)
            or not 1 <= len(identifier) <= 256
        ):
            raise FacadeValidationError("assistant tool call identity is invalid")
        if "index" in call:
            index = call["index"]
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < 64
                or index in indexes
            ):
                raise FacadeValidationError("assistant tool call index is invalid")
            indexes.add(index)
        function = call.get("function")
        if not isinstance(function, dict) or set(function) - {"name", "arguments"}:
            raise FacadeValidationError("assistant tool call function is invalid")
        name = function.get("name")
        arguments = function.get("arguments")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 128
            or not isinstance(arguments, str)
        ):
            raise FacadeValidationError("assistant tool call payload is invalid")


def _validate_messages(messages: Any, limits: HermesFacadeLimits) -> None:
    if not isinstance(messages, list) or not 1 <= len(messages) <= limits.max_messages:
        raise FacadeValidationError("messages must be a non-empty bounded list")
    for message in messages:
        if not isinstance(message, dict) or set(message) - _MESSAGE_FIELDS:
            raise FacadeValidationError("message fields are invalid")
        role = message.get("role")
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise FacadeValidationError("message role is invalid")
        content = message.get("content")
        if content is not None and not isinstance(content, (str, list)):
            raise FacadeValidationError("message content is invalid")
        if "name" in message and (
            not isinstance(message["name"], str) or not 1 <= len(message["name"]) <= 128
        ):
            raise FacadeValidationError("message name is invalid")
        if "tool_call_id" in message and (
            not isinstance(message["tool_call_id"], str)
            or not 1 <= len(message["tool_call_id"]) <= 256
        ):
            raise FacadeValidationError("message tool_call_id is invalid")
        if role != "assistant" and "tool_calls" in message:
            raise FacadeValidationError("tool_calls are only valid on assistant messages")
        if "tool_calls" in message:
            _validate_tool_calls(message["tool_calls"])
        if role == "tool" and not isinstance(message.get("tool_call_id"), str):
            raise FacadeValidationError("tool message requires tool_call_id")


def _validate_tools(tools: Any, limits: HermesFacadeLimits) -> None:
    if not isinstance(tools, list) or len(tools) > limits.max_tools:
        raise FacadeValidationError("tools must be a bounded list")
    for tool in tools:
        if not isinstance(tool, dict) or set(tool) != {"type", "function"} or tool.get("type") != "function":
            raise FacadeValidationError("tool definition is invalid")
        function = tool.get("function")
        if not isinstance(function, dict) or set(function) - {"name", "description", "parameters", "strict"}:
            raise FacadeValidationError("tool function definition is invalid")
        name = function.get("name")
        if not isinstance(name, str) or not 1 <= len(name) <= 128:
            raise FacadeValidationError("tool function name is invalid")
        if "description" in function and not isinstance(function["description"], str):
            raise FacadeValidationError("tool function description is invalid")
        if "parameters" in function and not isinstance(function["parameters"], dict):
            raise FacadeValidationError("tool function parameters are invalid")
        if "strict" in function and not isinstance(function["strict"], bool):
            raise FacadeValidationError("tool function strict flag is invalid")


def _usage(document: Mapping[str, Any]) -> tuple[int, int] | None:
    value = document.get("usage")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise UpstreamFailure("invalid_upstream_usage")
    prompt = value.get("prompt_tokens")
    completion = value.get("completion_tokens")
    if (
        isinstance(prompt, bool)
        or not isinstance(prompt, int)
        or prompt < 0
        or isinstance(completion, bool)
        or not isinstance(completion, int)
        or completion < 0
    ):
        raise UpstreamFailure("invalid_upstream_usage")
    return (prompt, completion)


def _validate_optional_request_shapes(value: Mapping[str, Any], *, stream: bool) -> None:
    if "tool_choice" in value:
        choice = value["tool_choice"]
        named = (
            isinstance(choice, dict)
            and set(choice) == {"type", "function"}
            and choice.get("type") == "function"
            and isinstance(choice.get("function"), dict)
            and set(choice["function"]) == {"name"}
            and isinstance(choice["function"].get("name"), str)
            and 1 <= len(choice["function"]["name"]) <= 128
        )
        if not (isinstance(choice, str) and choice in {"none", "auto", "required"}) and not named:
            raise FacadeValidationError("tool_choice is invalid")
    if "stream_options" in value:
        options = value["stream_options"]
        if (
            not stream
            or not isinstance(options, dict)
            or set(options) - {"include_usage"}
            or not isinstance(options.get("include_usage", False), bool)
        ):
            raise FacadeValidationError("stream_options is invalid")
    if "response_format" in value:
        response_format = value["response_format"]
        if not isinstance(response_format, dict) or not isinstance(response_format.get("type"), str):
            raise FacadeValidationError("response_format is invalid")
        if response_format["type"] not in {"text", "json_object", "json_schema"}:
            raise FacadeValidationError("response_format type is invalid")
        allowed = {"type", "json_schema"} if response_format["type"] == "json_schema" else {"type"}
        if set(response_format) - allowed:
            raise FacadeValidationError("response_format fields are invalid")
        if response_format["type"] == "json_schema" and not isinstance(
            response_format.get("json_schema"), dict
        ):
            raise FacadeValidationError("response_format json_schema is invalid")


def _header(headers: Mapping[str, str], name: str) -> str:
    lowered = name.lower()
    return next((str(value) for key, value in headers.items() if key.lower() == lowered), "")


def _openai_error(status: int, code: str, message: str) -> FacadeResponse:
    body = json.dumps(
        {
            "error": {
                "message": message,
                "type": "invalid_request_error" if status < 500 else "server_error",
                "param": None,
                "code": code,
            }
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return FacadeResponse(status, _JSON_CONTENT_TYPE, body)


def _validate_stream_tool_call_deltas(value: Any) -> None:
    """Validate the fragmented tool-call shape used by OpenAI-compatible SSE."""

    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise UpstreamFailure("invalid_upstream_sse_tool_calls")
    indexes: set[int] = set()
    for call in value:
        if not isinstance(call, dict) or set(call) - {"index", "id", "type", "function"}:
            raise UpstreamFailure("invalid_upstream_sse_tool_call")
        index = call.get("index")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index < 64
            or index in indexes
        ):
            raise UpstreamFailure("invalid_upstream_sse_tool_call_index")
        indexes.add(index)
        if set(call) == {"index"}:
            raise UpstreamFailure("empty_upstream_sse_tool_call")
        if "id" in call and (
            not isinstance(call["id"], str) or not 1 <= len(call["id"]) <= 256
        ):
            raise UpstreamFailure("invalid_upstream_sse_tool_call_id")
        if "type" in call and call["type"] != "function":
            raise UpstreamFailure("invalid_upstream_sse_tool_call_type")
        if "function" in call:
            function = call["function"]
            if (
                not isinstance(function, dict)
                or not function
                or set(function) - {"name", "arguments"}
            ):
                raise UpstreamFailure("invalid_upstream_sse_tool_call_function")
            if "name" in function and (
                not isinstance(function["name"], str)
                or not 1 <= len(function["name"]) <= 128
            ):
                raise UpstreamFailure("invalid_upstream_sse_tool_call_name")
            if "arguments" in function and not isinstance(function["arguments"], str):
                raise UpstreamFailure("invalid_upstream_sse_tool_call_arguments")


def _validate_stream_chunk(document: Mapping[str, Any]) -> None:
    """Validate choices and deltas before exposing a provider SSE event."""

    choices = document.get("choices")
    if not isinstance(choices, list) or len(choices) > 1:
        raise UpstreamFailure("invalid_upstream_sse_choices")
    if not choices:
        if _usage(document) is None:
            raise UpstreamFailure("empty_upstream_sse_event")
        return
    choice = choices[0]
    if not isinstance(choice, dict) or set(choice) - {
        "index",
        "delta",
        "finish_reason",
        "logprobs",
    }:
        raise UpstreamFailure("invalid_upstream_sse_choice")
    index = choice.get("index")
    if isinstance(index, bool) or index != 0:
        raise UpstreamFailure("invalid_upstream_sse_choice_index")
    delta = choice.get("delta")
    if not isinstance(delta, dict) or set(delta) - {
        "role",
        "content",
        "refusal",
        "reasoning_content",
        "tool_calls",
    }:
        raise UpstreamFailure("invalid_upstream_sse_delta")
    if "role" in delta and delta["role"] != "assistant":
        raise UpstreamFailure("invalid_upstream_sse_delta_role")
    for field in ("content", "refusal", "reasoning_content"):
        if field in delta and delta[field] is not None and not isinstance(delta[field], str):
            raise UpstreamFailure("invalid_upstream_sse_delta_content")
    if "tool_calls" in delta:
        _validate_stream_tool_call_deltas(delta["tool_calls"])
    finish_reason = choice.get("finish_reason")
    if finish_reason not in {None, "stop", "length", "tool_calls", "content_filter"}:
        raise UpstreamFailure("invalid_upstream_sse_finish_reason")
    if choice.get("logprobs") is not None and not isinstance(choice["logprobs"], dict):
        raise UpstreamFailure("invalid_upstream_sse_logprobs")


class _AccountedSSEStream:
    """Incrementally validates SSE and finalizes exactly one reservation."""

    def __init__(
        self,
        *,
        manager: ContextManager[UpstreamResponse],
        response: UpstreamResponse,
        ledger: BudgetLedger,
        reservation: Reservation,
        limits: HermesFacadeLimits,
        price_resolver: Callable[[int], tuple[int, int]],
    ):
        self._manager = manager
        self._response = response
        self._ledger = ledger
        self._reservation = reservation
        self._limits = limits
        self._price_resolver = price_resolver
        self._usage: tuple[int, int] | None = None
        self._bytes_read = 0
        self._bytes_emitted = 0
        self._finalized = False
        self._closed = False

    def __iter__(self) -> "_AccountedSSEStream":
        return self

    def _uncertain(self, code: str, usage: tuple[int, int] | None = None) -> bool:
        if self._finalized:
            return True
        prices = None
        if usage is not None:
            try:
                prices = self._price_resolver(max(1, usage[0]))
            except (ConfigurationError, ValueError):
                prices = self._reservation_prices()
        try:
            self._ledger.mark_uncertain(
                self._reservation,
                code,
                input_tokens=usage[0] if usage else None,
                output_tokens=usage[1] if usage else None,
                prices=prices,
            )
        except (sqlite3.Error, RuntimeError):
            # A reservation left in ``reserved`` state remains fully charged.
            # Do not let a secondary accounting failure keep the socket open.
            return False
        self._finalized = True
        return True

    def _reservation_prices(self) -> tuple[int, int]:
        """Derive a conservative pair already covered by the reservation."""

        tokens = max(1, self._reservation.reserved_input_tokens)
        try:
            return self._price_resolver(tokens)
        except (ConfigurationError, ValueError):
            # This is used only while retaining a full reservation; zero prices
            # cannot reduce the already reserved charge in mark_uncertain.
            return (0, 0)

    def _finish(self) -> None:
        if self._usage is None:
            if not self._uncertain("usage_missing"):
                raise UpstreamFailure("budget_ledger_unavailable")
            return
        prompt, completion = self._usage
        if (
            prompt > self._reservation.reserved_input_tokens
            or completion > self._reservation.reserved_output_tokens
        ):
            self._uncertain("usage_exceeded_reservation", self._usage)
            raise UpstreamFailure("upstream usage exceeded reservation")
        try:
            self._ledger.complete(
                self._reservation,
                input_tokens=prompt,
                output_tokens=completion,
                prices=self._price_resolver(max(1, prompt)),
            )
        except (sqlite3.Error, RuntimeError) as exc:
            self._uncertain("budget_ledger_failure", self._usage)
            raise UpstreamFailure("budget_ledger_unavailable") from exc
        self._finalized = True

    def _read_event_data(self) -> bytes:
        """Read one SSE event, accepting standard metadata and comments."""

        data_lines: list[bytes] = []
        while True:
            line = self._response.body.readline(self._limits.max_response_bytes + 1)
            if not line:
                raise UpstreamFailure("upstream_sse_ended_without_done")
            self._bytes_read += len(line)
            if self._bytes_read > self._limits.max_response_bytes:
                raise UpstreamFailure("upstream_sse_exceeds_byte_limit")
            if line.endswith(b"\n"):
                line = line[:-1]
                if line.endswith(b"\r"):
                    line = line[:-1]
            if not line:
                if data_lines:
                    return b"\n".join(data_lines)
                continue
            if line.startswith(b":"):
                # SSE comments are keep-alives and never reach the OpenAI client.
                continue
            field, separator, value = line.partition(b":")
            if separator and value.startswith(b" "):
                value = value[1:]
            if field == b"data":
                data_lines.append(value)
                continue
            if field in {b"event", b"id"}:
                if len(value) > 1_024 or b"\x00" in value:
                    raise UpstreamFailure("invalid_upstream_sse_metadata")
                try:
                    value.decode("utf-8", errors="strict")
                except UnicodeError as exc:
                    raise UpstreamFailure("invalid_upstream_sse_metadata") from exc
                continue
            if field == b"retry":
                if not value.isdigit() or len(value) > 10:
                    raise UpstreamFailure("invalid_upstream_sse_retry")
                continue
            # The SSE specification ignores unknown fields.  They are stripped
            # by this normalizing facade rather than forwarded to Hermes.

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        try:
            while True:
                data = self._read_event_data()
                if data == b"[DONE]":
                    outgoing = b"data: [DONE]\n\n"
                    if self._bytes_emitted + len(outgoing) > self._limits.max_response_bytes:
                        raise UpstreamFailure("normalized_upstream_sse_exceeds_byte_limit")
                    self._finish()
                    self.close()
                    self._bytes_emitted += len(outgoing)
                    return outgoing
                document = _strict_upstream_json(data, self._limits)
                _validate_stream_chunk(document)
                observed = _usage(document)
                if observed is not None:
                    if self._usage is not None and observed != self._usage:
                        raise UpstreamFailure("inconsistent_upstream_usage")
                    self._usage = observed
                document["model"] = CLIENT_MODEL_ALIAS
                outgoing = b"data: " + json.dumps(
                    document, separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8") + b"\n\n"
                if self._bytes_emitted + len(outgoing) > self._limits.max_response_bytes:
                    raise UpstreamFailure("normalized_upstream_sse_exceeds_byte_limit")
                self._bytes_emitted += len(outgoing)
                return outgoing
        except StopIteration:
            raise
        except BaseException as exc:
            self._uncertain(type(exc).__name__, self._usage)
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if not self._finalized:
                self._uncertain("client_disconnect", self._usage)
        finally:
            try:
                self._manager.__exit__(None, None, None)
            except Exception:
                # The reservation is already conservative; close failures must
                # not cause a second state transition or leak an exception.
                pass


def _strict_upstream_json(data: bytes, limits: HermesFacadeLimits) -> dict[str, Any]:
    try:
        value = _strict_json_loads(data)
        _validate_json_tree(value, limits)
    except FacadeValidationError as exc:
        raise UpstreamFailure("invalid_upstream_json") from exc
    if not isinstance(value, dict):
        raise UpstreamFailure("upstream response root is invalid")
    return value


class HermesFacade:
    """Validate, budget and proxy one constrained Hermes chat completion."""

    def __init__(
        self,
        *,
        provider: ProviderConfig,
        ledger: BudgetLedger,
        client_token_path: str | Path,
        environment: Mapping[str, str] | None = None,
        limits: HermesFacadeLimits,
        transport: UpstreamTransport | None = None,
        identity_factory: Callable[[], tuple[str, str]] | None = None,
    ):
        limits.validate()
        if _uses_reviewed_qwen_contract(provider):
            _validate_reviewed_price_tiers(provider)
        self.provider = provider
        self.ledger = ledger
        self.client_token_path = Path(client_token_path)
        self.environment = os.environ if environment is None else environment
        self.limits = limits
        self.transport = transport or DirectUpstreamTransport()
        self.identity_factory = identity_factory or self._identity

    @classmethod
    def from_app_config(
        cls,
        config: AppConfig,
        *,
        provider_id: str,
        ledger: BudgetLedger,
        client_token_path: str | Path,
        environment: Mapping[str, str] | None = None,
        transport: UpstreamTransport | None = None,
        identity_factory: Callable[[], tuple[str, str]] | None = None,
    ) -> "HermesFacade":
        return cls(
            provider=config.provider(provider_id),
            ledger=ledger,
            client_token_path=client_token_path,
            environment=environment,
            limits=HermesFacadeLimits.from_app_config(config),
            transport=transport,
            identity_factory=identity_factory,
        )

    @staticmethod
    def _identity() -> tuple[str, str]:
        """Return a unique route and a restart-stable UTC-day budget window.

        OpenAI's optional ``user`` field is client-controlled and therefore is
        not a trustworthy budget identity: a caller could rotate it on every
        turn.  All authenticated Hermes calls instead share one conservative
        mission-cost window per UTC day.  The durable ledger makes the window
        stable across facade restarts while daily/monthly provider caps remain
        an independent outer bound.  The slash namespaces this internal
        identity outside the API/MCP ``mission_id`` grammar, so another local
        caller cannot consume Hermes' mission bucket with an ordinary route.
        """

        identifier = uuid.uuid4().hex
        utc_day = datetime.now(timezone.utc).date().isoformat()
        return (f"hermes-chat-{identifier}", f"hermes/coordinator/utc-day/{utc_day}")

    def authorized(self, authorization: str | None) -> bool:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            return False
        candidate = authorization[7:]
        if not candidate or len(candidate) > 512 or candidate != candidate.strip():
            return False
        return hmac.compare_digest(candidate, _private_token(self.client_token_path))

    def health(self) -> FacadeResponse:
        """Check local configuration and private files without provider egress."""

        try:
            _private_token(self.client_token_path)
            if self.provider.kind != "openai_chat" or self.provider.location != "remote":
                raise ConfigurationError("facade provider type is invalid")
            if not self.provider.activated(self.environment):
                raise ConfigurationError("facade provider is unavailable")
            base_url = self.provider.resolved_base_url(self.environment)
            model = self.provider.resolved_model(self.environment)
            if _uses_reviewed_qwen_contract(self.provider):
                _validate_reviewed_provider_contract(self.provider, base_url, model)
            _provider_prices(self.provider, self.environment, 1)
            provider_key_path = self.provider.api_key_path(self.environment)
            if provider_key_path is None:
                raise ConfigurationError("provider credential path is unavailable")
            _private_token(provider_key_path)
        except (ConfigurationError, ProviderUnavailable):
            status = 503
            state = "unavailable"
        else:
            status = 200
            state = "ok"
        body = json.dumps(
            {
                "status": state,
                "model": CLIENT_MODEL_ALIAS,
                "provider_network_probe": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return FacadeResponse(status, _JSON_CONTENT_TYPE, body)

    def dispatch(self, authorization: str | None, raw_body: bytes) -> FacadeResponse:
        try:
            if not self.authorized(authorization):
                return _openai_error(401, "invalid_api_key", "invalid bearer credential")
        except ConfigurationError:
            return _openai_error(503, "facade_unavailable", "facade authentication is unavailable")
        try:
            prepared = self.prepare(raw_body)
        except FacadeValidationError as exc:
            return _openai_error(exc.status, exc.code, str(exc))
        except (ConfigurationError, ProviderUnavailable):
            return _openai_error(503, "provider_unavailable", "configured provider is unavailable")
        return self._invoke(prepared)

    def prepare(self, raw_body: bytes) -> PreparedRequest:
        if not isinstance(raw_body, bytes) or not 2 <= len(raw_body) <= self.limits.max_request_bytes:
            raise FacadeValidationError("request body size is outside the accepted range")
        value = _strict_json_loads(raw_body)
        _validate_json_tree(value, self.limits)
        if not isinstance(value, dict):
            raise FacadeValidationError("request root must be an object")
        unexpected = sorted(set(value) - _REQUEST_FIELDS)
        if unexpected:
            raise FacadeValidationError("unknown request fields: " + ", ".join(unexpected))
        if value.get("model") != CLIENT_MODEL_ALIAS:
            raise FacadeValidationError("only the coordinator model alias is accepted", code="model_not_allowed")
        _validate_messages(value.get("messages"), self.limits)
        if "tools" in value:
            _validate_tools(value["tools"], self.limits)
        if "n" in value and (isinstance(value["n"], bool) or value["n"] != 1):
            raise FacadeValidationError("n must equal 1")
        stream = value.get("stream", False)
        if not isinstance(stream, bool):
            raise FacadeValidationError("stream must be boolean")
        if "parallel_tool_calls" in value and not isinstance(value["parallel_tool_calls"], bool):
            raise FacadeValidationError("parallel_tool_calls must be boolean")
        if "temperature" in value:
            _number(value["temperature"], "temperature", 0, 2)
        if "top_p" in value:
            _number(value["top_p"], "top_p", 0, 1)
        for label in ("presence_penalty", "frequency_penalty"):
            if label in value:
                _number(value[label], label, -2, 2)
        if "seed" in value and (isinstance(value["seed"], bool) or not isinstance(value["seed"], int)):
            raise FacadeValidationError("seed must be an integer")
        if "user" in value and (not isinstance(value["user"], str) or len(value["user"]) > 128):
            raise FacadeValidationError("user is invalid")
        if "stop" in value:
            stop = value["stop"]
            if not isinstance(stop, str) and not (
                isinstance(stop, list)
                and len(stop) <= 4
                and all(isinstance(item, str) for item in stop)
            ):
                raise FacadeValidationError("stop is invalid")
        if "enable_thinking" in value and not isinstance(value["enable_thinking"], bool):
            raise FacadeValidationError("enable_thinking must be boolean")
        if "reasoning_effort" in value and (
            not isinstance(value["reasoning_effort"], str)
            or value["reasoning_effort"] not in _HERMES_REASONING_EFFORTS
        ):
            raise FacadeValidationError("reasoning_effort is invalid")
        _validate_optional_request_shapes(value, stream=stream)

        requested_limits = [value[key] for key in ("max_tokens", "max_completion_tokens") if key in value]
        if len(requested_limits) > 1:
            raise FacadeValidationError("only one output-token limit may be set")
        requested_output = requested_limits[0] if requested_limits else self.limits.max_output_tokens
        if isinstance(requested_output, bool) or not isinstance(requested_output, int) or requested_output < 1:
            raise FacadeValidationError("output-token limit must be a positive integer")
        maximum_output = min(requested_output, self.limits.max_output_tokens)

        if self.provider.kind != "openai_chat" or self.provider.location != "remote":
            raise ConfigurationError("Hermes facade provider must be a remote OpenAI chat provider")
        if not self.provider.activated(self.environment):
            raise ProviderUnavailable("provider is not activated")
        base_url = self.provider.resolved_base_url(self.environment)
        provider_model = self.provider.resolved_model(self.environment)
        if _uses_reviewed_qwen_contract(self.provider):
            _validate_reviewed_provider_contract(self.provider, base_url, provider_model)
        provider_key_path = self.provider.api_key_path(self.environment)
        if provider_key_path is None:
            raise ConfigurationError("remote provider key path is unavailable")
        provider_key = _private_token(provider_key_path)

        outbound = _normalize_provider_payload(
            value,
            self.provider,
            provider_model=provider_model,
            maximum_output=maximum_output,
            stream=stream,
        )
        body = json.dumps(outbound, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(body) > self.limits.max_request_bytes:
            raise FacadeValidationError("normalized request exceeds its byte bound")
        estimated_input = len(body) + self.limits.protocol_overhead_tokens
        if estimated_input > self.limits.max_input_tokens:
            raise FacadeValidationError("request exceeds the reviewed input-token bound")
        prices = _provider_prices(self.provider, self.environment, estimated_input)
        return PreparedRequest(
            body=body,
            stream=stream,
            estimated_input_tokens=estimated_input,
            maximum_output_tokens=maximum_output,
            prices=prices,
            provider_url=_chat_endpoint(base_url),
            provider_key=provider_key,
            provider_model=provider_model,
        )

    def _reserve(self, prepared: PreparedRequest) -> Reservation:
        route_id, mission_id = self.identity_factory()
        return self.ledger.reserve(
            route_id=route_id,
            mission_id=mission_id,
            provider=self.provider,
            model=prepared.provider_model,
            reason="Hermes coordinator chat completion",
            input_tokens=prepared.estimated_input_tokens,
            max_output_tokens=prepared.maximum_output_tokens,
            requested_max_cost_microusd=None,
            prices=prepared.prices,
        )

    def _mark_uncertain(
        self,
        reservation: Reservation,
        code: str,
        *,
        prices: tuple[int, int],
        usage: tuple[int, int] | None = None,
    ) -> bool:
        """Retain the reservation, containing an unavailable SQLite ledger."""

        try:
            self.ledger.mark_uncertain(
                reservation,
                code,
                input_tokens=usage[0] if usage else None,
                output_tokens=usage[1] if usage else None,
                prices=prices,
            )
        except (sqlite3.Error, RuntimeError):
            # ``reserved`` is the fail-closed state and remains fully charged.
            return False
        return True

    @staticmethod
    def _close_upstream(manager: ContextManager[UpstreamResponse]) -> None:
        try:
            manager.__exit__(None, None, None)
        except Exception:
            pass

    def _invoke(self, prepared: PreparedRequest) -> FacadeResponse:
        try:
            reservation = self._reserve(prepared)
        except BudgetExceeded:
            return _openai_error(429, "budget_exceeded", "coordinator budget is exhausted")
        except (sqlite3.Error, RuntimeError):
            return _openai_error(503, "budget_ledger_unavailable", "coordinator budget ledger is unavailable")
        headers = {
            "Authorization": f"Bearer {prepared.provider_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if prepared.stream else "application/json",
            "User-Agent": "ops-orchestrator-hermes-facade/1",
        }
        manager = self.transport.open(
            url=prepared.provider_url,
            headers=headers,
            body=prepared.body,
            timeout=float(self.limits.provider_timeout_seconds),
        )
        try:
            response = manager.__enter__()
        except BaseException as exc:
            accounting_ok = self._mark_uncertain(
                reservation,
                type(exc).__name__,
                prices=prepared.prices,
            )
            if not accounting_ok:
                return _openai_error(
                    503,
                    "budget_ledger_unavailable",
                    "coordinator budget ledger is unavailable",
                )
            return _openai_error(502, "upstream_unavailable", "model provider is unavailable")
        if response.status < 200 or response.status >= 300:
            accounting_ok = self._mark_uncertain(
                reservation,
                "upstream_http_status",
                prices=prepared.prices,
            )
            self._close_upstream(manager)
            if not accounting_ok:
                return _openai_error(
                    503,
                    "budget_ledger_unavailable",
                    "coordinator budget ledger is unavailable",
                )
            return _openai_error(502, "upstream_error", "model provider rejected the request")
        expected_type = "text/event-stream" if prepared.stream else "application/json"
        if _header(response.headers, "Content-Type").split(";", 1)[0].strip().lower() != expected_type:
            accounting_ok = self._mark_uncertain(
                reservation,
                "upstream_content_type",
                prices=prepared.prices,
            )
            self._close_upstream(manager)
            if not accounting_ok:
                return _openai_error(
                    503,
                    "budget_ledger_unavailable",
                    "coordinator budget ledger is unavailable",
                )
            return _openai_error(502, "upstream_error", "model provider response type is invalid")
        if prepared.stream:
            return FacadeResponse(
                200,
                _SSE_CONTENT_TYPE,
                _AccountedSSEStream(
                    manager=manager,
                    response=response,
                    ledger=self.ledger,
                    reservation=reservation,
                    limits=self.limits,
                    price_resolver=lambda input_tokens: _provider_prices(
                        self.provider,
                        self.environment,
                        input_tokens,
                    ),
                ),
            )
        try:
            body = response.body.read(self.limits.max_response_bytes + 1)
            if len(body) > self.limits.max_response_bytes:
                raise UpstreamFailure("upstream response exceeds byte limit")
            document = _strict_upstream_json(body, self.limits)
            choices = document.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise UpstreamFailure("upstream choices are invalid")
            message = choices[0].get("message")
            if not isinstance(message, dict):
                raise UpstreamFailure("upstream message is invalid")
            if "tool_calls" in message:
                _validate_tool_calls(message["tool_calls"])
            if message.get("content") is None and not message.get("tool_calls"):
                raise UpstreamFailure("upstream message is empty")
            usage = _usage(document)
            document["model"] = CLIENT_MODEL_ALIAS
            normalized = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            if len(normalized) > self.limits.max_response_bytes:
                raise UpstreamFailure("normalized upstream response exceeds byte limit")
            if usage is None:
                if not self._mark_uncertain(
                    reservation,
                    "usage_missing",
                    prices=prepared.prices,
                ):
                    return _openai_error(
                        503,
                        "budget_ledger_unavailable",
                        "coordinator budget ledger is unavailable",
                    )
            elif usage[0] > reservation.reserved_input_tokens or usage[1] > reservation.reserved_output_tokens:
                try:
                    actual_prices = _provider_prices(
                        self.provider,
                        self.environment,
                        max(1, usage[0]),
                    )
                except (ConfigurationError, ValueError):
                    actual_prices = prepared.prices
                accounting_ok = self._mark_uncertain(
                    reservation,
                    "usage_exceeded_reservation",
                    usage=usage,
                    prices=actual_prices,
                )
                if not accounting_ok:
                    return _openai_error(
                        503,
                        "budget_ledger_unavailable",
                        "coordinator budget ledger is unavailable",
                    )
                return _openai_error(502, "upstream_usage_invalid", "model provider usage exceeded its reservation")
            else:
                try:
                    self.ledger.complete(
                        reservation,
                        input_tokens=usage[0],
                        output_tokens=usage[1],
                        prices=_provider_prices(
                            self.provider,
                            self.environment,
                            max(1, usage[0]),
                        ),
                    )
                except (sqlite3.Error, RuntimeError):
                    self._mark_uncertain(
                        reservation,
                        "budget_ledger_failure",
                        usage=usage,
                        prices=prepared.prices,
                    )
                    return _openai_error(
                        503,
                        "budget_ledger_unavailable",
                        "coordinator budget ledger is unavailable",
                    )
            return FacadeResponse(200, _JSON_CONTENT_TYPE, normalized)
        except BaseException as exc:
            accounting_ok = self._mark_uncertain(
                reservation,
                type(exc).__name__,
                prices=prepared.prices,
            )
            if isinstance(exc, sqlite3.Error) or not accounting_ok:
                return _openai_error(
                    503,
                    "budget_ledger_unavailable",
                    "coordinator budget ledger is unavailable",
                )
            return _openai_error(502, "upstream_error", "model provider response is invalid")
        finally:
            self._close_upstream(manager)


class FacadeApplication(Protocol):
    def authorized(self, authorization: str | None) -> bool: ...

    def health(self) -> FacadeResponse: ...

    def dispatch(self, authorization: str | None, raw_body: bytes) -> FacadeResponse: ...


class MultiProviderHermesFacade:
    """Cost-first coordinator pool with fail-safe provider diversity.

    Every candidate is fully validated and priced before any network egress.
    The pool may move to the next provider only when the first provider's hard
    budget rejected the reservation (HTTP 429), which is known to happen before
    egress.  It deliberately never retries transport or upstream failures: the
    first provider might have received the request, so retrying could duplicate
    cost, tool calls, or side effects.
    """

    def __init__(
        self,
        facades: Sequence[HermesFacade],
        *,
        client_token_path: str | Path,
    ):
        if not 1 <= len(facades) <= 32:
            raise ConfigurationError("Hermes facade pool must contain 1..32 providers")
        identifiers = [item.provider.provider_id for item in facades]
        if len(set(identifiers)) != len(identifiers):
            raise ConfigurationError("Hermes facade pool contains duplicate providers")
        self.facades = tuple(facades)
        # HTTP ingress must enforce the strictest request limit before reading.
        self.limits = min((item.limits for item in facades), key=lambda limits: limits.max_request_bytes)
        self.client_token_path = Path(client_token_path)

    @classmethod
    def from_app_config(
        cls,
        config: AppConfig,
        *,
        provider_ids: Sequence[str],
        ledger: BudgetLedger,
        client_token_path: str | Path,
        environment: Mapping[str, str] | None = None,
        transport_factory: Callable[[ProviderConfig], UpstreamTransport] | None = None,
        identity_factory: Callable[[], tuple[str, str]] | None = None,
    ) -> "MultiProviderHermesFacade":
        facades = []
        for provider_id in provider_ids:
            provider = config.provider(provider_id)
            transport = (
                transport_factory(provider)
                if transport_factory is not None
                else DirectUpstreamTransport()
            )
            facades.append(
                HermesFacade(
                    provider=provider,
                    ledger=ledger,
                    client_token_path=client_token_path,
                    environment=environment,
                    limits=HermesFacadeLimits.from_app_config(config),
                    transport=transport,
                    identity_factory=identity_factory,
                )
            )
        return cls(facades, client_token_path=client_token_path)

    def authorized(self, authorization: str | None) -> bool:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            return False
        candidate = authorization[7:]
        if not candidate or len(candidate) > 512 or candidate != candidate.strip():
            return False
        return hmac.compare_digest(candidate, _private_token(self.client_token_path))

    def health(self) -> FacadeResponse:
        try:
            _private_token(self.client_token_path)
            available = sum(item.health().status == 200 for item in self.facades)
        except ConfigurationError:
            available = 0
        status = 200 if available else 503
        body = json.dumps(
            {
                "status": "ok" if available else "unavailable",
                "model": CLIENT_MODEL_ALIAS,
                "available_providers": available,
                "configured_providers": len(self.facades),
                "provider_network_probe": False,
                "selection": "lowest_reserved_cost_then_config_order",
                "unsafe_retry_after_egress": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return FacadeResponse(status, _JSON_CONTENT_TYPE, body)

    def dispatch(self, authorization: str | None, raw_body: bytes) -> FacadeResponse:
        try:
            if not self.authorized(authorization):
                return _openai_error(401, "invalid_api_key", "invalid bearer credential")
        except ConfigurationError:
            return _openai_error(
                503,
                "facade_unavailable",
                "facade authentication is unavailable",
            )

        candidates: list[tuple[int, int, HermesFacade, PreparedRequest]] = []
        for order, facade in enumerate(self.facades):
            try:
                prepared = facade.prepare(raw_body)
            except FacadeValidationError as exc:
                return _openai_error(exc.status, exc.code, str(exc))
            except (ConfigurationError, ProviderUnavailable, ValueError):
                continue
            reserved_cost = estimated_cost_microusd(
                prepared.estimated_input_tokens,
                prepared.maximum_output_tokens,
                *prepared.prices,
            )
            candidates.append((reserved_cost, order, facade, prepared))

        if not candidates:
            return _openai_error(
                503,
                "provider_unavailable",
                "no configured provider is locally available",
            )
        candidates.sort(key=lambda item: (item[0], item[1]))
        last_budget_response: FacadeResponse | None = None
        for _cost, _order, facade, prepared in candidates:
            response = facade._invoke(prepared)
            if response.status != 429:
                return response
            last_budget_response = response
        return last_budget_response or _openai_error(
            429,
            "budget_exceeded",
            "all coordinator provider budgets are exhausted",
        )


class ThreadingHermesFacadeServer(BoundedThreadingMixIn, HTTPServer):
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        facade: FacadeApplication,
        *,
        client_timeout_seconds: float = DEFAULT_CLIENT_TIMEOUT_SECONDS,
        max_request_workers: int = DEFAULT_MAX_REQUEST_WORKERS,
    ):
        _validate_loopback_address(*address)
        self.facade = facade
        super().__init__(
            address,
            HermesFacadeHandler,
            client_timeout_seconds=client_timeout_seconds,
            max_request_workers=max_request_workers,
        )


class HermesFacadeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ops-orchestrator-hermes-facade"
    sys_version = ""

    @property
    def facade(self) -> FacadeApplication:
        return self.server.facade  # type: ignore[attr-defined]

    def log_message(self, format: str, *args) -> None:
        return

    def _send(self, response: FacadeResponse) -> None:
        stream = None if isinstance(response.body, bytes) else response.body
        try:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            if isinstance(response.body, bytes):
                self.send_header("Content-Length", str(len(response.body)))
            self.end_headers()
            if isinstance(response.body, bytes):
                self.wfile.write(response.body)
            else:
                for chunk in response.body:
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, UpstreamFailure):
            pass
        finally:
            if stream is not None:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
            self.close_connection = True

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self._send(_openai_error(404, "not_found", "endpoint not found"))
            return
        try:
            if self.headers.get("Transfer-Encoding") or self.headers.get("Expect"):
                raise FacadeValidationError("streamed request bodies are not accepted")
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise FacadeValidationError("Content-Type must be application/json")
            raw_length = self.headers.get("Content-Length")
            if raw_length is None or not raw_length.isdigit():
                raise FacadeValidationError("valid Content-Length is required")
            length = int(raw_length)
            if not 2 <= length <= self.facade.limits.max_request_bytes:
                raise FacadeValidationError("request body size is outside the accepted range")
            if not self.facade.authorized(self.headers.get("Authorization")):
                self._send(_openai_error(401, "invalid_api_key", "invalid bearer credential"))
                return
            raw_body = self.rfile.read(length)
            if len(raw_body) != length:
                raise FacadeValidationError("request body is incomplete")
            self._send(self.facade.dispatch(self.headers.get("Authorization"), raw_body))
        except FacadeValidationError as exc:
            self._send(_openai_error(exc.status, exc.code, str(exc)))
        except ConfigurationError:
            self._send(_openai_error(503, "facade_unavailable", "facade authentication is unavailable"))

    def do_GET(self) -> None:
        if self.path != HEALTH_PATH:
            self._send(_openai_error(404, "not_found", "endpoint not found"))
            return
        self._send(self.facade.health())


def _validate_loopback_address(host: str, port: int) -> None:
    """Reject wildcard, DNS and non-loopback binds before opening a socket."""

    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("facade bind host must be an IP literal") from exc
    if not address.is_loopback or address.version != 4:
        raise ValueError("facade bind host must be an IPv4 loopback address")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65_535:
        raise ValueError("facade port is invalid")


def build_loopback_server(
    host: str = DEFAULT_BIND_HOST,
    port: int = DEFAULT_BIND_PORT,
    facade: FacadeApplication | None = None,
    *,
    client_timeout_seconds: float = DEFAULT_CLIENT_TIMEOUT_SECONDS,
    max_request_workers: int = DEFAULT_MAX_REQUEST_WORKERS,
) -> ThreadingHermesFacadeServer:
    """Build a server only for an explicit IPv4 loopback address."""

    _validate_loopback_address(host, port)
    if facade is None:
        raise ValueError("facade instance is required")
    return ThreadingHermesFacadeServer(
        (host, port),
        facade,
        client_timeout_seconds=client_timeout_seconds,
        max_request_workers=max_request_workers,
    )


def serve_loopback(
    facade: FacadeApplication,
    host: str = DEFAULT_BIND_HOST,
    port: int = DEFAULT_BIND_PORT,
) -> None:
    server = build_loopback_server(host, port, facade)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    """Run the facade as ``python -m ops_orchestrator.hermes_facade``."""

    parser = argparse.ArgumentParser(description="Budgeted loopback facade for Hermes")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument(
        "--providers",
        default="qwen-utility-api,alibaba-deepseek-ops-api,deepseek-ops-api",
        help="ordered, comma-separated reviewed coordinator deployment IDs",
    )
    parser.add_argument(
        "--provider",
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--host", default=DEFAULT_BIND_HOST)
    parser.add_argument("--port", default=DEFAULT_BIND_PORT, type=int)
    arguments = parser.parse_args(argv)

    config = load_config(arguments.config)
    database = Database(config.database_path)
    database.initialize()
    raw_provider_ids = arguments.provider or arguments.providers
    provider_ids = tuple(item.strip() for item in raw_provider_ids.split(",") if item.strip())
    if not provider_ids or len(provider_ids) > 32 or len(set(provider_ids)) != len(provider_ids):
        parser.error("--providers must contain 1..32 unique deployment IDs")
    facade = MultiProviderHermesFacade.from_app_config(
        config,
        provider_ids=provider_ids,
        ledger=BudgetLedger.from_config(database, config),
        client_token_path=arguments.token_file,
    )
    serve_loopback(facade, host=arguments.host, port=arguments.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
