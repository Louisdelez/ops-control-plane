from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re
from typing import Any

from .errors import ValidationError


class Role(StrEnum):
    TINY = "ROLE_TINY"
    LOCAL_OPS = "ROLE_LOCAL_OPS"
    REASONING = "ROLE_REASONING"
    PREMIUM = "ROLE_PREMIUM"
    CODER = "ROLE_CODER"
    EMBEDDING = "ROLE_EMBEDDING"
    RERANKER = "ROLE_RERANKER"


class TaskType(StrEnum):
    OBSERVE = "OBSERVE"
    ANALYZE = "ANALYZE"
    PLAN = "PLAN"
    SUMMARIZE = "SUMMARIZE"
    CODE = "CODE"
    EMBED = "EMBED"
    RERANK = "RERANK"


class Risk(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Urgency(StrEnum):
    ROUTINE = "ROUTINE"
    NORMAL = "NORMAL"
    URGENT = "URGENT"
    EMERGENCY = "EMERGENCY"


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True)
class Context:
    instructions: str
    mission: str
    current_state: str = ""
    relevant_memories: tuple[str, ...] = ()
    tool_results: tuple[str, ...] = ()

    def sections(self) -> list[tuple[str, str]]:
        result = [("instructions", self.instructions), ("mission", self.mission)]
        if self.current_state:
            result.append(("current_state", self.current_state))
        result.extend(("relevant_memory", value) for value in self.relevant_memories)
        result.extend(("tool_result", value) for value in self.tool_results)
        return result

    @property
    def character_count(self) -> int:
        return sum(len(value) for _, value in self.sections())

    @property
    def estimated_tokens(self) -> int:
        # Deliberately pessimistic for budget reservation and 4 GiB constraints.
        return max(1, (self.character_count + 2) // 3)


@dataclass(frozen=True)
class RouteRequest:
    mission_id: str
    project_id: str
    task_type: TaskType
    risk: Risk
    complexity: float
    impact: float
    confidence: float
    urgency: Urgency
    deterministic_available: bool
    remote_allowed: bool
    context: Context
    requested_max_cost_usd: str | None = None
    verification_mode: bool = False


@dataclass(frozen=True)
class ProviderResult:
    status: str
    confidence: float
    summary: str
    plan: tuple[str, ...] = ()
    proposal: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    raw_usage_known: bool = False
    verification: str | None = None


@dataclass(frozen=True)
class ExecutorResult:
    role: Role
    provider_id: str
    provider_account_id: str | None
    chat_family: str | None
    model: str
    confidence: float
    summary: str
    plan: tuple[str, ...] = ()
    proposal: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "provider_id": self.provider_id,
            "provider_account_id": self.provider_account_id,
            "chat_family": self.chat_family,
            "model": self.model,
            "confidence": self.confidence,
            "summary": self.summary,
            "plan": list(self.plan),
            "proposal": self.proposal,
        }


@dataclass(frozen=True)
class VerificationOutcome:
    required: bool
    status: str
    verifier_role: Role | None = None
    provider_id: str | None = None
    provider_account_id: str | None = None
    chat_family: str | None = None
    model: str | None = None
    confidence: float | None = None
    summary: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "required": self.required,
            "status": self.status,
            "verifier_role": (
                self.verifier_role.value if self.verifier_role else None
            ),
            "provider_id": self.provider_id,
            "provider_account_id": self.provider_account_id,
            "chat_family": self.chat_family,
            "model": self.model,
            "confidence": self.confidence,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class RouteDecision:
    route_id: str
    mission_id: str
    selected_role: Role | None
    selected_provider: str | None
    selected_model: str | None
    disposition: str
    reason: str
    confidence: float
    summary: str
    plan: tuple[str, ...] = ()
    proposal: str | None = None
    requires_human_approval: bool = False
    next_role: Role | None = None
    degraded: bool = False
    trace_sequence: int | None = None
    executor_result: ExecutorResult | None = None
    verification: VerificationOutcome | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "mission_id": self.mission_id,
            "selected_role": self.selected_role.value if self.selected_role else None,
            "selected_provider": self.selected_provider,
            "selected_model": self.selected_model,
            "disposition": self.disposition,
            "reason": self.reason,
            "confidence": self.confidence,
            "summary": self.summary,
            "plan": list(self.plan),
            "proposal": self.proposal,
            "requires_human_approval": self.requires_human_approval,
            "next_role": self.next_role.value if self.next_role else None,
            "degraded": self.degraded,
            "trace_sequence": self.trace_sequence,
            "executor_result": (
                self.executor_result.as_dict() if self.executor_result else None
            ),
            "verification": (
                self.verification.as_dict() if self.verification else None
            ),
        }


def _strict_keys(value: dict[str, Any], allowed: set[str], location: str) -> None:
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise ValidationError(f"unknown {location} fields: {', '.join(unexpected)}")


def _bounded_text(value: Any, field_name: str, maximum: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string")
    value = value.strip()
    if required and not value:
        raise ValidationError(f"{field_name} must not be empty")
    if len(value) > maximum:
        raise ValidationError(f"{field_name} exceeds {maximum} characters")
    return value


def _enum(enum_type: type[StrEnum], value: Any, field_name: str) -> StrEnum:
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string")
    try:
        return enum_type(value.upper())
    except ValueError as exc:
        raise ValidationError(f"invalid {field_name}") from exc


def parse_route_request(payload: Any, limits: Any) -> RouteRequest:
    if not isinstance(payload, dict):
        raise ValidationError("request must be an object")
    _strict_keys(
        payload,
        {
            "mission_id", "project_id", "task_type", "risk", "complexity",
            "impact", "confidence", "urgency", "deterministic_available", "context",
            "remote_allowed", "requested_max_cost_usd",
        },
        "request",
    )
    mission_id = _bounded_text(payload.get("mission_id"), "mission_id", 128)
    project_id = _bounded_text(payload.get("project_id"), "project_id", 128)
    if not _IDENTIFIER.fullmatch(mission_id) or not _IDENTIFIER.fullmatch(project_id):
        raise ValidationError("mission_id and project_id must be safe identifiers")
    for number_name in ("complexity", "impact", "confidence"):
        number = payload.get(number_name)
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            raise ValidationError(f"{number_name} must be a number")
        if not 0.0 <= float(number) <= 1.0:
            raise ValidationError(f"{number_name} must be between 0 and 1")
    deterministic = payload.get("deterministic_available")
    if not isinstance(deterministic, bool):
        raise ValidationError("deterministic_available must be boolean")
    remote_allowed = payload.get("remote_allowed", False)
    if not isinstance(remote_allowed, bool):
        raise ValidationError("remote_allowed must be boolean")
    context_value = payload.get("context")
    if not isinstance(context_value, dict):
        raise ValidationError("context must be an object")
    _strict_keys(
        context_value,
        {"instructions", "mission", "current_state", "relevant_memories", "tool_results"},
        "context",
    )
    memories = context_value.get("relevant_memories", [])
    tools = context_value.get("tool_results", [])
    if not isinstance(memories, list) or len(memories) > limits.max_memories:
        raise ValidationError(f"relevant_memories must contain at most {limits.max_memories} strings")
    if not isinstance(tools, list) or len(tools) > limits.max_tool_results:
        raise ValidationError(f"tool_results must contain at most {limits.max_tool_results} strings")
    context = Context(
        instructions=_bounded_text(context_value.get("instructions"), "instructions", limits.max_section_chars),
        mission=_bounded_text(context_value.get("mission"), "mission", limits.max_section_chars),
        current_state=_bounded_text(context_value.get("current_state", ""), "current_state", limits.max_section_chars, required=False),
        relevant_memories=tuple(
            _bounded_text(item, "relevant_memory", limits.max_section_chars) for item in memories
        ),
        tool_results=tuple(
            _bounded_text(item, "tool_result", limits.max_section_chars) for item in tools
        ),
    )
    if context.character_count > limits.max_context_chars:
        raise ValidationError("minimal context character limit exceeded")
    if context.estimated_tokens > limits.max_context_tokens:
        raise ValidationError("minimal context token limit exceeded")
    requested_cost = payload.get("requested_max_cost_usd")
    if requested_cost is not None:
        if not isinstance(requested_cost, str) or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?", requested_cost):
            raise ValidationError("requested_max_cost_usd must be a non-negative decimal string")
    return RouteRequest(
        mission_id=mission_id,
        project_id=project_id,
        task_type=_enum(TaskType, payload.get("task_type"), "task_type"),
        risk=_enum(Risk, payload.get("risk"), "risk"),
        complexity=float(payload["complexity"]),
        impact=float(payload["impact"]),
        confidence=float(payload["confidence"]),
        urgency=_enum(Urgency, payload.get("urgency", "NORMAL"), "urgency"),
        deterministic_available=deterministic,
        remote_allowed=remote_allowed,
        context=context,
        requested_max_cost_usd=requested_cost,
    )
