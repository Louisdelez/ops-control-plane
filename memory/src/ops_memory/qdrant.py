from __future__ import annotations

import json
import os
import stat
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import QdrantConfig, classification_rank
from .errors import BackendUnavailableError


@dataclass(frozen=True)
class VectorHit:
    id: str
    score: float


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


class QdrantClient:
    """Minimal Qdrant REST client with no third-party runtime dependency."""

    def __init__(self, config: QdrantConfig, dimensions: int) -> None:
        self.config = config
        self.dimensions = dimensions
        self._ensure_lock = threading.Lock()
        self._ensured = False

    def _request(self, method: str, path: str, payload: object | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        key = self._api_key()
        if key:
            headers["api-key"] = key
        request = urllib.request.Request(
            f"{self.config.url}{path}", data=data, headers=headers, method=method
        )
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), _RejectRedirects()
            )
            with opener.open(request, timeout=self.config.timeout_seconds) as response:
                raw = response.read(8_000_000)
            parsed = json.loads(raw) if raw else {}
        except (OSError, urllib.error.HTTPError, json.JSONDecodeError) as exc:
            raise BackendUnavailableError(f"Qdrant request failed: {type(exc).__name__}") from exc
        if not isinstance(parsed, dict) or parsed.get("status") == "error":
            raise BackendUnavailableError("Qdrant returned an invalid response")
        return parsed

    def _api_key(self) -> str:
        key_path = self.config.api_key_file
        is_systemd_credential = False
        if self.config.api_key_credential:
            credential_directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
            if not credential_directory or not os.path.isabs(credential_directory):
                raise BackendUnavailableError("systemd credential directory is unavailable")
            key_path = Path(credential_directory) / self.config.api_key_credential
            is_systemd_credential = True
        if key_path is not None:
            try:
                info = key_path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    raise BackendUnavailableError("Qdrant API key path is unsafe")
                if info.st_uid not in {0, os.geteuid()}:
                    raise BackendUnavailableError("Qdrant API key owner is unsafe")
                # systemd exposes credentials to non-root services as 0440 in
                # a private, service-scoped mount. A regular configured file
                # remains restricted to owner-only access.
                unsafe_permissions = (
                    info.st_mode & 0o037
                    if is_systemd_credential
                    else info.st_mode & 0o077
                )
                if unsafe_permissions:
                    raise BackendUnavailableError(
                        "Qdrant API key is group- or world-accessible"
                    )
                descriptor = os.open(key_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
                try:
                    opened = os.fstat(descriptor)
                    if opened.st_dev != info.st_dev or opened.st_ino != info.st_ino:
                        raise BackendUnavailableError("Qdrant API key changed while opening")
                    raw = os.read(descriptor, 513)
                    if len(raw) > 512 or os.read(descriptor, 1):
                        raise BackendUnavailableError("Qdrant API key is oversized")
                finally:
                    os.close(descriptor)
                key = raw.rstrip(b"\r\n").decode("ascii", errors="strict")
            except OSError as exc:
                raise BackendUnavailableError("Qdrant API key file is unavailable") from exc
            except UnicodeError as exc:
                raise BackendUnavailableError("Qdrant API key file is invalid") from exc
            if len(key) < 32 or len(key) > 512 or any(character.isspace() for character in key):
                raise BackendUnavailableError("Qdrant API key file is invalid")
            return key
        if self.config.api_key_env:
            key = os.environ.get(self.config.api_key_env, "")
            if not key:
                raise BackendUnavailableError("Qdrant API key environment is unavailable")
            return key
        return ""

    @property
    def collection_path(self) -> str:
        collection = urllib.parse.quote(self.config.collection, safe="")
        return f"/collections/{collection}"

    def ensure_collection(self) -> None:
        with self._ensure_lock:
            if self._ensured:
                return
            try:
                response = self._request("GET", self.collection_path)
            except BackendUnavailableError:
                self._request(
                    "PUT",
                    self.collection_path,
                    {"vectors": {"size": self.dimensions, "distance": "Cosine"}},
                )
                response = {}
            try:
                size = response["result"]["config"]["params"]["vectors"]["size"]
            except (KeyError, TypeError):
                # Different Qdrant versions expose slightly different GET shapes.
                pass
            else:
                if int(size) != self.dimensions:
                    raise BackendUnavailableError("Qdrant collection vector dimension mismatch")
            indexes = {
                "project": "keyword",
                "environment": "keyword",
                "classification_rank": "integer",
                "allowed_roles": "keyword",
                "category": "keyword",
                "kind": "keyword",
                "valid": "bool",
            }
            for field_name, field_schema in indexes.items():
                self._request(
                    "PUT",
                    f"{self.collection_path}/index?wait=true",
                    {"field_name": field_name, "field_schema": field_schema},
                )
            self._ensured = True

    def health(self) -> bool:
        self._request("GET", "/collections")
        return True

    def upsert(self, record: dict[str, Any], vector: tuple[float, ...]) -> None:
        if len(vector) != self.dimensions:
            raise BackendUnavailableError("refusing vector with unexpected dimension")
        payload = {
            "content": record["content"],
            "project": record["project"],
            "environment": record["environment"],
            "classification": record["classification"],
            "classification_rank": record["classification_rank"],
            "category": record["category"],
            "kind": record["kind"],
            "allowed_roles": record["allowed_roles"],
            "source_type": record["source_type"],
            "source_uri": record["source_uri"],
            "source_ref": record["source_ref"],
            "source_authority": record["source_authority"],
            "authority_status": record["authority_status"],
            "metadata": record["metadata"],
            "importance": record["importance"],
            "tier": record["tier"],
            "valid_from": record["valid_from"],
            "valid_until": record["valid_until"],
            "valid": True,
        }
        self._request(
            "PUT",
            f"{self.collection_path}/points?wait=true",
            {"points": [{"id": record["id"], "vector": list(vector), "payload": payload}]},
        )

    def delete(self, record_id: str) -> None:
        self._request(
            "POST",
            f"{self.collection_path}/points/delete?wait=true",
            {"points": [record_id]},
        )

    def search(
        self,
        vector: tuple[float, ...],
        *,
        project: str,
        environment: str,
        max_classification: str,
        roles: tuple[str, ...],
        categories: tuple[str, ...],
        kinds: tuple[str, ...],
        limit: int,
    ) -> list[VectorHit]:
        must: list[dict[str, Any]] = [
            {"key": "project", "match": {"value": project}},
            {"key": "environment", "match": {"value": environment}},
            {"key": "classification_rank", "range": {"lte": classification_rank(max_classification)}},
            {"key": "allowed_roles", "match": {"any": list(roles)}},
            {"key": "valid", "match": {"value": True}},
        ]
        if categories:
            must.append({"key": "category", "match": {"any": list(categories)}})
        if kinds:
            must.append({"key": "kind", "match": {"any": list(kinds)}})
        response = self._request(
            "POST",
            f"{self.collection_path}/points/search",
            {
                "vector": list(vector),
                "limit": limit,
                "with_payload": False,
                "with_vector": False,
                "filter": {"must": must},
            },
        )
        result = response.get("result")
        if not isinstance(result, list):
            raise BackendUnavailableError("Qdrant search response is invalid")
        hits: list[VectorHit] = []
        for item in result:
            if not isinstance(item, dict) or "id" not in item or "score" not in item:
                raise BackendUnavailableError("Qdrant search hit is invalid")
            hits.append(VectorHit(str(item["id"]), float(item["score"])))
        return hits
