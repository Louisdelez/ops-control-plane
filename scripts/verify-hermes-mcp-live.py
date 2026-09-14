#!/usr/bin/python3
"""Read-only live handshake for every fixed-identity Hermes MCP profile."""

from __future__ import annotations

import asyncio
import os

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


PROFILES = {
    "default": ("hermes-coordinator", "hermes-coordinator"),
    "minecraft-ops": ("minecraft-monitor", "minecraft-ops"),
    "infra-shared": ("infra-operator", "infra-shared"),
    "network-shared": ("infra-network", "network-shared"),
    "monitoring-shared": ("monitoring-shared", "monitoring-shared"),
    "backup-shared": ("backup-shared", "backup-shared"),
    "deploy-ops": ("deploy-ops", "deploy-ops"),
    "security-ops": ("security-ops", "security-ops"),
}


async def check(
    profile: str,
    server: str,
    command: str,
    arguments: list[str],
    expected_count: int,
    health_tool: str,
    health_arguments: dict[str, str],
) -> None:
    parameters = StdioServerParameters(command=command, args=arguments, env=dict(os.environ))
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await asyncio.wait_for(session.initialize(), timeout=15)
            available = await asyncio.wait_for(session.list_tools(), timeout=15)
            if len(available.tools) != expected_count:
                raise RuntimeError(
                    f"{profile}/{server}: expected {expected_count} tools, "
                    f"got {len(available.tools)}"
                )
            result = await asyncio.wait_for(
                session.call_tool(health_tool, health_arguments), timeout=20
            )
            if result.is_error:
                raise RuntimeError(f"{profile}/{server}: read-only health check failed")
    print(f"{profile}/{server}: ok ({expected_count} tools)")


async def main() -> None:
    if os.geteuid() == 0:
        raise RuntimeError("run this verifier as the hermesd service identity")
    for profile, (broker_actor, local_actor) in PROFILES.items():
        await check(
            profile,
            "broker",
            "/usr/local/libexec/ops-broker-mcp-profile",
            [broker_actor],
            31,
            "verify_audit_chain",
            {},
        )
        await check(
            profile,
            "orchestrator",
            "/usr/local/libexec/ops-orchestrator-mcp-profile",
            [profile],
            5,
            "get_orchestrator_health",
            {"actor_id": local_actor},
        )
        await check(
            profile,
            "memory",
            "/usr/local/libexec/ops-memory-mcp-profile",
            [profile],
            7,
            "memory_health",
            {},
        )


if __name__ == "__main__":
    asyncio.run(main())
