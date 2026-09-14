from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import ActorPolicy, MemoryConfig, classification_rank
from .embedding import EmbeddingResult, LocalHashEmbedding, OpenAICompatibleEmbedding
from .errors import (
    AuthorizationError,
    BackendUnavailableError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from .models import IngestRequest, SearchRequest, check_for_secrets, utc_now
from .qdrant import QdrantClient, VectorHit
from .rerank import APIReranker, LocalReranker, RankedCandidate
from .security import SecurityPolicy
from .semantic_index import SemanticIndex
from .storage import MemoryCatalog


DURABLE_KINDS = frozenset({"information", "decision", "rule", "procedure", "incident", "summary"})
REVIEW_REQUIRED_KINDS = frozenset({"decision", "rule", "procedure"})


def _public_record(record: dict[str, Any], *, source_truth: tuple[str, ...] = ()) -> dict[str, Any]:
    keep = {
        "id",
        "content",
        "project",
        "environment",
        "classification",
        "category",
        "kind",
        "tier",
        "allowed_roles",
        "source_type",
        "source_uri",
        "source_ref",
        "source_authority",
        "authority_status",
        "metadata",
        "importance",
        "valid_from",
        "valid_until",
        "invalidated_at",
        "invalid_reason",
        "stale_at",
        "stale",
        "supersedes_id",
        "superseded_by_id",
        "reviewed",
        "created_by",
        "created_at",
        "updated_at",
        "qdrant_state",
    }
    result = {name: record[name] for name in keep if name in record}
    result["advisory_only"] = True
    result["verify_against"] = list(source_truth)
    return result


class MemoryService:
    def __init__(
        self,
        config: MemoryConfig,
        *,
        catalog: MemoryCatalog | None = None,
        qdrant: QdrantClient | None = None,
    ) -> None:
        self.config = config
        self.catalog = catalog or MemoryCatalog(config.database_path)
        self.qdrant = qdrant or QdrantClient(config.qdrant, config.embedding.dimensions)
        self.security = SecurityPolicy(config)
        self.local_embedding = LocalHashEmbedding(config.embedding.dimensions, config.embedding.model_id)
        self.api_embedding = OpenAICompatibleEmbedding(config.embedding.api, config.embedding.api_dimensions)
        self.semantic = SemanticIndex(config, self.catalog, self.api_embedding)
        self.local_reranker = LocalReranker(config.reranker.model_id)
        self.api_reranker = APIReranker(config.reranker.api)

    def actor(self, name: str, peer_uid: int, capability: str) -> ActorPolicy:
        actor = self.security.authenticate(name, peer_uid)
        self.security.require(actor, capability)
        return actor

    def _authority_status(self, actor: ActorPolicy, request: IngestRequest) -> str:
        if request.source_type in {"llm", "model", "generated"}:
            return "derived"
        sources = self.config.source_truth.get(request.category, ())
        if (
            request.source_type in sources
            and request.source_type in actor.authoritative_source_types
            and request.reviewed
        ):
            return "authoritative"
        if request.reviewed:
            return "verified"
        if request.kind in {"incident", "summary"}:
            return "historical"
        return "derived"

    def _embed(self, actor: str, operation: str, text: str, *, allow_api: bool, important: bool) -> EmbeddingResult:
        local = self.local_embedding.embed(text)
        api = self.config.embedding.api
        should_escalate = allow_api and (important or local.confidence < self.config.embedding.api_min_local_confidence)
        if not should_escalate or not api.enabled or not self.semantic.has_records():
            return local
        call_id = self.catalog.reserve_provider_call(
            actor, operation, "embedding-api", api.model, api.daily_call_budget
        )
        if call_id is None:
            return local
        try:
            result = (self.api_embedding.embed(text, input_type="query")
                      if api.protocol in {"voyage", "jina"} else self.api_embedding.embed(text))
        except (BackendUnavailableError, ValidationError):
            self.catalog.complete_provider_call(call_id, "failed")
            return local
        self.catalog.complete_provider_call(call_id, "success")
        return result

    def _rerank(
        self,
        actor: str,
        query: str,
        candidates: list[dict[str, Any]],
        top_k: int,
        *,
        allow_api: bool,
        important: bool,
    ) -> tuple[list[RankedCandidate], str]:
        api = self.config.reranker.api
        if allow_api and important and api.enabled:
            call_id = self.catalog.reserve_provider_call(
                actor, "search", "reranker-api", api.model, api.daily_call_budget
            )
            if call_id is not None:
                try:
                    result = self.api_reranker.rerank(query, candidates, top_k)
                except BackendUnavailableError:
                    self.catalog.complete_provider_call(call_id, "failed")
                else:
                    self.catalog.complete_provider_call(call_id, "success")
                    return result, "api"
        return self.local_reranker.rerank(query, candidates, top_k), "local"

    def ingest(self, raw: object, peer_uid: int) -> dict[str, Any]:
        request = IngestRequest.from_dict(raw, max_content_bytes=self.config.max_content_bytes)
        actor = self.actor(request.actor, peer_uid, "write")
        self.security.authorize_ingest(actor, request)
        if request.category == "secrets" or request.source_type == "openbao-secret":
            raise ValidationError("secrets belong in OpenBao and cannot enter semantic memory")
        if request.kind in {"noise", "conversation"} and not request.promote:
            self.catalog.audit(
                request.actor,
                "ingest",
                "ignored",
                project=request.project,
                environment=request.environment,
                classification=request.classification,
                detail=f"selective_ingestion:{request.kind}",
            )
            return {"stored": False, "reason": "selective_ingestion", "kind": request.kind}
        if request.kind in REVIEW_REQUIRED_KINDS and not request.reviewed:
            raise ValidationError(f"{request.kind} memory requires reviewed=true")
        if request.source_type == "zulip" and request.kind in DURABLE_KINDS and not request.promote:
            raise ValidationError("durable Zulip memory requires explicit promote=true")
        if request.kind == "observation" and request.valid_until is None:
            expiry = datetime.now(UTC) + timedelta(days=self.config.observation_ttl_days)
            request = replace(request, valid_until=expiry.isoformat(timespec="seconds"))
        if request.valid_until is not None and request.valid_until <= request.valid_from:
            raise ValidationError("valid_until must be after valid_from")
        if request.supersedes_id is not None:
            try:
                previous = self.catalog.get(request.supersedes_id)
            except NotFoundError:
                raise NotFoundError("superseded memory does not exist") from None
            if not self.security.record_visible(actor, previous):
                raise NotFoundError("superseded memory does not exist")
            widening = (
                classification_rank(request.classification)
                < classification_rank(previous["classification"])
                or not set(request.allowed_roles).issubset(previous["allowed_roles"])
            )
            if widening and "declassify" not in actor.capabilities:
                raise AuthorizationError("supersession would widen memory visibility")
            if request.category != previous["category"] and "reclassify" not in actor.capabilities:
                raise AuthorizationError("supersession cannot change category")

        embedding = self._embed(request.actor, "ingest", request.content, allow_api=False, important=False)
        authority = self._authority_status(actor, request)
        record, duplicate, superseded_id = self.catalog.ingest(
            request, embedding.vector, embedding.model, authority
        )
        degraded = False
        if not duplicate:
            try:
                self.qdrant.ensure_collection()
                self.qdrant.upsert(record, embedding.vector)
                self.catalog.mark_qdrant(record["id"], "indexed")
                record["qdrant_state"] = "indexed"
                if superseded_id:
                    try:
                        self.qdrant.delete(superseded_id)
                        self.catalog.mark_qdrant(superseded_id, "deleted")
                    except BackendUnavailableError:
                        self.catalog.mark_qdrant(superseded_id, "delete_pending", "Qdrant unavailable")
            except BackendUnavailableError as exc:
                self.catalog.mark_qdrant(record["id"], "failed", str(exc))
                record["qdrant_state"] = "failed"
                degraded = True
                if not self.config.allow_degraded_writes:
                    self.catalog.audit(
                        request.actor,
                        "ingest",
                        "backend_unavailable",
                        project=request.project,
                        environment=request.environment,
                        classification=request.classification,
                        target_id=record["id"],
                    )
                    raise
        self.catalog.audit(
            request.actor,
            "ingest",
            "deduplicated" if duplicate else ("degraded" if degraded else "stored"),
            project=request.project,
            environment=request.environment,
            classification=request.classification,
            target_id=record["id"],
            detail=f"kind={request.kind};source={request.source_type}",
        )
        if self.security.record_visible(actor, record):
            public_memory = _public_record(
                record, source_truth=self.config.source_truth.get(request.category, ())
            )
        else:
            public_memory = {
                "id": record["id"],
                "project": record["project"],
                "environment": record["environment"],
                "classification": record["classification"],
                "category": record["category"],
                "kind": record["kind"],
                "allowed_roles": record["allowed_roles"],
                "qdrant_state": record["qdrant_state"],
            }
        return {
            "stored": True,
            "deduplicated": duplicate,
            "degraded": degraded,
            "embedding_provider": embedding.provider,
            "memory": public_memory,
        }

    def reindex_api(self, actor_name: str, peer_uid: int, ids: list[str]) -> dict[str, Any]:
        actor = self.actor(actor_name, peer_uid, "write")
        if "api-escalate" not in actor.capabilities:
            raise AuthorizationError("actor cannot request an external provider")
        if not isinstance(ids,list) or not 1 <= len(ids) <= 20 or any(not isinstance(i,str) for i in ids) or len(set(ids)) != len(ids):
            raise ValidationError("reindex requires 1 to 20 distinct memory IDs")
        records=self.catalog.get_many(ids)
        # Validate the complete batch before disclosing any document to an API.
        if len(records)!=len(ids) or any(not self.catalog.is_current(r) or not self.security.record_visible(actor,r) for r in records):
            raise NotFoundError("one or more memories are not available")
        result=self.semantic.reindex(actor_name,records)
        self.catalog.audit(actor_name,"reindex_api","partial" if result['pending'] else "success",detail=self.semantic.space)
        return result

    def search(self, raw: object, peer_uid: int) -> dict[str, Any]:
        request = SearchRequest.from_dict(
            raw,
            result_limit=self.config.result_limit,
            candidate_limit=self.config.candidate_limit,
        )
        actor = self.actor(request.actor, peer_uid, "read")
        self.security.authorize_search(actor, request)
        if request.allow_api and "api-escalate" not in actor.capabilities:
            raise AuthorizationError("actor cannot request an external provider")
        embedding = self._embed(
            request.actor,
            "search",
            request.query,
            allow_api=request.allow_api,
            important=request.important,
        )
        backend = "qdrant"
        filters = dict(project=request.project,environment=request.environment,
            max_classification=request.max_classification,roles=actor.roles,
            categories=request.categories,kinds=request.kinds,limit=request.candidate_limit)
        try:
            # Keep unconverted memories searchable. Scores from different vector
            # spaces are combined by rank, never by cosine arithmetic.
            local_vector=self.local_embedding.embed(request.query).vector
            local_hits=self.qdrant.search(local_vector,**filters)
            hits=local_hits
            if embedding.provider == "api":
                try:
                    remote_hits=self.semantic.qdrant.search(embedding.vector,**filters)
                except BackendUnavailableError:
                    embedding=self.local_embedding.embed(request.query)
                else:
                    scores={}
                    for ranked_hits in (local_hits,remote_hits):
                        for rank,hit in enumerate(ranked_hits):
                            scores[hit.id]=scores.get(hit.id,0.0)+1.0/(rank+1)
                    hits=[VectorHit(i,min(score/2,1.0)) for i,score in sorted(scores.items(),key=lambda v:(-v[1],v[0]))][:request.candidate_limit]
            candidates = self.catalog.get_many([hit.id for hit in hits])
            scores = {hit.id: hit.score for hit in hits}
            for candidate in candidates:
                candidate["semantic_score"] = scores.get(candidate["id"], 0.0)
            candidates = [
                candidate
                for candidate in candidates
                if self.catalog.is_current(candidate)
                and self.security.record_visible(actor, candidate)
                and candidate["project"] == request.project
                and candidate["environment"] == request.environment
                and classification_rank(candidate["classification"])
                <= classification_rank(request.max_classification)
                and (not request.categories or candidate["category"] in request.categories)
                and (not request.kinds or candidate["kind"] in request.kinds)
            ]
        except BackendUnavailableError:
            if not self.config.allow_degraded_reads:
                self.catalog.audit(
                    request.actor,
                    "search",
                    "backend_unavailable",
                    project=request.project,
                    environment=request.environment,
                    classification=request.max_classification,
                    query=request.query,
                )
                raise
            backend = "sqlite-lexical-degraded"
            candidates = self.catalog.fallback_candidates(
                request, actor.roles, self.config.fallback_scan_limit
            )[: request.candidate_limit]

        # Stale historical context remains discoverable but cannot outrank a
        # current fact with an otherwise equivalent score.
        for candidate in candidates:
            if candidate.get("stale"):
                candidate["semantic_score"] = float(candidate.get("semantic_score", 0.0)) * 0.5
        ranked, reranker_provider = self._rerank(
            request.actor,
            request.query,
            candidates,
            request.top_k,
            allow_api=request.allow_api,
            important=request.important,
        )
        results: list[dict[str, Any]] = []
        for item in ranked:
            public = _public_record(
                item.record,
                source_truth=self.config.source_truth.get(str(item.record["category"]), ()),
            )
            public["relevance_score"] = item.score
            results.append(public)
        self.catalog.audit(
            request.actor,
            "search",
            "degraded" if backend != "qdrant" else "success",
            project=request.project,
            environment=request.environment,
            classification=request.max_classification,
            query=request.query,
            detail=f"backend={backend};results={len(results)}",
        )
        return {
            "results": results,
            "candidate_count": len(candidates),
            "backend": backend,
            "degraded": backend != "qdrant",
            "embedding": {"provider": embedding.provider, "model": embedding.model},
            "reranker": {"provider": reranker_provider, "model": self.config.reranker.api.model if reranker_provider == "api" else self.config.reranker.model_id},
            "context_policy": {"max_results": self.config.result_limit, "secrets": "OpenBao only"},
        }

    def get(self, actor_name: str, peer_uid: int, record_id: str) -> dict[str, Any]:
        actor = self.actor(actor_name, peer_uid, "read")
        try:
            record_id = str(uuid.UUID(record_id))
        except ValueError as exc:
            raise ValidationError("id must be a UUID") from exc
        record = self.catalog.get(record_id)
        if not self.security.record_visible(actor, record):
            # Do not reveal whether an out-of-scope record exists.
            raise NotFoundError("memory does not exist")
        self.catalog.audit(
            actor_name,
            "get",
            "success",
            project=record["project"],
            environment=record["environment"],
            classification=record["classification"],
            target_id=record_id,
        )
        return _public_record(record, source_truth=self.config.source_truth.get(record["category"], ()))

    def invalidate(self, raw: object, peer_uid: int) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValidationError("request must be an object")
        unknown = set(raw) - {"actor", "id", "reason"}
        if unknown:
            raise ValidationError(f"unknown invalidation fields: {sorted(unknown)}")
        actor_name = str(raw.get("actor", ""))
        actor = self.actor(actor_name, peer_uid, "invalidate")
        record_id = str(raw.get("id", ""))
        reason = str(raw.get("reason", "")).strip()
        if not reason or len(reason) > 512:
            raise ValidationError("reason must be non-empty and at most 512 characters")
        check_for_secrets(reason, {})
        record = self.catalog.get(record_id)
        if not self.security.record_visible(actor, record):
            raise NotFoundError("memory does not exist")
        result = self.catalog.invalidate(record_id, reason)
        try:
            self.qdrant.delete(record_id)
            self.catalog.mark_qdrant(record_id, "deleted")
            result["qdrant_state"] = "deleted"
        except BackendUnavailableError:
            result["qdrant_state"] = "delete_pending"
        self.catalog.audit(
            actor_name,
            "invalidate",
            "success",
            project=record["project"],
            environment=record["environment"],
            classification=record["classification"],
            target_id=record_id,
            detail=reason,
        )
        return _public_record(result, source_truth=self.config.source_truth.get(record["category"], ()))

    def summarize(self, raw: object, peer_uid: int) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValidationError("request must be an object")
        unknown = set(raw) - {
            "actor", "record_ids", "summary", "category", "metadata", "importance"
        }
        if unknown:
            raise ValidationError(f"unknown summary fields: {sorted(unknown)}")
        actor_name = str(raw.get("actor", ""))
        actor = self.actor(actor_name, peer_uid, "summarize")
        ids = raw.get("record_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
            raise ValidationError("record_ids must contain between 1 and 100 IDs")
        try:
            ids = [str(uuid.UUID(str(item))) for item in ids]
        except ValueError as exc:
            raise ValidationError("record_ids contains an invalid UUID") from exc
        if len(ids) != len(set(ids)):
            raise ValidationError("record_ids contains duplicates")
        records = self.catalog.get_many(ids)
        if len(records) != len(ids) or any(not self.security.record_visible(actor, item) for item in records):
            raise NotFoundError("one or more memories do not exist")
        scopes = {(item["project"], item["environment"]) for item in records}
        if len(scopes) != 1:
            raise ConflictError("summary members must share project and environment")
        structured = raw.get("summary")
        if not isinstance(structured, dict):
            raise ValidationError("summary must be an object")
        allowed_fields = ("objective", "decisions", "changes", "open_items")
        if set(structured) - set(allowed_fields):
            raise ValidationError("summary contains unsupported fields")
        lines: list[str] = []
        for field in allowed_fields:
            value = structured.get(field)
            if value in (None, "", []):
                continue
            if isinstance(value, str):
                values = [value]
            elif isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
                values = value
            else:
                raise ValidationError(f"summary.{field} must be text or a string list")
            lines.append(field.replace("_", " ").title() + ":")
            lines.extend(f"- {item.strip()}" for item in values)
        if not lines:
            raise ValidationError("summary must not be empty")
        content = "\n".join(lines)
        check_for_secrets(content, {})
        max_class = max(records, key=lambda item: classification_rank(item["classification"]))[
            "classification"
        ]
        common_roles = set(records[0]["allowed_roles"])
        for item in records[1:]:
            common_roles.intersection_update(item["allowed_roles"])
        common_roles.intersection_update(actor.grantable_roles)
        if not common_roles:
            raise AuthorizationError("summary has no common authorized audience")
        project, environment = next(iter(scopes))
        digest = hashlib.sha256("\n".join(ids).encode("ascii")).hexdigest()
        ingest_data = {
            "actor": actor_name,
            "content": content,
            "project": project,
            "environment": environment,
            "classification": max_class,
            "category": str(raw.get("category", "mission-summary")),
            "kind": "summary",
            "tier": "cold",
            "allowed_roles": sorted(common_roles),
            "source_type": "memory-consolidation",
            "source_uri": f"memory://summary/{digest}",
            "source_ref": digest,
            "source_authority": "ops-memory",
            "metadata": raw.get("metadata", {}),
            "importance": raw.get("importance", 0.7),
            "reviewed": True,
            "promote": True,
        }
        result = self.ingest(ingest_data, peer_uid)
        summary_id = result["memory"]["id"]
        self.catalog.link_summary(summary_id, ids)
        return result

    def maintain(self, actor_name: str, peer_uid: int) -> dict[str, Any]:
        self.actor(actor_name, peer_uid, "maintenance")
        started = utc_now()
        expired, stale = self.catalog.consolidate_lifecycle(self.config.stale_after_days)
        reindexed = 0
        failures = 0
        try:
            self.qdrant.ensure_collection()
        except BackendUnavailableError:
            failures += 1
        else:
            for record in self.catalog.pending_deletions():
                try:
                    self.qdrant.delete(record["id"])
                    self.catalog.mark_qdrant(record["id"], "deleted")
                except BackendUnavailableError as exc:
                    self.catalog.mark_qdrant(record["id"], "delete_pending", str(exc))
                    failures += 1
                    break
            for record in self.catalog.pending_for_reindex():
                try:
                    vector = tuple(float(item) for item in json.loads(record["vector_json"]))
                    self.qdrant.upsert(record, vector)
                    self.catalog.mark_qdrant(record["id"], "indexed")
                    reindexed += 1
                except (BackendUnavailableError, TypeError, ValueError) as exc:
                    self.catalog.mark_qdrant(record["id"], "failed", str(exc))
                    failures += 1
                    break
        run_id = self.catalog.record_maintenance(started, expired, stale, reindexed, failures)
        self.catalog.audit(actor_name, "maintain", "partial" if failures else "success", detail=run_id)
        return {
            "run_id": run_id,
            "expired": expired,
            "marked_stale": stale,
            "reindexed": reindexed,
            "failures": failures,
            "degraded": bool(failures),
        }

    def health(self, actor_name: str, peer_uid: int) -> dict[str, Any]:
        self.actor(actor_name, peer_uid, "health")
        qdrant_ok = False
        try:
            qdrant_ok = self.qdrant.health()
        except BackendUnavailableError:
            pass
        stats = self.catalog.stats()
        return {
            "status": "ok" if qdrant_ok else "degraded",
            "qdrant": "ok" if qdrant_ok else "unavailable",
            "sqlite": "ok",
            "degraded_reads_enabled": self.config.allow_degraded_reads,
            "degraded_writes_enabled": self.config.allow_degraded_writes,
            "stats": stats,
        }

    def stats(self, actor_name: str, peer_uid: int) -> dict[str, Any]:
        self.actor(actor_name, peer_uid, "stats")
        return self.catalog.stats()
