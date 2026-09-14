from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .config import APIProviderConfig
from .credentials import provider_credential
from .embedding import tokenize
from .errors import BackendUnavailableError


@dataclass(frozen=True)
class RankedCandidate:
    record: dict[str, Any]
    score: float


AUTHORITY_WEIGHT = {
    "authoritative": 1.0,
    "verified": 0.8,
    "derived": 0.45,
    "historical": 0.25,
}


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


class LocalReranker:
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id

    def rerank(self, query: str, candidates: list[dict[str, Any]], top_k: int) -> list[RankedCandidate]:
        query_tokens = set(tokenize(query))
        query_phrase = query.casefold().strip()
        ranked: list[RankedCandidate] = []
        now = datetime.now(UTC)
        for candidate in candidates:
            content = str(candidate["content"])
            content_tokens = set(tokenize(content))
            union = query_tokens | content_tokens
            overlap = len(query_tokens & content_tokens) / len(union) if union else 0.0
            phrase = 1.0 if query_phrase and query_phrase in content.casefold() else 0.0
            semantic = float(candidate.get("semantic_score", 0.0))
            authority = AUTHORITY_WEIGHT.get(str(candidate.get("authority_status", "derived")), 0.0)
            importance = float(candidate.get("importance", 0.0))
            try:
                created = datetime.fromisoformat(str(candidate["created_at"]))
                age_days = max(0.0, (now - created).total_seconds() / 86_400)
                recency = 1.0 / (1.0 + age_days / 180.0)
            except (KeyError, TypeError, ValueError):
                recency = 0.0
            score = (
                0.37 * semantic
                + 0.30 * overlap
                + 0.08 * phrase
                + 0.10 * authority
                + 0.10 * importance
                + 0.05 * recency
            )
            ranked.append(RankedCandidate(candidate, round(score, 8)))
        ranked.sort(key=lambda item: (-item.score, str(item.record["id"])))
        return ranked[:top_k]


class APIReranker:
    def __init__(self, config: APIProviderConfig) -> None:
        self.config = config

    def rerank(self, query: str, candidates: list[dict[str, Any]], top_k: int) -> list[RankedCandidate]:
        if not self.config.enabled:
            raise BackendUnavailableError("reranker API is disabled")
        if not candidates:
            return []
        if len(query.encode("utf-8"))+sum(len(str(item["content"]).encode("utf-8")) for item in candidates)>65536:
            raise BackendUnavailableError("reranker input exceeds the bounded API request")
        credential = provider_credential(self.config, "reranker API")
        documents = [str(item["content"]) for item in candidates]
        payload = json.dumps(
            {"model": self.config.model, "query": query, "documents": documents, "top_n": top_k}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.config.url,
            data=payload,
            headers={"Authorization": f"Bearer {credential}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _RejectRedirects()
            )
            with opener.open(request, timeout=self.config.timeout_seconds) as response:
                results = json.loads(response.read(2_000_000))["results"]
            if not isinstance(results,list) or len(results)>top_k:
                raise ValueError("invalid reranker result count")
            ranked=[];seen=set()
            for item in results:
                index=item["index"];score=item["relevance_score"]
                if type(index) is not int or index < 0 or index >= len(candidates) or index in seen:
                    raise ValueError("invalid reranker index")
                if type(score) not in (int,float) or not math.isfinite(score) or not 0 <= score <= 1:
                    raise ValueError("invalid reranker score")
                seen.add(index);ranked.append(RankedCandidate(candidates[index],float(score)))
        except (OSError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError, urllib.error.HTTPError) as exc:
            raise BackendUnavailableError(f"reranker API failed: {type(exc).__name__}") from exc
        return ranked


class RerankerRouter:
    def __init__(self, local: LocalReranker, remote: APIReranker) -> None:
        self.local = local
        self.remote = remote

    def rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
        top_k: int,
        *,
        allow_api: bool = False,
        important: bool = False,
    ) -> tuple[list[RankedCandidate], str]:
        if allow_api and important:
            try:
                return self.remote.rerank(query, candidates, top_k), "api"
            except BackendUnavailableError:
                pass
        return self.local.rerank(query, candidates, top_k), "local"
