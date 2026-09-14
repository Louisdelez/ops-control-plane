"""Environment-driven paths; no credential values are accepted here."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_path: Path
    runbooks_path: Path
    action_policy_path: Path
    rbac_policy_path: Path
    executables_policy_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        source_control_plane = Path(__file__).resolve().parents[3]
        control_plane = Path(os.environ.get("OPS_CONTROL_PLANE_ROOT", source_control_plane))
        broker = control_plane / "broker"
        return cls(
            database_path=Path(
                os.environ.get("OPS_BROKER_DATABASE", "/var/lib/ops-broker/state.db")
            ),
            runbooks_path=Path(os.environ.get("OPS_BROKER_RUNBOOKS", control_plane / "runbooks")),
            action_policy_path=Path(
                os.environ.get("OPS_BROKER_ACTION_POLICY", control_plane / "policies/actions.yaml")
            ),
            rbac_policy_path=Path(
                os.environ.get("OPS_BROKER_RBAC_POLICY", broker / "config/rbac.yaml")
            ),
            executables_policy_path=Path(
                os.environ.get("OPS_BROKER_EXECUTABLES_POLICY", broker / "config/executables.yaml")
            ),
        )
