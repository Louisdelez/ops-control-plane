#!/usr/bin/python3
"""Destructive-but-self-cleaning live smoke test for the local memory API."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, "/opt/ops-memory/src")

from ops_memory.protocol import PROTOCOL_VERSION, socket_request  # noqa: E402


SOCKET = Path("/run/ops-memory/ops-memory.sock")
ACTOR = "codex-supervised"


def call(operation: str, payload: dict, actor: str = ACTOR) -> dict:
    response = socket_request(
        SOCKET,
        {
            "version": PROTOCOL_VERSION,
            "request_id": str(uuid.uuid4()),
            "operation": operation,
            "actor": actor,
            "payload": payload,
        },
        15.0,
    )
    if response.get("ok") is not True:
        raise RuntimeError(f"{operation} failed: {response.get('error', {}).get('code', 'unknown')}")
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(f"{operation} returned an invalid result")
    return result


def record(content: str, source_ref: str) -> dict:
    return {
        "content": content,
        "project": "minecraft",
        "environment": "development",
        "classification": "internal",
        "category": "documentation",
        "kind": "information",
        "tier": "cold",
        "allowed_roles": ["minecraft-ops"],
        "source_type": "git-docs",
        "source_uri": "git://ops-control-plane/tests/live-memory-smoke",
        "source_ref": source_ref,
        "source_authority": "ops-control-plane",
        "metadata": {"mission_id": "live-smoke-test"},
        "importance": 0.1,
        "reviewed": True,
        "promote": True,
    }


def main() -> int:
    cleanup_ids: list[str] = []
    try:
        health = call("health", {})
        if health.get("status") != "ok" or health.get("qdrant") != "ok":
            raise RuntimeError("memory is not fully healthy")

        first = call("ingest", record("Validation mémoire locale numéro un.", "live-smoke:first"))
        first_id = first["memory"]["id"]
        cleanup_ids.append(first_id)
        if first.get("degraded") is not False or first["memory"].get("qdrant_state") != "indexed":
            raise RuntimeError("first record was not indexed by Qdrant")

        duplicate = call("ingest", record("Validation mémoire locale numéro un.", "live-smoke:duplicate"))
        if duplicate.get("deduplicated") is not True or duplicate["memory"]["id"] != first_id:
            raise RuntimeError("deduplication contract failed")

        second = call("ingest", record("Validation mémoire locale numéro deux.", "live-smoke:second"))
        second_id = second["memory"]["id"]
        cleanup_ids.append(second_id)

        found = call(
            "search",
            {
                "query": "validation mémoire locale numéro un",
                "project": "minecraft",
                "environment": "development",
                "max_classification": "internal",
                "categories": ["documentation"],
                "kinds": ["information"],
                "top_k": 5,
                "candidate_limit": 10,
                "important": False,
                "allow_api": False,
            },
        )
        if found.get("backend") != "qdrant" or first_id not in {item["id"] for item in found.get("results", [])}:
            raise RuntimeError("Qdrant retrieval contract failed")

        summary = call(
            "summarize",
            {
                "record_ids": [first_id, second_id],
                "summary": {
                    "objective": "Valider la mémoire locale",
                    "decisions": ["Conserver le cloisonnement"],
                    "changes": ["Test de bout en bout exécuté"],
                    "open_items": [],
                },
                "category": "mission-summary",
                "metadata": {"mission_id": "live-smoke-test"},
                "importance": 0.1,
            },
        )
        summary_id = summary["memory"]["id"]
        cleanup_ids.append(summary_id)

        denied = socket_request(
            SOCKET,
            {
                "version": PROTOCOL_VERSION,
                "request_id": str(uuid.uuid4()),
                "operation": "health",
                "actor": "minecraft-ops",
                "payload": {},
            },
            5.0,
        )
        if denied.get("ok") is not False or denied.get("error", {}).get("code") != "authentication_failed":
            raise RuntimeError("Unix identity binding did not fail closed")

        print(json.dumps({"ok": True, "backend": "qdrant", "deduplication": True, "summary": True, "uid_binding": True}))
        return 0
    finally:
        for memory_id in reversed(cleanup_ids):
            try:
                call("invalidate", {"id": memory_id, "reason": "Fin du test live auto-nettoyant"})
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
