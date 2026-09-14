from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from ops_broker.database import Database
from ops_broker.executor import ExecutionResult
from ops_broker.policy import ActionPolicy, RBACPolicy
from ops_broker.runbooks import ExecutablePolicy, RunbookRegistry
from ops_broker.service import BrokerService


RUNBOOKS = {
    "check.yaml": """
id: test.check.v1
version: 1
project: infra
action_class: A
description: Bounded read-only check
timeout_seconds: 5
command:
  argv: [/usr/bin/true]
parameters: {}
output:
  max_bytes: 1024
  redact: true
""",
    "restart.yaml": """
id: test.restart-once.v1
version: 1
project: infra
action_class: B
description: Restart one named service at most once per incident
timeout_seconds: 5
budget:
  key: "restart:{incident_id}:{service}"
  max_attempts: 1
  window_seconds: 3600
command:
  argv: [/usr/bin/true, "{service}"]
parameters:
  incident_id:
    type: uuid
  service:
    type: enum
    values: [worker.service]
output:
  max_bytes: 1024
  redact: true
""",
    "change.yaml": """
id: test.change.v1
version: 1
project: infra
action_class: C
description: Human-approved test change
timeout_seconds: 5
command:
  argv: [/usr/bin/true]
parameters: {}
output:
  max_bytes: 1024
  redact: true
""",
}


class RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def execute(
        self,
        argv: tuple[str, ...],
        timeout_seconds: int,
        max_bytes: int,
    ) -> ExecutionResult:
        self.calls.append(argv)
        return ExecutionResult(0, "ok\n", "", False, False)


@dataclass
class Harness:
    service: BrokerService
    database: Database
    executor: RecordingExecutor


def request_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    runbook_root = tmp_path / "runbooks"
    runbook_root.mkdir()
    for name, content in RUNBOOKS.items():
        (runbook_root / name).write_text(content.lstrip(), encoding="utf-8")

    registry = RunbookRegistry.load(
        runbook_root,
        ExecutablePolicy(executables=frozenset({"/usr/bin/true"})),
    )
    action_policy = ActionPolicy.from_data(
        {
            "version": 1,
            "default": "deny",
            "classes": {
                "A": {"approval": "none"},
                "B": {
                    "approval": "policy_budget",
                    "limits": {"max_attempts": 1, "window_seconds": 3600},
                },
                "C": {"approval": "human_required", "approval_ttl_seconds": 1800},
            },
            "zulip": {
                "authorized_actor_ids": ["operator", "zulip:42"],
                "fail_closed_when_unconfigured": True,
            },
        }
    )
    rbac = RBACPolicy.from_data(
        {
            "version": 1,
            "default": "deny",
            "roles": {
                "operator": {
                    "projects": ["infra"],
                    "runbooks": [
                        "test.check.v1",
                        "test.restart-once.v1",
                        "test.change.v1",
                    ],
                    "action_classes": ["A", "B", "C"],
                    "lifecycle_capabilities": [
                        "mission.status.write",
                        "mission.context.write",
                        "mission.checkpoint.write",
                        "mission.record.write",
                        "mission.queue.claim",
                        "incident.status.write",
                        "incident.detail.write",
                        "resource.lock",
                        "model.trace.write",
                    ],
                },
                "reader": {
                    "projects": ["infra"],
                    "runbooks": ["test.check.v1"],
                    "action_classes": ["A"],
                },
                "other-reader": {
                    "projects": ["other"],
                    "runbooks": [],
                    "action_classes": [],
                },
                "zulip-mobile-intake": {
                    "projects": ["infra"],
                    "runbooks": [],
                    "action_classes": [],
                    "lifecycle_capabilities": [
                        "mission.status.write",
                        "mission.record.write",
                    ],
                },
            },
            "actors": {
                "operator": {"roles": ["operator"]},
                "reader": {"roles": ["reader"]},
                "other-reader": {"roles": ["other-reader"]},
                "zulip-mobile": {"roles": ["zulip-mobile-intake"]},
            },
        }
    )
    database = Database(tmp_path / "state" / "broker.db")
    executor = RecordingExecutor()
    service = BrokerService(
        database,
        registry,
        rbac,
        action_policy,
        executor=executor,
    )
    return Harness(service, database, executor)
