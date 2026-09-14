from __future__ import annotations

import copy
import hashlib
import http.client
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


BENCHMARK_DIR = Path(__file__).resolve().parents[1]
ROOT = BENCHMARK_DIR.parent
sys.path.insert(0, str(BENCHMARK_DIR))

import ollama_benchmark as bench  # noqa: E402


CONFIG_PATH = BENCHMARK_DIR / "fixtures" / "simulated_config.json"
FIXTURE_PATH = BENCHMARK_DIR / "fixtures" / "simulated_responses.json"
PROMPTS_PATH = BENCHMARK_DIR / "prompts.json"
SCHEMA_PATH = BENCHMARK_DIR / "decision.schema.json"
REPORT_SCHEMA_PATH = BENCHMARK_DIR / "report.schema.json"


def loaded() -> tuple[dict, dict, dict, dict]:
    return (
        bench.load_json(CONFIG_PATH),
        bench.load_json(PROMPTS_PATH),
        bench.load_json(SCHEMA_PATH),
        bench.load_json(FIXTURE_PATH),
    )


class SchemaTests(unittest.TestCase):
    def test_all_expected_responses_match_schema(self) -> None:
        _, prompts, schema, _ = loaded()
        for scenario in prompts["scenarios"]:
            with self.subTest(scenario=scenario["id"]):
                self.assertEqual(
                    [],
                    bench.validate_schema(scenario["expected"]["response"], schema),
                )

    def test_unknown_property_is_rejected(self) -> None:
        _, prompts, schema, _ = loaded()
        response = copy.deepcopy(prompts["scenarios"][0]["expected"]["response"])
        response["shell"] = "commande interdite"
        errors = bench.validate_schema(response, schema)
        self.assertTrue(any("supplementaire" in error for error in errors))

    def test_invalid_confidence_is_rejected(self) -> None:
        _, prompts, schema, _ = loaded()
        response = copy.deepcopy(prompts["scenarios"][0]["expected"]["response"])
        response["confidence"] = 1.2
        errors = bench.validate_schema(response, schema)
        self.assertTrue(any("superieure" in error for error in errors))

    def test_non_standard_number_is_rejected_by_strict_parser(self) -> None:
        with self.assertRaises(ValueError):
            bench.strict_json_loads('{"confidence": NaN}')

    def test_duplicate_key_is_rejected_by_strict_parser(self) -> None:
        with self.assertRaises(ValueError):
            bench.strict_json_loads('{"decision": "refuse", "decision": "execute"}')


class OfflinePipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config, self.prompts, self.schema, self.fixture = loaded()
        self.report = bench.run_benchmark(
            config=self.config,
            prompt_suite=self.prompts,
            response_schema=self.schema,
            client=bench.FixtureClient(self.fixture),
        )

    def test_safe_model_is_eligible_but_never_promoted(self) -> None:
        by_model = {item["model"]: item for item in self.report["models"]}
        safe = by_model["safe-simulated:latest"]
        self.assertEqual(1.0, safe["quality"]["fail_closed_score"])
        self.assertTrue(safe["gate"]["promotion_eligible"])
        self.assertFalse(safe["gate"]["promotion_performed"])
        self.assertFalse(self.report["selection"]["automatic_promotion"])
        self.assertFalse(self.report["selection"]["promotion_performed"])
        self.assertTrue(self.report["selection"]["requires_human_approval"])

    def test_unsafe_model_fails_closed_and_canary_is_redacted(self) -> None:
        by_model = {item["model"]: item for item in self.report["models"]}
        unsafe = by_model["unsafe-simulated:latest"]
        self.assertFalse(unsafe["gate"]["promotion_eligible"])
        self.assertLess(unsafe["quality"]["fail_closed_score"], 1.0)
        failing = [
            run
            for run in unsafe["runs"]
            if run["scenario_id"] == "refuse_secret_exfiltration"
        ][0]
        self.assertFalse(failing["evaluation"]["fail_closed_pass"])
        serialized = json.dumps(failing["evaluation"], ensure_ascii=False)
        self.assertNotIn("DO_NOT_ECHO_CANARY_7F3A", serialized)
        self.assertIn("[REDACTED_FORBIDDEN]", serialized)

    def test_report_matches_minimal_report_schema(self) -> None:
        report_schema = bench.load_json(REPORT_SCHEMA_PATH)
        self.assertEqual([], bench.validate_schema(self.report, report_schema))

    def test_reports_are_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "one-run"
            json_path, markdown_path = bench.write_reports(self.report, output)
            parsed = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertEqual(1, parsed["schema_version"])
            self.assertIn("Promotion automatique : **non**", markdown)
            self.assertIn("Aucune promotion n'a ete effectuee", markdown)

    def test_config_is_not_modified(self) -> None:
        before = hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()
        bench.run_benchmark(
            config=self.config,
            prompt_suite=self.prompts,
            response_schema=self.schema,
            client=bench.FixtureClient(self.fixture),
        )
        after = hashlib.sha256(CONFIG_PATH.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_fixture_path_never_opens_a_network_client(self) -> None:
        client = bench.FixtureClient(self.fixture)
        self.assertTrue(client.host_info()["simulation"])
        self.assertEqual(
            {"safe-simulated:latest", "unsafe-simulated:latest"},
            set(client.list_installed()),
        )

    def test_exact_model_filter_keeps_only_requested_candidate(self) -> None:
        filtered = bench.select_configured_models(
            self.config, ["safe-simulated:latest"]
        )
        self.assertEqual(
            ["safe-simulated:latest"],
            [candidate["model"] for candidate in filtered["candidates"]],
        )
        self.assertEqual(2, len(self.config["candidates"]))

    def test_unknown_model_filter_is_rejected(self) -> None:
        with self.assertRaises(bench.BenchmarkError):
            bench.select_configured_models(self.config, ["unknown:latest"])

    def test_interruption_invalidates_an_already_eligible_candidate(self) -> None:
        class InterruptingClient(bench.FixtureClient):
            def __init__(self, fixture: dict) -> None:
                super().__init__(fixture)
                self.chat_calls = 0

            def chat(self, **kwargs):
                self.chat_calls += 1
                if self.chat_calls == 11:
                    invocation = bench.Invocation(
                        error="Ollama indisponible sur loopback",
                        interruption_reason="ollama_unavailable",
                    )
                    raise bench.CampaignInterrupted(
                        "ollama_unavailable",
                        "Ollama indisponible sur loopback",
                        invocation=invocation,
                    )
                return super().chat(**kwargs)

        client = InterruptingClient(self.fixture)
        report = bench.run_benchmark(
            config=self.config,
            prompt_suite=self.prompts,
            response_schema=self.schema,
            client=client,
        )

        self.assertEqual(11, client.chat_calls)
        self.assertEqual("interrupted", report["campaign"]["status"])
        self.assertFalse(report["campaign"]["complete"])
        self.assertEqual([], report["selection"]["eligible_models"])
        self.assertEqual(2, len(report["models"]))
        safe = report["models"][0]
        interrupted = report["models"][1]
        self.assertFalse(safe["gate"]["promotion_eligible"])
        self.assertIn("campaign_complete", safe["gate"]["failures"])
        self.assertEqual("interrupted", interrupted["status"])


class ThermalAndInterruptionTests(unittest.TestCase):
    def test_cooldown_requires_consecutive_safe_samples(self) -> None:
        with (
            mock.patch.object(
                bench, "read_system_temperature", side_effect=[85.0, 80.0, 79.0]
            ),
            mock.patch.object(bench.time, "monotonic", side_effect=[0.0, 1.0, 2.0]),
            mock.patch.object(bench.time, "sleep") as sleep,
        ):
            result = bench.wait_for_temperature_cooldown(
                resume_temperature_c=80.0,
                timeout_seconds=10.0,
                poll_seconds=1.0,
                stable_samples=2,
            )
        self.assertEqual(79.0, result)
        self.assertEqual(2, sleep.call_count)

    def test_cooldown_timeout_is_a_typed_interruption(self) -> None:
        with (
            mock.patch.object(bench, "read_system_temperature", return_value=85.0),
            mock.patch.object(bench.time, "monotonic", side_effect=[0.0, 301.0]),
        ):
            with self.assertRaises(bench.CampaignInterrupted) as raised:
                bench.wait_for_temperature_cooldown(
                    resume_temperature_c=80.0,
                    timeout_seconds=300.0,
                    poll_seconds=5.0,
                    stable_samples=3,
                )
        self.assertEqual("thermal_cooldown_timeout", raised.exception.reason)

    def test_chat_refuses_a_stream_without_done(self) -> None:
        config, prompts, schema, _ = loaded()
        config["execution"].update(
            {
                "num_thread": 2,
                "num_batch": 128,
                "cooldown_temperature_c": 80,
                "cooldown_timeout_seconds": 300,
                "cooldown_poll_seconds": 5,
                "cooldown_stable_samples": 3,
            }
        )
        client = bench.OllamaClient(config)

        class Response:
            def __init__(self, done_value):
                self.done_value = done_value

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                yield (
                    json.dumps(
                        {"message": {"content": "{}"}, "done": self.done_value}
                    ).encode("utf-8")
                    + b"\n"
                )

        class Sampler:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                pass

            def stop(self):
                return {}

        for done_value in (False, "true"):
            with self.subTest(done=done_value):
                with (
                    mock.patch.object(client, "_wait_for_cooldown"),
                    mock.patch.object(bench, "HardwareSampler", Sampler),
                    mock.patch.object(
                        bench.urllib.request,
                        "urlopen",
                        return_value=Response(done_value),
                    ),
                ):
                    with self.assertRaises(bench.CampaignInterrupted) as raised:
                        client.chat(
                            candidate=config["candidates"][0],
                            scenario=prompts["scenarios"][0],
                            system_prompt=prompts["system_prompt"],
                            response_schema=schema,
                        )
                self.assertEqual(
                    "ollama_stream_incomplete", raised.exception.reason
                )
                self.assertIsNotNone(raised.exception.invocation)
                self.assertIsNotNone(raised.exception.invocation.error)

    def test_chat_classifies_an_abrupt_http_stream_as_interrupted(self) -> None:
        config, prompts, schema, _ = loaded()
        config["execution"].update(
            {
                "num_thread": 2,
                "num_batch": 128,
                "cooldown_temperature_c": 80,
                "cooldown_timeout_seconds": 300,
                "cooldown_poll_seconds": 5,
                "cooldown_stable_samples": 3,
            }
        )
        client = bench.OllamaClient(config)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                yield b'{"message":{"content":"partial"},"done":false}\n'
                raise http.client.IncompleteRead(b"partial")

        class Sampler:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                pass

            def stop(self):
                return {}

        with (
            mock.patch.object(client, "_wait_for_cooldown"),
            mock.patch.object(bench, "HardwareSampler", Sampler),
            mock.patch.object(
                bench.urllib.request, "urlopen", return_value=Response()
            ),
        ):
            with self.assertRaises(bench.CampaignInterrupted) as raised:
                client.chat(
                    candidate=config["candidates"][0],
                    scenario=prompts["scenarios"][0],
                    system_prompt=prompts["system_prompt"],
                    response_schema=schema,
                )
        self.assertEqual("ollama_stream_incomplete", raised.exception.reason)
        self.assertIsNotNone(raised.exception.invocation)
        self.assertEqual(
            "ollama_stream_incomplete",
            raised.exception.invocation.interruption_reason,
        )

    def test_metadata_request_classifies_abrupt_http_close_as_interrupted(self) -> None:
        config, _, _, _ = loaded()
        client = bench.OllamaClient(config)

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _maximum_bytes):
                raise http.client.IncompleteRead(b"partial")

        with mock.patch.object(
            bench.urllib.request, "urlopen", return_value=Response()
        ):
            with self.assertRaises(bench.CampaignInterrupted) as raised:
                client.list_installed()
        self.assertEqual("ollama_unavailable", raised.exception.reason)

    def test_thermal_execution_options_are_bounded_and_forwarded(self) -> None:
        config, prompts, schema, _ = loaded()
        config["execution"].update(
            {
                "num_thread": 2,
                "num_batch": 128,
                "cooldown_temperature_c": 80,
                "cooldown_timeout_seconds": 300,
                "cooldown_poll_seconds": 5,
                "cooldown_stable_samples": 3,
            }
        )
        bench.validate_inputs(config, prompts, schema)
        options = bench.OllamaClient(config)._chat_options(config["candidates"][0])
        self.assertEqual(2, options["num_thread"])
        self.assertEqual(128, options["num_batch"])

        for key, value in (
            ("num_thread", True),
            ("num_batch", 0),
            ("cooldown_timeout_seconds", float("inf")),
            ("cooldown_poll_seconds", 301),
            ("cooldown_stable_samples", False),
        ):
            with self.subTest(key=key, value=value):
                invalid = copy.deepcopy(config)
                invalid["execution"][key] = value
                with self.assertRaises(bench.BenchmarkError):
                    bench.validate_inputs(invalid, prompts, schema)


class NetworkSafetyTests(unittest.TestCase):
    def test_only_expected_ollama_endpoints_exist(self) -> None:
        self.assertEqual(
            {"/api/tags", "/api/chat", "/api/generate"},
            set(bench.ALLOWED_OLLAMA_ENDPOINTS),
        )
        self.assertNotIn("/api/pull", bench.ALLOWED_OLLAMA_ENDPOINTS)

    def test_non_loopback_url_is_rejected(self) -> None:
        config, _, _, _ = loaded()
        config["ollama"]["base_url"] = "http://192.0.2.10:11434"
        with self.assertRaises(bench.BenchmarkError):
            bench.OllamaClient(config)

    def test_url_with_credentials_is_rejected(self) -> None:
        config, _, _, _ = loaded()
        config["ollama"]["base_url"] = "http://user:password@127.0.0.1:11434"
        with self.assertRaises(bench.BenchmarkError):
            bench.OllamaClient(config)

    def test_endpoint_allowlist_fails_closed(self) -> None:
        config, _, _, _ = loaded()
        client = bench.OllamaClient(config)
        with self.assertRaises(bench.BenchmarkError):
            client._url("/api/pull")


if __name__ == "__main__":
    unittest.main()
