"""Bounded, explainable routing adjustments derived from local observations.

The reviewed catalogue remains immutable.  This module only adjusts the
estimated cost used to order providers inside an already selected capability
tier.  Quality and completed-call latency use separate evidence thresholds;
their combined effect remains narrowly bounded so sparse or adversarial
feedback cannot cause large jumps.
"""

from __future__ import annotations

from .database import Database


ALGORITHM_VERSION = "adaptive-quality-latency-v2"
BASELINE_QUALITY_MILLI = 750
PRIOR_WEIGHT_UNITS = 48  # twelve validated outcomes at four units each
VALIDATION_WEIGHT_UNITS = 4
ATTEMPT_WEIGHT_UNITS = 1
MINIMUM_VALIDATIONS = 5
MINIMUM_ATTEMPTS = 8
MAXIMUM_HISTORY_ROWS = 500
MAXIMUM_ADJUSTMENT_PPM = 150_000
MINIMUM_LATENCY_SAMPLES = 5
MAXIMUM_LATENCY_SAMPLE_MS = 300_000
LATENCY_POLICY = {
    "ROUTINE": (12_000, 25_000),
    "NORMAL": (8_000, 50_000),
    "URGENT": (4_000, 100_000),
    "EMERGENCY": (2_000, 150_000),
}


def policy_snapshot() -> dict[str, object]:
    """Public, non-secret constants needed to audit a routing score."""

    return {
        "algorithm_version": ALGORITHM_VERSION,
        "baseline_quality_milli": BASELINE_QUALITY_MILLI,
        "prior_weight_units": PRIOR_WEIGHT_UNITS,
        "validation_weight_units": VALIDATION_WEIGHT_UNITS,
        "attempt_weight_units": ATTEMPT_WEIGHT_UNITS,
        "minimum_validations": MINIMUM_VALIDATIONS,
        "minimum_attempts": MINIMUM_ATTEMPTS,
        "maximum_history_rows": MAXIMUM_HISTORY_ROWS,
        "maximum_adjustment_ppm": MAXIMUM_ADJUSTMENT_PPM,
        "minimum_latency_samples": MINIMUM_LATENCY_SAMPLES,
        "maximum_latency_sample_ms": MAXIMUM_LATENCY_SAMPLE_MS,
        "latency_policy": {
            urgency.lower(): {
                "target_ms": values[0],
                "maximum_adjustment_ppm": values[1],
            }
            for urgency, values in LATENCY_POLICY.items()
        },
        "catalogue_mutation": False,
    }


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def candidate_score(
    database: Database,
    *,
    task_type: str,
    provider_id: str,
    provider_account_id: str | None,
    model: str,
    estimated_cost_microusd: int,
    urgency: str | None = None,
    usage_role: str | None = None,
) -> dict[str, object]:
    """Return a deterministic cost-equivalent score and its full evidence."""

    if estimated_cost_microusd < 0:
        raise ValueError("estimated cost must be non-negative")
    if urgency is not None and urgency not in LATENCY_POLICY:
        raise ValueError("urgency is outside the reviewed latency policy")
    history = database.model_performance_history(
        task_type=task_type,
        provider_id=provider_id,
        model=model,
        maximum_rows=MAXIMUM_HISTORY_ROWS,
        usage_role=usage_role,
    )
    validations = history["validations"]
    if not isinstance(validations, list):  # defensive internal contract check
        raise RuntimeError("adaptive validation history is malformed")
    validation_points = 0
    successful_validations = 0
    for validation in validations:
        succeeded = validation["outcome"] == "succeeded"
        successful_validations += int(succeeded)
        quality = int(validation["quality_milli"])
        corrections = min(int(validation["corrections_required"]), 5)
        # A failed task can never contribute more than 25% quality.  Corrections
        # then subtract at most another 20 percentage points.
        base = quality if succeeded else min(quality, 250)
        validation_points += max(0, base - corrections * 40)

    attempt_count = int(history["attempt_count"])
    completed_attempts = int(history["completed_attempts"])
    uncertain_attempts = int(history["uncertain_attempts"])
    validation_count = len(validations)
    denominator = (
        PRIOR_WEIGHT_UNITS
        + validation_count * VALIDATION_WEIGHT_UNITS
        + attempt_count * ATTEMPT_WEIGHT_UNITS
    )
    numerator = (
        PRIOR_WEIGHT_UNITS * BASELINE_QUALITY_MILLI
        + VALIDATION_WEIGHT_UNITS * validation_points
        + ATTEMPT_WEIGHT_UNITS
        * completed_attempts
        * BASELINE_QUALITY_MILLI
    )
    posterior_quality_milli = (numerator + denominator // 2) // denominator
    posterior_quality_milli = max(0, min(1000, posterior_quality_milli))

    enough_evidence = (
        validation_count >= MINIMUM_VALIDATIONS or attempt_count >= MINIMUM_ATTEMPTS
    )
    quality_adjustment_ppm = 0
    if enough_evidence:
        quality_adjustment_ppm = (
            BASELINE_QUALITY_MILLI - posterior_quality_milli
        ) * 500
        quality_adjustment_ppm = max(
            -MAXIMUM_ADJUSTMENT_PPM,
            min(MAXIMUM_ADJUSTMENT_PPM, quality_adjustment_ppm),
        )

    latency_samples = history.get("completed_latency_ms")
    if (
        not isinstance(latency_samples, list)
        or any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= MAXIMUM_LATENCY_SAMPLE_MS
            for value in latency_samples
        )
    ):
        raise RuntimeError("adaptive latency history is malformed")
    latency_sample_count = len(latency_samples)
    observed_latency_ms = _median(latency_samples)
    latency_evidence_sufficient = latency_sample_count >= MINIMUM_LATENCY_SAMPLES
    latency_target_ms: int | None = None
    maximum_latency_adjustment_ppm = 0
    latency_adjustment_ppm = 0
    if urgency is not None:
        latency_target_ms, maximum_latency_adjustment_ppm = LATENCY_POLICY[urgency]
        if latency_evidence_sufficient and observed_latency_ms is not None:
            relative_gap_ppm = (
                (observed_latency_ms - latency_target_ms) * 1_000_000
            ) // latency_target_ms
            relative_gap_ppm = max(-1_000_000, min(1_000_000, relative_gap_ppm))
            latency_adjustment_ppm = (
                relative_gap_ppm * maximum_latency_adjustment_ppm
            ) // 1_000_000

    adjustment_ppm = max(
        -MAXIMUM_ADJUSTMENT_PPM,
        min(
            MAXIMUM_ADJUSTMENT_PPM,
            quality_adjustment_ppm + latency_adjustment_ppm,
        ),
    )
    multiplier_ppm = 1_000_000 + adjustment_ppm
    adaptive_cost = (
        estimated_cost_microusd * multiplier_ppm + 1_000_000 - 1
    ) // 1_000_000

    return {
        "provider_id": provider_id,
        "provider_account_id": provider_account_id,
        "model": model,
        "history_usage_role": usage_role,
        "estimated_cost_microusd": estimated_cost_microusd,
        "attempt_count": attempt_count,
        "completed_attempts": completed_attempts,
        "uncertain_attempts": uncertain_attempts,
        "validation_count": validation_count,
        "successful_validations": successful_validations,
        "posterior_quality_milli": posterior_quality_milli,
        "quality_adjustment_ppm": quality_adjustment_ppm,
        "observed_latency_ms": observed_latency_ms,
        "latency_sample_count": latency_sample_count,
        "latency_target_ms": latency_target_ms,
        "latency_evidence_sufficient": latency_evidence_sufficient,
        "maximum_latency_adjustment_ppm": maximum_latency_adjustment_ppm,
        "latency_adjustment_ppm": latency_adjustment_ppm,
        "adjustment_ppm": adjustment_ppm,
        "adaptive_cost_microusd": adaptive_cost,
        "algorithm_version": ALGORITHM_VERSION,
    }
