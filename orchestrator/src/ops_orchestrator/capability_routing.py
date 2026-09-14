"""Deterministic capability requirements used by the execution router.

The catalogue preview and the real execution path both use hard 0..10
capability thresholds.  This module translates the already bounded task facts
into those thresholds; it never guesses from free-form prompt text and never
selects a provider brand.
"""

from __future__ import annotations

from typing import Mapping

from .models import Risk, RouteRequest, TaskType, Urgency


_RISK_RELIABILITY = {
    Risk.LOW: 5,
    Risk.MEDIUM: 6,
    Risk.HIGH: 8,
    Risk.CRITICAL: 9,
}


def requirements_for(request: RouteRequest) -> dict[str, int]:
    """Return auditable hard minimums from structured request attributes."""

    complexity = int(request.complexity * 3 + 0.5)
    uncertainty = 1 if request.confidence < 0.60 else 0
    reliability = _RISK_RELIABILITY[request.risk]
    factuality = min(9, reliability)
    common = {
        "instructions": min(9, 5 + complexity // 2),
        "reliability": reliability,
    }

    if request.task_type in {TaskType.EMBED, TaskType.RERANK}:
        return {}
    if request.task_type is TaskType.OBSERVE:
        result = {**common, "general": 4 + complexity, "factuality": factuality}
    elif request.task_type is TaskType.SUMMARIZE:
        result = {
            **common,
            "general": 5 + complexity,
            "factuality": factuality,
            "multilingual": 6,
        }
    elif request.task_type is TaskType.CODE:
        result = {
            **common,
            "reasoning": min(9, 5 + complexity + uncertainty),
            "code": min(10, 7 + complexity),
            "tools": 7,
            "agentic": min(9, 6 + complexity),
        }
    elif request.task_type is TaskType.PLAN:
        result = {
            **common,
            "reasoning": min(10, 5 + complexity + uncertainty),
            "tools": min(9, 6 + complexity),
            "agentic": min(9, 6 + complexity),
            "factuality": factuality,
        }
    else:
        result = {
            **common,
            "reasoning": min(10, 4 + complexity + uncertainty),
            "factuality": factuality,
            "general": min(9, 4 + complexity),
        }

    if request.urgency is Urgency.EMERGENCY:
        result["speed"] = 4
    return {key: max(0, min(10, value)) for key, value in sorted(result.items())}


def capability_check(
    profile: Mapping[str, int],
    requirements: Mapping[str, int],
) -> tuple[bool, dict[str, int], int]:
    """Return pass/fail, deficits and total positive capability margin."""

    deficits = {
        dimension: minimum - int(profile.get(dimension, -1))
        for dimension, minimum in requirements.items()
        if int(profile.get(dimension, -1)) < minimum
    }
    margin = sum(
        max(0, int(profile.get(dimension, 0)) - minimum)
        for dimension, minimum in requirements.items()
    )
    return (not deficits, deficits, margin)
