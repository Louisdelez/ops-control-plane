from __future__ import annotations

from pathlib import Path

import pytest

from ops_broker.runbooks import ExecutablePolicy, RunbookRegistry, RunbookValidationError


def test_shell_cannot_be_allowlisted(tmp_path: Path) -> None:
    root = tmp_path / "runbooks"
    root.mkdir()
    (root / "unsafe.yaml").write_text(
        """
id: test.unsafe.v1
version: 1
project: infra
action_class: A
description: This must never load
timeout_seconds: 5
command:
  argv: [/bin/sh, -c, whoami]
parameters: {}
output:
  max_bytes: 1024
  redact: true
""".lstrip(),
        encoding="utf-8",
    )
    with pytest.raises(RunbookValidationError, match="shell"):
        RunbookRegistry.load(root, ExecutablePolicy(executables=frozenset({"/bin/sh"})))
