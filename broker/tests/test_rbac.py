from __future__ import annotations

from pathlib import Path
import uuid

import pytest

from ops_broker.errors import AuthorizationDenied, Conflict
from ops_broker.policy import PolicyValidationError, RBACPolicy

from conftest import Harness, request_id


def test_rbac_is_deny_by_default_and_grants_are_exact(harness: Harness) -> None:
    with pytest.raises(AuthorizationDenied):
        harness.service.list_runbooks("unknown")

    allowed = harness.service.request_action(
        actor_id="reader",
        request_id=request_id(),
        runbook_id="test.check.v1",
        parameters={},
        reason="routine bounded check",
    )
    assert allowed["status"] == "succeeded"

    with pytest.raises(AuthorizationDenied):
        harness.service.request_action(
            actor_id="reader",
            request_id=request_id(),
            runbook_id="test.restart-once.v1",
            parameters={
                "incident_id": str(uuid.uuid4()),
                "service": "worker.service",
            },
            reason="reader must not restart",
        )

    with harness.database.read() as connection:
        denied = connection.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'action.denied'"
        ).fetchone()[0]
    assert denied == 1
    assert harness.database.verify_audit_chain().valid


def test_action_request_id_is_idempotent_but_not_reusable(harness: Harness) -> None:
    stable_request_id = request_id()
    first = harness.service.request_action(
        actor_id="operator",
        request_id=stable_request_id,
        runbook_id="test.check.v1",
        parameters={},
        reason="same operation",
    )
    replay = harness.service.request_action(
        actor_id="operator",
        request_id=stable_request_id,
        runbook_id="test.check.v1",
        parameters={},
        reason="same operation",
    )
    assert replay["id"] == first["id"]
    assert len(harness.executor.calls) == 1

    with pytest.raises(Conflict):
        harness.service.request_action(
            actor_id="operator",
            request_id=stable_request_id,
            runbook_id="test.check.v1",
            parameters={},
            reason="different operation",
        )


def test_unknown_lifecycle_capability_is_rejected() -> None:
    with pytest.raises(PolicyValidationError, match="lifecycle capability"):
        RBACPolicy.from_data(
            {
                "version": 1,
                "default": "deny",
                "roles": {
                    "unsafe": {
                        "projects": ["infra"],
                        "runbooks": [],
                        "action_classes": [],
                        "lifecycle_capabilities": ["mission.delete"],
                    }
                },
                "actors": {"unsafe": {"roles": ["unsafe"]}},
            }
        )


def test_production_hermes_profile_roles_are_exact_and_cross_project_denied() -> None:
    policy = RBACPolicy.load(
        Path(__file__).resolve().parents[1] / "config" / "rbac.yaml"
    )
    for actor in (
        "hermes-coordinator", "minecraft-monitor", "infra-operator",
        "infra-network", "monitoring-shared", "backup-shared", "deploy-ops",
        "security-ops",
    ):
        assert policy.is_known_actor(actor)

    assert policy.can_runbook(
        "monitoring-shared", "minecraft", "minecraft.crash-triage.v1", "A"
    )
    assert not policy.can_runbook(
        "monitoring-shared", "infra-shared", "local.restart-once.v1", "B"
    )
    assert policy.can_runbook(
        "backup-shared", "infra-shared", "local.health.v1", "A"
    )
    assert not policy.can_runbook(
        "backup-shared", "infra-shared", "shared.change-request.v1", "C"
    )
    assert policy.can_runbook(
        "deploy-ops", "infra-shared", "shared.change-request.v1", "C"
    )
    assert not policy.can_runbook(
        "deploy-ops", "infra-shared", "local.restart-once.v1", "B"
    )
    assert policy.can_runbook(
        "security-ops", "infra-shared", "local.service-status.v1", "A"
    )
    assert not policy.can_runbook(
        "security-ops", "infra-shared", "shared.change-request.v1", "C"
    )

    policy.require_lifecycle("backup-shared", "backup-shared", "mission.queue.claim")
    policy.require_lifecycle("deploy-ops", "minecraft", "resource.lock")
    with pytest.raises(AuthorizationDenied):
        policy.require_lifecycle("monitoring-shared", "minecraft", "resource.lock")
    with pytest.raises(AuthorizationDenied):
        policy.require_lifecycle("security-ops", "infra-shared", "mission.queue.claim")
    with pytest.raises(AuthorizationDenied):
        policy.require_project("minecraft-monitor", "backup-shared")
