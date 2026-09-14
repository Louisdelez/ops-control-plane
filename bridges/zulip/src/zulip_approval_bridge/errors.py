"""Exceptions with deliberately non-sensitive messages."""

from __future__ import annotations


class BridgeError(Exception):
    """Base class for bridge failures safe to write to the service journal."""


class ConfigurationError(BridgeError):
    """The process cannot start safely with the supplied configuration."""


class ExternalServiceRejected(BridgeError):
    """A remote service returned a permanent rejection."""


class BrokerRejected(BridgeError):
    """The broker safely refused an approval."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"the broker rejected the approval (HTTP {status_code})")


class RetryableBridgeError(BridgeError):
    """A transient failure; the current event must not be acknowledged."""


class QueueExpired(RetryableBridgeError):
    """Zulip garbage-collected the current long-poll queue."""


class ResourceNotFound(BridgeError):
    """A user or message referenced by an event no longer exists."""


class ProtocolError(RetryableBridgeError):
    """A trusted endpoint returned a malformed response."""
