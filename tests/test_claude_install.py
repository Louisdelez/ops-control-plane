from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).parents[1]


def test_claude_installer_parses_and_is_executable() -> None:
    installer = ROOT / "scripts" / "install-claude-code.sh"
    subprocess.run(["bash", "-n", str(installer)], check=True)
    assert os.access(installer, os.X_OK)
    source = installer.read_text(encoding="utf-8")
    assert "readonly claude_version=2.1.236" in source
    assert "gpg-pubkey-31ddde24ddfab679f42d7bd2baa929ff1a7ecace" in source
    assert "claude auth login" in source


def test_managed_claude_mcp_is_exactly_the_local_control_plane() -> None:
    document = json.loads(
        (ROOT / "config" / "claude" / "managed-mcp.json").read_text(encoding="utf-8")
    )
    assert set(document) == {"mcpServers"}
    servers = document["mcpServers"]
    assert set(servers) == {"ops-broker", "ops-orchestrator", "ops-memory"}
    assert all(server["type"] == "stdio" for server in servers.values())
    assert servers["ops-broker"]["command"] == "/usr/bin/sudo"
    assert servers["ops-orchestrator"]["command"].endswith("-mcp-claude")
    assert servers["ops-memory"]["env"] == {
        "OPS_MEMORY_MCP_ACTOR": "claude-supervised"
    }
