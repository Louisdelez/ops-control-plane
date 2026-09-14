from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
from pathlib import Path
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "zulip-openbao-launcher"


def load_script():
    loader = importlib.machinery.SourceFileLoader("zulip_openbao_launcher", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def valid_secret() -> dict[str, str]:
    return {
        "realm_url": "https://ops.example.test",
        "bot_email": "ops-bot@example.test",
        "api_key": "zulip-api-key-0123456789",
        "bot_user_id": "99",
        "approver_user_ids": "42,43",
        "approval_stream": "Operations",
        "approval_stream_id": "1001",
        "approval_topic": "Approvals",
        "alert_stream": "Operations",
        "alert_stream_id": "1001",
        "alert_topic": "Alerts",
        "daily_stream": "Operations",
        "daily_stream_id": "1001",
        "daily_topic": "Daily status",
    }


class ZulipOpenBaoLauncherTests(unittest.TestCase):
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
        if path == self.module.KV_PATH:
            return {"data": {"data": valid_secret()}}
        if path == "/v1/auth/token/revoke-self":
            self.assertFalse(kwargs["expect_json"])
            return None
        self.fail(f"unexpected path: {path}")

    def resolve(self):
        return self.module.resolve_environment(
            credential_loader=self.credentials.__getitem__,
            request_callable=self.request,
        )

    def test_one_exact_kv_read_then_revoke(self):
        environment = self.resolve()
        self.assertEqual(
            self.calls,
            [
                ("POST", "/v1/auth/approle/login"),
                ("GET", self.module.KV_PATH),
                ("POST", "/v1/auth/token/revoke-self"),
            ],
        )
        self.assertEqual(environment["ZULIP_BOT_USER_ID"], "99")
        self.assertEqual(environment["ZULIP_APPROVER_USER_IDS"], "42,43")
        self.assertNotIn("CREDENTIALS_DIRECTORY", environment)
        self.assertNotIn("BAO_TOKEN", environment)
        self.assertTrue(set(environment).issubset(set(self.module.FIELD_TO_ENV.values())))

    def test_unknown_kv_field_is_rejected(self):
        document = valid_secret()
        document["unreviewed"] = "value"
        with self.assertRaises(self.module.LaunchError):
            self.module.validate_secret_data(document)

    def test_bot_cannot_approve_and_duplicates_are_rejected(self):
        document = valid_secret()
        document["approver_user_ids"] = "42,99"
        with self.assertRaises(self.module.LaunchError):
            self.module.validate_secret_data(document)
        document["approver_user_ids"] = "42,42"
        with self.assertRaises(self.module.LaunchError):
            self.module.validate_secret_data(document)

    def test_injection_is_rejected_and_token_still_revoked(self):
        original_request = self.request

        def injecting_request(method, path, **kwargs):
            if path == self.module.KV_PATH:
                self.calls.append((method, path))
                document = valid_secret()
                document["api_key"] = "safe-value-123456\nLD_PRELOAD=x"
                return {"data": {"data": document}}
            return original_request(method, path, **kwargs)

        with self.assertRaises(self.module.LaunchError):
            self.module.resolve_environment(
                credential_loader=self.credentials.__getitem__,
                request_callable=injecting_request,
            )
        self.assertEqual(self.calls[-1], ("POST", "/v1/auth/token/revoke-self"))

    def test_failed_revoke_discards_valid_environment(self):
        original_request = self.request

        def failing_revoke(method, path, **kwargs):
            if path == "/v1/auth/token/revoke-self":
                self.calls.append((method, path))
                raise self.module.LaunchError
            return original_request(method, path, **kwargs)

        with self.assertRaises(self.module.LaunchError):
            self.module.resolve_environment(
                credential_loader=self.credentials.__getitem__,
                request_callable=failing_revoke,
            )

    def test_cli_failure_emits_no_secret_or_environment(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(self.module, "launch", side_effect=self.module.LaunchError):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = self.module.main(["zulip-openbao-launcher"])
        self.assertEqual(result, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn("ZULIP_API_KEY", stderr.getvalue())
        self.assertNotIn("zulip-api-key", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
