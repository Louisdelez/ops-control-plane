from __future__ import annotations

import os
import pwd
import stat
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from .errors import ConfigurationError


CLASSIFICATIONS = ("public", "internal", "restricted", "confidential")
MEMORY_KINDS = (
    "noise",
    "conversation",
    "observation",
    "information",
    "decision",
    "rule",
    "procedure",
    "incident",
    "summary",
)
TIERS = ("working", "cold")


def classification_rank(value: str) -> int:
    try:
        return CLASSIFICATIONS.index(value)
    except ValueError as exc:
        raise ConfigurationError(f"unknown classification: {value}") from exc


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{name} must be a non-empty string")
    return value.strip()


def _boolean(value: object, name: str) -> bool:
    """Parse TOML booleans without accepting truthy strings or integers."""
    if type(value) is not bool:
        raise ConfigurationError(f"{name} must be a boolean")
    return value


def _table(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{name} must be a table")
    return value


def _string_tuple(value: object, name: str, *, nonempty: bool = True) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ConfigurationError(f"{name} must be a list of non-empty strings")
    if nonempty and not value:
        raise ConfigurationError(f"{name} must not be empty")
    if len(value) != len(set(value)):
        raise ConfigurationError(f"{name} contains duplicates")
    return tuple(value)


def _secure_url(value: str, name: str, *, loopback_only: bool = False) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError(f"{name} must be an http(s) URL")
    is_loopback = parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if loopback_only and not is_loopback:
        raise ConfigurationError(f"{name} must use a loopback host")
    if parsed.scheme != "https" and not is_loopback:
        raise ConfigurationError(f"{name} must use HTTPS outside loopback")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigurationError(f"{name} must not contain credentials, query or fragment")
    return value.rstrip("/")


@dataclass(frozen=True)
class ActorPolicy:
    name: str
    unix_users: tuple[str, ...]
    roles: tuple[str, ...]
    grantable_roles: tuple[str, ...]
    projects: tuple[str, ...]
    environments: tuple[str, ...]
    max_classification: str
    capabilities: frozenset[str]
    source_types: tuple[str, ...] = ()
    authoritative_source_types: tuple[str, ...] = ()

    @property
    def max_classification_rank(self) -> int:
        return classification_rank(self.max_classification)

    def resolved_uids(self) -> frozenset[int]:
        result: set[int] = set()
        for username in self.unix_users:
            try:
                result.add(pwd.getpwnam(username).pw_uid)
            except KeyError:
                continue
        return frozenset(result)

    def permits_scope(self, project: str, environment: str, classification: str) -> bool:
        project_ok = project in self.projects or "*" in self.projects
        environment_ok = environment in self.environments or "*" in self.environments
        return (
            project_ok
            and environment_ok
            and classification_rank(classification) <= self.max_classification_rank
        )


@dataclass(frozen=True)
class QdrantConfig:
    url: str = "http://127.0.0.1:6333"
    collection: str = "ops_memory_v1"
    timeout_seconds: float = 3.0
    api_key_env: str = ""
    api_key_file: Path | None = None
    api_key_credential: str = ""


@dataclass(frozen=True)
class APIProviderConfig:
    protocol: str = "openai"
    enabled: bool = False
    url: str = ""
    model: str = ""
    credential_env: str = ""
    credential_credential: str = ""
    timeout_seconds: float = 5.0
    max_calls_per_operation: int = 1
    daily_call_budget: int = 0


@dataclass(frozen=True)
class EmbeddingConfig:
    dimensions: int = 256
    api_dimensions: int = 1024
    model_id: str = "ops-hash-embedding-v1"
    api_min_local_confidence: float = 0.35
    api: APIProviderConfig = field(default_factory=APIProviderConfig)


@dataclass(frozen=True)
class RerankerConfig:
    model_id: str = "ops-lexical-reranker-v1"
    api: APIProviderConfig = field(default_factory=APIProviderConfig)


@dataclass(frozen=True)
class MemoryConfig:
    database_path: Path
    socket_path: Path
    socket_group: str
    socket_mode: int
    qdrant: QdrantConfig
    embedding: EmbeddingConfig
    reranker: RerankerConfig
    actors: dict[str, ActorPolicy]
    source_truth: dict[str, tuple[str, ...]]
    max_content_bytes: int = 131_072
    candidate_limit: int = 20
    result_limit: int = 5
    fallback_scan_limit: int = 5_000
    allow_degraded_reads: bool = True
    allow_degraded_writes: bool = True
    observation_ttl_days: int = 30
    stale_after_days: int = 365

    @classmethod
    def from_toml(cls, path: str | Path) -> "MemoryConfig":
        config_path = Path(path)
        try:
            info = config_path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ConfigurationError("configuration path must not be a symlink")
            if info.st_uid not in {0, os.geteuid()} or info.st_mode & 0o022:
                raise ConfigurationError("configuration ownership or permissions are unsafe")
            with config_path.open("rb") as handle:
                data = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(f"cannot read configuration: {exc}") from exc

        runtime = _table(data.get("runtime", {}), "runtime")
        limits = _table(data.get("limits", {}), "limits")
        qdrant_data = _table(data.get("qdrant", {}), "qdrant")
        embedding_data = _table(data.get("embedding", {}), "embedding")
        reranker_data = _table(data.get("reranker", {}), "reranker")

        qdrant = QdrantConfig(
            url=_secure_url(str(qdrant_data.get("url", "http://127.0.0.1:6333")), "qdrant.url", loopback_only=True),
            collection=_require_string(qdrant_data.get("collection", "ops_memory_v1"), "qdrant.collection"),
            timeout_seconds=float(qdrant_data.get("timeout_seconds", 3.0)),
            api_key_env=str(qdrant_data.get("api_key_env", "")),
            api_key_file=(
                Path(str(qdrant_data["api_key_file"]))
                if qdrant_data.get("api_key_file")
                else None
            ),
            api_key_credential=str(qdrant_data.get("api_key_credential", "")),
        )
        embedding = EmbeddingConfig(
            api_dimensions=int(embedding_data.get("api_dimensions", 1024)),
            dimensions=int(embedding_data.get("dimensions", 256)),
            model_id=_require_string(embedding_data.get("model_id", "ops-hash-embedding-v1"), "embedding.model_id"),
            api_min_local_confidence=float(embedding_data.get("api_min_local_confidence", 0.35)),
            api=_parse_api_provider(embedding_data.get("api", {}), "embedding.api"),
        )
        reranker = RerankerConfig(
            model_id=_require_string(reranker_data.get("model_id", "ops-lexical-reranker-v1"), "reranker.model_id"),
            api=_parse_api_provider(reranker_data.get("api", {}), "reranker.api"),
        )
        actors_data = data.get("actors")
        if not isinstance(actors_data, dict) or not actors_data:
            raise ConfigurationError("at least one actor policy is required")
        actors: dict[str, ActorPolicy] = {}
        known_roles: set[str] = set()
        for name, actor_data in actors_data.items():
            if not isinstance(actor_data, dict):
                raise ConfigurationError(f"actors.{name} must be a table")
            roles = _string_tuple(actor_data.get("roles"), f"actors.{name}.roles")
            grantable = _string_tuple(
                actor_data.get("grantable_roles", list(roles)),
                f"actors.{name}.grantable_roles",
            )
            actor = ActorPolicy(
                name=name,
                unix_users=_string_tuple(actor_data.get("unix_users"), f"actors.{name}.unix_users"),
                roles=roles,
                grantable_roles=grantable,
                projects=_string_tuple(actor_data.get("projects"), f"actors.{name}.projects"),
                environments=_string_tuple(actor_data.get("environments"), f"actors.{name}.environments"),
                max_classification=_require_string(
                    actor_data.get("max_classification", "internal"),
                    f"actors.{name}.max_classification",
                ),
                capabilities=frozenset(
                    _string_tuple(actor_data.get("capabilities"), f"actors.{name}.capabilities")
                ),
                source_types=_string_tuple(
                    actor_data.get("source_types", []),
                    f"actors.{name}.source_types",
                    nonempty=False,
                ),
                authoritative_source_types=_string_tuple(
                    actor_data.get("authoritative_source_types", []),
                    f"actors.{name}.authoritative_source_types",
                    nonempty=False,
                ),
            )
            classification_rank(actor.max_classification)
            if not set(actor.authoritative_source_types).issubset(actor.source_types):
                raise ConfigurationError(
                    f"actors.{name}.authoritative_source_types must be allowed source types"
                )
            actors[name] = actor
            known_roles.update(roles)
            known_roles.update(grantable)
        for actor in actors.values():
            unknown = set(actor.grantable_roles) - known_roles
            if unknown:
                raise ConfigurationError(f"actor {actor.name} grants unknown roles: {sorted(unknown)}")

        truth_data = data.get("source_truth", {})
        if not isinstance(truth_data, dict):
            raise ConfigurationError("source_truth must be a table")
        source_truth = {
            str(category): _string_tuple(sources, f"source_truth.{category}")
            for category, sources in truth_data.items()
        }

        result = cls(
            database_path=Path(_require_string(runtime.get("database_path"), "runtime.database_path")),
            socket_path=Path(_require_string(runtime.get("socket_path"), "runtime.socket_path")),
            socket_group=_require_string(runtime.get("socket_group", "ops-memory-api"), "runtime.socket_group"),
            socket_mode=int(str(runtime.get("socket_mode", "0660")), 8),
            qdrant=qdrant,
            embedding=embedding,
            reranker=reranker,
            actors=actors,
            source_truth=source_truth,
            max_content_bytes=int(limits.get("max_content_bytes", 131_072)),
            candidate_limit=int(limits.get("candidate_limit", 20)),
            result_limit=int(limits.get("result_limit", 5)),
            fallback_scan_limit=int(limits.get("fallback_scan_limit", 5_000)),
            allow_degraded_reads=_boolean(
                runtime.get("allow_degraded_reads", True), "runtime.allow_degraded_reads"
            ),
            allow_degraded_writes=_boolean(
                runtime.get("allow_degraded_writes", True), "runtime.allow_degraded_writes"
            ),
            observation_ttl_days=int(limits.get("observation_ttl_days", 30)),
            stale_after_days=int(limits.get("stale_after_days", 365)),
        )
        result.validate()
        return result

    def validate(self) -> None:
        if not 32 <= self.embedding.api_dimensions <= 4096:
            raise ConfigurationError("API embedding dimensions must be between 32 and 4096")
        if self.embedding.dimensions < 32 or self.embedding.dimensions > 4096:
            raise ConfigurationError("embedding dimensions must be between 32 and 4096")
        if not 0.1 <= self.qdrant.timeout_seconds <= 30:
            raise ConfigurationError("qdrant.timeout_seconds must be between 0.1 and 30")
        if not 0 <= self.embedding.api_min_local_confidence <= 1:
            raise ConfigurationError("embedding.api_min_local_confidence must be between 0 and 1")
        if not 10 <= self.candidate_limit <= 30:
            raise ConfigurationError("candidate_limit must be between 10 and 30")
        if not 1 <= self.result_limit <= 5 or self.result_limit > self.candidate_limit:
            raise ConfigurationError("result_limit must be between 1 and 5")
        if self.fallback_scan_limit < self.candidate_limit:
            raise ConfigurationError("fallback_scan_limit is too small")
        if not 1 <= self.max_content_bytes <= 1_048_576:
            raise ConfigurationError("max_content_bytes is outside the safe range")
        if self.observation_ttl_days < 1 or self.stale_after_days < 1:
            raise ConfigurationError("memory retention periods must be positive")
        if self.socket_mode & 0o007:
            raise ConfigurationError("socket must not grant access to other users")
        if not self.database_path.is_absolute() or not self.socket_path.is_absolute():
            raise ConfigurationError("database_path and socket_path must be absolute")
        key_sources = sum(
            bool(value)
            for value in (
                self.qdrant.api_key_env,
                self.qdrant.api_key_file,
                self.qdrant.api_key_credential,
            )
        )
        if key_sources > 1:
            raise ConfigurationError("configure only one Qdrant API key source")
        if self.qdrant.api_key_file is not None and not self.qdrant.api_key_file.is_absolute():
            raise ConfigurationError("qdrant.api_key_file must be absolute")
        if self.qdrant.api_key_credential and (
            "/" in self.qdrant.api_key_credential
            or self.qdrant.api_key_credential in {".", ".."}
        ):
            raise ConfigurationError("qdrant.api_key_credential must be a credential name")
        for name, provider in (
            ("embedding.api", self.embedding.api),
            ("reranker.api", self.reranker.api),
        ):
            if not 0.1 <= provider.timeout_seconds <= 30:
                raise ConfigurationError(f"{name}.timeout_seconds must be between 0.1 and 30")
            if provider.max_calls_per_operation != 1:
                raise ConfigurationError(f"{name}.max_calls_per_operation must be exactly 1")
            if provider.daily_call_budget < 0:
                raise ConfigurationError(f"{name}.daily_call_budget must not be negative")
            credential_sources = sum(
                bool(value) for value in (provider.credential_env, provider.credential_credential)
            )
            if provider.enabled and credential_sources != 1:
                raise ConfigurationError(f"{name} requires exactly one credential source")
            if provider.credential_credential and (
                "/" in provider.credential_credential
                or provider.credential_credential in {".", ".."}
            ):
                raise ConfigurationError(f"{name}.credential_credential must be a credential name")


def _parse_api_provider(data: object, name: str) -> APIProviderConfig:
    data = _table(data, name)
    enabled = _boolean(data.get("enabled", False), f"{name}.enabled")
    url = str(data.get("url", ""))
    model = str(data.get("model", ""))
    credential_env = str(data.get("credential_env", ""))
    credential_credential = str(data.get("credential_credential", ""))
    if enabled:
        url = _secure_url(url, f"{name}.url")
        _require_string(model, f"{name}.model")
        if credential_env and credential_env not in os.environ:
            # Startup remains possible without the optional API. Calls fail closed
            # and transparently retain the local result.
            pass
    protocol = str(data.get("protocol", "openai"))
    if protocol not in {"openai", "voyage", "cohere", "jina"}:
        raise ConfigurationError("unsupported API protocol")
    return APIProviderConfig(
        protocol=protocol,
        enabled=enabled,
        url=url,
        model=model,
        credential_env=credential_env,
        credential_credential=credential_credential,
        timeout_seconds=float(data.get("timeout_seconds", 5.0)),
        max_calls_per_operation=int(data.get("max_calls_per_operation", 1)),
        daily_call_budget=int(data.get("daily_call_budget", 0)),
    )
