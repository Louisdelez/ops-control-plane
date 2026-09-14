from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import APIProviderConfig
from .credentials import provider_credential
from .errors import BackendUnavailableError, ValidationError


TOKEN_RE = re.compile(r"[\w./:@-]+", re.UNICODE)


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


@dataclass(frozen=True)
class EmbeddingResult:
    vector: tuple[float, ...]
    model: str
    provider: str
    confidence: float


def tokenize(text: str) -> tuple[str, ...]:
    return tuple(token.casefold() for token in TOKEN_RE.findall(text) if len(token) > 1)


class LocalHashEmbedding:
    """Small deterministic CPU feature-hashing embedding.

    It is intentionally dependency-free and predictable. It provides a useful
    local baseline and degraded semantic-ish index without consuming GPU VRAM.
    A benchmarked sentence-transformer can later implement the same interface.
    """

    def __init__(self, dimensions: int, model_id: str) -> None:
        self.dimensions = dimensions
        self.model_id = model_id

    def embed(self, text: str) -> EmbeddingResult:
        tokens = tokenize(text)
        vector = [0.0] * self.dimensions
        features = list(tokens)
        features.extend(f"{left}\x1f{right}" for left, right in zip(tokens, tokens[1:]))
        for position, feature in enumerate(features):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=16, person=b"ops-memory-v1").digest()
            index = int.from_bytes(digest[:8], "big") % self.dimensions
            sign = 1.0 if digest[8] & 1 else -1.0
            weight = 1.0 if position < len(tokens) else 0.65
            vector[index] += sign * weight
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        unique = len(set(tokens))
        confidence = min(1.0, unique / 8.0) if tokens else 0.0
        return EmbeddingResult(tuple(vector), self.model_id, "local", confidence)


class OpenAICompatibleEmbedding:
    def __init__(self, config: APIProviderConfig, expected_dimensions: int) -> None:
        self.config = config
        self.expected_dimensions = expected_dimensions

    def embed(self, text: str, *, input_type: str = "document") -> EmbeddingResult:
        if not self.config.enabled:
            raise BackendUnavailableError("embedding API is disabled")
        if not text or len(text.encode("utf-8")) > 32768:
            raise BackendUnavailableError("embedding input exceeds the bounded API request")
        credential = provider_credential(self.config, "embedding API")
        body = {"model": self.config.model, "input": text}
        if self.config.protocol == "voyage":
            if input_type not in {"query", "document"}:
                raise ValidationError("invalid embedding input type")
            body.update(input=[text], input_type=input_type, output_dimension=self.expected_dimensions,
                        output_dtype="float", truncation=False)
        if self.config.protocol == "jina":
            if input_type not in {"query", "document"}:
                raise ValidationError("invalid embedding input type")
            body.update(input=[text], task="retrieval.query" if input_type == "query" else "retrieval.passage",
                        dimensions=self.expected_dimensions, embedding_type="float", normalized=True, truncate=False)
        payload = json.dumps(body).encode("utf-8")
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
                parsed = json.loads(response.read(2_000_000))
            vector = parsed["data"][0]["embedding"]
        except (OSError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError, urllib.error.HTTPError) as exc:
            raise BackendUnavailableError(f"embedding API failed: {type(exc).__name__}") from exc
        if not isinstance(vector, list) or len(vector) != self.expected_dimensions:
            raise ValidationError("embedding API returned an unexpected vector dimension")
        if not all(type(item) in (int, float) and math.isfinite(item) for item in vector):
            raise ValidationError("embedding API returned an invalid vector")
        return EmbeddingResult(tuple(float(item) for item in vector), self.config.model, "api", 1.0)


class EmbeddingRouter:
    def __init__(self, local: LocalHashEmbedding, remote: OpenAICompatibleEmbedding, threshold: float) -> None:
        self.local = local
        self.remote = remote
        self.threshold = threshold

    def embed(self, text: str, *, allow_api: bool = False, important: bool = False) -> EmbeddingResult:
        local = self.local.embed(text)
        if allow_api and (important or local.confidence < self.threshold):
            try:
                return self.remote.embed(text)
            except BackendUnavailableError:
                return local
        return local
