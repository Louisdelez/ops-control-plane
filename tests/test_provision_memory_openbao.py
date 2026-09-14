from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "provision-memory-openbao"


def load_script():
    loader = importlib.machinery.SourceFileLoader("provision_memory_openbao", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ProvisionMemoryOpenBaoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_script()
        self.safe_role = {
            "data": {
                "token_policies": ["ops-memory-runtime"],
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

    def test_extra_policy_is_rejected(self):
        self.safe_role["data"]["token_policies"].append("default")
        with self.assertRaises(self.module.ProvisioningError):
            self.module._validate_role(self.safe_role)

    def test_runtime_token_must_have_exactly_two_uses(self):
        self.safe_role["data"]["token_num_uses"] = 3
        with self.assertRaises(self.module.ProvisioningError):
            self.module._validate_role(self.safe_role)

    def test_secret_id_is_bounded(self):
        self.safe_role["data"]["secret_id_num_uses"] = 0
        with self.assertRaises(self.module.ProvisioningError):
            self.module._validate_role(self.safe_role)

    def test_only_role_and_secret_id_are_persistent_runtime_credentials(self):
        self.assertEqual(
            self.module.CREDENTIAL_NAMES,
            ("ops-memory-openbao-role-id", "ops-memory-openbao-secret-id"),
        )
        self.assertNotIn("accessor", " ".join(self.module.CREDENTIAL_NAMES))

    def test_runtime_test_reads_then_revokes(self):
        calls = []

        def request(method, path, **kwargs):
            calls.append((method, path))
            if path == "/v1/auth/approle/login":
                return {"auth": {"client_token": "token-memory-runtime-12345"}}
            if path == self.module.KV_PATH:
                return {"data": {"data": {"api_key": "Q" * 48}}}
            if path == "/v1/auth/token/revoke-self":
                return None
            self.fail(path)

        self.module._request = request
        self.module._test_runtime(
            "role-identifier-12345", "secret-identifier-12345", "Q" * 48
        )
        self.assertEqual(
            calls,
            [
                ("POST", "/v1/auth/approle/login"),
                ("GET", self.module.KV_PATH),
                ("POST", "/v1/auth/token/revoke-self"),
            ],
        )

    def test_failed_migration_restores_legacy_blob_and_removes_new_ids(self):
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as temporary:
            store = Path(temporary) / "credstore"
            store.mkdir()
            role = store / self.module.CREDENTIAL_NAMES[0]
            secret = store / self.module.CREDENTIAL_NAMES[1]
            role.write_bytes(b"encrypted-role")
            secret.write_bytes(b"encrypted-secret")
            staging = store / ".ops-memory-openbao-transaction"
            staging.mkdir()
            (staging / "legacy-qdrant-api-key").write_bytes(b"encrypted-legacy")
            legacy = store / "ops-memory-qdrant-api-key"
            self.module.CREDSTORE = store
            self.module.LEGACY_CREDENTIAL = legacy

            def cleanup(path):
                self.assertFalse((path / "legacy-qdrant-api-key").exists())
                path.rmdir()

            self.module._cleanup_staging = cleanup
            self.module._rollback_local_transaction(
                (role, secret),
                staging,
                installed_new_credentials=True,
                legacy_moved=True,
            )

            self.assertFalse(os.path.lexists(role))
            self.assertFalse(os.path.lexists(secret))
            self.assertEqual(legacy.read_bytes(), b"encrypted-legacy")
            self.assertFalse(staging.exists())


if __name__ == "__main__":
    unittest.main()
