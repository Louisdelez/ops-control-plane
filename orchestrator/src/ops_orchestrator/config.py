from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import ConfigurationError
from .models import Role


ALLOWED_PROVIDER_KINDS = {
    "ollama_chat",
    "ollama_embedding",
    "openai_chat",
    "openai_responses",
    "anthropic_chat",
    "openai_embedding",
    "http_rerank",
}

ALIBABA_SHARED_BASE_URL = (
    "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
)


@dataclass(frozen=True)
class Limits:
    max_request_bytes: int
    max_section_chars: int
    max_context_chars: int
    max_context_tokens: int
    max_memories: int
    max_tool_results: int
    provider_timeout_seconds: float
    max_iterations: int
    max_provider_calls: int
    max_output_tokens: int
    max_response_bytes: int


@dataclass(frozen=True)
class Thresholds:
    high_confidence: float
    medium_confidence: float
    simple_complexity: float
    complex_complexity: float
    minimum_provider_confidence: float


@dataclass(frozen=True)
class BudgetLimits:
    daily_calls: int
    monthly_calls: int
    daily_tokens: int
    monthly_tokens: int
    daily_cost_microusd: int
    monthly_cost_microusd: int
    mission_cost_microusd: int


@dataclass(frozen=True)
class ProviderAccountBudget:
    """Hard limits shared by every deployment billed to one account."""

    account_id: str
    daily_calls: int
    monthly_calls: int
    daily_tokens: int
    monthly_tokens: int
    daily_cost_microusd: int
    monthly_cost_microusd: int
    task_calls: int
    task_tokens: int
    task_cost_microusd: int


@dataclass(frozen=True)
class PriceTier:
    max_input_tokens: int
    input_price_microusd_per_million: int
    output_price_microusd_per_million: int


@dataclass(frozen=True)
class RouteGrant:
    username: str
    projects: frozenset[str]
    remote_allowed: bool


@dataclass(frozen=True)
class ProviderConfig:
    provider_id: str
    account_id: str | None
    capability_profile: str
    role: Role
    kind: str
    location: str
    enabled: bool
    activation_env: str | None
    base_url: str | None
    base_url_env: str | None
    required_host_suffix: str | None
    required_path: str | None
    model: str | None
    model_env: str | None
    api_key_file_env: str | None
    request_path: str | None
    auth_scheme: str | None
    input_price_per_million: str | None
    input_price_env: str | None
    output_price_per_million: str | None
    output_price_env: str | None
    price_tiers: tuple[PriceTier, ...]
    chat_family: str | None
    chat_dialect: str | None
    thinking_mode: str | None
    budget: BudgetLimits
    keep_alive: str
    promotion_file: str | None
    promotion_slot: str | None

    def activated(self, environment: Mapping[str, str] | None = None) -> bool:
        environment = os.environ if environment is None else environment
        if not self.enabled:
            return False
        if self.activation_env is None:
            return True
        return environment.get(self.activation_env) == "1"

    def resolved_base_url(self, environment: Mapping[str, str] | None = None) -> str:
        environment = os.environ if environment is None else environment
        value = environment.get(self.base_url_env, "") if self.base_url_env else (self.base_url or "")
        value = value.rstrip("/")
        if not value:
            raise ConfigurationError(f"provider {self.provider_id} has no base URL")
        try:
            parsed = urlparse(value)
            hostname = parsed.hostname
            parsed.port
        except ValueError as exc:
            raise ConfigurationError(f"provider {self.provider_id} base URL is invalid") from exc
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ConfigurationError(f"provider {self.provider_id} base URL contains forbidden components")
        if self.location == "local":
            if parsed.scheme != "http" or hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ConfigurationError(f"local provider {self.provider_id} must use loopback HTTP")
        elif parsed.scheme != "https" or not hostname:
            raise ConfigurationError(f"remote provider {self.provider_id} must use HTTPS")
        if self.required_host_suffix and not (hostname or "").endswith(self.required_host_suffix):
            raise ConfigurationError(f"provider {self.provider_id} base URL host is outside its reviewed region")
        if self.required_path is not None and parsed.path != self.required_path:
            raise ConfigurationError(f"provider {self.provider_id} base URL path is not the reviewed API path")
        return value

    def resolved_model(self, environment: Mapping[str, str] | None = None) -> str:
        environment = os.environ if environment is None else environment
        value = environment.get(self.model_env, "") if self.model_env else (self.model or "")
        value = value.strip()
        if not value or len(value) > 200:
            raise ConfigurationError(f"provider {self.provider_id} has no valid model")
        return value

    def api_key_path(self, environment: Mapping[str, str] | None = None) -> Path | None:
        environment = os.environ if environment is None else environment
        if self.location == "local":
            return None
        if not self.api_key_file_env:
            raise ConfigurationError(f"remote provider {self.provider_id} has no credential file variable")
        raw = environment.get(self.api_key_file_env, "")
        if not raw or not os.path.isabs(raw):
            raise ConfigurationError(f"provider {self.provider_id} credential file is not configured")
        return Path(raw)

    def price_microusd_per_million(
        self,
        environment: Mapping[str, str] | None = None,
        *,
        input_tokens: int | None = None,
    ) -> tuple[int, int]:
        environment = os.environ if environment is None else environment
        if self.location == "local":
            return (0, 0)
        if self.price_tiers:
            if input_tokens is None:
                # Availability checks validate static tier configuration without
                # claiming that a specific request fits it.
                tier = self.price_tiers[0]
            else:
                if (
                    isinstance(input_tokens, bool)
                    or not isinstance(input_tokens, int)
                    or input_tokens < 0
                ):
                    raise ConfigurationError(
                        f"provider {self.provider_id} input token estimate is invalid"
                    )
                tier = next(
                    (
                        candidate
                        for candidate in self.price_tiers
                        if input_tokens <= candidate.max_input_tokens
                    ),
                    None,
                )
                if tier is None:
                    raise ConfigurationError(
                        f"provider {self.provider_id} input tokens exceed configured price tiers"
                    )
            return (
                tier.input_price_microusd_per_million,
                tier.output_price_microusd_per_million,
            )
        raw_input = environment.get(self.input_price_env, "") if self.input_price_env else self.input_price_per_million
        raw_output = environment.get(self.output_price_env, "") if self.output_price_env else self.output_price_per_million
        return (
            _usd_to_microusd(raw_input, f"{self.provider_id} input price", require_positive=True),
            _usd_to_microusd(raw_output, f"{self.provider_id} output price", require_positive=True),
        )

    def verify_promotion(self, environment: Mapping[str, str] | None = None) -> None:
        if self.location != "local":
            return
        if not self.promotion_file or not self.promotion_slot:
            raise ConfigurationError(f"local provider {self.provider_id} has no benchmark promotion gate")
        promotion_path = Path(self.promotion_file)
        try:
            info = promotion_path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size > 262144:
                raise ConfigurationError("promotion registry is not a bounded regular file")
            registry = json.loads(promotion_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"cannot read promotion registry for {self.provider_id}") from exc
        if not isinstance(registry, dict) or registry.get("schema_version") != 1 or registry.get("automatic_promotion") is not False:
            raise ConfigurationError("promotion registry header is invalid")
        promotions = registry.get("promotions")
        record = promotions.get(self.promotion_slot) if isinstance(promotions, dict) else None
        if not isinstance(record, dict):
            raise ConfigurationError(f"provider {self.provider_id} has not been promoted")
        expected = {
            "model", "eligible", "human_approved", "benchmark_report", "benchmark_sha256",
            "benchmark_generated_at", "approved_at", "approved_by",
        }
        if set(record) != expected:
            raise ConfigurationError(f"promotion record for {self.provider_id} is malformed")
        if record.get("model") != self.resolved_model(environment):
            raise ConfigurationError(f"promoted model does not match {self.provider_id}")
        if record.get("eligible") is not True or record.get("human_approved") is not True:
            raise ConfigurationError(f"provider {self.provider_id} did not pass promotion gates")
        digest = record.get("benchmark_sha256")
        report = record.get("benchmark_report")
        if not isinstance(digest, str) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ConfigurationError("promotion benchmark digest is invalid")
        if not isinstance(report, str) or not os.path.isabs(report):
            raise ConfigurationError("promotion benchmark report path is invalid")
        report_path = Path(report)
        try:
            report_info = report_path.lstat()
            if stat.S_ISLNK(report_info.st_mode) or not stat.S_ISREG(report_info.st_mode) or report_info.st_size > 10_000_000:
                raise ConfigurationError("benchmark report is not a bounded regular file")
            report_bytes = report_path.read_bytes()
            calculated = hashlib.sha256(report_bytes).hexdigest()
            report_data = json.loads(report_bytes.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError("promoted benchmark report is unavailable") from exc
        if calculated != digest:
            raise ConfigurationError("promoted benchmark report digest does not match")
        for field_name in ("benchmark_generated_at", "approved_at", "approved_by"):
            value = record.get(field_name)
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                raise ConfigurationError(f"promotion {field_name} is invalid")
        if not isinstance(report_data, dict) or report_data.get("schema_version") != 1:
            raise ConfigurationError("benchmark report schema is invalid")
        if report_data.get("generated_at") != record.get("benchmark_generated_at"):
            raise ConfigurationError("benchmark report timestamp does not match promotion")
        eligible = False
        models = report_data.get("models")
        if isinstance(models, list):
            campaign = report_data.get("campaign")
            inputs = report_data.get("inputs")
            campaign_complete = (
                isinstance(campaign, dict)
                and campaign.get("status") == "completed"
                and campaign.get("complete") is True
                and campaign.get("interruption_reason") is None
                and isinstance(inputs, dict)
                and isinstance(inputs.get("configured_candidate_count"), int)
                and not isinstance(inputs.get("configured_candidate_count"), bool)
                and inputs.get("configured_candidate_count") > 0
                and isinstance(inputs.get("evaluated_candidate_count"), int)
                and not isinstance(inputs.get("evaluated_candidate_count"), bool)
                and inputs.get("configured_candidate_count")
                == inputs.get("evaluated_candidate_count")
                and inputs.get("configured_candidate_count") == len(models)
            )
            matching = [item for item in models if isinstance(item, dict) and item.get("model") == record.get("model")]
            if len(matching) == 1 and isinstance(matching[0].get("gate"), dict):
                gate = matching[0]["gate"]
                eligible = (
                    campaign_complete
                    and matching[0].get("status") == "completed"
                    and gate.get("promotion_eligible") is True
                    and gate.get("promotion_performed") is False
                )
            selection = report_data.get("selection")
            if (
                not isinstance(selection, dict)
                or selection.get("automatic_promotion") is not False
                or selection.get("promotion_performed") is not False
                or selection.get("requires_human_approval") is not True
                or not isinstance(selection.get("eligible_models"), list)
                or record.get("model") not in selection.get("eligible_models", [])
            ):
                eligible = False
        elif (
            self.kind in {"ollama_embedding", "http_rerank"}
            and set(report_data)
            == {
                "schema_version",
                "generated_at",
                "model",
                "promotion_eligible",
                "automatic_promotion",
                "promotion_performed",
            }
            and report_data.get("model") == record.get("model")
        ):
            eligible = (
                report_data.get("promotion_eligible") is True
                and report_data.get("automatic_promotion") is False
                and report_data.get("promotion_performed") is False
            )
        if not eligible:
            raise ConfigurationError(f"benchmark report does not make {self.provider_id} eligible")


@dataclass(frozen=True)
class AppConfig:
    database_path: Path
    socket_path: Path
    finance_socket_path: Path
    finance_socket_user: str
    finance_socket_group: str
    finance_client_user: str
    socket_group: str
    metrics_path: Path
    catalogue_path: Path
    provider_integrations_path: Path
    catalogue_auxiliary_deployment_ids: frozenset[str]
    catalogue_runtime_enabled: bool
    catalogue_runtime_account_budget: ProviderAccountBudget
    catalogue_runtime_bindings: dict[str, str]
    catalogue_runtime_reasons: dict[str, str]
    route_grants: dict[str, RouteGrant]
    provider_account_budgets: dict[str, ProviderAccountBudget]
    limits: Limits
    thresholds: Thresholds
    providers_by_role: dict[Role, tuple[ProviderConfig, ...]]

    @property
    def providers(self) -> tuple[ProviderConfig, ...]:
        return tuple(provider for role in Role for provider in self.providers_by_role[role])

    def provider(self, provider_id: str) -> ProviderConfig:
        for provider in self.providers:
            if provider.provider_id == provider_id:
                return provider
        raise ConfigurationError(f"unknown provider: {provider_id}")

    def route_grant(self, username: str, project_id: str) -> RouteGrant | None:
        grant = self.route_grants.get(username)
        if grant is None or project_id not in grant.projects:
            return None
        return grant

    def provider_account_budget(self, account_id: str) -> ProviderAccountBudget:
        try:
            return self.provider_account_budgets[account_id]
        except KeyError as exc:
            raise ConfigurationError(f"unknown provider account budget: {account_id}") from exc


def _strict_keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unexpected = sorted(set(value) - allowed)
    if unexpected:
        raise ConfigurationError(f"unknown {label} fields: {', '.join(unexpected)}")


def _system_identity(value: Any, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-z_][a-z0-9_-]{0,63}", value
    ):
        raise ConfigurationError(f"{label} is invalid")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigurationError(f"{label} must be an integer between {minimum} and {maximum}")
    return value


def _number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{label} must be numeric")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ConfigurationError(f"{label} must be between {minimum} and {maximum}")
    return result


def _usd_to_microusd(value: Any, label: str, *, require_positive: bool = False) -> int:
    if not isinstance(value, str):
        raise ConfigurationError(f"{label} must be a decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ConfigurationError(f"{label} is not a decimal") from exc
    if not amount.is_finite():
        raise ConfigurationError(f"{label} must be finite")
    minimum = Decimal("0.000001") if require_positive else Decimal("0")
    if amount < minimum or amount > Decimal("1000000"):
        raise ConfigurationError(f"{label} is outside the accepted range")
    scaled = amount * Decimal(1_000_000)
    if scaled != scaled.to_integral_value():
        raise ConfigurationError(f"{label} supports at most six decimal places")
    return int(scaled)


def _path(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.startswith("/") or "\x00" in value:
        raise ConfigurationError(f"{label} must be an absolute path")
    return Path(value)


def _env_name(value: Any, label: str, *, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or not value.replace("_", "A").isalnum() or not value[0].isalpha():
        raise ConfigurationError(f"{label} must be an environment variable name")
    return value


_UNIX_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _parse_route_grants(value: Any) -> dict[str, RouteGrant]:
    if not isinstance(value, list) or not value or len(value) > 64:
        raise ConfigurationError("access.route_grants must be a non-empty bounded list")
    result: dict[str, RouteGrant] = {}
    for index, entry in enumerate(value):
        label = f"access.route_grants[{index}]"
        if not isinstance(entry, dict):
            raise ConfigurationError(f"{label} must be an object")
        _strict_keys(entry, {"user", "projects", "remote_allowed"}, label)
        username = entry.get("user")
        projects = entry.get("projects")
        remote_allowed = entry.get("remote_allowed")
        if not isinstance(username, str) or not _UNIX_USER_RE.fullmatch(username):
            raise ConfigurationError(f"{label}.user is invalid")
        if username in result:
            raise ConfigurationError(f"duplicate route grant for user {username}")
        if (
            not isinstance(projects, list)
            or not projects
            or len(projects) > 32
            or any(not isinstance(project, str) or not _PROJECT_RE.fullmatch(project) for project in projects)
            or len(set(projects)) != len(projects)
        ):
            raise ConfigurationError(f"{label}.projects is invalid")
        if not isinstance(remote_allowed, bool):
            raise ConfigurationError(f"{label}.remote_allowed must be boolean")
        result[username] = RouteGrant(username, frozenset(projects), remote_allowed)
    return result


def _parse_budget(value: Any, label: str) -> BudgetLimits:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{label} must be an object")
    _strict_keys(
        value,
        {
            "daily_calls", "monthly_calls", "daily_tokens", "monthly_tokens",
            "daily_cost_usd", "monthly_cost_usd", "mission_cost_usd",
        },
        label,
    )
    daily_calls = _integer(value.get("daily_calls"), f"{label}.daily_calls", 1, 1_000_000)
    monthly_calls = _integer(value.get("monthly_calls"), f"{label}.monthly_calls", daily_calls, 10_000_000)
    daily_tokens = _integer(value.get("daily_tokens"), f"{label}.daily_tokens", 1, 10_000_000_000)
    monthly_tokens = _integer(value.get("monthly_tokens"), f"{label}.monthly_tokens", daily_tokens, 100_000_000_000)
    daily_cost = _usd_to_microusd(value.get("daily_cost_usd"), f"{label}.daily_cost_usd")
    monthly_cost = _usd_to_microusd(value.get("monthly_cost_usd"), f"{label}.monthly_cost_usd")
    mission_cost = _usd_to_microusd(value.get("mission_cost_usd"), f"{label}.mission_cost_usd")
    if monthly_cost < daily_cost or mission_cost > monthly_cost:
        raise ConfigurationError(f"{label} cost limits are inconsistent")
    return BudgetLimits(
        daily_calls=daily_calls,
        monthly_calls=monthly_calls,
        daily_tokens=daily_tokens,
        monthly_tokens=monthly_tokens,
        daily_cost_microusd=daily_cost,
        monthly_cost_microusd=monthly_cost,
        mission_cost_microusd=mission_cost,
    )


def _parse_provider_account_budgets(value: Any) -> dict[str, ProviderAccountBudget]:
    if not isinstance(value, list) or not value or len(value) > 64:
        raise ConfigurationError("provider_accounts must be a non-empty bounded list")
    result: dict[str, ProviderAccountBudget] = {}
    for index, entry in enumerate(value):
        label = f"provider_accounts[{index}]"
        if not isinstance(entry, dict):
            raise ConfigurationError(f"{label} must be an object")
        _strict_keys(entry, {"id", "budget"}, label)
        account_id = entry.get("id")
        if (
            not isinstance(account_id, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", account_id)
        ):
            raise ConfigurationError(f"{label}.id is invalid")
        if account_id in result:
            raise ConfigurationError(f"duplicate provider account budget: {account_id}")
        budget = entry.get("budget")
        if not isinstance(budget, dict):
            raise ConfigurationError(f"{label}.budget must be an object")
        _strict_keys(
            budget,
            {
                "daily_calls",
                "monthly_calls",
                "daily_tokens",
                "monthly_tokens",
                "daily_cost_usd",
                "monthly_cost_usd",
                "task_calls",
                "task_tokens",
                "task_cost_usd",
            },
            f"{label}.budget",
        )
        daily_calls = _integer(
            budget.get("daily_calls"), f"{label}.budget.daily_calls", 1, 1_000_000
        )
        monthly_calls = _integer(
            budget.get("monthly_calls"),
            f"{label}.budget.monthly_calls",
            daily_calls,
            10_000_000,
        )
        daily_tokens = _integer(
            budget.get("daily_tokens"),
            f"{label}.budget.daily_tokens",
            1,
            10_000_000_000,
        )
        monthly_tokens = _integer(
            budget.get("monthly_tokens"),
            f"{label}.budget.monthly_tokens",
            daily_tokens,
            100_000_000_000,
        )
        task_calls = _integer(
            budget.get("task_calls"), f"{label}.budget.task_calls", 1, 12
        )
        task_tokens = _integer(
            budget.get("task_tokens"),
            f"{label}.budget.task_tokens",
            1,
            1_000_000,
        )
        daily_cost = _usd_to_microusd(
            budget.get("daily_cost_usd"), f"{label}.budget.daily_cost_usd"
        )
        monthly_cost = _usd_to_microusd(
            budget.get("monthly_cost_usd"), f"{label}.budget.monthly_cost_usd"
        )
        task_cost = _usd_to_microusd(
            budget.get("task_cost_usd"), f"{label}.budget.task_cost_usd"
        )
        if monthly_cost < daily_cost or task_cost > monthly_cost:
            raise ConfigurationError(f"{label}.budget cost limits are inconsistent")
        if task_calls > daily_calls or task_tokens > daily_tokens:
            raise ConfigurationError(f"{label}.budget task limits are inconsistent")
        result[account_id] = ProviderAccountBudget(
            account_id=account_id,
            daily_calls=daily_calls,
            monthly_calls=monthly_calls,
            daily_tokens=daily_tokens,
            monthly_tokens=monthly_tokens,
            daily_cost_microusd=daily_cost,
            monthly_cost_microusd=monthly_cost,
            task_calls=task_calls,
            task_tokens=task_tokens,
            task_cost_microusd=task_cost,
        )
    return result


def _parse_price_tiers(value: Any, label: str) -> tuple[PriceTier, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not value or len(value) > 32:
        raise ConfigurationError(f"{label} must be a non-empty bounded list")
    tiers: list[PriceTier] = []
    previous_maximum = 0
    previous_input_price = 0
    previous_output_price = 0
    for index, entry in enumerate(value):
        entry_label = f"{label}[{index}]"
        if not isinstance(entry, dict):
            raise ConfigurationError(f"{entry_label} must be an object")
        _strict_keys(
            entry,
            {
                "max_input_tokens",
                "input_price_per_million_usd",
                "output_price_per_million_usd",
            },
            entry_label,
        )
        maximum = _integer(
            entry.get("max_input_tokens"),
            f"{entry_label}.max_input_tokens",
            1,
            10_000_000_000,
        )
        if maximum <= previous_maximum:
            raise ConfigurationError(f"{label} max_input_tokens must be strictly increasing")
        input_price = _usd_to_microusd(
            entry.get("input_price_per_million_usd"),
            f"{entry_label}.input_price_per_million_usd",
            require_positive=True,
        )
        output_price = _usd_to_microusd(
            entry.get("output_price_per_million_usd"),
            f"{entry_label}.output_price_per_million_usd",
            require_positive=True,
        )
        if input_price < previous_input_price or output_price < previous_output_price:
            raise ConfigurationError(f"{label} prices must be non-decreasing")
        tiers.append(
            PriceTier(
                max_input_tokens=maximum,
                input_price_microusd_per_million=input_price,
                output_price_microusd_per_million=output_price,
            )
        )
        previous_maximum = maximum
        previous_input_price = input_price
        previous_output_price = output_price
    return tuple(tiers)


def _parse_provider(value: Any, role: Role) -> ProviderConfig:
    if not isinstance(value, dict):
        raise ConfigurationError(f"provider for {role.value} must be an object")
    allowed = {
        "id", "account_id", "capability_profile", "kind", "location", "enabled", "activation_env", "base_url", "base_url_env",
        "required_host_suffix", "required_path",
        "model", "model_env", "api_key_file_env", "input_price_per_million_usd",
        "input_price_env", "output_price_per_million_usd", "output_price_env", "price_tiers", "budget",
        "keep_alive", "chat_family", "chat_dialect", "thinking_mode",
        "promotion_file", "promotion_slot", "request_path", "auth_scheme",
    }
    _strict_keys(value, allowed, f"provider {role.value}")
    provider_id = value.get("id")
    if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 64:
        raise ConfigurationError("provider id is invalid")
    account_id = value.get("account_id")
    if account_id is not None and (
        not isinstance(account_id, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", account_id)
    ):
        raise ConfigurationError(f"provider {provider_id} account_id is invalid")
    capability_profile = value.get("capability_profile")
    if (
        not isinstance(capability_profile, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,127}", capability_profile)
    ):
        raise ConfigurationError(f"provider {provider_id} capability_profile is invalid")
    kind = value.get("kind")
    if kind not in ALLOWED_PROVIDER_KINDS:
        raise ConfigurationError(f"provider {provider_id} kind is invalid")
    location = value.get("location")
    if location not in {"local", "remote"}:
        raise ConfigurationError(f"provider {provider_id} location is invalid")
    enabled = value.get("enabled")
    if not isinstance(enabled, bool):
        raise ConfigurationError(f"provider {provider_id} enabled must be boolean")
    base_url = value.get("base_url")
    if base_url is not None and not isinstance(base_url, str):
        raise ConfigurationError(f"provider {provider_id} base_url must be a string")
    required_host_suffix = value.get("required_host_suffix")
    if (
        required_host_suffix is not None
        and (
            not isinstance(required_host_suffix, str)
            or not required_host_suffix.startswith(".")
            or len(required_host_suffix) > 253
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-." for character in required_host_suffix)
        )
    ):
        raise ConfigurationError(f"provider {provider_id} required_host_suffix is invalid")
    required_path = value.get("required_path")
    if (
        required_path is not None
        and (
            not isinstance(required_path, str)
            or not required_path.startswith("/")
            or "//" in required_path
            or len(required_path) > 256
        )
    ):
        raise ConfigurationError(f"provider {provider_id} required_path is invalid")
    model = value.get("model")
    if model is not None and not isinstance(model, str):
        raise ConfigurationError(f"provider {provider_id} model must be a string")
    keep_alive = value.get("keep_alive", "0")
    if not isinstance(keep_alive, str) or len(keep_alive) > 16:
        raise ConfigurationError(f"provider {provider_id} keep_alive is invalid")
    provider = ProviderConfig(
        provider_id=provider_id,
        account_id=account_id,
        capability_profile=capability_profile,
        role=role,
        kind=kind,
        location=location,
        enabled=enabled,
        activation_env=_env_name(value.get("activation_env"), f"{provider_id}.activation_env"),
        base_url=base_url,
        base_url_env=_env_name(value.get("base_url_env"), f"{provider_id}.base_url_env"),
        required_host_suffix=required_host_suffix,
        required_path=required_path,
        model=model,
        model_env=_env_name(value.get("model_env"), f"{provider_id}.model_env"),
        api_key_file_env=_env_name(value.get("api_key_file_env"), f"{provider_id}.api_key_file_env"),
        request_path=value.get("request_path"),
        auth_scheme=value.get("auth_scheme"),
        input_price_per_million=value.get("input_price_per_million_usd"),
        input_price_env=_env_name(value.get("input_price_env"), f"{provider_id}.input_price_env"),
        output_price_per_million=value.get("output_price_per_million_usd"),
        output_price_env=_env_name(value.get("output_price_env"), f"{provider_id}.output_price_env"),
        price_tiers=_parse_price_tiers(value.get("price_tiers"), f"{provider_id}.price_tiers"),
        chat_family=value.get("chat_family"),
        chat_dialect=value.get("chat_dialect"),
        thinking_mode=value.get("thinking_mode"),
        budget=_parse_budget(value.get("budget"), f"{provider_id}.budget"),
        keep_alive=keep_alive,
        promotion_file=value.get("promotion_file"),
        promotion_slot=value.get("promotion_slot"),
    )
    # Validate static settings now. Dynamic environment settings are validated at use time.
    if provider.base_url is not None and provider.base_url_env is None:
        provider.resolved_base_url({})
    if provider.model is not None and provider.model_env is None:
        provider.resolved_model({})
    flat_or_environment_prices = any(
        item is not None
        for item in (
            provider.input_price_per_million,
            provider.input_price_env,
            provider.output_price_per_million,
            provider.output_price_env,
        )
    )
    if provider.price_tiers and flat_or_environment_prices:
        raise ConfigurationError(
            f"provider {provider_id} price_tiers are mutually exclusive with flat or environment prices"
        )
    if location == "local" and provider.price_tiers:
        raise ConfigurationError(f"local provider {provider_id} must not declare API price tiers")
    if (
        location == "remote"
        and provider.input_price_env is None
        and provider.output_price_env is None
    ):
        provider.price_microusd_per_million({})
    if location == "remote" and not provider.api_key_file_env:
        raise ConfigurationError(f"remote provider {provider_id} requires api_key_file_env")
    if provider.request_path is not None and (
        not isinstance(provider.request_path, str)
        or not provider.request_path.startswith("/")
        or "//" in provider.request_path
        or len(provider.request_path) > 256
    ):
        raise ConfigurationError(f"provider {provider_id} request_path is invalid")
    if provider.auth_scheme not in {None, "bearer", "google_api_key", "anthropic"}:
        raise ConfigurationError(f"provider {provider_id} auth_scheme is invalid")
    if kind == "anthropic_chat" and (
        provider.auth_scheme != "anthropic" or provider.request_path != "/v1/messages"
    ):
        raise ConfigurationError(
            f"provider {provider_id} anthropic adapter contract is incomplete"
        )
    if kind == "openai_chat" and provider.auth_scheme == "anthropic":
        raise ConfigurationError(f"provider {provider_id} auth scheme does not match its adapter")
    if kind == "openai_responses" and (
        provider.auth_scheme != "bearer"
        or provider.request_path != "/responses"
        or provider.chat_family != "openai"
    ):
        raise ConfigurationError(
            f"provider {provider_id} OpenAI Responses adapter contract is incomplete"
        )
    if kind.startswith("ollama_") and location != "local":
        raise ConfigurationError(f"Ollama provider {provider_id} must be local")
    if provider.chat_family is not None and (
        not isinstance(provider.chat_family, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", provider.chat_family)
    ):
        raise ConfigurationError(f"provider {provider_id} chat_family is invalid")
    if provider.chat_dialect not in {
        None, "generic", "alibaba", "deepseek", "moonshot_k3", "openai_reasoning"
    }:
        raise ConfigurationError(f"provider {provider_id} chat_dialect is invalid")
    if provider.thinking_mode not in {None, "enabled", "disabled"}:
        raise ConfigurationError(f"provider {provider_id} thinking_mode is invalid")
    if kind not in {"openai_chat", "openai_responses", "anthropic_chat"} and (
        provider.chat_family is not None
        or provider.chat_dialect is not None
        or provider.thinking_mode is not None
    ):
        raise ConfigurationError(f"provider {provider_id} chat options require openai_chat")
    if provider.chat_family in {"qwen", "deepseek"} and provider.chat_dialect not in {
        "alibaba",
        "deepseek",
    }:
        raise ConfigurationError(
            f"provider {provider_id} reviewed chat_family requires an explicit provider dialect"
        )
    if provider.thinking_mode is not None and provider.chat_dialect not in {"alibaba", "deepseek"}:
        raise ConfigurationError(
            f"provider {provider_id} thinking_mode requires alibaba or deepseek chat_dialect"
        )
    if provider.chat_dialect in {"alibaba", "deepseek"} and provider.thinking_mode is None:
        raise ConfigurationError(
            f"provider {provider_id} reviewed chat_dialect requires an explicit thinking_mode"
        )
    if provider.chat_dialect == "alibaba" and provider.chat_family not in {"qwen", "deepseek"}:
        raise ConfigurationError(
            f"provider {provider_id} alibaba chat_dialect requires a reviewed model family"
        )
    if provider.chat_dialect == "alibaba" and (
        provider.base_url != ALIBABA_SHARED_BASE_URL
        or provider.base_url_env is not None
        or provider.required_host_suffix is not None
        or provider.required_path != "/compatible-mode/v1"
    ):
        raise ConfigurationError(
            f"provider {provider_id} alibaba chat_dialect requires the exact reviewed shared endpoint"
        )
    if provider.chat_dialect == "deepseek" and provider.chat_family != "deepseek":
        raise ConfigurationError(
            f"provider {provider_id} deepseek chat_dialect requires deepseek chat_family"
        )
    if provider.chat_dialect == "moonshot_k3" and (
        provider.chat_family != "moonshot" or provider.model != "kimi-k3"
    ):
        raise ConfigurationError(
            f"provider {provider_id} Moonshot K3 dialect requires the exact reviewed model"
        )
    if provider.chat_dialect == "openai_reasoning" and provider.chat_family != "openai":
        raise ConfigurationError(
            f"provider {provider_id} OpenAI reasoning dialect requires the OpenAI family"
        )
    if provider.chat_dialect == "deepseek" and (
        provider.base_url != "https://api.deepseek.com"
        or provider.base_url_env is not None
        or provider.required_host_suffix is not None
        or provider.required_path is not None
    ):
        raise ConfigurationError(
            f"provider {provider_id} deepseek chat_dialect requires the reviewed direct endpoint"
        )
    chat_roles = {
        Role.TINY,
        Role.LOCAL_OPS,
        Role.REASONING,
        Role.PREMIUM,
        Role.CODER,
    }
    if role in chat_roles and kind not in {
        "ollama_chat", "openai_chat", "openai_responses", "anthropic_chat"
    }:
        raise ConfigurationError(f"{role.value} provider {provider_id} must be a chat provider")
    if role is Role.EMBEDDING and kind not in {"ollama_embedding", "openai_embedding"}:
        raise ConfigurationError(f"{role.value} provider {provider_id} must be an embedding provider")
    if role is Role.RERANKER and kind != "http_rerank":
        raise ConfigurationError(f"{role.value} provider {provider_id} must be a reranker")
    if location == "local":
        if not isinstance(provider.promotion_file, str) or not provider.promotion_file.startswith("/"):
            raise ConfigurationError(f"local provider {provider_id} requires an absolute promotion_file")
        if not isinstance(provider.promotion_slot, str) or not provider.promotion_slot or len(provider.promotion_slot) > 64:
            raise ConfigurationError(f"local provider {provider_id} requires promotion_slot")
    elif provider.promotion_file is not None or provider.promotion_slot is not None:
        raise ConfigurationError(f"remote provider {provider_id} must not use a local promotion gate")
    return provider


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    try:
        info = config_path.lstat()
    except OSError as exc:
        raise ConfigurationError(f"cannot stat configuration: {exc}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfigurationError("configuration must be a regular non-symlink file")
    if info.st_size > 1_000_000:
        raise ConfigurationError("configuration is too large")
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot read configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("configuration root must be an object")
    _strict_keys(
        raw,
        {
            "schema_version",
            "paths",
            "access",
            "provider_accounts",
            "catalogue_auxiliary_deployments",
            "catalogue_runtime",
            "limits",
            "thresholds",
            "roles",
        },
        "configuration",
    )
    if raw.get("schema_version") != 1:
        raise ConfigurationError("unsupported configuration schema")
    paths = raw.get("paths")
    access = raw.get("access")
    provider_accounts = raw.get("provider_accounts")
    limits = raw.get("limits")
    thresholds = raw.get("thresholds")
    roles = raw.get("roles")
    if not all(isinstance(item, dict) for item in (paths, access, limits, thresholds, roles)):
        raise ConfigurationError("paths, access, limits, thresholds and roles must be objects")
    _strict_keys(
        paths,
        {
            "database", "socket", "finance_socket", "finance_socket_user",
            "finance_socket_group", "finance_client_user", "socket_group", "metrics", "model_catalogue",
            "provider_integrations",
        },
        "paths",
    )
    _strict_keys(access, {"route_grants"}, "access")
    route_grants = _parse_route_grants(access.get("route_grants"))
    provider_account_budgets = _parse_provider_account_budgets(provider_accounts)
    catalogue_runtime = raw.get("catalogue_runtime")
    if not isinstance(catalogue_runtime, dict):
        raise ConfigurationError("catalogue_runtime must be an object")
    _strict_keys(catalogue_runtime, {"enabled", "account_budget"}, "catalogue_runtime")
    catalogue_runtime_enabled = catalogue_runtime.get("enabled")
    if not isinstance(catalogue_runtime_enabled, bool):
        raise ConfigurationError("catalogue_runtime.enabled must be boolean")
    catalogue_runtime_budget = _parse_provider_account_budgets(
        [{"id": "catalogue-runtime", "budget": catalogue_runtime.get("account_budget")}]
    )["catalogue-runtime"]
    _strict_keys(
        limits,
        {
            "max_request_bytes", "max_section_chars", "max_context_chars", "max_context_tokens",
            "max_memories", "max_tool_results", "provider_timeout_seconds", "max_iterations",
            "max_provider_calls", "max_output_tokens", "max_response_bytes",
        },
        "limits",
    )
    parsed_limits = Limits(
        max_request_bytes=_integer(limits.get("max_request_bytes"), "max_request_bytes", 1024, 1_048_576),
        max_section_chars=_integer(limits.get("max_section_chars"), "max_section_chars", 128, 65536),
        max_context_chars=_integer(limits.get("max_context_chars"), "max_context_chars", 512, 262144),
        max_context_tokens=_integer(limits.get("max_context_tokens"), "max_context_tokens", 128, 65536),
        max_memories=_integer(limits.get("max_memories"), "max_memories", 0, 64),
        max_tool_results=_integer(limits.get("max_tool_results"), "max_tool_results", 0, 64),
        provider_timeout_seconds=_number(limits.get("provider_timeout_seconds"), "provider_timeout_seconds", 1, 300),
        max_iterations=_integer(limits.get("max_iterations"), "max_iterations", 1, 12),
        max_provider_calls=_integer(limits.get("max_provider_calls"), "max_provider_calls", 1, 12),
        max_output_tokens=_integer(limits.get("max_output_tokens"), "max_output_tokens", 32, 4096),
        max_response_bytes=_integer(limits.get("max_response_bytes"), "max_response_bytes", 1024, 4_194_304),
    )
    if parsed_limits.max_context_chars < parsed_limits.max_section_chars:
        raise ConfigurationError("max_context_chars must not be below max_section_chars")
    _strict_keys(
        thresholds,
        {"high_confidence", "medium_confidence", "simple_complexity", "complex_complexity", "minimum_provider_confidence"},
        "thresholds",
    )
    parsed_thresholds = Thresholds(
        high_confidence=_number(thresholds.get("high_confidence"), "high_confidence", 0, 1),
        medium_confidence=_number(thresholds.get("medium_confidence"), "medium_confidence", 0, 1),
        simple_complexity=_number(thresholds.get("simple_complexity"), "simple_complexity", 0, 1),
        complex_complexity=_number(thresholds.get("complex_complexity"), "complex_complexity", 0, 1),
        minimum_provider_confidence=_number(thresholds.get("minimum_provider_confidence"), "minimum_provider_confidence", 0, 1),
    )
    if not parsed_thresholds.medium_confidence < parsed_thresholds.high_confidence:
        raise ConfigurationError("confidence thresholds are inconsistent")
    if not parsed_thresholds.simple_complexity < parsed_thresholds.complex_complexity:
        raise ConfigurationError("complexity thresholds are inconsistent")
    expected_roles = {role.value for role in Role}
    if set(roles) != expected_roles:
        missing = sorted(expected_roles - set(roles))
        extra = sorted(set(roles) - expected_roles)
        raise ConfigurationError(f"roles must be exact; missing={missing}, extra={extra}")
    providers_by_role: dict[Role, tuple[ProviderConfig, ...]] = {}
    provider_ids: set[str] = set()
    for role in Role:
        entries = roles[role.value]
        if not isinstance(entries, list) or not entries:
            raise ConfigurationError(f"{role.value} must contain at least one provider")
        parsed = tuple(_parse_provider(entry, role) for entry in entries)
        for provider in parsed:
            if provider.provider_id in provider_ids:
                raise ConfigurationError(f"duplicate provider id: {provider.provider_id}")
            provider_ids.add(provider.provider_id)
        providers_by_role[role] = parsed
    raw_auxiliary_deployments = raw.get("catalogue_auxiliary_deployments")
    if (
        not isinstance(raw_auxiliary_deployments, list)
        or len(raw_auxiliary_deployments) > 32
        or any(
            not isinstance(item, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", item)
            for item in raw_auxiliary_deployments
        )
        or len(set(raw_auxiliary_deployments)) != len(raw_auxiliary_deployments)
    ):
        raise ConfigurationError(
            "catalogue_auxiliary_deployments must be a unique bounded identifier list"
        )
    auxiliary_deployment_ids = frozenset(raw_auxiliary_deployments)
    unknown_auxiliary_deployments = sorted(auxiliary_deployment_ids - provider_ids)
    if unknown_auxiliary_deployments:
        raise ConfigurationError(
            "catalogue auxiliary deployments are not configured providers: "
            + ", ".join(unknown_auxiliary_deployments)
        )
    referenced_account_ids = {
        provider.account_id
        for providers in providers_by_role.values()
        for provider in providers
        if provider.account_id is not None
    }
    configured_account_ids = set(provider_account_budgets)
    if referenced_account_ids != configured_account_ids:
        missing = sorted(referenced_account_ids - configured_account_ids)
        unused = sorted(configured_account_ids - referenced_account_ids)
        raise ConfigurationError(
            f"provider account budgets must exactly match references; missing={missing}, unused={unused}"
        )
    socket_group = _system_identity(paths.get("socket_group"), "socket_group")
    return AppConfig(
        database_path=_path(paths.get("database"), "database path"),
        socket_path=_path(paths.get("socket"), "socket path"),
        finance_socket_path=_path(
            paths.get("finance_socket"), "provider finance socket path"
        ),
        finance_socket_user=_system_identity(
            paths.get("finance_socket_user"), "finance_socket_user"
        ),
        finance_socket_group=_system_identity(
            paths.get("finance_socket_group"), "finance_socket_group"
        ),
        finance_client_user=_system_identity(
            paths.get("finance_client_user"), "finance_client_user"
        ),
        socket_group=socket_group,
        metrics_path=_path(paths.get("metrics"), "metrics path"),
        catalogue_path=_path(paths.get("model_catalogue"), "model catalogue path"),
        provider_integrations_path=_path(
            paths.get("provider_integrations"), "provider integrations path"
        ),
        catalogue_auxiliary_deployment_ids=auxiliary_deployment_ids,
        catalogue_runtime_enabled=catalogue_runtime_enabled,
        catalogue_runtime_account_budget=catalogue_runtime_budget,
        catalogue_runtime_bindings={},
        catalogue_runtime_reasons={},
        route_grants=route_grants,
        provider_account_budgets=provider_account_budgets,
        limits=parsed_limits,
        thresholds=parsed_thresholds,
        providers_by_role=providers_by_role,
    )


def expand_catalogue_runtime(
    config: AppConfig,
    *,
    catalogue_path: Path | None = None,
    provider_integrations_path: Path | None = None,
) -> AppConfig:
    """Return an idempotently expanded config using only reviewed API adapters."""

    if not config.catalogue_runtime_enabled or config.catalogue_runtime_bindings:
        return config
    from .catalogue_runtime import build_catalogue_runtime_plan

    plan = build_catalogue_runtime_plan(
        catalogue_path or config.catalogue_path,
        provider_integrations_path or config.provider_integrations_path,
    )
    existing_ids = {provider.provider_id for provider in config.providers}
    expanded: dict[Role, tuple[ProviderConfig, ...]] = {}
    for role in Role:
        additions = tuple(
            _parse_provider(raw_provider, role)
            for raw_provider in plan.providers_by_role.get(role.value, ())
        )
        duplicate = sorted(
            provider.provider_id for provider in additions if provider.provider_id in existing_ids
        )
        if duplicate:
            raise ConfigurationError(
                "generated catalogue deployment collides with configured provider: "
                + ", ".join(duplicate)
            )
        existing_ids.update(provider.provider_id for provider in additions)
        expanded[role] = config.providers_by_role[role] + additions
    referenced_accounts = {
        provider.account_id for providers in expanded.values() for provider in providers
        if provider.account_id is not None
    }
    expanded_budgets = dict(config.provider_account_budgets)
    for account_id in sorted(referenced_accounts - set(expanded_budgets)):
        expanded_budgets[account_id] = replace(
            config.catalogue_runtime_account_budget, account_id=account_id
        )
    return replace(
        config,
        providers_by_role=expanded,
        provider_account_budgets=expanded_budgets,
        catalogue_runtime_bindings=dict(plan.bindings),
        catalogue_runtime_reasons=dict(plan.ineligibility),
    )
