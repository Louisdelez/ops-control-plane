from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "deploy" / "ops-memory-qdrant-render-config"


def _load():
    loader = importlib.machinery.SourceFileLoader("ops_memory_qdrant_renderer", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_renderer_keeps_key_out_of_template_and_writes_private_runtime_file(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load()
    credential_dir = tmp_path / "credentials"
    runtime_dir = tmp_path / "runtime"
    credential_dir.mkdir(mode=0o700)
    runtime_dir.mkdir(mode=0o700)
    credential = credential_dir / "qdrant-api-key"
    credential.write_text("A" * 48 + "\n", encoding="ascii")
    credential.chmod(0o600)
    template = tmp_path / "qdrant.yaml"
    template.write_text("log_level: WARN\n\nservice:\n  host: 127.0.0.1\n", encoding="utf-8")
    template.chmod(0o644)
    output = runtime_dir / "production.yaml"
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credential_dir))
    monkeypatch.setattr(module, "EXPECTED_OUTPUT", output)
    monkeypatch.setattr(module, "EXPECTED_OUTPUT_OWNER", os.getuid())

    module.render(template, output)

    rendered = output.read_text(encoding="utf-8")
    assert 'api_key: "' + "A" * 48 + '"' in rendered
    assert "api_key" not in template.read_text(encoding="utf-8")
    assert output.stat().st_mode & 0o777 == 0o600
