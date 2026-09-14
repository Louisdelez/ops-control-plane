#!/usr/bin/env python3
"""Offline invariants for the pinned Hermes 0.21 operations profiles."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import sys
import tomllib
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parent.parent
HERMES = ROOT / "config" / "hermes"
BROKER_WRAPPER = "/usr/local/libexec/ops-broker-mcp-profile"
ORCHESTRATOR_WRAPPER = "/usr/local/libexec/ops-orchestrator-mcp-profile"
MEMORY_WRAPPER = "/usr/local/libexec/ops-memory-mcp-profile"
FACADE_PROVIDER = "custom:ops-orchestrator-hermes"
FACADE_MODEL = "qwen-coordinator"
FACADE_ENTRY = {
    "api": "http://127.0.0.1:8643/v1",
    "key_env": "HERMES_FACADE_TOKEN",
    "transport": "chat_completions",
    "default_model": FACADE_MODEL,
    "discover_models": False,
    "models": {FACADE_MODEL: {"context_length": 64_000}},
}
DISABLED = {
    "terminal", "file", "code_execution", "delegation", "browser",
    "computer_use", "cronjob", "web",
    "kanban",
}
IDENTITIES = {
    "default": "hermes-coordinator",
    "minecraft-ops": "minecraft-monitor",
    "infra-shared": "infra-operator",
    "network-shared": "infra-network",
    "monitoring-shared": "monitoring-shared",
    "backup-shared": "backup-shared",
    "deploy-ops": "deploy-ops",
    "security-ops": "security-ops",
}
PROFILES = set(IDENTITIES)
BROKER_BASE = {
    "list_authorized_runbooks", "create_mission", "list_open_missions",
    "get_mission", "list_actions_for_mission", "create_incident",
    "list_open_incidents", "get_incident", "list_actions_for_incident",
    "request_runbook_action", "get_action", "get_mission_state",
    "list_mission_checkpoints", "list_mission_records", "list_model_traces",
    "get_incident_details", "get_action_artifacts", "verify_audit_chain",
}
BROKER_EXPECTED = {
    "minecraft-ops": BROKER_BASE,
    "infra-shared": BROKER_BASE,
    "network-shared": BROKER_BASE | {"execute_approved_action"},
    "monitoring-shared": BROKER_BASE | {
        "set_incident_status", "add_mission_record", "record_model_trace",
        "update_incident_details",
    },
    "backup-shared": BROKER_BASE | {
        "set_mission_status", "set_incident_status", "update_mission_state",
        "create_mission_checkpoint", "add_mission_record", "claim_next_mission",
        "update_mission_lease", "acquire_resource_lock", "update_resource_lock",
        "record_model_trace", "update_incident_details",
    },
    "deploy-ops": set(),  # Replaced with the complete broker surface below.
    "security-ops": BROKER_BASE | {
        "set_incident_status", "update_mission_state", "create_mission_checkpoint",
        "add_mission_record", "record_model_trace", "update_incident_details",
    },
}
ORCHESTRATOR_EXPECTED = {
    "get_orchestrator_health", "get_model_budgets", "get_model_trace",
    "get_model_performance", "route_model_task", "record_orchestrator_signal",
}
MEMORY_EXPECTED = {
    "default": {
        "memory_search", "memory_get", "memory_ingest", "memory_summarize",
        "memory_invalidate", "memory_stats", "memory_health",
    },
    "minecraft-ops": {"memory_search", "memory_get", "memory_ingest", "memory_summarize", "memory_health"},
    "infra-shared": {"memory_search", "memory_get", "memory_ingest", "memory_summarize", "memory_health"},
    "network-shared": {"memory_search", "memory_get", "memory_ingest", "memory_summarize", "memory_health"},
    "monitoring-shared": {"memory_search", "memory_get", "memory_ingest", "memory_stats", "memory_health"},
    "backup-shared": {"memory_search", "memory_get", "memory_ingest", "memory_health"},
    "deploy-ops": {"memory_search", "memory_get", "memory_ingest", "memory_summarize", "memory_health"},
    "security-ops": {
        "memory_search", "memory_get", "memory_ingest", "memory_summarize",
        "memory_invalidate", "memory_stats", "memory_health",
    },
}
errors: list[str] = []


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader: UniqueLoader, node: yaml.nodes.MappingNode, deep: bool = False) -> dict[Any, Any]:
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def fail(path: Path, message: str) -> None:
    errors.append(f"{path.relative_to(ROOT)}: {message}")


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except Exception as exc:
        fail(path, f"invalid YAML: {exc}")
        return {}
    if not isinstance(value, dict):
        fail(path, "top level must be a mapping")
        return {}
    return value


def config_path(profile: str) -> Path:
    if profile == "default":
        return HERMES / "default" / "config.yaml"
    return HERMES / "profiles" / profile / "config.yaml"


def decorated_mcp_tools(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    tools: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            call = decorator if isinstance(decorator, ast.Call) else None
            attribute = call.func if call is not None else decorator
            if (
                isinstance(attribute, ast.Attribute)
                and isinstance(attribute.value, ast.Name)
                and attribute.value.id == "mcp"
                and attribute.attr == "tool"
            ):
                tools.add(node.name)
    return tools


BROKER_TOOLS = decorated_mcp_tools(ROOT / "broker" / "src" / "ops_broker" / "mcp_server.py")
ORCHESTRATOR_TOOLS = decorated_mcp_tools(
    ROOT / "orchestrator" / "src" / "ops_orchestrator" / "mcp_server.py"
)
MEMORY_TOOLS = decorated_mcp_tools(ROOT / "memory" / "src" / "ops_memory" / "mcp_server.py")
BROKER_EXPECTED["default"] = BROKER_TOOLS
BROKER_EXPECTED["deploy-ops"] = BROKER_TOOLS


def validate_mcp_server(
    path: Path,
    name: str,
    server: Any,
    *,
    command: str,
    argument: str,
    expected_tools: set[str],
    actual_tools: set[str],
) -> None:
    if not isinstance(server, dict):
        fail(path, f"mcp_servers.{name} must be a mapping")
        return
    if server.get("command") != command or server.get("args") != [argument]:
        fail(path, f"{name} command or fixed identity does not match the profile")
    if server.get("env") != {} or server.get("enabled") is not True:
        fail(path, f"{name} must be enabled with an empty explicit environment")
    if server.get("trust") != "untrusted":
        fail(path, f"{name} must retain the untrusted boundary")
    if "url" in server or "headers" in server:
        fail(path, f"network transport is forbidden for {name}")
    if server.get("sampling", {}).get("enabled") is not False:
        fail(path, f"MCP sampling must be disabled for {name}")
    if server.get("elicitation", {}).get("enabled") is not False:
        fail(path, f"MCP elicitation must be disabled for {name}")
    if server.get("supports_parallel_tool_calls") is not False:
        fail(path, f"parallel MCP calls must be disabled for {name}")
    tools = server.get("tools", {})
    includes = set(tools.get("include", [])) if isinstance(tools, dict) else set()
    if includes != expected_tools:
        fail(path, f"{name} tool allowlist does not match the least-privilege profile")
    if not includes <= actual_tools:
        fail(path, f"{name} includes a tool absent from the installed adapter")
    if tools.get("resources") is not False or tools.get("prompts") is not False:
        fail(path, f"{name} resources and prompts must be disabled")
    forbidden = {tool for tool in includes if "secret" in tool or tool.startswith("approve")}
    if forbidden:
        fail(path, f"{name} exposes secret or approval tools: {sorted(forbidden)}")


def validate_profile(profile: str) -> None:
    path = config_path(profile)
    cfg = load_yaml(path)
    if cfg.get("_config_version") != 39:
        fail(path, "schema marker must match Hermes 0.21 config version 39")
    model = cfg.get("model", {})
    if model != {
        "provider": FACADE_PROVIDER,
        "default": FACADE_MODEL,
        "context_length": 64_000,
        "api_mode": "chat_completions",
    }:
        fail(path, "the main model must be the exact local orchestrator facade alias")
    if cfg.get("providers") != {"ops-orchestrator-hermes": FACADE_ENTRY}:
        fail(path, "the exact loopback facade must be the only named provider")
    if "fallback_providers" in cfg or "fallback_model" in cfg:
        fail(path, "direct Hermes provider fallback is forbidden; the orchestrator owns escalation")
    if "enabled_toolsets" in cfg.get("agent", {}):
        fail(path, "agent.enabled_toolsets is not a persisted 0.21 config key")
    if cfg.get("agent", {}).get("reasoning_effort") != "none":
        fail(path, "the custom facade provider must keep Hermes reasoning disabled")
    if set(cfg.get("agent", {}).get("disabled_toolsets", [])) != DISABLED:
        fail(path, "unsafe built-in toolsets are not all disabled")
    expected_secret = f"/usr/local/libexec/hermes-secrets-env {profile}"
    if cfg.get("secrets", {}).get("command", {}).get("command") != expected_secret:
        fail(path, "unexpected secrets.command helper")
    skills = cfg.get("skills", {})
    expected_skill_policy = {
        "external_dirs": [], "project_discovery": False, "inline_shell": False,
        "guard_agent_created": True, "write_approval": True,
    }
    if skills != expected_skill_policy or cfg.get("curator", {}).get("enabled") is not False:
        fail(path, "procedural skill policy is not fail closed")
    for platform in ("api_server", "cli"):
        if cfg.get("platform_toolsets", {}).get(platform) != ["clarify", "skills"]:
            fail(path, f"platform_toolsets.{platform} must contain clarify and guarded skills")
    if profile != "default" and cfg.get("platforms", {}).get("api_server", {}).get("enabled") is not False:
        fail(path, "secondary multiplex profile must not bind an API listener")

    servers = cfg.get("mcp_servers")
    if not isinstance(servers, dict) or set(servers) != {"ops-broker", "ops-orchestrator", "ops-memory"}:
        fail(path, "exactly the three local MCP servers must be configured")
        return
    validate_mcp_server(
        path, "ops-broker", servers["ops-broker"],
        command=BROKER_WRAPPER, argument=IDENTITIES[profile],
        expected_tools=BROKER_EXPECTED[profile], actual_tools=BROKER_TOOLS,
    )
    validate_mcp_server(
        path, "ops-orchestrator", servers["ops-orchestrator"],
        command=ORCHESTRATOR_WRAPPER, argument=profile,
        expected_tools=ORCHESTRATOR_EXPECTED, actual_tools=ORCHESTRATOR_TOOLS,
    )
    validate_mcp_server(
        path, "ops-memory", servers["ops-memory"],
        command=MEMORY_WRAPPER, argument=profile,
        expected_tools=MEMORY_EXPECTED[profile], actual_tools=MEMORY_TOOLS,
    )


def validate_skill(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        fail(path, "missing YAML frontmatter")
        return
    end = text.find("\n---\n", 4)
    metadata = yaml.load(text[4:end], Loader=UniqueLoader)
    if not isinstance(metadata, dict):
        fail(path, "frontmatter must be a mapping")
        return
    if metadata.get("name") != path.parent.name:
        fail(path, "frontmatter name must match its directory")
    description = metadata.get("description")
    if not isinstance(description, str) or not 1 <= len(description) <= 1024:
        fail(path, "description must contain 1..1024 characters")


def main() -> int:
    for profile in sorted(PROFILES):
        validate_profile(profile)

    template = load_yaml(HERMES / "profile-config.yaml.in")
    if template.get("mcp_servers") != {}:
        fail(HERMES / "profile-config.yaml.in", "new-profile template must fail closed")
    if template.get("agent", {}).get("reasoning_effort") != "none":
        fail(HERMES / "profile-config.yaml.in", "new-profile template must keep Hermes reasoning disabled")
    if template.get("model") != {
        "provider": FACADE_PROVIDER,
        "default": FACADE_MODEL,
        "context_length": 64_000,
        "api_mode": "chat_completions",
    } or template.get("providers") != {"ops-orchestrator-hermes": FACADE_ENTRY}:
        fail(HERMES / "profile-config.yaml.in", "new-profile template must use only the loopback facade")

    for path in sorted((HERMES / "skills").glob("*/SKILL.md")):
        validate_skill(path)
    expected_skills = {
        "incident-triage", "deploy-release", "backup-verification",
        "shared-infra-change", "ops-deterministic-workflows",
    }
    actual_skills = {path.parent.name for path in (HERMES / "skills").glob("*/SKILL.md")}
    if actual_skills != expected_skills:
        errors.append("config/hermes/skills: unexpected procedural skill set")
    registry_path = HERMES / "skill-registry.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        entries = registry.get("skills", []) if isinstance(registry, dict) else []
        registry_names = [entry.get("name") for entry in entries if isinstance(entry, dict)]
    except (OSError, json.JSONDecodeError):
        registry_names = []
    if (
        len(registry_names) != len(expected_skills)
        or set(registry_names) != expected_skills
        or any(not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) for name in registry_names)
    ):
        fail(registry_path, "registry must name the exact five safe V1 skill directories")

    if len(BROKER_TOOLS) != 31 or len(ORCHESTRATOR_TOOLS) != 9 or len(MEMORY_TOOLS) != 8:
        errors.append(
            "MCP adapters: expected exactly 31 broker, 9 orchestrator and 8 memory tools"
        )

    rbac = load_yaml(ROOT / "broker" / "config" / "rbac.yaml")
    actors = rbac.get("actors", {})
    for profile, actor in IDENTITIES.items():
        if not isinstance(actors, dict) or actor not in actors:
            fail(ROOT / "broker" / "config" / "rbac.yaml", f"missing Hermes actor {profile}:{actor}")

    memory_config = tomllib.loads(
        (ROOT / "memory" / "config" / "memory.toml").read_text(encoding="utf-8")
    )
    memory_actors = memory_config.get("actors", {})
    memory_names = {
        "default": "hermes-coordinator",
        **{profile: profile for profile in PROFILES if profile != "default"},
    }
    for profile, actor in memory_names.items():
        if actor not in memory_actors:
            fail(ROOT / "memory" / "config" / "memory.toml", f"missing fixed memory actor for {profile}")

    corpus = "\n".join(path.read_text(encoding="utf-8") for path in HERMES.rglob("*") if path.is_file())
    for forbidden in ("127.0.0.1:8765", "mcp-ops-broker", "OPS_BROKER_TOKEN", "api_mode: responses"):
        if forbidden in corpus:
            errors.append(f"config/hermes: forbidden legacy value present: {forbidden}")

    selection_path = ROOT / "config" / "ollama" / "selection.json"
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    if selection.get("production_model") is not None or selection.get("automatic_promotion") is not False:
        fail(selection_path, "a local model must not be promoted automatically")
    if selection.get("hermes_runtime", {}).get("eligible") is not False:
        fail(selection_path, "local Ollama must remain ineligible for Hermes runtime")

    hermes_unit = (ROOT / "systemd" / "hermes-gateway.service").read_text(encoding="utf-8")
    for required_unit_rail in (
        "Requires=ops-orchestrator.service ops-orchestrator-hermes-facade.service ops-memory.service",
        "Conflicts=ollama.service",
        "IPAddressDeny=any",
        "IPAddressAllow=localhost",
    ):
        if required_unit_rail not in hermes_unit:
            fail(ROOT / "systemd" / "hermes-gateway.service", f"missing rail: {required_unit_rail}")

    if errors:
        print("Hermes configuration validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        f"Hermes configuration OK: {len(PROFILES)} profiles, {len(actual_skills)} skills, "
        f"31+6+7 allowlisted MCP tools (9 orchestrator available), "
        "one budgeted multi-provider API facade, no promoted local model"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
