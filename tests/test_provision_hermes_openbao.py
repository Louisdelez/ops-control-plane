from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
import re
import stat
from types import SimpleNamespace
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "provision-hermes-openbao"
REPOSITORY = SCRIPT.parents[1]


def load_script():
    loader = importlib.machinery.SourceFileLoader("provision_hermes_openbao", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ProvisionHermesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_script()
        self.safe_role = {
            "data": {
                "token_policies": ["hermes-runtime"],
                "bind_secret_id": True,
                "token_no_default_policy": True,
                "secret_id_num_uses": 1024,
                "token_num_uses": 2,
                "token_period": 0,
                "token_type": "service",
                "secret_id_bound_cidrs": ["127.0.0.1/32"],
                "token_bound_cidrs": ["127.0.0.1/32"],
                "secret_id_ttl": 2_592_000,
                "token_ttl": 60,
                "token_max_ttl": 120,
                "token_explicit_max_ttl": 120,
            }
        }

    def test_exact_runtime_role_is_accepted(self):
        self.module._validate_role(self.safe_role)

    def test_canonical_loopback_host_cidr_is_accepted(self):
        self.safe_role["data"]["token_bound_cidrs"] = ["127.0.0.1"]
        self.module._validate_role(self.safe_role)

    def test_broader_loopback_network_is_rejected(self):
        self.safe_role["data"]["token_bound_cidrs"] = ["127.0.0.0/8"]
        with self.assertRaises(self.module.ProvisioningError):
            self.module._validate_role(self.safe_role)

    def test_added_policy_is_rejected(self):
        self.safe_role["data"]["token_policies"].append("default")
        with self.assertRaises(self.module.ProvisioningError):
            self.module._validate_role(self.safe_role)

    def test_unbounded_secret_id_is_rejected(self):
        self.safe_role["data"]["secret_id_ttl"] = 0
        with self.assertRaises(self.module.ProvisioningError):
            self.module._validate_role(self.safe_role)

    def test_periodic_token_is_rejected(self):
        self.safe_role["data"]["token_period"] = 1200
        with self.assertRaises(self.module.ProvisioningError):
            self.module._validate_role(self.safe_role)

    def test_newline_secret_is_rejected(self):
        document = {"data": {"data": {"orchestrator_api_key": "safe-value-123456\nINJECT=x"}}}
        with self.assertRaises(self.module.ProvisioningError):
            self.module._secret_field(document, "orchestrator_api_key")

    def test_runtime_policy_is_exact_and_has_no_provider_key_read(self):
        policy = (
            REPOSITORY / "config/openbao/policies/hermes-runtime.hcl"
        ).read_text(encoding="utf-8")
        self.assertEqual(
            self.module._normalise_policy(policy),
            self.module._EXPECTED_RUNTIME_POLICY,
        )
        effective = self.module._normalise_policy(policy)
        self.assertNotIn("llm/qwen", effective)
        self.assertNotIn("llm/deepseek", effective)
        self.assertEqual(effective.count('capabilities = ["read"]'), 1)

    def test_legacy_policy_sources_are_exact_revoke_only(self):
        for name in ("deepseek-client", "hermes-coordinator"):
            with self.subTest(name=name):
                policy = (
                    REPOSITORY / f"config/openbao/policies/{name}.hcl"
                ).read_text(encoding="utf-8")
                effective = self.module._normalise_policy(policy)
                self.assertEqual(effective, self.module._EXPECTED_QUARANTINE_POLICY)
                self.assertNotIn("read", effective)
                self.assertNotIn("renew-self", effective)

    def test_extra_policy_stanza_is_rejected(self):
        unsafe = self.module._EXPECTED_RUNTIME_POLICY + "\n" + (
            'path "kv-infra-shared/data/llm/deepseek" {\n'
            'capabilities = ["read"]\n}'
        )
        with self.assertRaises(self.module.ProvisioningError):
            self.module._validate_policy_scope(
                unsafe, self.module._EXPECTED_RUNTIME_POLICY, "runtime policy"
            )

    def test_policy_source_requires_root_ownership(self):
        unsafe = SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=1000)
        with mock.patch.object(Path, "lstat", return_value=unsafe):
            with self.assertRaises(self.module.ProvisioningError):
                self.module._validate_root_regular(Path("/installed/policy"), "policy")

    def test_reconcile_quarantines_deletes_then_applies_exact_runtime(self):
        requests = []

        def fake_request(method, path, **kwargs):
            requests.append((method, path, kwargs))
            if method == "GET" and path.endswith("/hermes-runtime"):
                return self.safe_role
            return None

        with mock.patch.object(self.module, "_request", side_effect=fake_request):
            self.module._reconcile_runtime(
                "human-token-value-12345",
                "runtime-policy",
                {
                    "deepseek-client": "deepseek-quarantine",
                    "hermes-coordinator": "coordinator-quarantine",
                },
            )

        methods_and_paths = [(method, path) for method, path, _ in requests]
        self.assertEqual(
            methods_and_paths,
            [
                ("POST", "/v1/sys/policies/acl/deepseek-client"),
                ("POST", "/v1/sys/policies/acl/hermes-coordinator"),
                ("DELETE", "/v1/auth/approle/role/deepseek-client"),
                ("DELETE", "/v1/auth/approle/role/hermes-coordinator"),
                ("GET", "/v1/auth/approle/role/deepseek-client"),
                ("GET", "/v1/auth/approle/role/hermes-coordinator"),
                ("POST", "/v1/sys/policies/acl/hermes-runtime"),
                ("POST", "/v1/auth/approle/role/hermes-runtime"),
                ("GET", "/v1/auth/approle/role/hermes-runtime"),
            ],
        )
        self.assertEqual(
            requests[0][2]["payload"], {"policy": "deepseek-quarantine"}
        )
        self.assertEqual(
            requests[1][2]["payload"], {"policy": "coordinator-quarantine"}
        )
        role_payload = requests[7][2]["payload"]
        self.assertEqual(role_payload["token_policies"], ["hermes-runtime"])
        self.assertEqual(role_payload["token_num_uses"], 2)
        self.assertEqual(role_payload["secret_id_num_uses"], 1024)
        self.assertEqual(role_payload["token_ttl"], "60s")
        self.assertEqual(role_payload["token_type"], "service")

    def test_legacy_deletion_failure_is_fail_closed_and_actionable(self):
        requests = []

        def fake_request(method, path, **kwargs):
            requests.append((method, path, kwargs))
            if method == "DELETE":
                raise self.module.ProvisioningError("unavailable")
            return None

        with mock.patch.object(self.module, "_request", side_effect=fake_request):
            with self.assertRaisesRegex(
                self.module.ProvisioningError,
                "keep Hermes stopped.*auth/approle/role/deepseek-client",
            ):
                self.module._reconcile_runtime(
                    "human-token-value-12345",
                    "runtime-policy",
                    {
                        "deepseek-client": "deepseek-quarantine",
                        "hermes-coordinator": "coordinator-quarantine",
                    },
                )
        self.assertEqual([entry[0] for entry in requests[:3]], ["POST", "POST", "DELETE"])
        self.assertFalse(
            any(path.endswith("/hermes-runtime") for _, path, _ in requests),
            "runtime credentials must not be reconciled after failed legacy deletion",
        )

    def test_direct_provider_key_request_is_not_allowlisted(self):
        with self.assertRaises(self.module.ProvisioningError):
            self.module._request(
                "GET",
                "/v1/kv-infra-shared/data/llm/deepseek",
                token="human-token-value-12345",
            )
        with self.assertRaises(self.module.ProvisioningError):
            self.module._request(
                "GET",
                "/v1/kv-infra-shared/data/llm/qwen",
                token="human-token-value-12345",
            )

    def test_bootstrap_quarantines_and_does_not_recreate_legacy_roles(self):
        bootstrap = (REPOSITORY / "scripts/bootstrap-openbao.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("if [[ $policy_name == deepseek-client ]]", bootstrap)
        self.assertIn(
            'bao policy write deepseek-client "$policy_dir/deepseek-client.hcl"',
            bootstrap,
        )
        role_loop = re.search(r"for role in ([^;]+); do", bootstrap)
        self.assertIsNotNone(role_loop)
        assert role_loop is not None
        self.assertNotIn("deepseek-client", role_loop.group(1))
        self.assertNotIn("hermes-coordinator", role_loop.group(1))
        quarantine_at = bootstrap.index("bao policy write deepseek-client")
        delete_at = bootstrap.index('bao delete "auth/approle/role/${legacy_role}"')
        runtime_at = bootstrap.index('bao write "auth/approle/role/hermes-runtime"')
        self.assertLess(quarantine_at, delete_at)
        self.assertLess(delete_at, runtime_at)

    def test_configure_installs_only_the_safe_legacy_policy_sources(self):
        configure = (REPOSITORY / "scripts/configure-openbao.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'for policy_file in "$repo_dir"/config/openbao/policies/*.hcl; do',
            configure,
        )
        for name in ("deepseek-client", "hermes-coordinator"):
            policy = (
                REPOSITORY / f"config/openbao/policies/{name}.hcl"
            ).read_text(encoding="utf-8")
            self.assertEqual(
                self.module._normalise_policy(policy),
                self.module._EXPECTED_QUARANTINE_POLICY,
            )

    def test_reconciliation_happens_before_secret_id_rotation(self):
        source = SCRIPT.read_text(encoding="utf-8")
        reconcile_at = source.index(
            "_reconcile_runtime(human_token, runtime_policy, quarantine_policies)",
            source.index("def provision"),
        )
        rotate_at = source.index(
            '"POST", f"/v1/auth/approle/role/{ROLE_NAME}/secret-id"',
            reconcile_at,
        )
        self.assertLess(reconcile_at, rotate_at)


if __name__ == "__main__":
    unittest.main()
