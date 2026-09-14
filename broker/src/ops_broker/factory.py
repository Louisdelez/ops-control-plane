"""Composition root shared by HTTP and MCP adapters."""

from __future__ import annotations

from .config import Settings
from .database import Database
from .policy import ActionPolicy, RBACPolicy
from .runbooks import ExecutablePolicy, RunbookRegistry
from .service import BrokerService


def build_service(settings: Settings | None = None) -> BrokerService:
    settings = settings or Settings.from_env()
    executable_policy = ExecutablePolicy.load(settings.executables_policy_path)
    registry = RunbookRegistry.load(settings.runbooks_path, executable_policy)
    action_policy = ActionPolicy.load(settings.action_policy_path)
    rbac = RBACPolicy.load(settings.rbac_policy_path)
    return BrokerService(Database(settings.database_path), registry, rbac, action_policy)
