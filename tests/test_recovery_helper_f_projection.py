from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import runpy
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
HELPER_F = ROOT / "artifacts/recovery-patch/control-plane-bootstrap.f"
MANIFEST_D = ROOT / "tests/fixtures/atlas-anchor-d/release-manifest.v1.json"
MANIFEST_E = ROOT / "artifacts/recovery-patch/release-manifest.e.v1.json"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def test_f_interface_projects_to_exact_d_deployment_manifest() -> None:
    program = runpy.run_path(str(HELPER_F), run_name="test_recovery_f_projection")
    manifest_d = json.loads(MANIFEST_D.read_bytes())
    manifest_f = copy.deepcopy(json.loads(MANIFEST_E.read_bytes()))
    manifest_f["bootstrap"]["helper_sha256"] = "f" * 64
    manifest_f["bootstrap"]["resume_patch"] = program["RESUME_PATCH_CONTRACT"]

    projected = program["_resume_patch_deployment_manifest"](
        manifest_f,
        program["RESUME_PATCH_DEPLOYMENT"]["source_tree_sha256"],
    )

    assert projected == manifest_d
    assert hashlib.sha256(_canonical(projected) + b"\n").hexdigest() == (
        program["RESUME_PATCH_DEPLOYMENT_MANIFEST_CANONICAL_SHA256"]
    )


def test_hidden_setter_modes_are_exact_dispatches() -> None:
    source = HELPER_F.read_text(encoding="utf-8")
    assert 'arguments == ["--network-baseline-apply"]' in source
    assert '["--network-baseline-restore"]' in source
    assert "return network_baseline_unit_entry(" in source
