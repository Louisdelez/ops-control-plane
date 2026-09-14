"""Strict YAML runbook registry and argv renderer."""

from __future__ import annotations

import re
import string
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import BrokerError, NotFound
from .util import contains_sensitive_name


_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9.-]{1,127}$")
_PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_PLACEHOLDER = re.compile(r"^\{([a-zA-Z_][a-zA-Z0-9_]*)\}$")
_ACTOR_SAFE_VALUE = re.compile(r"^[^\x00\r\n]{1,4096}$")
_ARGV_ENUM_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._@/+,-]{0,255}$")
_SHELL_EXECUTABLES = {
    "/bin/bash",
    "/bin/dash",
    "/bin/sh",
    "/bin/zsh",
    "/usr/bin/bash",
    "/usr/bin/env",
    "/usr/bin/fish",
    "/usr/bin/sh",
    "/usr/bin/zsh",
}


class RunbookValidationError(ValueError):
    pass


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RunbookValidationError(f"{location} must be a mapping")
    return value


def _only_keys(value: dict[str, Any], allowed: set[str], location: str) -> None:
    extra = set(value) - allowed
    if extra:
        raise RunbookValidationError(f"{location} has unsupported keys: {sorted(extra)}")


@dataclass(frozen=True)
class ExecutablePolicy:
    executables: frozenset[str]
    sudo_executable: str | None = None
    sudo_required_prefix: tuple[str, ...] = ("-n",)
    sudo_helpers: frozenset[str] = frozenset()

    @classmethod
    def load(cls, path: Path) -> "ExecutablePolicy":
        raw = _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), str(path))
        _only_keys(raw, {"version", "executables", "sudo"}, str(path))
        if raw.get("version") != 1:
            raise RunbookValidationError(f"{path} has an unsupported version")
        executables = raw.get("executables")
        if not isinstance(executables, list) or not all(isinstance(item, str) for item in executables):
            raise RunbookValidationError(f"{path}: executables must be a string list")

        sudo_raw = raw.get("sudo")
        sudo_executable: str | None = None
        sudo_prefix: tuple[str, ...] = ("-n",)
        sudo_helpers: frozenset[str] = frozenset()
        if sudo_raw is not None:
            sudo = _mapping(sudo_raw, f"{path}:sudo")
            _only_keys(sudo, {"executable", "required_prefix", "allowed_helpers"}, f"{path}:sudo")
            sudo_executable = sudo.get("executable")
            prefix = sudo.get("required_prefix", ["-n"])
            helpers = sudo.get("allowed_helpers", [])
            if not isinstance(sudo_executable, str):
                raise RunbookValidationError(f"{path}: sudo.executable must be a string")
            if not isinstance(prefix, list) or not all(isinstance(item, str) for item in prefix):
                raise RunbookValidationError(f"{path}: sudo.required_prefix must be a string list")
            if not isinstance(helpers, list) or not all(isinstance(item, str) for item in helpers):
                raise RunbookValidationError(f"{path}: sudo.allowed_helpers must be a string list")
            sudo_prefix = tuple(prefix)
            sudo_helpers = frozenset(helpers)

        policy = cls(
            executables=frozenset(executables),
            sudo_executable=sudo_executable,
            sudo_required_prefix=sudo_prefix,
            sudo_helpers=sudo_helpers,
        )
        policy._validate_paths()
        return policy

    def _validate_paths(self) -> None:
        all_paths = set(self.executables) | set(self.sudo_helpers)
        if self.sudo_executable:
            all_paths.add(self.sudo_executable)
        for path in all_paths:
            if not Path(path).is_absolute():
                raise RunbookValidationError(f"allowlisted executable is not absolute: {path}")
            if path in _SHELL_EXECUTABLES:
                raise RunbookValidationError(f"shell executables cannot be allowlisted: {path}")
        if self.sudo_executable and self.sudo_required_prefix != ("-n",):
            raise RunbookValidationError("V0 requires sudo's exact non-interactive -n prefix")

    def validate_argv_template(self, argv: tuple[str, ...], location: str) -> None:
        executable = argv[0]
        if executable in _SHELL_EXECUTABLES:
            raise RunbookValidationError(f"{location}: shell execution is forbidden")
        if self.sudo_executable and executable == self.sudo_executable:
            prefix_end = 1 + len(self.sudo_required_prefix)
            if tuple(argv[1:prefix_end]) != self.sudo_required_prefix:
                raise RunbookValidationError(f"{location}: sudo must use the required fixed prefix")
            if len(argv) <= prefix_end or argv[prefix_end] not in self.sudo_helpers:
                raise RunbookValidationError(f"{location}: sudo helper is not allowlisted")
            return
        if executable not in self.executables:
            raise RunbookValidationError(f"{location}: executable is not allowlisted: {executable}")


@dataclass(frozen=True)
class ParameterSpec:
    kind: str
    values: tuple[str, ...] = ()
    max_length: int = 0
    minimum: int | None = None
    maximum: int | None = None

    def validate(self, name: str, value: Any) -> str | int | bool:
        if self.kind == "enum":
            if not isinstance(value, str) or value not in self.values:
                raise BrokerError("invalid_parameters", f"{name} is not an allowed value", status_code=422)
            return value
        if self.kind == "uuid":
            if not isinstance(value, str):
                raise BrokerError("invalid_parameters", f"{name} must be a UUID", status_code=422)
            try:
                return str(uuid.UUID(value))
            except ValueError as exc:
                raise BrokerError("invalid_parameters", f"{name} must be a UUID", status_code=422) from exc
        if self.kind == "string":
            if not isinstance(value, str) or not _ACTOR_SAFE_VALUE.fullmatch(value):
                raise BrokerError("invalid_parameters", f"{name} must be a bounded text string", status_code=422)
            if len(value) > self.max_length:
                raise BrokerError("invalid_parameters", f"{name} is too long", status_code=422)
            return value
        if self.kind == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise BrokerError("invalid_parameters", f"{name} must be an integer", status_code=422)
            if self.minimum is not None and value < self.minimum:
                raise BrokerError("invalid_parameters", f"{name} is below its minimum", status_code=422)
            if self.maximum is not None and value > self.maximum:
                raise BrokerError("invalid_parameters", f"{name} is above its maximum", status_code=422)
            return value
        if self.kind == "boolean":
            if not isinstance(value, bool):
                raise BrokerError("invalid_parameters", f"{name} must be a boolean", status_code=422)
            return value
        raise AssertionError(f"unhandled parameter kind: {self.kind}")


@dataclass(frozen=True)
class BudgetSpec:
    key_template: str
    max_attempts: int
    window_seconds: int


@dataclass(frozen=True)
class Runbook:
    id: str
    version: int
    project: str
    action_class: str
    description: str
    timeout_seconds: int
    argv_template: tuple[str, ...] | None
    parameters: dict[str, ParameterSpec]
    budget: BudgetSpec | None
    output_max_bytes: int
    source: Path

    def validate_parameters(self, supplied: dict[str, Any]) -> dict[str, str | int | bool]:
        if not isinstance(supplied, dict):
            raise BrokerError("invalid_parameters", "parameters must be an object", status_code=422)
        missing = set(self.parameters) - set(supplied)
        unknown = set(supplied) - set(self.parameters)
        if missing or unknown:
            raise BrokerError(
                "invalid_parameters",
                "parameter names do not match the runbook schema",
                status_code=422,
                details={"missing": sorted(missing), "unknown": sorted(unknown)},
            )
        return {name: spec.validate(name, supplied[name]) for name, spec in self.parameters.items()}

    def render_argv(self, parameters: dict[str, str | int | bool]) -> tuple[str, ...] | None:
        if self.argv_template is None:
            return None
        rendered: list[str] = []
        for item in self.argv_template:
            match = _PLACEHOLDER.fullmatch(item)
            if match:
                value = str(parameters[match.group(1)])
                if value.startswith("-") or "\x00" in value or "\n" in value or "\r" in value:
                    raise BrokerError("invalid_parameters", "parameter is unsafe in argv", status_code=422)
                rendered.append(value)
            else:
                rendered.append(item)
        return tuple(rendered)

    def budget_key(self, parameters: dict[str, str | int | bool]) -> str | None:
        if self.budget is None:
            return None
        rendered: list[str] = []
        formatter = string.Formatter()
        for literal, field_name, format_spec, conversion in formatter.parse(self.budget.key_template):
            rendered.append(literal)
            if field_name is not None:
                if format_spec or conversion:
                    raise AssertionError("format options were rejected at registry load")
                rendered.append(str(parameters[field_name]))
        return f"{self.id}:{''.join(rendered)}"

    def public_metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "project": self.project,
            "action_class": self.action_class,
            "description": self.description,
            "parameters": {
                name: {
                    "type": spec.kind,
                    **({"values": list(spec.values)} if spec.values else {}),
                    **({"max_length": spec.max_length} if spec.max_length else {}),
                }
                for name, spec in self.parameters.items()
            },
        }


class RunbookRegistry:
    def __init__(self, runbooks: dict[str, Runbook]) -> None:
        self._runbooks = dict(runbooks)

    @classmethod
    def load(cls, root: Path, executables: ExecutablePolicy) -> "RunbookRegistry":
        root = root.resolve(strict=True)
        runbooks: dict[str, Runbook] = {}
        for path in sorted(root.rglob("*.yaml")):
            if path.is_symlink():
                raise RunbookValidationError(f"symlinked runbook is forbidden: {path}")
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise RunbookValidationError(f"runbook escapes registry root: {path}")
            if path.stat().st_size > 131_072:
                raise RunbookValidationError(f"runbook is too large: {path}")
            runbook = _load_runbook(path, executables)
            if runbook.id in runbooks:
                raise RunbookValidationError(f"duplicate runbook id: {runbook.id}")
            runbooks[runbook.id] = runbook
        if not runbooks:
            raise RunbookValidationError(f"no runbooks found under {root}")
        return cls(runbooks)

    def get(self, runbook_id: str) -> Runbook:
        try:
            return self._runbooks[runbook_id]
        except KeyError as exc:
            raise NotFound("runbook") from exc

    def all(self) -> tuple[Runbook, ...]:
        return tuple(self._runbooks[key] for key in sorted(self._runbooks))


def _parameter_spec(name: str, raw_value: Any, location: str) -> ParameterSpec:
    if contains_sensitive_name(name):
        raise RunbookValidationError(f"{location}: secret-like parameter names are forbidden")
    raw = _mapping(raw_value, location)
    _only_keys(raw, {"type", "values", "max_length", "minimum", "maximum"}, location)
    kind = raw.get("type")
    if kind not in {"enum", "uuid", "string", "integer", "boolean"}:
        raise RunbookValidationError(f"{location}: unsupported parameter type")
    if kind == "enum":
        values = raw.get("values")
        if not isinstance(values, list) or not values or not all(isinstance(item, str) for item in values):
            raise RunbookValidationError(f"{location}: enum values must be a non-empty string list")
        if any(not _ARGV_ENUM_VALUE.fullmatch(item) for item in values):
            raise RunbookValidationError(f"{location}: enum contains an unsafe argv value")
        return ParameterSpec(kind=kind, values=tuple(values))
    if kind == "string":
        max_length = raw.get("max_length")
        if not isinstance(max_length, int) or isinstance(max_length, bool) or not 1 <= max_length <= 4096:
            raise RunbookValidationError(f"{location}: string max_length must be between 1 and 4096")
        return ParameterSpec(kind=kind, max_length=max_length)
    if kind == "integer":
        minimum = raw.get("minimum")
        maximum = raw.get("maximum")
        if minimum is not None and (not isinstance(minimum, int) or isinstance(minimum, bool)):
            raise RunbookValidationError(f"{location}: minimum must be an integer")
        if maximum is not None and (not isinstance(maximum, int) or isinstance(maximum, bool)):
            raise RunbookValidationError(f"{location}: maximum must be an integer")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise RunbookValidationError(f"{location}: minimum exceeds maximum")
        return ParameterSpec(kind=kind, minimum=minimum, maximum=maximum)
    return ParameterSpec(kind=kind)


def _load_runbook(path: Path, executables: ExecutablePolicy) -> Runbook:
    raw = _mapping(yaml.safe_load(path.read_text(encoding="utf-8")), str(path))
    _only_keys(
        raw,
        {
            "id",
            "version",
            "project",
            "action_class",
            "description",
            "timeout_seconds",
            "budget",
            "command",
            "parameters",
            "output",
        },
        str(path),
    )
    runbook_id = raw.get("id")
    project = raw.get("project")
    version = raw.get("version")
    action_class = raw.get("action_class")
    description = raw.get("description")
    timeout = raw.get("timeout_seconds")
    if not isinstance(runbook_id, str) or not _IDENTIFIER.fullmatch(runbook_id):
        raise RunbookValidationError(f"{path}: invalid id")
    if not isinstance(project, str) or not _PROJECT.fullmatch(project):
        raise RunbookValidationError(f"{path}: invalid project")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise RunbookValidationError(f"{path}: invalid version")
    if action_class not in {"A", "B", "C"}:
        raise RunbookValidationError(f"{path}: action_class must be A, B or C")
    if not isinstance(description, str) or not 1 <= len(description) <= 500:
        raise RunbookValidationError(f"{path}: description must be bounded text")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 300:
        raise RunbookValidationError(f"{path}: timeout_seconds must be between 1 and 300")

    parameters_raw = _mapping(raw.get("parameters", {}), f"{path}:parameters")
    parameters = {
        name: _parameter_spec(name, value, f"{path}:parameters.{name}")
        for name, value in parameters_raw.items()
        if isinstance(name, str)
    }
    if len(parameters) != len(parameters_raw):
        raise RunbookValidationError(f"{path}: parameter names must be strings")

    command_raw = raw.get("command")
    argv: tuple[str, ...] | None = None
    if command_raw is not None:
        command = _mapping(command_raw, f"{path}:command")
        _only_keys(command, {"argv"}, f"{path}:command")
        raw_argv = command.get("argv")
        if not isinstance(raw_argv, list) or not raw_argv or not all(isinstance(item, str) for item in raw_argv):
            raise RunbookValidationError(f"{path}: command.argv must be a non-empty string list")
        argv = tuple(raw_argv)
        for index, item in enumerate(argv):
            if not item or "\x00" in item or "\n" in item or "\r" in item:
                raise RunbookValidationError(f"{path}: argv[{index}] is unsafe")
            placeholder = _PLACEHOLDER.fullmatch(item)
            if "{" in item or "}" in item:
                if placeholder is None or placeholder.group(1) not in parameters:
                    raise RunbookValidationError(f"{path}: argv placeholders must occupy a full argument")
                if parameters[placeholder.group(1)].kind == "string":
                    raise RunbookValidationError(f"{path}: free text cannot be interpolated into argv")
        if not Path(argv[0]).is_absolute():
            raise RunbookValidationError(f"{path}: argv executable must be absolute")
        executables.validate_argv_template(argv, str(path))
    elif action_class != "C":
        raise RunbookValidationError(f"{path}: only class C may omit a command")

    budget: BudgetSpec | None = None
    budget_raw = raw.get("budget")
    if action_class == "B":
        budget_map = _mapping(budget_raw, f"{path}:budget")
        _only_keys(budget_map, {"key", "max_attempts", "window_seconds"}, f"{path}:budget")
        key_template = budget_map.get("key")
        max_attempts = budget_map.get("max_attempts")
        window_seconds = budget_map.get("window_seconds")
        if not isinstance(key_template, str) or not 1 <= len(key_template) <= 256:
            raise RunbookValidationError(f"{path}: invalid budget key")
        for _, field_name, format_spec, conversion in string.Formatter().parse(key_template):
            if field_name is not None and field_name not in parameters:
                raise RunbookValidationError(f"{path}: unknown budget placeholder: {field_name}")
            if format_spec or conversion:
                raise RunbookValidationError(f"{path}: budget formatting options are forbidden")
        if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or not 1 <= max_attempts <= 10:
            raise RunbookValidationError(f"{path}: invalid max_attempts")
        if not isinstance(window_seconds, int) or isinstance(window_seconds, bool) or not 1 <= window_seconds <= 86_400:
            raise RunbookValidationError(f"{path}: invalid window_seconds")
        budget = BudgetSpec(key_template, max_attempts, window_seconds)
    elif budget_raw is not None:
        raise RunbookValidationError(f"{path}: budgets are only valid for class B")

    output_raw = _mapping(raw.get("output"), f"{path}:output")
    _only_keys(output_raw, {"max_bytes", "redact"}, f"{path}:output")
    max_bytes = output_raw.get("max_bytes")
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or not 1 <= max_bytes <= 65_536:
        raise RunbookValidationError(f"{path}: output.max_bytes must be between 1 and 65536")
    if output_raw.get("redact") is not True:
        raise RunbookValidationError(f"{path}: output.redact must be true")

    return Runbook(
        id=runbook_id,
        version=version,
        project=project,
        action_class=action_class,
        description=description,
        timeout_seconds=timeout,
        argv_template=argv,
        parameters=parameters,
        budget=budget,
        output_max_bytes=max_bytes,
        source=path,
    )
