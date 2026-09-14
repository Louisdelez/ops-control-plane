from __future__ import annotations

from .config import ActorPolicy, MemoryConfig, classification_rank
from .errors import AuthenticationError, AuthorizationError
from .models import IngestRequest, SearchRequest


class SecurityPolicy:
    def __init__(self, config: MemoryConfig) -> None:
        self.config = config

    def authenticate(self, actor_name: str, peer_uid: int) -> ActorPolicy:
        actor = self.config.actors.get(actor_name)
        if actor is None:
            raise AuthenticationError("unknown actor")
        if peer_uid not in actor.resolved_uids():
            raise AuthenticationError("actor does not match the Unix peer identity")
        return actor

    @staticmethod
    def require(actor: ActorPolicy, capability: str) -> None:
        if capability not in actor.capabilities:
            raise AuthorizationError(f"actor lacks capability: {capability}")

    @staticmethod
    def authorize_ingest(actor: ActorPolicy, request: IngestRequest) -> None:
        if not actor.permits_scope(request.project, request.environment, request.classification):
            raise AuthorizationError("memory scope is not authorized")
        if not set(request.allowed_roles).issubset(actor.grantable_roles):
            raise AuthorizationError("actor cannot grant one or more allowed_roles")
        if request.source_type not in actor.source_types:
            raise AuthorizationError("source_type is not authorized for this actor")
        if request.reviewed and "review" not in actor.capabilities:
            raise AuthorizationError("actor cannot mark memory as reviewed")

    @staticmethod
    def authorize_search(actor: ActorPolicy, request: SearchRequest) -> None:
        if not actor.permits_scope(request.project, request.environment, request.max_classification):
            raise AuthorizationError("search scope is not authorized")

    @staticmethod
    def record_visible(actor: ActorPolicy, record: dict[str, object]) -> bool:
        roles = set(record.get("allowed_roles", ()))
        return (
            actor.permits_scope(
                str(record["project"]),
                str(record["environment"]),
                str(record["classification"]),
            )
            and bool(roles.intersection(actor.roles))
            and classification_rank(str(record["classification"])) <= actor.max_classification_rank
        )
