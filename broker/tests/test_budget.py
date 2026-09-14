from __future__ import annotations

import pytest

from ops_broker.errors import BudgetExceeded
from ops_broker.policy import PolicyValidationError

from conftest import Harness, request_id


def test_second_restart_for_same_incident_is_blocked(harness: Harness) -> None:
    incident = harness.service.create_incident(
        actor_id="operator",
        request_id=request_id(),
        project_id="infra",
        title="worker crash loop",
        severity="critical",
    )
    parameters = {"incident_id": incident["id"], "service": "worker.service"}

    first = harness.service.request_action(
        actor_id="operator",
        request_id=request_id(),
        runbook_id="test.restart-once.v1",
        parameters=parameters,
        reason="one bounded recovery attempt",
        incident_id=incident["id"],
    )
    assert first["status"] == "succeeded"

    with pytest.raises(BudgetExceeded) as raised:
        harness.service.request_action(
            actor_id="operator",
            request_id=request_id(),
            runbook_id="test.restart-once.v1",
            parameters=parameters,
            reason="second crash must escalate",
            incident_id=incident["id"],
        )

    blocked = harness.service.get_action("operator", raised.value.details["action_id"])
    assert blocked["status"] == "budget_exhausted"
    assert len(harness.executor.calls) == 1
    assert harness.database.verify_audit_chain().valid


def test_runbook_budget_cannot_weaken_global_policy(harness: Harness) -> None:
    harness.service.action_policy.validate_runbook_budget(1, 7200)
    with pytest.raises(PolicyValidationError):
        harness.service.action_policy.validate_runbook_budget(2, 3600)
    with pytest.raises(PolicyValidationError):
        harness.service.action_policy.validate_runbook_budget(1, 60)
