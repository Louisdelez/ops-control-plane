class OrchestratorError(Exception):
    """Base class for errors safe to expose to a local client."""


class ConfigurationError(OrchestratorError):
    pass


class ValidationError(OrchestratorError):
    pass


class ProviderUnavailable(OrchestratorError):
    pass


class ProviderProtocolError(OrchestratorError):
    pass


class BudgetExceeded(OrchestratorError):
    def __init__(self, provider: str, period: str, limit: str):
        super().__init__(f"budget blocked for {provider}: {period} {limit}")
        self.provider = provider
        self.period = period
        self.limit = limit
