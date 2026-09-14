from __future__ import annotations


class MemoryErrorBase(Exception):
    """Base error carrying a stable protocol code."""

    code = "memory_error"


class ConfigurationError(MemoryErrorBase):
    code = "configuration_error"


class AuthenticationError(MemoryErrorBase):
    code = "authentication_failed"


class AuthorizationError(MemoryErrorBase):
    code = "authorization_denied"


class ValidationError(MemoryErrorBase):
    code = "validation_error"


class SecretDetectedError(ValidationError):
    code = "secret_detected"


class NotFoundError(MemoryErrorBase):
    code = "not_found"


class ConflictError(MemoryErrorBase):
    code = "conflict"


class BackendUnavailableError(MemoryErrorBase):
    code = "backend_unavailable"


class ProtocolError(MemoryErrorBase):
    code = "protocol_error"
