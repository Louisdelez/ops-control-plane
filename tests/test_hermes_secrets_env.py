from __future__ import annotations

import importlib.machinery
import importlib.util
import contextlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hermes-secrets-env"


def load_script():
    loader = importlib.machinery.SourceFileLoader("hermes_secrets_env", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class HermesSecretsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_script()
        self.calls: list[tuple[str, str]] = []
        self.credentials = {
            self.module.ROLE_ID_CREDENTIAL: "role-0123456789abcdef",
            self.module.SECRET_ID_CREDENTIAL: "secret-0123456789abcdef",
        }

    def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        if path == "/v1/auth/approle/login":
            self.assertEqual(
                kwargs["payload"],
                {
                    "role_id": self.credentials[self.module.ROLE_ID_CREDENTIAL],
                    "secret_id": self.credentials[self.module.SECRET_ID_CREDENTIAL],
                },
            )
            return {"auth": {"client_token": "token-0123456789abcdef"}}
        if path == "/v1/kv-infra-shared/data/hermes/gateway":
            return {
                "data": {
                    "data": {
                        "api_server_key": "gw-0123456789abcdef",
                        "orchestrator_api_key": "facade-0123456789abcdef",
                    }
                }
            }
        if path == "/v1/auth/token/revoke-self":
            self.assertFalse(kwargs["expect_json"])
            return None
        self.fail(f"unexpected path: {path}")

    def resolve(self, profile="default"):
        return self.module.resolve_secrets(
            profile,
            credential_loader=self.credentials.__getitem__,
            request_callable=self.request,
        )

    def test_default_reads_one_exact_path_then_revokes(self):
        self.assertEqual(
            self.resolve(),
            {
                "HERMES_FACADE_TOKEN": "facade-0123456789abcdef",
                "API_SERVER_KEY": "gw-0123456789abcdef",
            },
        )
        self.assertEqual(
            self.calls,
            [
                ("POST", "/v1/auth/approle/login"),
                ("GET", "/v1/kv-infra-shared/data/hermes/gateway"),
                ("POST", "/v1/auth/token/revoke-self"),
            ],
        )

    def test_secondary_profile_emits_only_the_facade_token(self):
        self.assertEqual(
            self.resolve("minecraft-ops"),
            {"HERMES_FACADE_TOKEN": "facade-0123456789abcdef"},
        )
        self.assertIn(("GET", "/v1/kv-infra-shared/data/hermes/gateway"), self.calls)
        self.assertEqual(self.calls[-1], ("POST", "/v1/auth/token/revoke-self"))

    def test_invalid_profile_fails_before_login(self):
        with self.assertRaises(self.module.SecretResolutionError):
            self.resolve("../../default")
        self.assertEqual(self.calls, [])

    def test_bad_secret_value_is_not_returned_and_token_is_revoked(self):
        original_request = self.request

        def bad_request(method, path, **kwargs):
            if path == "/v1/kv-infra-shared/data/hermes/gateway":
                self.calls.append((method, path))
                return {
                    "data": {
                        "data": {
                            "orchestrator_api_key": "valid\nAPI_SERVER_KEY=leak",
                            "api_server_key": "gw-0123456789abcdef",
                        }
                    }
                }
            return original_request(method, path, **kwargs)

        with self.assertRaises(self.module.SecretResolutionError):
            self.module.resolve_secrets(
                "default",
                credential_loader=self.credentials.__getitem__,
                request_callable=bad_request,
            )
        self.assertEqual(self.calls[-1], ("POST", "/v1/auth/token/revoke-self"))

    def test_failed_read_still_revokes(self):
        original_request = self.request

        def failing_request(method, path, **kwargs):
            if path == "/v1/kv-infra-shared/data/hermes/gateway":
                self.calls.append((method, path))
                raise self.module.SecretResolutionError
            return original_request(method, path, **kwargs)

        with self.assertRaises(self.module.SecretResolutionError):
            self.module.resolve_secrets(
                "default",
                credential_loader=self.credentials.__getitem__,
                request_callable=failing_request,
            )
        self.assertEqual(self.calls[-1], ("POST", "/v1/auth/token/revoke-self"))

    def test_failed_revoke_discards_successful_reads(self):
        original_request = self.request

        def failing_revoke(method, path, **kwargs):
            if path == "/v1/auth/token/revoke-self":
                self.calls.append((method, path))
                raise self.module.SecretResolutionError
            return original_request(method, path, **kwargs)

        with self.assertRaises(self.module.SecretResolutionError):
            self.module.resolve_secrets(
                "default",
                credential_loader=self.credentials.__getitem__,
                request_callable=failing_revoke,
            )

    def test_cli_failure_emits_no_dotenv_payload(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(
            self.module,
            "resolve_secrets",
            side_effect=self.module.SecretResolutionError,
        ):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                return_code = self.module.main(["hermes-secrets-env", "default"])
        self.assertEqual(return_code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("HERMES_FACADE_TOKEN", stderr.getvalue())
        self.assertNotIn("API_SERVER_KEY", stderr.getvalue())

    def test_loader_accepts_read_only_systemd_group_credential(self):
        with tempfile.TemporaryDirectory() as directory:
            credential_dir = Path(directory)
            credential = credential_dir / self.module.ROLE_ID_CREDENTIAL
            credential.write_text("role-0123456789abcdef\n", encoding="ascii")
            credential.chmod(0o440)
            with mock.patch.object(self.module, "CREDENTIALS_DIRECTORY", credential_dir):
                with mock.patch.dict(
                    os.environ, {"CREDENTIALS_DIRECTORY": str(credential_dir)}, clear=False
                ):
                    self.assertEqual(
                        self.module._load_credential(self.module.ROLE_ID_CREDENTIAL),
                        "role-0123456789abcdef",
                    )

    def test_loader_rejects_group_write_or_world_read(self):
        with tempfile.TemporaryDirectory() as directory:
            credential_dir = Path(directory)
            credential = credential_dir / self.module.ROLE_ID_CREDENTIAL
            credential.write_text("role-0123456789abcdef\n", encoding="ascii")
            with mock.patch.object(self.module, "CREDENTIALS_DIRECTORY", credential_dir):
                with mock.patch.dict(
                    os.environ, {"CREDENTIALS_DIRECTORY": str(credential_dir)}, clear=False
                ):
                    for unsafe_mode in (0o460, 0o444):
                        credential.chmod(unsafe_mode)
                        with self.assertRaises(self.module.SecretResolutionError):
                            self.module._load_credential(self.module.ROLE_ID_CREDENTIAL)


if __name__ == "__main__":
    unittest.main()
