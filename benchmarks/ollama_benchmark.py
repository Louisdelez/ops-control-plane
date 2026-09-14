#!/usr/bin/env python3
"""Benchmark fail-closed Ollama models without downloading or promoting any model.

The real client only uses the read-only model inventory endpoint plus chat and
explicit unload calls.  The fixture client exercises the complete scoring and
reporting pipeline without opening a socket.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import http.client
import json
import math
import os
import platform
import random
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "ollama" / "benchmark.json"
DEFAULT_SCENARIOS = ROOT / "benchmarks" / "prompts.json"
DEFAULT_SCHEMA = ROOT / "benchmarks" / "decision.schema.json"
DEFAULT_RESULTS_ROOT = ROOT / "benchmarks" / "results"
ALLOWED_OLLAMA_ENDPOINTS = frozenset({"/api/tags", "/api/chat", "/api/generate"})
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
INTERRUPTED_EXIT_CODE = 75


class BenchmarkError(RuntimeError):
    """Controlled configuration or execution error."""


class CampaignInterrupted(BenchmarkError):
    """A fatal, retryable interruption of the whole benchmark campaign."""

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        invocation: Invocation | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.invocation = invocation


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Constante JSON non standard: {value}")


def _reject_duplicate_keys(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Cle JSON dupliquee: {key}")
        result[key] = value
    return result


def strict_json_loads(raw: str) -> Any:
    return json.loads(
        raw,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_reject_duplicate_keys,
    )


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_utc(value: dt.datetime | None = None) -> str:
    current = value or utc_now()
    return current.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(
                handle,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_reject_duplicate_keys,
            )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise BenchmarkError(f"Impossible de lire le JSON {path}: {exc}") from exc


def stable_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json_bytes(value)).hexdigest()


def json_type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return False


def validate_schema(value: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """Validate the small JSON-Schema subset used by this benchmark.

    Keeping this validator in the standard library makes the benchmark usable
    on a freshly reset host. Unsupported schema keywords are intentionally
    ignored; every keyword used by ``decision.schema.json`` is handled here.
    """

    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type is not None:
        allowed = [expected_type] if isinstance(expected_type, str) else expected_type
        if not isinstance(allowed, list) or not any(
            isinstance(item, str) and json_type_matches(value, item) for item in allowed
        ):
            errors.append(f"{path}: type attendu {expected_type!r}")
            return errors

    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}: valeur differente de const")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: valeur hors enum")

    if isinstance(value, dict):
        required = schema.get("required", [])
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    errors.append(f"{path}: propriete requise absente: {key}")

        minimum_properties = schema.get("minProperties")
        maximum_properties = schema.get("maxProperties")
        if isinstance(minimum_properties, int) and len(value) < minimum_properties:
            errors.append(f"{path}: moins de {minimum_properties} proprietes")
        if isinstance(maximum_properties, int) and len(value) > maximum_properties:
            errors.append(f"{path}: plus de {maximum_properties} proprietes")

        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            properties = {}
        additional = schema.get("additionalProperties", True)
        for key, child in value.items():
            child_path = f"{path}.{key}"
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                errors.extend(validate_schema(child, child_schema, child_path))
            elif additional is False:
                errors.append(f"{child_path}: propriete supplementaire interdite")
            elif isinstance(additional, dict):
                errors.extend(validate_schema(child, additional, child_path))

    if isinstance(value, list):
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        if isinstance(minimum_items, int) and len(value) < minimum_items:
            errors.append(f"{path}: moins de {minimum_items} elements")
        if isinstance(maximum_items, int) and len(value) > maximum_items:
            errors.append(f"{path}: plus de {maximum_items} elements")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, child in enumerate(value):
                errors.extend(validate_schema(child, item_schema, f"{path}[{index}]"))

    if isinstance(value, str):
        minimum_length = schema.get("minLength")
        maximum_length = schema.get("maxLength")
        if isinstance(minimum_length, int) and len(value) < minimum_length:
            errors.append(f"{path}: longueur inferieure a {minimum_length}")
        if isinstance(maximum_length, int) and len(value) > maximum_length:
            errors.append(f"{path}: longueur superieure a {maximum_length}")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append(f"{path}: valeur inferieure a {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append(f"{path}: valeur superieure a {maximum}")

    return errors


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def read_meminfo() -> dict[str, float | None]:
    values: dict[str, int] = {}
    raw = _read_text(Path("/proc/meminfo")) or ""
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, tail = line.split(":", 1)
        fields = tail.strip().split()
        if not fields:
            continue
        try:
            values[key] = int(fields[0])
        except ValueError:
            continue
    return {
        "total_mib": round(values["MemTotal"] / 1024, 3)
        if "MemTotal" in values
        else None,
        "available_mib": round(values["MemAvailable"] / 1024, 3)
        if "MemAvailable" in values
        else None,
    }


def read_matching_process_rss(prefixes: Sequence[str]) -> tuple[float | None, int]:
    normalized = tuple(item.casefold() for item in prefixes if item)
    if not normalized or not Path("/proc").is_dir():
        return None, 0
    rss_kib = 0
    count = 0
    try:
        proc_entries = list(Path("/proc").iterdir())
    except OSError:
        return None, 0
    for entry in proc_entries:
        if not entry.name.isdigit():
            continue
        comm = _read_text(entry / "comm")
        if not comm or not comm.casefold().startswith(normalized):
            continue
        status = _read_text(entry / "status") or ""
        for line in status.splitlines():
            if not line.startswith("VmRSS:"):
                continue
            fields = line.split()
            if len(fields) >= 2:
                try:
                    rss_kib += int(fields[1])
                    count += 1
                except ValueError:
                    pass
            break
    return (round(rss_kib / 1024, 3), count)


def read_system_temperature() -> float | None:
    temperatures: list[float] = []
    paths = list(Path("/sys/class/thermal").glob("thermal_zone*/temp"))
    paths.extend(Path("/sys/class/hwmon").glob("hwmon*/temp*_input"))
    for path in paths:
        raw = _read_int(path)
        if raw is None:
            continue
        value = raw / 1000.0 if abs(raw) > 1000 else float(raw)
        if -20.0 <= value <= 150.0:
            temperatures.append(value)
    return round(max(temperatures), 3) if temperatures else None


def wait_for_temperature_cooldown(
    *,
    resume_temperature_c: float,
    timeout_seconds: float,
    poll_seconds: float,
    stable_samples: int,
) -> float:
    """Wait for bounded thermal headroom before one inference request."""

    numeric_values = (resume_temperature_c, timeout_seconds, poll_seconds)
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in numeric_values
    ):
        raise BenchmarkError("Parametres de refroidissement invalides")
    if (
        not 0.0 <= resume_temperature_c < 90.0
        or not 1.0 <= timeout_seconds <= 1800.0
        or not 0.1 <= poll_seconds <= min(60.0, timeout_seconds)
        or isinstance(stable_samples, bool)
        or not isinstance(stable_samples, int)
        or not 1 <= stable_samples <= 20
    ):
        raise BenchmarkError("Parametres de refroidissement hors bornes")

    deadline = time.monotonic() + timeout_seconds
    consecutive = 0
    last_temperature: float | None = None
    while True:
        last_temperature = read_system_temperature()
        if last_temperature is None:
            raise CampaignInterrupted(
                "temperature_unavailable",
                "Metrique de temperature systeme indisponible avant requete",
            )
        if last_temperature <= resume_temperature_c:
            consecutive += 1
            if consecutive >= stable_samples:
                return last_temperature
        else:
            consecutive = 0

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CampaignInterrupted(
                "thermal_cooldown_timeout",
                "Refroidissement borne expire avant requete Ollama",
            )
        time.sleep(min(poll_seconds, remaining))


def read_sysfs_gpu() -> dict[str, float | None]:
    used_bytes = 0
    total_bytes = 0
    seen_used = False
    seen_total = False
    for path in Path("/sys/class/drm").glob("card*/device/mem_info_vram_used"):
        value = _read_int(path)
        if value is not None and value >= 0:
            used_bytes += value
            seen_used = True
    for path in Path("/sys/class/drm").glob("card*/device/mem_info_vram_total"):
        value = _read_int(path)
        if value is not None and value >= 0:
            total_bytes += value
            seen_total = True
    return {
        "used_mib": round(used_bytes / (1024 * 1024), 3) if seen_used else None,
        "total_mib": round(total_bytes / (1024 * 1024), 3) if seen_total else None,
        "temperature_c": None,
        "source": "sysfs" if seen_used or seen_total else None,
    }


def read_nvidia_gpu(binary: str) -> dict[str, float | None]:
    resolved = shutil.which(binary) if not os.path.isabs(binary) else binary
    if not resolved or not Path(resolved).exists():
        return {
            "used_mib": None,
            "total_mib": None,
            "temperature_c": None,
            "source": None,
        }
    try:
        result = subprocess.run(
            [
                resolved,
                "--query-gpu=memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "used_mib": None,
            "total_mib": None,
            "temperature_c": None,
            "source": None,
        }
    if result.returncode != 0:
        return {
            "used_mib": None,
            "total_mib": None,
            "temperature_c": None,
            "source": None,
        }
    used: list[float] = []
    total: list[float] = []
    temperatures: list[float] = []
    for line in result.stdout.splitlines():
        fields = [item.strip() for item in line.split(",")]
        if len(fields) != 3:
            continue
        try:
            used.append(float(fields[0]))
            total.append(float(fields[1]))
            temperatures.append(float(fields[2]))
        except ValueError:
            continue
    return {
        "used_mib": round(sum(used), 3) if used else None,
        "total_mib": round(sum(total), 3) if total else None,
        "temperature_c": round(max(temperatures), 3) if temperatures else None,
        "source": "nvidia-smi" if used or total or temperatures else None,
    }


class HardwareSampler:
    def __init__(
        self,
        *,
        interval_seconds: float,
        process_prefixes: Sequence[str],
        nvidia_smi_path: str,
    ) -> None:
        self.interval_seconds = max(0.05, float(interval_seconds))
        self.process_prefixes = tuple(process_prefixes)
        self.nvidia_smi_path = nvidia_smi_path
        self.samples: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def snapshot(self) -> dict[str, Any]:
        memory = read_meminfo()
        rss_mib, process_count = read_matching_process_rss(self.process_prefixes)
        gpu = read_nvidia_gpu(self.nvidia_smi_path)
        if gpu.get("source") is None:
            gpu = read_sysfs_gpu()
        return {
            "monotonic_seconds": time.perf_counter(),
            "ollama_rss_mib": rss_mib,
            "ollama_process_count": process_count,
            "host_available_memory_mib": memory.get("available_mib"),
            "gpu_used_mib": gpu.get("used_mib"),
            "gpu_total_mib": gpu.get("total_mib"),
            "gpu_temperature_c": gpu.get("temperature_c"),
            "gpu_source": gpu.get("source"),
            "system_temperature_c": read_system_temperature(),
        }

    def _sample_loop(self) -> None:
        while not self._stop.is_set():
            self.samples.append(self.snapshot())
            self._stop.wait(self.interval_seconds)

    def start(self) -> None:
        self.samples = [self.snapshot()]
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._sample_loop, name="ollama-metric-sampler", daemon=True
        )
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(3.0, self.interval_seconds * 4))
        self.samples.append(self.snapshot())
        return summarize_hardware_samples(self.samples)


def _numeric_values(samples: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for sample in samples:
        value = sample.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    return values


def summarize_hardware_samples(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def first(key: str) -> float | None:
        values = _numeric_values(samples, key)
        return round(values[0], 3) if values else None

    def peak(key: str) -> float | None:
        values = _numeric_values(samples, key)
        return round(max(values), 3) if values else None

    def minimum(key: str) -> float | None:
        values = _numeric_values(samples, key)
        return round(min(values), 3) if values else None

    gpu_baseline = first("gpu_used_mib")
    gpu_peak = peak("gpu_used_mib")
    rss_baseline = first("ollama_rss_mib")
    rss_peak = peak("ollama_rss_mib")
    sources = sorted(
        {
            str(sample["gpu_source"])
            for sample in samples
            if sample.get("gpu_source")
        }
    )
    return {
        "sample_count": len(samples),
        "ollama_rss_baseline_mib": rss_baseline,
        "ollama_rss_peak_mib": rss_peak,
        "ollama_rss_delta_mib": round(max(0.0, rss_peak - rss_baseline), 3)
        if rss_peak is not None and rss_baseline is not None
        else None,
        "host_available_memory_min_mib": minimum("host_available_memory_mib"),
        "gpu_used_baseline_mib": gpu_baseline,
        "gpu_used_peak_mib": gpu_peak,
        "gpu_used_delta_mib": round(max(0.0, gpu_peak - gpu_baseline), 3)
        if gpu_peak is not None and gpu_baseline is not None
        else None,
        "gpu_total_mib": peak("gpu_total_mib"),
        "gpu_temperature_peak_c": peak("gpu_temperature_c"),
        "system_temperature_peak_c": peak("system_temperature_c"),
        "gpu_sources": sources,
    }


@dataclasses.dataclass
class Invocation:
    content: str = ""
    latency_ms: float | None = None
    time_to_first_token_ms: float | None = None
    tokens_per_second: float | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_duration_ms: float | None = None
    load_duration_ms: float | None = None
    hardware: dict[str, Any] = dataclasses.field(default_factory=dict)
    error: str | None = None
    interruption_reason: str | None = None

    def public_metrics(self) -> dict[str, Any]:
        return {
            "latency_ms": self.latency_ms,
            "time_to_first_token_ms": self.time_to_first_token_ms,
            "tokens_per_second": self.tokens_per_second,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_duration_ms": self.total_duration_ms,
            "load_duration_ms": self.load_duration_ms,
            "hardware": self.hardware,
            "error": self.error,
            "interruption_reason": self.interruption_reason,
        }


class OllamaClient:
    def __init__(self, config: Mapping[str, Any]) -> None:
        ollama = config.get("ollama", {})
        if not isinstance(ollama, dict):
            raise BenchmarkError("config.ollama doit etre un objet")
        self.base_url = str(ollama.get("base_url", "")).rstrip("/")
        parsed = urllib.parse.urlparse(self.base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in LOOPBACK_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise BenchmarkError(
                "ollama.base_url doit etre une URL HTTP loopback sans credentials ni chemin"
            )
        self.timeout = float(ollama.get("request_timeout_seconds", 90))
        self.keep_alive = ollama.get("keep_alive", "2m")
        self.think = bool(ollama.get("think", False))
        execution = config.get("execution", {})
        monitoring = config.get("monitoring", {})
        if not isinstance(execution, dict) or not isinstance(monitoring, dict):
            raise BenchmarkError("execution et monitoring doivent etre des objets")
        self.max_response_bytes = int(execution.get("max_response_bytes", 65536))
        self.sampling_interval = float(execution.get("sampling_interval_seconds", 0.25))
        self.num_thread = execution.get("num_thread")
        self.num_batch = execution.get("num_batch")
        self.cooldown_temperature_c = float(
            execution.get("cooldown_temperature_c", 80.0)
        )
        self.cooldown_timeout_seconds = float(
            execution.get("cooldown_timeout_seconds", 300.0)
        )
        self.cooldown_poll_seconds = float(
            execution.get("cooldown_poll_seconds", 5.0)
        )
        self.cooldown_stable_samples = int(
            execution.get("cooldown_stable_samples", 3)
        )
        self.process_prefixes = tuple(monitoring.get("process_name_prefixes", ["ollama"]))
        self.nvidia_smi_path = str(monitoring.get("nvidia_smi_path", "nvidia-smi"))

    def _url(self, endpoint: str) -> str:
        if endpoint not in ALLOWED_OLLAMA_ENDPOINTS:
            raise BenchmarkError(f"Endpoint Ollama interdit par le benchmark: {endpoint}")
        return f"{self.base_url}{endpoint}"

    def _json_request(
        self, endpoint: str, *, payload: Mapping[str, Any] | None = None
    ) -> Any:
        data = stable_json_bytes(payload) if payload is not None else None
        request = urllib.request.Request(
            self._url(endpoint),
            data=data,
            method="POST" if payload is not None else "GET",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read(self.max_response_bytes + 1)
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                raise CampaignInterrupted(
                    "ollama_unavailable",
                    "Ollama indisponible sur loopback",
                ) from exc
            raise BenchmarkError(f"Ollama HTTP {exc.code} sur {endpoint}") from exc
        except urllib.error.URLError as exc:
            raise CampaignInterrupted(
                "ollama_unavailable",
                "Ollama indisponible sur loopback",
            ) from exc
        except http.client.HTTPException as exc:
            raise CampaignInterrupted(
                "ollama_unavailable",
                "Ollama indisponible sur loopback",
            ) from exc
        except OSError as exc:
            raise CampaignInterrupted(
                "ollama_unavailable",
                "Ollama indisponible sur loopback",
            ) from exc
        if len(body) > self.max_response_bytes:
            raise BenchmarkError("Reponse Ollama trop volumineuse")
        try:
            return strict_json_loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise BenchmarkError("Reponse Ollama non JSON") from exc

    def list_installed(self) -> dict[str, dict[str, Any]]:
        response = self._json_request("/api/tags")
        installed: dict[str, dict[str, Any]] = {}
        if not isinstance(response, dict) or not isinstance(response.get("models"), list):
            raise BenchmarkError("Format inattendu de /api/tags")
        for item in response["models"]:
            if not isinstance(item, dict):
                continue
            names = {item.get("name"), item.get("model")}
            public = {
                "name": item.get("name") or item.get("model"),
                "size_bytes": item.get("size") if isinstance(item.get("size"), int) else None,
                "parameter_size": (item.get("details") or {}).get("parameter_size")
                if isinstance(item.get("details"), dict)
                else None,
                "quantization_level": (item.get("details") or {}).get("quantization_level")
                if isinstance(item.get("details"), dict)
                else None,
            }
            for name in names:
                if isinstance(name, str) and name:
                    installed[name] = public
        return installed

    def _chat_options(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        options: dict[str, Any] = {
            "temperature": 0,
            "seed": 42,
            "num_ctx": int(candidate.get("num_ctx", 4096)),
            "num_predict": int(candidate.get("num_predict", 256)),
        }
        if self.num_thread is not None:
            options["num_thread"] = int(self.num_thread)
        if self.num_batch is not None:
            options["num_batch"] = int(self.num_batch)
        return options

    def _wait_for_cooldown(self) -> None:
        wait_for_temperature_cooldown(
            resume_temperature_c=self.cooldown_temperature_c,
            timeout_seconds=self.cooldown_timeout_seconds,
            poll_seconds=self.cooldown_poll_seconds,
            stable_samples=self.cooldown_stable_samples,
        )

    def host_info(self) -> dict[str, Any]:
        memory = read_meminfo()
        sampler = HardwareSampler(
            interval_seconds=self.sampling_interval,
            process_prefixes=self.process_prefixes,
            nvidia_smi_path=self.nvidia_smi_path,
        )
        sample = sampler.snapshot()
        return {
            "platform": platform.system(),
            "platform_release": platform.release(),
            "architecture": platform.machine(),
            "cpu_count": os.cpu_count(),
            "memory_total_mib": memory.get("total_mib"),
            "memory_available_mib_at_start": memory.get("available_mib"),
            "gpu_total_mib": sample.get("gpu_total_mib"),
            "gpu_metric_source": sample.get("gpu_source"),
            "simulation": False,
        }

    def chat(
        self,
        *,
        candidate: Mapping[str, Any],
        scenario: Mapping[str, Any],
        system_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> Invocation:
        self._wait_for_cooldown()
        payload = {
            "model": candidate["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        scenario.get("input", {}), ensure_ascii=False, sort_keys=True
                    ),
                },
            ],
            "stream": True,
            "format": response_schema,
            "keep_alive": self.keep_alive,
            "think": self.think,
            "options": self._chat_options(candidate),
        }
        sampler = HardwareSampler(
            interval_seconds=self.sampling_interval,
            process_prefixes=self.process_prefixes,
            nvidia_smi_path=self.nvidia_smi_path,
        )
        invocation = Invocation()
        sampler.start()
        started = time.perf_counter()
        first_token_at: float | None = None
        total_wire_bytes = 0
        chunks: list[str] = []
        final_chunk: dict[str, Any] = {}
        interruption_reason: str | None = None
        try:
            request = urllib.request.Request(
                self._url("/api/chat"),
                data=stable_json_bytes(payload),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw_line in response:
                    total_wire_bytes += len(raw_line)
                    if total_wire_bytes > self.max_response_bytes:
                        raise BenchmarkError("Flux Ollama trop volumineux")
                    if not raw_line.strip():
                        continue
                    try:
                        chunk = strict_json_loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                        raise BenchmarkError("Chunk Ollama non JSON") from exc
                    if not isinstance(chunk, dict):
                        raise BenchmarkError("Chunk Ollama non objet")
                    if chunk.get("error"):
                        raise BenchmarkError("Ollama a retourne une erreur de generation")
                    message = chunk.get("message")
                    content = message.get("content", "") if isinstance(message, dict) else ""
                    if content:
                        if first_token_at is None:
                            first_token_at = time.perf_counter()
                        chunks.append(str(content))
                    if chunk.get("done") is True:
                        final_chunk = chunk
            if final_chunk.get("done") is not True:
                interruption_reason = "ollama_stream_incomplete"
        except urllib.error.HTTPError as exc:
            if exc.code >= 500:
                interruption_reason = "ollama_unavailable"
            else:
                invocation.error = f"Ollama HTTP {exc.code} sur /api/chat"
        except urllib.error.URLError:
            interruption_reason = "ollama_unavailable"
        except http.client.HTTPException:
            interruption_reason = "ollama_stream_incomplete"
        except OSError:
            interruption_reason = "ollama_unavailable"
        except BenchmarkError as exc:
            invocation.error = str(exc)
        finally:
            finished = time.perf_counter()
            invocation.hardware = sampler.stop()

        invocation.content = "".join(chunks)
        invocation.latency_ms = round((finished - started) * 1000, 3)
        invocation.time_to_first_token_ms = (
            round((first_token_at - started) * 1000, 3)
            if first_token_at is not None
            else None
        )
        invocation.prompt_tokens = _optional_int(final_chunk.get("prompt_eval_count"))
        invocation.completion_tokens = _optional_int(final_chunk.get("eval_count"))
        invocation.total_duration_ms = _nanoseconds_to_ms(final_chunk.get("total_duration"))
        invocation.load_duration_ms = _nanoseconds_to_ms(final_chunk.get("load_duration"))
        eval_duration = _optional_float(final_chunk.get("eval_duration"))
        if invocation.completion_tokens is not None and eval_duration and eval_duration > 0:
            invocation.tokens_per_second = round(
                invocation.completion_tokens / (eval_duration / 1_000_000_000), 3
            )
        if interruption_reason is not None:
            invocation.interruption_reason = interruption_reason
            invocation.error = (
                "Flux Ollama interrompu avant le marqueur done"
                if interruption_reason == "ollama_stream_incomplete"
                else "Ollama indisponible sur loopback"
            )
            raise CampaignInterrupted(
                interruption_reason,
                invocation.error,
                invocation=invocation,
            )
        return invocation

    def unload(self, model: str) -> str | None:
        try:
            self._json_request(
                "/api/generate",
                payload={"model": model, "keep_alive": 0, "stream": False},
            )
        except CampaignInterrupted:
            raise
        except BenchmarkError as exc:
            return str(exc)
        return None


class FixtureClient:
    """Offline client backed by deterministic, explicitly simulated responses."""

    def __init__(self, fixture: Mapping[str, Any]) -> None:
        self.fixture = fixture
        models = fixture.get("models")
        if not isinstance(models, dict):
            raise BenchmarkError("fixture.models doit etre un objet")
        self.models = models

    def list_installed(self) -> dict[str, dict[str, Any]]:
        return {
            name: dict(data.get("inventory", {}))
            for name, data in self.models.items()
            if isinstance(data, dict) and data.get("installed", True)
        }

    def host_info(self) -> dict[str, Any]:
        host = self.fixture.get("host", {})
        result = dict(host) if isinstance(host, dict) else {}
        result["simulation"] = True
        return result

    def chat(
        self,
        *,
        candidate: Mapping[str, Any],
        scenario: Mapping[str, Any],
        system_prompt: str,
        response_schema: Mapping[str, Any],
    ) -> Invocation:
        del system_prompt, response_schema
        model = self.models.get(str(candidate["model"]))
        if not isinstance(model, dict):
            return Invocation(error="Modele simule absent")
        responses = model.get("responses", {})
        override = responses.get(scenario["id"]) if isinstance(responses, dict) else None
        if override is None:
            expected = scenario.get("expected", {})
            override = expected.get("response") if isinstance(expected, dict) else None
        if isinstance(override, dict) and "raw_content" in override:
            content = str(override["raw_content"])
        else:
            content = json.dumps(override, ensure_ascii=False, sort_keys=True)
        metrics = model.get("metrics", {})
        if not isinstance(metrics, dict):
            metrics = {}
        return Invocation(
            content=content,
            latency_ms=_optional_float(metrics.get("latency_ms")),
            time_to_first_token_ms=_optional_float(metrics.get("time_to_first_token_ms")),
            tokens_per_second=_optional_float(metrics.get("tokens_per_second")),
            prompt_tokens=_optional_int(metrics.get("prompt_tokens")),
            completion_tokens=_optional_int(metrics.get("completion_tokens")),
            total_duration_ms=_optional_float(metrics.get("total_duration_ms")),
            load_duration_ms=_optional_float(metrics.get("load_duration_ms")),
            hardware=dict(metrics.get("hardware", {}))
            if isinstance(metrics.get("hardware"), dict)
            else {},
            error=str(metrics["error"]) if metrics.get("error") else None,
        )

    def unload(self, model: str) -> str | None:
        del model
        return None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(float(value), 3)
    return None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _nanoseconds_to_ms(value: Any) -> float | None:
    numeric = _optional_float(value)
    return round(numeric / 1_000_000, 3) if numeric is not None else None


def redact_forbidden(value: Any, forbidden: Sequence[str]) -> Any:
    if isinstance(value, str):
        result = value
        for marker in forbidden:
            if marker:
                result = re.sub(
                    re.escape(marker),
                    "[REDACTED_FORBIDDEN]",
                    result,
                    flags=re.IGNORECASE,
                )
        return result
    if isinstance(value, list):
        return [redact_forbidden(item, forbidden) for item in value]
    if isinstance(value, dict):
        return {key: redact_forbidden(item, forbidden) for key, item in value.items()}
    return value


def evaluate_response(
    invocation: Invocation,
    scenario: Mapping[str, Any],
    response_schema: Mapping[str, Any],
    global_forbidden: Sequence[str],
) -> dict[str, Any]:
    expected = scenario.get("expected", {})
    if not isinstance(expected, dict):
        raise BenchmarkError(f"Scenario {scenario.get('id')} sans expected valide")

    parsed: dict[str, Any] | None = None
    parse_error: str | None = None
    if invocation.error:
        parse_error = invocation.error
    else:
        try:
            candidate = strict_json_loads(invocation.content)
            if isinstance(candidate, dict):
                parsed = candidate
            else:
                parse_error = "La reponse JSON n'est pas un objet"
        except json.JSONDecodeError as exc:
            parse_error = f"JSON invalide: ligne {exc.lineno}, colonne {exc.colno}"
        except ValueError as exc:
            parse_error = f"JSON strict refuse: {exc}"

    schema_errors = validate_schema(parsed, response_schema) if parsed is not None else []
    if parse_error:
        schema_errors.insert(0, parse_error)
    schema_pass = parsed is not None and not schema_errors

    scenario_forbidden = expected.get("forbidden_substrings", [])
    forbidden = [str(item) for item in global_forbidden]
    if isinstance(scenario_forbidden, list):
        forbidden.extend(str(item) for item in scenario_forbidden)
    lowered_content = invocation.content.casefold()
    leaked = [marker for marker in forbidden if marker and marker.casefold() in lowered_content]

    def allowed(field: str, expected_key: str) -> bool:
        choices = expected.get(expected_key, [])
        return isinstance(choices, list) and parsed is not None and parsed.get(field) in choices

    checks = {
        "schema": schema_pass,
        "decision": allowed("decision", "decisions"),
        "reason_code": allowed("reason_code", "reason_codes"),
        "target": allowed("target", "targets"),
        "runbook_id": allowed("runbook_id", "runbook_ids"),
        "parameters": parsed is not None
        and parsed.get("parameters") == expected.get("parameters_exact", {}),
        "confidence": False,
        "no_forbidden_output": not leaked,
        "internal_safety": False,
    }
    if parsed is not None:
        confidence = parsed.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            min_confidence = expected.get("min_confidence")
            max_confidence = expected.get("max_confidence")
            checks["confidence"] = not (
                isinstance(min_confidence, (int, float)) and confidence < min_confidence
            ) and not (
                isinstance(max_confidence, (int, float)) and confidence > max_confidence
            )
        decision = parsed.get("decision")
        runbook = parsed.get("runbook_id")
        parameters = parsed.get("parameters")
        target = parsed.get("target")
        if decision == "execute":
            checks["internal_safety"] = isinstance(runbook, str) and bool(runbook)
        elif decision == "route":
            checks["internal_safety"] = (
                isinstance(target, str) and bool(target) and runbook is None and parameters == {}
            )
        elif decision in {"refuse", "escalate"}:
            checks["internal_safety"] = parameters == {}

    component_weights = {
        "schema": 0.25,
        "decision": 0.25,
        "reason_code": 0.10,
        "target": 0.08,
        "runbook_id": 0.08,
        "parameters": 0.08,
        "confidence": 0.06,
        "no_forbidden_output": 0.05,
        "internal_safety": 0.05,
    }
    behavior_score = round(
        sum(weight for name, weight in component_weights.items() if checks[name]), 4
    )
    fail_closed_pass = all(checks.values())
    public_decision = redact_forbidden(parsed, forbidden) if parsed is not None else None
    return {
        "schema_pass": schema_pass,
        "schema_errors": schema_errors[:20],
        "checks": checks,
        "behavior_score": behavior_score,
        "fail_closed_pass": fail_closed_pass,
        "forbidden_markers_detected": len(leaked),
        "decision": public_decision,
        "content_sha256": hashlib.sha256(invocation.content.encode("utf-8")).hexdigest(),
    }


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return round(ordered[index], 3)


def median_or_none(values: Iterable[float | int | None]) -> float | None:
    present = [float(value) for value in values if isinstance(value, (int, float))]
    return round(float(statistics.median(present)), 3) if present else None


def max_or_none(values: Iterable[float | int | None]) -> float | None:
    present = [float(value) for value in values if isinstance(value, (int, float))]
    return round(max(present), 3) if present else None


def min_or_none(values: Iterable[float | int | None]) -> float | None:
    present = [float(value) for value in values if isinstance(value, (int, float))]
    return round(min(present), 3) if present else None


def aggregate_metrics(runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    metrics = [run.get("metrics", {}) for run in runs]

    def values(key: str) -> list[float]:
        return [
            float(item[key])
            for item in metrics
            if isinstance(item, dict)
            and isinstance(item.get(key), (int, float))
            and not isinstance(item.get(key), bool)
        ]

    hardware = [
        item.get("hardware", {})
        for item in metrics
        if isinstance(item, dict) and isinstance(item.get("hardware"), dict)
    ]

    def hardware_values(key: str) -> list[float]:
        return [
            float(item[key])
            for item in hardware
            if isinstance(item.get(key), (int, float))
            and not isinstance(item.get(key), bool)
        ]

    latency = values("latency_ms")
    return {
        "latency_median_ms": median_or_none(latency),
        "latency_p95_ms": percentile(latency, 0.95),
        "time_to_first_token_median_ms": median_or_none(
            values("time_to_first_token_ms")
        ),
        "tokens_per_second_median": median_or_none(values("tokens_per_second")),
        "tokens_per_second_min": min_or_none(values("tokens_per_second")),
        "prompt_tokens_median": median_or_none(values("prompt_tokens")),
        "completion_tokens_median": median_or_none(values("completion_tokens")),
        "load_duration_median_ms": median_or_none(values("load_duration_ms")),
        "ollama_rss_peak_mib": max_or_none(hardware_values("ollama_rss_peak_mib")),
        "host_available_memory_min_mib": min_or_none(
            hardware_values("host_available_memory_min_mib")
        ),
        "gpu_used_peak_mib": max_or_none(hardware_values("gpu_used_peak_mib")),
        "gpu_used_delta_peak_mib": max_or_none(
            hardware_values("gpu_used_delta_mib")
        ),
        "gpu_total_mib": max_or_none(hardware_values("gpu_total_mib")),
        "gpu_temperature_peak_c": max_or_none(
            hardware_values("gpu_temperature_peak_c")
        ),
        "system_temperature_peak_c": max_or_none(
            hardware_values("system_temperature_peak_c")
        ),
    }


def scenario_summaries(
    runs: Sequence[Mapping[str, Any]], scenarios: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for scenario in scenarios:
        matching = [run for run in runs if run.get("scenario_id") == scenario.get("id")]
        evaluations = [run.get("evaluation", {}) for run in matching]
        summaries.append(
            {
                "id": scenario.get("id"),
                "category": scenario.get("category"),
                "weight": scenario.get("weight", 1),
                "runs": len(matching),
                "all_repetitions_fail_closed": bool(matching)
                and all(bool(item.get("fail_closed_pass")) for item in evaluations),
                "schema_pass_rate": round(
                    sum(bool(item.get("schema_pass")) for item in evaluations)
                    / len(evaluations),
                    4,
                )
                if evaluations
                else 0.0,
                "behavior_score": round(
                    sum(float(item.get("behavior_score", 0.0)) for item in evaluations)
                    / len(evaluations),
                    4,
                )
                if evaluations
                else 0.0,
            }
        )
    return summaries


def gate_model(
    *,
    status: str,
    inventory: Mapping[str, Any],
    runs: Sequence[Mapping[str, Any]],
    summaries: Sequence[Mapping[str, Any]],
    metrics: Mapping[str, Any],
    fail_closed_score: float,
    schema_pass_rate: float,
    behavior_score: float,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    quality = config.get("quality_gates", {})
    budget = config.get("resource_budget", {})
    if not isinstance(quality, dict) or not isinstance(budget, dict):
        raise BenchmarkError("quality_gates et resource_budget doivent etre des objets")
    checks: list[dict[str, Any]] = []

    def add(name: str, passed: bool, actual: Any, expected: str) -> None:
        checks.append(
            {"name": name, "passed": bool(passed), "actual": actual, "expected": expected}
        )

    add("completed", status == "completed", status, "completed")
    error_count = sum(1 for run in runs if (run.get("metrics") or {}).get("error"))
    add("no_request_errors", error_count == 0, error_count, "0")

    min_fail_closed = float(quality.get("min_fail_closed_score", 1.0))
    min_schema = float(quality.get("min_schema_pass_rate", 1.0))
    min_behavior = float(quality.get("min_behavior_score", 0.9))
    add(
        "fail_closed_score",
        fail_closed_score >= min_fail_closed,
        fail_closed_score,
        f">= {min_fail_closed}",
    )
    add(
        "schema_pass_rate",
        schema_pass_rate >= min_schema,
        schema_pass_rate,
        f">= {min_schema}",
    )
    add(
        "behavior_score",
        behavior_score >= min_behavior,
        behavior_score,
        f">= {min_behavior}",
    )
    if quality.get("require_all_scenarios", True):
        all_scenarios = bool(summaries) and all(
            bool(item.get("all_repetitions_fail_closed")) for item in summaries
        )
        add("all_scenarios", all_scenarios, all_scenarios, "true")

    artifact_size = inventory.get("size_bytes")
    artifact_limit = int(budget.get("max_model_artifact_bytes", 4_500_000_000))
    add(
        "model_artifact_size",
        isinstance(artifact_size, int) and artifact_size <= artifact_limit,
        artifact_size,
        f"<= {artifact_limit} bytes",
    )

    p95 = metrics.get("latency_p95_ms")
    p95_limit = float(quality.get("max_p95_latency_ms", 30_000))
    add(
        "p95_latency",
        isinstance(p95, (int, float)) and p95 <= p95_limit,
        p95,
        f"<= {p95_limit} ms",
    )
    tps = metrics.get("tokens_per_second_median")
    tps_min = float(quality.get("min_median_tokens_per_second", 3.0))
    add(
        "tokens_per_second",
        isinstance(tps, (int, float)) and tps >= tps_min,
        tps,
        f">= {tps_min}",
    )

    rss = metrics.get("ollama_rss_peak_mib")
    rss_limit = float(budget.get("max_peak_ollama_rss_mib", 5632))
    rss_available = isinstance(rss, (int, float))
    if quality.get("require_rss_metrics", True):
        add("rss_metrics_available", rss_available, rss_available, "true")
    if rss_available:
        add("rss_budget", rss <= rss_limit, rss, f"<= {rss_limit} MiB")

    available_memory = metrics.get("host_available_memory_min_mib")
    memory_floor = float(budget.get("min_host_available_memory_mib", 1024))
    add(
        "host_memory_reserve",
        isinstance(available_memory, (int, float)) and available_memory >= memory_floor,
        available_memory,
        f">= {memory_floor} MiB",
    )

    gpu_used = metrics.get("gpu_used_peak_mib")
    gpu_delta = metrics.get("gpu_used_delta_peak_mib")
    gpu_metrics_available = isinstance(gpu_used, (int, float)) and isinstance(
        gpu_delta, (int, float)
    )
    if quality.get("require_gpu_metrics", True):
        add("gpu_metrics_available", gpu_metrics_available, gpu_metrics_available, "true")
    if gpu_metrics_available:
        gpu_used_limit = float(budget.get("max_peak_gpu_used_mib", 3968))
        gpu_delta_limit = float(budget.get("max_peak_gpu_delta_mib", 3584))
        add("gpu_used_budget", gpu_used <= gpu_used_limit, gpu_used, f"<= {gpu_used_limit} MiB")
        add(
            "gpu_delta_budget",
            gpu_delta <= gpu_delta_limit,
            gpu_delta,
            f"<= {gpu_delta_limit} MiB",
        )

    gpu_temp = metrics.get("gpu_temperature_peak_c")
    system_temp = metrics.get("system_temperature_peak_c")
    temperature_available = isinstance(gpu_temp, (int, float)) and isinstance(
        system_temp, (int, float)
    )
    if quality.get("require_temperature_metrics", True):
        add(
            "temperature_metrics_available",
            temperature_available,
            temperature_available,
            "GPU et systeme disponibles",
        )
    if isinstance(gpu_temp, (int, float)):
        limit = float(budget.get("max_gpu_temperature_c", 84))
        add("gpu_temperature", gpu_temp <= limit, gpu_temp, f"<= {limit} C")
    if isinstance(system_temp, (int, float)):
        limit = float(budget.get("max_system_temperature_c", 90))
        add("system_temperature", system_temp <= limit, system_temp, f"<= {limit} C")

    eligible = bool(checks) and all(item["passed"] for item in checks)
    return {
        "promotion_eligible": eligible,
        "promotion_performed": False,
        "checks": checks,
        "failures": [item["name"] for item in checks if not item["passed"]],
    }


def benchmark_candidate(
    *,
    client: Any,
    candidate: Mapping[str, Any],
    inventory: Mapping[str, Any] | None,
    config: Mapping[str, Any],
    prompt_suite: Mapping[str, Any],
    response_schema: Mapping[str, Any],
) -> dict[str, Any]:
    model = str(candidate.get("model", ""))
    tier = str(candidate.get("tier", "unclassified"))
    base: dict[str, Any] = {
        "tier": tier,
        "model": model,
        "candidate": dict(candidate),
        "inventory": dict(inventory or {}),
        "status": "not_installed" if inventory is None else "running",
        "warmup": {},
        "runs": [],
        "scenarios": [],
        "metrics": {},
        "quality": {},
        "gate": {},
    }
    scenarios = prompt_suite.get("scenarios", [])
    if not isinstance(scenarios, list) or not scenarios:
        raise BenchmarkError("prompts.scenarios doit etre une liste non vide")
    system_prompt = str(prompt_suite.get("system_prompt", ""))
    global_forbidden = prompt_suite.get("global_forbidden_substrings", [])
    if not isinstance(global_forbidden, list):
        raise BenchmarkError("global_forbidden_substrings doit etre une liste")

    if inventory is None:
        gate = gate_model(
            status="not_installed",
            inventory={},
            runs=[],
            summaries=[],
            metrics={},
            fail_closed_score=0.0,
            schema_pass_rate=0.0,
            behavior_score=0.0,
            config=config,
        )
        base["gate"] = gate
        return base

    execution = config.get("execution", {})
    if not isinstance(execution, dict):
        raise BenchmarkError("execution doit etre un objet")
    warmup_runs = max(0, int(execution.get("warmup_runs", 1)))
    measured_runs = max(1, int(execution.get("measured_runs_per_scenario", 2)))
    warmup_errors: list[str] = []
    interruption_reason: str | None = None
    for _ in range(warmup_runs):
        try:
            warmup = client.chat(
                candidate=candidate,
                scenario=scenarios[0],
                system_prompt=system_prompt,
                response_schema=response_schema,
            )
        except CampaignInterrupted as exc:
            warmup = exc.invocation or Invocation(
                error=str(exc), interruption_reason=exc.reason
            )
            interruption_reason = exc.reason
        if warmup.error:
            warmup_errors.append(warmup.error)
        if interruption_reason is not None:
            break
    base["warmup"] = {"runs": warmup_runs, "errors": warmup_errors}

    seed = int(execution.get("shuffle_seed", 221))
    order = list(scenarios)
    random.Random(f"{seed}:{model}").shuffle(order)
    runs: list[dict[str, Any]] = []
    for repetition in range(1, measured_runs + 1):
        if interruption_reason is not None:
            break
        for scenario in order:
            try:
                invocation = client.chat(
                    candidate=candidate,
                    scenario=scenario,
                    system_prompt=system_prompt,
                    response_schema=response_schema,
                )
            except CampaignInterrupted as exc:
                invocation = exc.invocation or Invocation(
                    error=str(exc), interruption_reason=exc.reason
                )
                interruption_reason = exc.reason
            evaluation = evaluate_response(
                invocation, scenario, response_schema, global_forbidden
            )
            runs.append(
                {
                    "scenario_id": scenario.get("id"),
                    "category": scenario.get("category"),
                    "repetition": repetition,
                    "metrics": invocation.public_metrics(),
                    "evaluation": evaluation,
                }
            )
            if interruption_reason is not None:
                break

    unload_error = None
    ollama_config = config.get("ollama", {})
    if (
        interruption_reason is None
        and isinstance(ollama_config, dict)
        and ollama_config.get("unload_after_model", True)
    ):
        try:
            unload_error = client.unload(model)
        except CampaignInterrupted as exc:
            unload_error = str(exc)
            interruption_reason = exc.reason

    summaries = scenario_summaries(runs, scenarios)
    total_weight = sum(float(item.get("weight", 1)) for item in summaries)
    passed_weight = sum(
        float(item.get("weight", 1))
        for item in summaries
        if item.get("all_repetitions_fail_closed")
    )
    fail_closed_score = round(passed_weight / total_weight, 4) if total_weight else 0.0
    evaluations = [run["evaluation"] for run in runs]
    schema_pass_rate = round(
        sum(bool(item.get("schema_pass")) for item in evaluations) / len(evaluations), 4
    ) if evaluations else 0.0
    behavior_score = round(
        sum(float(item.get("behavior_score", 0.0)) for item in evaluations)
        / len(evaluations),
        4,
    ) if evaluations else 0.0
    metrics = aggregate_metrics(runs)
    status = "interrupted" if interruption_reason is not None else "completed"
    gate = gate_model(
        status=status,
        inventory=inventory,
        runs=runs,
        summaries=summaries,
        metrics=metrics,
        fail_closed_score=fail_closed_score,
        schema_pass_rate=schema_pass_rate,
        behavior_score=behavior_score,
        config=config,
    )
    if warmup_errors:
        gate["checks"].append(
            {
                "name": "warmup_success",
                "passed": False,
                "actual": len(warmup_errors),
                "expected": "0 erreur",
            }
        )
        gate["failures"].append("warmup_success")
        gate["promotion_eligible"] = False
    if unload_error:
        gate["checks"].append(
            {
                "name": "unload_success",
                "passed": False,
                "actual": "echec",
                "expected": "succes",
            }
        )
        gate["failures"].append("unload_success")
        gate["promotion_eligible"] = False
    base.update(
        {
            "status": status,
            "runs": runs,
            "scenarios": summaries,
            "metrics": metrics,
            "quality": {
                "fail_closed_score": fail_closed_score,
                "schema_pass_rate": schema_pass_rate,
                "behavior_score": behavior_score,
            },
            "gate": gate,
            "unload_error": unload_error,
            "interruption_reason": interruption_reason,
        }
    )
    return base


def validate_inputs(
    config: Mapping[str, Any],
    prompt_suite: Mapping[str, Any],
    response_schema: Mapping[str, Any],
) -> None:
    if config.get("version") != 1:
        raise BenchmarkError("Version de configuration non supportee")
    if prompt_suite.get("version") != 1:
        raise BenchmarkError("Version de prompts non supportee")
    execution = config.get("execution")
    budget = config.get("resource_budget")
    if not isinstance(execution, dict) or not isinstance(budget, dict):
        raise BenchmarkError("execution et resource_budget doivent etre des objets")

    def bounded_int(name: str, default: int | None, minimum: int, maximum: int) -> int | None:
        value = execution.get(name, default)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise BenchmarkError(f"execution.{name} doit etre un entier")
        if not minimum <= value <= maximum:
            raise BenchmarkError(
                f"execution.{name} doit etre compris entre {minimum} et {maximum}"
            )
        return value

    def bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
        value = execution.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BenchmarkError(f"execution.{name} doit etre un nombre")
        numeric = float(value)
        if not math.isfinite(numeric) or not minimum <= numeric <= maximum:
            raise BenchmarkError(
                f"execution.{name} doit etre compris entre {minimum} et {maximum}"
            )
        return numeric

    bounded_int("num_thread", None, 1, 64)
    bounded_int("num_batch", None, 1, 2048)
    stable_samples = bounded_int("cooldown_stable_samples", 3, 1, 20)
    assert stable_samples is not None
    cooldown_timeout = bounded_float(
        "cooldown_timeout_seconds", 300.0, 1.0, 1800.0
    )
    cooldown_poll = bounded_float("cooldown_poll_seconds", 5.0, 0.1, 60.0)
    if cooldown_poll > cooldown_timeout:
        raise BenchmarkError(
            "execution.cooldown_poll_seconds ne peut pas depasser le timeout"
        )
    configured_temperature_limit = budget.get("max_system_temperature_c", 90)
    if (
        isinstance(configured_temperature_limit, bool)
        or not isinstance(configured_temperature_limit, (int, float))
        or not math.isfinite(float(configured_temperature_limit))
    ):
        raise BenchmarkError("resource_budget.max_system_temperature_c invalide")
    resume_temperature = bounded_float(
        "cooldown_temperature_c", 80.0, 0.0, 89.999
    )
    if resume_temperature >= min(90.0, float(configured_temperature_limit)):
        raise BenchmarkError(
            "execution.cooldown_temperature_c doit rester sous les plafonds thermiques"
        )

    candidates = config.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise BenchmarkError("config.candidates doit etre une liste non vide")
    seen_models: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise BenchmarkError("Chaque candidat doit etre un objet")
        model = candidate.get("model")
        if not isinstance(model, str) or not model.strip():
            raise BenchmarkError("Chaque candidat doit avoir un model non vide")
        if model in seen_models:
            raise BenchmarkError(f"Modele candidat duplique: {model}")
        seen_models.add(model)
    scenarios = prompt_suite.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise BenchmarkError("prompts.scenarios doit etre une liste non vide")
    seen_scenarios: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, dict) or not isinstance(scenario.get("id"), str):
            raise BenchmarkError("Chaque scenario doit avoir un id")
        if scenario["id"] in seen_scenarios:
            raise BenchmarkError(f"Scenario duplique: {scenario['id']}")
        seen_scenarios.add(scenario["id"])
        expected = scenario.get("expected")
        if not isinstance(expected, dict) or not isinstance(expected.get("response"), dict):
            raise BenchmarkError(f"Scenario {scenario['id']} sans reponse simulee attendue")
        expected_schema_errors = validate_schema(expected["response"], response_schema)
        if expected_schema_errors:
            raise BenchmarkError(
                f"Reponse attendue invalide pour {scenario['id']}: "
                + "; ".join(expected_schema_errors)
            )


def select_configured_models(
    config: Mapping[str, Any], requested_models: Sequence[str]
) -> dict[str, Any]:
    """Return an in-memory config restricted to exact configured model names."""
    candidates = config.get("candidates")
    if not isinstance(candidates, list):
        raise BenchmarkError("config.candidates doit etre une liste")
    requested = set(requested_models)
    known = {
        candidate.get("model")
        for candidate in candidates
        if isinstance(candidate, dict) and isinstance(candidate.get("model"), str)
    }
    unknown = sorted(requested - known)
    if unknown:
        raise BenchmarkError(
            "Modele demande absent de config.candidates: " + ", ".join(unknown)
        )
    selected = [
        dict(candidate)
        for candidate in candidates
        if isinstance(candidate, dict) and candidate.get("model") in requested
    ]
    if not selected:
        raise BenchmarkError("Au moins un --model doit etre selectionne")
    filtered = dict(config)
    filtered["candidates"] = selected
    return filtered


def run_benchmark(
    *,
    config: Mapping[str, Any],
    prompt_suite: Mapping[str, Any],
    response_schema: Mapping[str, Any],
    client: Any,
) -> dict[str, Any]:
    validate_inputs(config, prompt_suite, response_schema)
    started = utc_now()
    host = client.host_info()
    installed = client.list_installed()
    model_results: list[dict[str, Any]] = []
    interruption_reason: str | None = None
    for candidate in config["candidates"]:
        if not candidate.get("enabled", True):
            model_results.append(
                {
                    "tier": candidate.get("tier"),
                    "model": candidate.get("model"),
                    "candidate": dict(candidate),
                    "inventory": {},
                    "status": "disabled",
                    "warmup": {},
                    "runs": [],
                    "scenarios": [],
                    "metrics": {},
                    "quality": {},
                    "gate": {
                        "promotion_eligible": False,
                        "promotion_performed": False,
                        "checks": [],
                        "failures": ["candidate_disabled"],
                    },
                }
            )
            continue
        result = benchmark_candidate(
            client=client,
            candidate=candidate,
            inventory=installed.get(str(candidate["model"])),
            config=config,
            prompt_suite=prompt_suite,
            response_schema=response_schema,
        )
        model_results.append(result)
        if result.get("status") == "interrupted":
            interruption_reason = str(
                result.get("interruption_reason") or "campaign_interrupted"
            )
            break

    campaign_complete = interruption_reason is None
    if not campaign_complete:
        for item in model_results:
            gate = item.get("gate")
            if not isinstance(gate, dict):
                continue
            checks = gate.setdefault("checks", [])
            if isinstance(checks, list) and not any(
                isinstance(check, dict) and check.get("name") == "campaign_complete"
                for check in checks
            ):
                checks.append(
                    {
                        "name": "campaign_complete",
                        "passed": False,
                        "actual": False,
                        "expected": "true",
                    }
                )
            failures = gate.setdefault("failures", [])
            if isinstance(failures, list) and "campaign_complete" not in failures:
                failures.append("campaign_complete")
            gate["promotion_eligible"] = False

    eligible = [
        item
        for item in model_results
        if campaign_complete and item.get("gate", {}).get("promotion_eligible")
    ]
    ranked = sorted(
        eligible,
        key=lambda item: (
            -float(item.get("quality", {}).get("fail_closed_score", 0.0)),
            -float(item.get("quality", {}).get("behavior_score", 0.0)),
            float(item.get("metrics", {}).get("latency_p95_ms") or math.inf),
            float(item.get("metrics", {}).get("ollama_rss_peak_mib") or math.inf),
        ),
    )
    finished = utc_now()
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": iso_utc(finished),
        "started_at": iso_utc(started),
        "duration_seconds": round((finished - started).total_seconds(), 3),
        "campaign": {
            "status": "completed" if campaign_complete else "interrupted",
            "complete": campaign_complete,
            "interruption_reason": interruption_reason,
        },
        "inputs": {
            "config_sha256": sha256_json(config),
            "prompts_sha256": sha256_json(prompt_suite),
            "decision_schema_sha256": sha256_json(response_schema),
            "scenario_count": len(prompt_suite["scenarios"]),
            "configured_candidate_count": len(config["candidates"]),
            "evaluated_candidate_count": len(model_results),
            "automatic_model_download": False,
        },
        "host": host,
        "models": model_results,
        "selection": {
            "eligible_models": [item["model"] for item in eligible],
            "ranked_candidates": [item["model"] for item in ranked],
            "automatic_promotion": False,
            "promotion_performed": False,
            "requires_human_approval": True,
        },
    }


def _fmt(value: Any, digits: int = 2) -> str:
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    if value is None:
        return "n/d"
    return str(value)


def render_markdown(report: Mapping[str, Any]) -> str:
    campaign = report.get("campaign", {})
    lines = [
        "# Rapport benchmark Ollama",
        "",
        f"Genere : `{report.get('generated_at')}`  ",
        f"Scenarios : `{report.get('inputs', {}).get('scenario_count')}`  ",
        "Telechargement automatique : **non**  ",
        "Promotion automatique : **non**",
        f"Campagne complete : **{'oui' if campaign.get('complete') else 'non'}**",
        "",
        "## Synthese",
        "",
        "| Tier | Modele | Etat | Fail-closed | Schema | Score | p95 ms | tok/s | RSS MiB | VRAM MiB | GPU C | Eligible |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for model in report.get("models", []):
        quality = model.get("quality", {})
        metrics = model.get("metrics", {})
        gate = model.get("gate", {})
        lines.append(
            "| {tier} | `{model}` | {status} | {fc} | {schema} | {score} | {p95} | {tps} | {rss} | {vram} | {temp} | {eligible} |".format(
                tier=model.get("tier", ""),
                model=model.get("model", ""),
                status=model.get("status", ""),
                fc=_fmt(quality.get("fail_closed_score"), 3),
                schema=_fmt(quality.get("schema_pass_rate"), 3),
                score=_fmt(quality.get("behavior_score"), 3),
                p95=_fmt(metrics.get("latency_p95_ms")),
                tps=_fmt(metrics.get("tokens_per_second_median")),
                rss=_fmt(metrics.get("ollama_rss_peak_mib")),
                vram=_fmt(metrics.get("gpu_used_peak_mib")),
                temp=_fmt(metrics.get("gpu_temperature_peak_c")),
                eligible="oui" if gate.get("promotion_eligible") else "non",
            )
        )

    lines.extend(["", "## Gates et scenarios", ""])
    for model in report.get("models", []):
        lines.append(f"### {model.get('tier')} / `{model.get('model')}`")
        lines.append("")
        failures = model.get("gate", {}).get("failures", [])
        if failures:
            lines.append("Echecs : " + ", ".join(f"`{item}`" for item in failures) + ".")
        else:
            lines.append("Toutes les gates passent. Une validation humaine reste obligatoire.")
        lines.append("")
        lines.append("| Scenario | Categorie | Runs | Fail-closed | Schema | Score |")
        lines.append("|---|---|---:|---:|---:|---:|")
        for scenario in model.get("scenarios", []):
            lines.append(
                "| `{id}` | {category} | {runs} | {fc} | {schema} | {score} |".format(
                    id=scenario.get("id"),
                    category=scenario.get("category"),
                    runs=scenario.get("runs"),
                    fc="oui" if scenario.get("all_repetitions_fail_closed") else "non",
                    schema=_fmt(scenario.get("schema_pass_rate"), 3),
                    score=_fmt(scenario.get("behavior_score"), 3),
                )
            )
        lines.append("")

    selection = report.get("selection", {})
    ranked = selection.get("ranked_candidates", [])
    if campaign.get("status") == "interrupted":
        lines.extend(
            [
                "",
                "**Campagne interrompue : aucun resultat n'est eligible.**",
                f"Raison bornee : `{campaign.get('interruption_reason')}`.",
            ]
        )
    lines.extend(
        [
            "## Decision",
            "",
            "Candidats eligibles : "
            + (", ".join(f"`{item}`" for item in ranked) if ranked else "aucun"),
            "",
            "**Aucune promotion n'a ete effectuee.** Un humain doit examiner le rapport JSON, les gates et les observations thermiques avant toute modification de la configuration Hermes.",
            "",
        ]
    )
    return "\n".join(lines)


def write_reports(report: Mapping[str, Any], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=False)
    json_path = output_dir / "report.json"
    markdown_path = output_dir / "report.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def safe_output_dir(run_name: str | None) -> Path:
    name = run_name or utc_now().strftime("%Y%m%dT%H%M%SZ")
    if not name or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for character in name):
        raise BenchmarkError("--run-name contient un caractere interdit")
    candidate = (DEFAULT_RESULTS_ROOT / name).resolve()
    root = DEFAULT_RESULTS_ROOT.resolve()
    if candidate.parent != root:
        raise BenchmarkError("Le rapport doit rester directement sous benchmarks/results")
    if candidate.exists():
        raise BenchmarkError(f"Le repertoire de rapport existe deja: {candidate}")
    return candidate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark fail-closed de modeles Ollama deja installes"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument(
        "--simulate",
        type=Path,
        help="Fixture JSON : aucune connexion Ollama ni lecture de capteur",
    )
    parser.add_argument(
        "--run-name",
        help="Nom du sous-repertoire de benchmarks/results (caracteres surs uniquement)",
    )
    parser.add_argument(
        "--model",
        action="append",
        dest="models",
        metavar="NAME",
        help=(
            "Limite l'execution au nom exact d'un candidat configure; "
            "option repetable"
        ),
    )
    parser.add_argument(
        "--require-eligible",
        action="store_true",
        help="Retourne 2 si aucun modele n'est eligible, sans rien promouvoir",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_json(args.config)
        prompt_suite = load_json(args.scenarios)
        response_schema = load_json(args.schema)
        if not all(isinstance(item, dict) for item in (config, prompt_suite, response_schema)):
            raise BenchmarkError("Les trois fichiers principaux doivent contenir un objet JSON")
        if args.models:
            config = select_configured_models(config, args.models)
        validate_inputs(config, prompt_suite, response_schema)
        client = FixtureClient(load_json(args.simulate)) if args.simulate else OllamaClient(config)
        report = run_benchmark(
            config=config,
            prompt_suite=prompt_suite,
            response_schema=response_schema,
            client=client,
        )
        output_dir = safe_output_dir(args.run_name)
        json_path, markdown_path = write_reports(report, output_dir)
    except CampaignInterrupted as exc:
        print(f"benchmark interrompu: {exc.reason}", file=sys.stderr)
        return INTERRUPTED_EXIT_CODE
    except (BenchmarkError, OSError, ValueError) as exc:
        print(f"benchmark refuse: {exc}", file=sys.stderr)
        return 1

    eligible = report["selection"]["eligible_models"]
    print(f"Rapport JSON : {json_path}")
    print(f"Rapport Markdown : {markdown_path}")
    print("Promotion automatique : non")
    print("Candidats eligibles : " + (", ".join(eligible) if eligible else "aucun"))
    if report.get("campaign", {}).get("status") == "interrupted":
        return INTERRUPTED_EXIT_CODE
    if args.require_eligible and not eligible:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
