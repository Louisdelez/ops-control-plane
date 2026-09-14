"""Public, deliberately terse broker errors."""

from __future__ import annotations

from typing import Any


class BrokerError(Exception):
    """An expected refusal or validation failure safe to expose to callers."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class AuthorizationDenied(BrokerError):
    def __init__(self, message: str = "actor is not authorized") -> None:
        super().__init__("authorization_denied", message, status_code=403)


class NotFound(BrokerError):
    def __init__(self, resource: str) -> None:
        super().__init__("not_found", f"{resource} was not found", status_code=404)


class Conflict(BrokerError):
    def __init__(self, message: str) -> None:
        super().__init__("conflict", message, status_code=409)


class ApprovalRequired(BrokerError):
    def __init__(self, message: str = "a valid human approval is required") -> None:
        super().__init__("approval_required", message, status_code=409)


class BudgetExceeded(BrokerError):
    def __init__(self, action_id: str) -> None:
        super().__init__(
            "budget_exhausted",
            "the bounded-action budget is exhausted; escalation is required",
            status_code=429,
            details={"action_id": action_id, "decision": "escalate"},
        )
