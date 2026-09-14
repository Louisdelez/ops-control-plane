from __future__ import annotations

from dataclasses import dataclass

from .config import Thresholds
from .models import Risk, Role, RouteRequest, TaskType, Urgency


@dataclass(frozen=True)
class RoutingPlan:
    roles: tuple[Role, ...]
    reason: str
    deterministic: bool = False


class RoutingPolicy:
    """Pure policy: complexity, risk and confidence select logical roles only."""

    def __init__(self, thresholds: Thresholds, max_context_tokens: int = 4096):
        self.thresholds = thresholds
        self.max_context_tokens = max_context_tokens

    def plan(self, request: RouteRequest) -> RoutingPlan:
        if request.deterministic_available:
            return RoutingPlan((), "an explicitly available deterministic tool takes priority", True)
        if request.task_type is TaskType.EMBED:
            return RoutingPlan((Role.EMBEDDING,), "embedding task requires the embedding role")
        if request.task_type is TaskType.RERANK:
            return RoutingPlan((Role.RERANKER,), "reranking task requires the reranker role")
        if request.task_type is TaskType.CODE:
            return RoutingPlan(
                (Role.CODER, Role.PREMIUM),
                "missing code starts with isolated coders and escalates to premium only after failure",
            )
        if request.risk is Risk.CRITICAL:
            return RoutingPlan(
                (Role.PREMIUM,),
                "critical risk justifies the exceptional premium tier and still requires human approval",
            )
        if request.risk is Risk.HIGH:
            return RoutingPlan(
                (Role.REASONING, Role.PREMIUM),
                "high risk starts with expert reasoning and escalates to premium only on failure",
            )
        if request.impact >= 0.75:
            return RoutingPlan(
                (Role.REASONING, Role.PREMIUM),
                "high potential impact requires expert reasoning before any premium escalation",
            )
        if request.context.estimated_tokens >= int(self.max_context_tokens * 0.75):
            return RoutingPlan(
                (Role.REASONING, Role.PREMIUM),
                "large bounded context requires expert reasoning before any premium escalation",
            )
        if request.confidence < self.thresholds.medium_confidence:
            return RoutingPlan(
                (Role.REASONING, Role.PREMIUM),
                "low confidence requires expert reasoning before any premium escalation",
            )
        if request.complexity >= self.thresholds.complex_complexity:
            return RoutingPlan(
                (Role.REASONING, Role.PREMIUM),
                "high complexity requires expert reasoning before any premium escalation",
            )
        if request.urgency is Urgency.EMERGENCY and request.complexity > self.thresholds.simple_complexity:
            return RoutingPlan(
                (Role.REASONING, Role.PREMIUM),
                "urgent non-trivial analysis requires expert reasoning before any premium escalation",
            )
        if (
            request.confidence >= self.thresholds.high_confidence
            and request.complexity <= self.thresholds.simple_complexity
            and request.impact <= 0.25
            and request.risk is Risk.LOW
        ):
            return RoutingPlan(
                (Role.TINY, Role.LOCAL_OPS, Role.REASONING, Role.PREMIUM),
                "simple low-risk high-confidence work starts with the least-cost capable API tier",
            )
        return RoutingPlan(
            (Role.LOCAL_OPS, Role.REASONING, Role.PREMIUM),
            "moderate work starts with the standard API tier and escalates only if needed",
        )
