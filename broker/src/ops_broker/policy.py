"""Action-class rules and explicit actor-to-role RBAC."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import AuthorizationDenied


class PolicyValidationError(ValueError):
    pass


LIFECYCLE_CAPABILITIES = frozenset(
    {
        "mission.status.write",
        "mission.context.write",
        "mission.checkpoint.write",
        "mission.record.write",
        "mission.queue.claim",
        "incident.status.write",
        "incident.detail.write",
        "resource.lock",
        "model.trace.write",
    }
)


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PolicyValidationError(f"{location} must be a mapping")
    return value


def _only_keys(value: dict[str, Any], allowed: set[str], location: str) -> None:
    extra = set(value) - allowed
    if extra:
        raise PolicyValidationError(f"{location} has unsupported keys: {sorted(extra)}")


def _string_set(value: Any, location: str) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(item, (str, int)) for item in value):
        raise PolicyValidationError(f"{location} must be a list")
    return frozenset(str(item) for item in value)


@dataclass(frozen=True)
class ClassRule:
    approval: str
    max_attempts: int | None = None
    window_seconds: int | None = None
    approval_ttl_seconds: int | None = None


@dataclass(frozen=True)
class ActionPolicy:
    classes: dict[str, ClassRule]
    approval_actor_ids: frozenset[str]
    fail_closed_when_unconfigured: bool

    @classmethod
    def load(cls, path: Path) -> "ActionPolicy":
        return cls.from_data(yaml.safe_load(path.read_text(encoding="utf-8")), str(path))

    @classmethod
    def from_data(cls, raw_value: Any, location: str = "action policy") -> "ActionPolicy":
        raw = _mapping(raw_value, location)
        _only_keys(raw, {"version", "default", "classes", "zulip"}, location)
        if raw.get("version") != 1 or raw.get("default") != "deny":
            raise PolicyValidationError(f"{location} must be version 1 and deny by default")
        classes_raw = _mapping(raw.get("classes"), f"{location}:classes")
        if set(classes_raw) != {"A", "B", "C"}:
            raise PolicyValidationError(f"{location} must define exactly classes A, B and C")

        classes: dict[str, ClassRule] = {}
        for action_class, expected in {"A": "none", "B": "policy_budget", "C": "human_required"}.items():
            item = _mapping(classes_raw[action_class], f"{location}:classes.{action_class}")
            allowed_keys = {"approval", "examples"}
            if action_class == "B":
                allowed_keys.add("limits")
            if action_class == "C":
                allowed_keys.add("approval_ttl_seconds")
            _only_keys(item, allowed_keys, f"{location}:classes.{action_class}")
            if item.get("approval") != expected:
                raise PolicyValidationError(f"{location}: invalid approval rule for class {action_class}")
            examples = item.get("examples", [])
            if not isinstance(examples, list) or not all(isinstance(value, str) for value in examples):
                raise PolicyValidationError(f"{location}: examples must be a string list")
            limits = _mapping(item.get("limits", {}), f"{location}:classes.{action_class}.limits")
            if action_class == "B":
                _only_keys(
                    limits,
                    {"max_attempts", "window_seconds", "on_exhausted"},
                    f"{location}:classes.B.limits",
                )
                if limits.get("on_exhausted", "escalate") != "escalate":
                    raise PolicyValidationError(f"{location}: B.on_exhausted must be escalate")
            max_attempts = limits.get("max_attempts")
            window_seconds = limits.get("window_seconds")
            ttl = item.get("approval_ttl_seconds")
            if action_class == "B":
                if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
                    raise PolicyValidationError(f"{location}: B.max_attempts is invalid")
                if not isinstance(window_seconds, int) or isinstance(window_seconds, bool) or window_seconds < 1:
                    raise PolicyValidationError(f"{location}: B.window_seconds is invalid")
            if action_class == "C" and (
                not isinstance(ttl, int) or isinstance(ttl, bool) or not 60 <= ttl <= 86_400
            ):
                raise PolicyValidationError(f"{location}: C.approval_ttl_seconds is invalid")
            classes[action_class] = ClassRule(expected, max_attempts, window_seconds, ttl)

        zulip = _mapping(raw.get("zulip", {}), f"{location}:zulip")
        _only_keys(
            zulip,
            {"approval_reactions", "authorized_actor_ids", "fail_closed_when_unconfigured"},
            f"{location}:zulip",
        )
        reactions = _mapping(zulip.get("approval_reactions", {}), f"{location}:approval_reactions")
        if reactions:
            _only_keys(reactions, {"approve", "reject"}, f"{location}:approval_reactions")
            if set(reactions) != {"approve", "reject"} or not all(
                isinstance(value, str) and value for value in reactions.values()
            ):
                raise PolicyValidationError(f"{location}: approval reactions are invalid")
        actor_ids = _string_set(zulip.get("authorized_actor_ids", []), f"{location}:authorized_actor_ids")
        if any(not actor_id or len(actor_id) > 128 or any(char.isspace() for char in actor_id) for actor_id in actor_ids):
            raise PolicyValidationError(f"{location}: an approval actor id is malformed")
        fail_closed = zulip.get("fail_closed_when_unconfigured", True)
        if not isinstance(fail_closed, bool):
            raise PolicyValidationError(f"{location}: fail_closed_when_unconfigured must be boolean")
        if not fail_closed:
            raise PolicyValidationError(f"{location}: V0 requires fail-closed approvals")
        return cls(classes, actor_ids, fail_closed)

    def validate_runbook_budget(self, max_attempts: int, window_seconds: int) -> None:
        rule = self.classes["B"]
        assert rule.max_attempts is not None and rule.window_seconds is not None
        # A larger attempt count or a shorter window is less restrictive than
        # the global ceiling.  Longer windows are safe (for example, one retry
        # per day under a one-retry-per-hour global policy).
        if max_attempts > rule.max_attempts or window_seconds < rule.window_seconds:
            raise PolicyValidationError("runbook B budget exceeds the global policy")

    def require_approval_actor(self, actor_id: str) -> None:
        if not self.approval_actor_ids:
            raise AuthorizationDenied("human approval actors are not configured")
        if actor_id not in self.approval_actor_ids:
            raise AuthorizationDenied("actor is not authorized to approve actions")


@dataclass(frozen=True)
class RoleGrant:
    projects: frozenset[str]
    runbooks: frozenset[str]
    action_classes: frozenset[str]
    lifecycle_capabilities: frozenset[str]


class RBACPolicy:
    """Lookup-only RBAC. Unknown actors, roles and resources are denied."""

    def __init__(self, actors: dict[str, frozenset[str]], roles: dict[str, RoleGrant]) -> None:
        self._actors = dict(actors)
        self._roles = dict(roles)

    @classmethod
    def load(cls, path: Path) -> "RBACPolicy":
        return cls.from_data(yaml.safe_load(path.read_text(encoding="utf-8")), str(path))

    @classmethod
    def from_data(cls, raw_value: Any, location: str = "RBAC policy") -> "RBACPolicy":
        raw = _mapping(raw_value, location)
        _only_keys(raw, {"version", "default", "roles", "actors"}, location)
        if raw.get("version") != 1 or raw.get("default") != "deny":
            raise PolicyValidationError(f"{location} must be version 1 and deny by default")
        roles_raw = _mapping(raw.get("roles"), f"{location}:roles")
        actors_raw = _mapping(raw.get("actors"), f"{location}:actors")

        roles: dict[str, RoleGrant] = {}
        for name, value in roles_raw.items():
            if not isinstance(name, str):
                raise PolicyValidationError(f"{location}: role names must be strings")
            item = _mapping(value, f"{location}:roles.{name}")
            extra = set(item) - {
                "projects",
                "runbooks",
                "action_classes",
                "lifecycle_capabilities",
            }
            if extra:
                raise PolicyValidationError(f"{location}: unsupported role keys: {sorted(extra)}")
            projects = _string_set(item.get("projects", []), f"{location}:roles.{name}.projects")
            runbooks = _string_set(item.get("runbooks", []), f"{location}:roles.{name}.runbooks")
            classes = _string_set(item.get("action_classes", []), f"{location}:roles.{name}.classes")
            lifecycle_capabilities = _string_set(
                item.get("lifecycle_capabilities", []),
                f"{location}:roles.{name}.lifecycle_capabilities",
            )
            if not classes <= {"A", "B", "C"}:
                raise PolicyValidationError(f"{location}: role has an invalid action class")
            if not lifecycle_capabilities <= LIFECYCLE_CAPABILITIES:
                raise PolicyValidationError(
                    f"{location}: role has an invalid lifecycle capability"
                )
            roles[name] = RoleGrant(
                projects,
                runbooks,
                classes,
                lifecycle_capabilities,
            )

        actors: dict[str, frozenset[str]] = {}
        for actor_id, value in actors_raw.items():
            if not isinstance(actor_id, str):
                raise PolicyValidationError(f"{location}: actor ids must be strings")
            item = _mapping(value, f"{location}:actors.{actor_id}")
            if set(item) != {"roles"}:
                raise PolicyValidationError(f"{location}: actors may only declare roles")
            actor_roles = _string_set(item["roles"], f"{location}:actors.{actor_id}.roles")
            if not actor_roles or not actor_roles <= set(roles):
                raise PolicyValidationError(f"{location}: actor references an unknown role")
            actors[actor_id] = actor_roles
        return cls(actors, roles)

    def is_known_actor(self, actor_id: str) -> bool:
        return actor_id in self._actors

    def require_project(self, actor_id: str, project: str) -> None:
        roles = self._actors.get(actor_id)
        if roles is None:
            raise AuthorizationDenied("unknown actor")
        if not any(project in self._roles[role].projects for role in roles):
            raise AuthorizationDenied("actor has no grant for this project")

    def projects_for_actor(self, actor_id: str) -> frozenset[str]:
        roles = self._actors.get(actor_id)
        if roles is None:
            raise AuthorizationDenied("unknown actor")
        return frozenset(
            project
            for role in roles
            for project in self._roles[role].projects
        )

    def require_runbook(self, actor_id: str, project: str, runbook_id: str, action_class: str) -> None:
        self.require_project(actor_id, project)
        roles = self._actors[actor_id]
        if not any(
            project in self._roles[role].projects
            and runbook_id in self._roles[role].runbooks
            and action_class in self._roles[role].action_classes
            for role in roles
        ):
            raise AuthorizationDenied("actor has no grant for this runbook and action class")

    def require_lifecycle(self, actor_id: str, project: str, capability: str) -> None:
        if capability not in LIFECYCLE_CAPABILITIES:
            raise AuthorizationDenied("unknown lifecycle capability")
        self.require_project(actor_id, project)
        roles = self._actors[actor_id]
        if not any(
            project in self._roles[role].projects
            and capability in self._roles[role].lifecycle_capabilities
            for role in roles
        ):
            raise AuthorizationDenied("actor has no grant for this lifecycle operation")

    def can_runbook(self, actor_id: str, project: str, runbook_id: str, action_class: str) -> bool:
        try:
            self.require_runbook(actor_id, project, runbook_id, action_class)
            return True
        except AuthorizationDenied:
            return False
