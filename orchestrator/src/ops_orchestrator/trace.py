from __future__ import annotations

import hashlib
import json
import uuid

from .database import Database, utc_now


ZERO_HASH = "0" * 64


class ModelTrace:
    def __init__(self, database: Database):
        self.database = database

    def append(
        self,
        *,
        mission_id: str,
        route_id: str,
        event_type: str,
        reason: str,
        risk: str,
        complexity: float,
        impact: float = 0.0,
        role: str | None = None,
        provider_id: str | None = None,
        model: str | None = None,
        location: str | None = None,
        confidence_in: float | None = None,
        confidence_out: float | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_microusd: int = 0,
    ) -> int:
        if event_type not in {
            "ROUTE", "MODEL_START", "MODEL_RESULT", "MODEL_FAILURE", "BUDGET_BLOCK",
            "ESCALATION", "CAPABILITY_REJECT", "RETURN_LOCAL", "RETURN_CONTROL_PLANE",
            "HUMAN_ESCALATION", "DETERMINISTIC",
            "VERIFICATION_START", "VERIFICATION_RESULT", "VERIFICATION_FAILURE",
        }:
            raise ValueError("unsupported trace event type")
        event_id = str(uuid.uuid4())
        created_at = utc_now()
        with self.database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                """SELECT sequence, event_hash FROM model_trace
                   WHERE mission_id = ? ORDER BY sequence DESC LIMIT 1""",
                (mission_id,),
            ).fetchone()
            sequence = int(previous["sequence"]) + 1 if previous else 1
            previous_hash = str(previous["event_hash"]) if previous else ZERO_HASH
            canonical = {
                "event_id": event_id,
                "mission_id": mission_id,
                "route_id": route_id,
                "sequence": sequence,
                "created_at": created_at,
                "event_type": event_type,
                "role": role,
                "provider_id": provider_id,
                "model": model,
                "location": location,
                "reason": reason[:1000],
                "risk": risk,
                "complexity": complexity,
                "impact": impact,
                "confidence_in": confidence_in,
                "confidence_out": confidence_out,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_microusd": cost_microusd,
                "previous_hash": previous_hash,
            }
            event_hash = hashlib.sha256(
                json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO model_trace(
                    event_id, mission_id, route_id, sequence, created_at, event_type,
                    role, provider_id, model, location, reason, risk, complexity,
                    impact, confidence_in, confidence_out, input_tokens, output_tokens,
                    cost_microusd, previous_hash, event_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id, mission_id, route_id, sequence, created_at, event_type,
                    role, provider_id, model, location, reason[:1000], risk, complexity,
                    impact, confidence_in, confidence_out, input_tokens, output_tokens,
                    cost_microusd, previous_hash, event_hash,
                ),
            )
            connection.commit()
        return sequence

    def list(self, mission_id: str, limit: int = 100) -> list[dict[str, object]]:
        limit = min(max(limit, 1), 500)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, created_at, event_type, role, provider_id, model,
                       location, reason, risk, complexity, impact, confidence_in,
                       confidence_out, input_tokens, output_tokens,
                       cost_microusd, previous_hash, event_hash
                FROM model_trace WHERE mission_id = ?
                ORDER BY sequence DESC LIMIT ?
                """,
                (mission_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def verify(self, mission_id: str) -> bool:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM model_trace WHERE mission_id = ? ORDER BY sequence",
                (mission_id,),
            ).fetchall()
        previous_hash = ZERO_HASH
        for row in rows:
            canonical = {
                "event_id": row["event_id"], "mission_id": row["mission_id"],
                "route_id": row["route_id"], "sequence": row["sequence"],
                "created_at": row["created_at"], "event_type": row["event_type"],
                "role": row["role"], "provider_id": row["provider_id"],
                "model": row["model"], "location": row["location"],
                "reason": row["reason"], "risk": row["risk"],
                "complexity": row["complexity"], "impact": row["impact"],
                "confidence_in": row["confidence_in"],
                "confidence_out": row["confidence_out"], "input_tokens": row["input_tokens"],
                "output_tokens": row["output_tokens"], "cost_microusd": row["cost_microusd"],
                "previous_hash": previous_hash,
            }
            calculated = hashlib.sha256(
                json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
            ).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                return False
            previous_hash = calculated
        return True
