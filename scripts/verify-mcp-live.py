#!/usr/bin/python3
"""Read-only live handshake for every supervised MCP adapter."""

from __future__ import annotations

import argparse
import asyncio
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


SERVER_TEMPLATES = {
    "broker": {
        "command": "/usr/bin/sudo",
        "args": [
            "-n", "-u", "opsbroker", "-g", "opsbroker", "--",
            "/usr/local/libexec/ops-broker/ops-broker-mcp-{profile}",
        ],
        "count": 31,
        "health_tool": "verify_audit_chain",
        "arguments": {},
    },
    "orchestrator": {
        "command": "/usr/local/libexec/ops-orchestrator/ops-orchestrator-mcp-{profile}",
        "count": 5,
        "health_tool": "get_orchestrator_health",
        "arguments": {"actor_id": "{actor}"},
    },
    "memory": {
        "command": "/opt/ops-memory/bin/ops-memory-mcp",
        "count": 7,
        "health_tool": "memory_health",
        "arguments": {},
        "env": {"OPS_MEMORY_MCP_ACTOR": "{actor}"},
    },
}


def profile_spec(name: str, profile: str) -> dict[str, object]:
    actor = f"{profile}-supervised"
    template = SERVER_TEMPLATES[name]
    return {
        **template,
        "command": str(template["command"]).format(profile=profile),
        "args": [str(value).format(profile=profile) for value in template.get("args", [])],
        "arguments": {
            key: str(value).format(actor=actor)
            for key, value in template.get("arguments", {}).items()
        },
        "env": {
            key: str(value).format(actor=actor)
            for key, value in template.get("env", {}).items()
        },
    }


async def verify(name: str, profile: str) -> None:
    spec = profile_spec(name, profile)
    environment = dict(os.environ)
    environment.update(spec.get("env", {}))
    parameters = StdioServerParameters(
        command=spec["command"],
        args=spec.get("args", []),
        env=environment,
    )
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await asyncio.wait_for(session.initialize(), timeout=15)
            tools = await asyncio.wait_for(session.list_tools(), timeout=15)
            names = {tool.name for tool in tools.tools}
            if len(names) != spec["count"]:
                raise RuntimeError(f"{name}: expected {spec['count']} tools, got {len(names)}")
            result = await asyncio.wait_for(
                session.call_tool(spec["health_tool"], spec["arguments"]), timeout=20
            )
            structured = result.structured_content
            if result.is_error or not isinstance(structured, dict) or structured.get("ok") is not True:
                raise RuntimeError(f"{name}: read-only health tool failed")
    print(f"{profile}/{name}: ok ({spec['count']} tools)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("codex", "claude"), default="codex")
    parser.add_argument("servers", nargs="*", choices=sorted(SERVER_TEMPLATES))
    arguments = parser.parse_args()
    selected = arguments.servers or list(SERVER_TEMPLATES)
    for server in selected:
        asyncio.run(verify(server, arguments.profile))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
