from __future__ import annotations

import importlib.util
import importlib.machinery
import ast
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock
import json


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "deploy" / "zulip-local"
MODULE_PATH = PACKAGE / "lib" / "zulip_local.py"
SPEC = importlib.util.spec_from_file_location("zulip_local_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
zulip_local = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(zulip_local)

PROVISION_PATH = PACKAGE / "bin" / "provision-zulip.py"
PROVISION_SPEC = importlib.util.spec_from_file_location(
    "provision_zulip_under_test", PROVISION_PATH
)
assert PROVISION_SPEC is not None and PROVISION_SPEC.loader is not None
provision_zulip = importlib.util.module_from_spec(PROVISION_SPEC)
PROVISION_SPEC.loader.exec_module(provision_zulip)

PROVISION_OPENBAO_PATH = PACKAGE / "bin" / "provision-openbao.py"
PROVISION_OPENBAO_SPEC = importlib.util.spec_from_file_location(
    "provision_openbao_under_test", PROVISION_OPENBAO_PATH
)
assert PROVISION_OPENBAO_SPEC is not None and PROVISION_OPENBAO_SPEC.loader is not None
provision_openbao = importlib.util.module_from_spec(PROVISION_OPENBAO_SPEC)
PROVISION_OPENBAO_SPEC.loader.exec_module(provision_openbao)


def server_document() -> dict[str, str]:
    return {
        "avatar_salt": "v" * 64,
        "camo_key": "c" * 64,
        "postgres_password": "p" * 32,
        "memcached_password": "m" * 32,
        "rabbitmq_password": "r" * 32,
        "redis_password": "d" * 32,
        "secret_key": "s" * 64,
        "shared_secret": "h" * 64,
        "email_password": "e" * 32,
        "zulip_org_id": "123e4567-e89b-42d3-a456-426614174000",
        "zulip_org_key": "o" * 64,
        "tls_ca_certificate": "-----BEGIN CERTIFICATE-----\nTESTCA\n-----END CERTIFICATE-----\n",
        "tls_certificate": "-----BEGIN CERTIFICATE-----\nTESTCERT\n-----END CERTIFICATE-----\n",
        "tls_private_key": "-----BEGIN PRIVATE KEY-----\nTESTKEY\n-----END PRIVATE KEY-----\n",
    }


def bot_document() -> dict[str, str]:
    return {
        "realm_url": "https://zulip.ops.local:8443",
        "bot_email": "ops-bot@zulip.ops.local",
        "api_key": "a" * 32,
        "bot_user_id": "42",
        "approver_user_ids": "7",
        "approval_stream": "Operations",
        "approval_stream_id": "11",
        "approval_topic": "approvals",
        "alert_stream": "Alerts",
        "alert_stream_id": "12",
        "alert_topic": "alerts",
        "daily_stream": "Daily Reports",
        "daily_stream_id": "13",
        "daily_topic": "daily",
        "ca_bundle": "/etc/pki/ca-trust/source/anchors/zulip-ops-local-ca.crt",
    }


class ZulipLocalUnitTests(unittest.TestCase):
    def test_openbao_roles_explicitly_reject_periodic_tokens(self) -> None:
        payload = provision_openbao.role_payload("zulip-local-runtime", 2)
        self.assertEqual(payload["token_period"], "0s")
        returned = {
            "data": {
                **payload,
                "secret_id_ttl": 2_592_000,
                "token_ttl": 120,
                "token_max_ttl": 120,
                "token_explicit_max_ttl": 120,
                "token_period": 0,
            }
        }
        provision_openbao.validate_role_document(returned, "zulip-local-runtime", 2)
        returned["data"]["token_period"] = 60
        with self.assertRaises(provision_openbao.ZulipLocalError):
            provision_openbao.validate_role_document(returned, "zulip-local-runtime", 2)

    def test_exact_server_schema(self) -> None:
        valid = zulip_local.validate_server_secret(server_document())
        self.assertEqual(set(valid), zulip_local.SERVER_FIELDS)
        for mutation in ({"extra": "x" * 32}, {"postgres_password": "short"}):
            candidate = server_document()
            candidate.update(mutation)
            with self.assertRaises(zulip_local.ZulipLocalError):
                zulip_local.validate_server_secret(candidate)

    def test_exact_bridge_contract(self) -> None:
        valid = zulip_local.validate_bot_secret(bot_document())
        self.assertEqual(set(valid), zulip_local.BOT_FIELDS)
        candidate = bot_document()
        candidate["approver_user_ids"] = candidate["bot_user_id"]
        with self.assertRaises(zulip_local.ZulipLocalError):
            zulip_local.validate_bot_secret(candidate)

    def test_bridge_reconciliation_preserves_only_valid_tuning(self) -> None:
        existing = bot_document()
        existing.update(
            {
                "poll_timeout_seconds": "60.0",
                "retry_max_seconds": "30",
                "producer_poll_seconds": "5",
                "pending_page_size": "25",
            }
        )
        desired = bot_document()
        desired["api_key"] = "b" * 32
        reconciled = zulip_local.merge_bot_secret(existing, desired)
        self.assertEqual(reconciled["api_key"], "b" * 32)
        self.assertEqual(reconciled["poll_timeout_seconds"], "60")
        self.assertEqual(reconciled["pending_page_size"], "25")

        existing["pending_page_size"] = "51"
        with self.assertRaises(zulip_local.ZulipLocalError):
            zulip_local.merge_bot_secret(existing, desired)

    def test_generated_contract_is_accepted_by_existing_bridge_launcher(self) -> None:
        launcher_path = ROOT / "scripts" / "zulip-openbao-launcher"
        loader = importlib.machinery.SourceFileLoader(
            "zulip_openbao_launcher_contract_test", str(launcher_path)
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        assert spec is not None
        launcher = importlib.util.module_from_spec(spec)
        loader.exec_module(launcher)
        original_lstat = os.lstat

        def fake_lstat(path: object) -> object:
            if Path(path) == Path(
                "/etc/pki/ca-trust/source/anchors/zulip-ops-local-ca.crt"
            ):
                return os.stat_result((stat.S_IFREG | 0o644, 1, 1, 1, 0, 0, 1, 0, 0, 0))
            return original_lstat(path)

        with mock.patch.object(launcher.os, "lstat", side_effect=fake_lstat):
            environment = launcher.validate_secret_data(bot_document())
        self.assertEqual(environment["ZULIP_REALM_URL"], zulip_local.ZULIP_ORIGIN)
        self.assertEqual(environment["ZULIP_API_KEY"], "a" * 32)

    def test_bridge_contract_rejects_foreign_origin_and_extra_field(self) -> None:
        candidate = bot_document()
        candidate["realm_url"] = "https://example.invalid"
        with self.assertRaises(zulip_local.ZulipLocalError):
            zulip_local.validate_bot_secret(candidate)
        candidate = bot_document()
        candidate["unexpected"] = "x"
        with self.assertRaises(zulip_local.ZulipLocalError):
            zulip_local.validate_bot_secret(candidate)

    def test_renderer_writes_only_private_tree_with_exact_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "zulip-local"
            zulip_local.render_runtime_files(
                server_document(),
                run_dir,
                expected_uid=os.geteuid(),
                zulip_gid=os.getegid(),
                label_for_containers=False,
            )
            expected = {
                "secrets/postgres_password": 0o400,
                "secrets/memcached_password": 0o444,
                "secrets/rabbitmq_password": 0o400,
                "secrets/redis_password": 0o400,
                "tls/ca.crt": 0o444,
                "tls/zulip.combined-chain.crt": 0o444,
                "tls/zulip.key": 0o400,
                "admin/admin-password": 0o444,
                "zulip-secrets.conf": 0o640,
            }
            for relative, mode in expected.items():
                path = run_dir / relative
                self.assertTrue(path.is_file())
                self.assertFalse(path.is_symlink())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
            conf = (run_dir / "zulip-secrets.conf").read_text(encoding="utf-8")
            for key in zulip_local.ZULIP_SECRET_FIELDS:
                self.assertIn(f"{key} = ", conf)
            self.assertEqual((run_dir / "zulip-secrets.conf").stat().st_gid, os.getegid())

    def test_defaults_are_exact_and_non_secret(self) -> None:
        values = zulip_local.load_defaults(PACKAGE / "config" / "defaults.env")
        self.assertEqual(set(values), zulip_local.DEFAULT_KEYS)
        self.assertEqual(values["ZULIP_LOCAL_ORIGIN"], zulip_local.ZULIP_ORIGIN)
        self.assertNotIn("PASSWORD", values)
        self.assertNotIn("API_KEY", values)

    def test_owner_password_policy_matches_zulip_maximum(self) -> None:
        with mock.patch.object(
            provision_zulip.getpass, "getpass", side_effect=["x" * 100, "x" * 100]
        ):
            self.assertEqual(provision_zulip.load_password(None), "x" * 100)
        with mock.patch.object(
            provision_zulip.getpass, "getpass", side_effect=["x" * 101, "x" * 101]
        ):
            with self.assertRaises(provision_zulip.ZulipLocalError):
                provision_zulip.load_password(None)

    def test_http_clients_keep_credentials_out_of_urls(self) -> None:
        class Response:
            status = 200

            def __init__(self, payload: dict[str, object]) -> None:
                self.body = json.dumps(payload).encode()

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, size: int) -> bytes:
                result, self.body = self.body[:size], self.body[size:]
                return result

        class Opener:
            def __init__(self) -> None:
                self.request = None

            def open(self, request: object, timeout: float) -> Response:
                self.request = request
                return Response({"result": "success", "api_key": "k" * 32})

        opener = Opener()
        client = zulip_local.ZulipClient(opener=opener)
        self.assertEqual(client.fetch_api_key("admin@ops.local", "not-a-real-password"), "k" * 32)
        request = opener.request
        self.assertIsNotNone(request)
        self.assertNotIn("not-a-real-password", request.full_url)
        self.assertIn(b"password=not-a-real-password", request.data)

    def test_renderer_rejects_a_symlinked_runtime_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir(mode=0o700)
            link = root / "zulip-local"
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(zulip_local.ZulipLocalError):
                zulip_local.render_runtime_files(
                    server_document(),
                    link,
                    expected_uid=os.geteuid(),
                    zulip_gid=os.getegid(),
                    label_for_containers=False,
                )

    def test_lifecycle_lock_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "zulip.lock"
            first = zulip_local.acquire_deployment_lock(path)
            try:
                with self.assertRaises(zulip_local.ZulipLocalError):
                    zulip_local.acquire_deployment_lock(path)
            finally:
                os.close(first)


class ZulipLocalStaticTests(unittest.TestCase):
    def test_compose_is_exactly_digest_pinned_and_local_only(self) -> None:
        text = (PACKAGE / "compose.yaml").read_text(encoding="utf-8")
        pins = {
            "765f0ab3caa49041989132ee1879d98dbab1df7695c27e713eac1f114d167755",
            "e71ba8616fa42cdc1b248f51263d9290c29681cb8c1992eb9b498af0bb656b29",
            "c29847751abb41f4c268c84fb3087fee05d4edcbda44409ccb5086e26148e8a7",
            "15e7b5e60af2d2147f8d74eef5b93c29501cd644361ca93bc854e409c2dde624",
            "becdda6c7f4b3fb42e42fd7f120bbf5c54c4caaaf16f26da24e4563d2c1f0576",
        }
        for digest in pins:
            self.assertEqual(text.count("sha256:" + digest), 1)
        self.assertEqual(text.count("platform: linux/amd64"), 5)
        self.assertEqual(text.count('host_ip: "127.0.0.1"'), 1)
        self.assertEqual(text.count(":rw,Z"), 1)
        self.assertEqual(text.count(":ro,Z"), 1)
        self.assertNotIn("create_host_path", text)
        self.assertEqual(text.count("logging: *local-logging"), 5)
        self.assertIn('published: "8443"', text)
        self.assertIn('SETTING_EXTERNAL_HOST: "zulip.ops.local:8443"', text)
        self.assertNotIn("/run/secrets/zulip__", text.split("  zulip:\n", 1)[1].split("\nsecrets:\n", 1)[0])
        self.assertNotIn('published: "80"', text)
        self.assertNotIn('published: "443"', text)
        self.assertNotIn("--requirepass", text)
        self.assertIn("redis_config=/run/zulip-local/redis.conf", text)
        self.assertNotIn("RABBITMQ_DEFAULT_PASS", text)
        self.assertNotIn("RABBITMQ_DEFAULT_USER", text)
        self.assertIn("rabbitmq_config=/run/zulip-local/rabbitmq.conf", text)
        self.assertIn("RABBITMQ_CONFIG_FILE: /run/zulip-local/rabbitmq.conf", text)
        self.assertIn('chmod 0400 "$$rabbitmq_config_tmp"', text)
        self.assertIn('chown rabbitmq:rabbitmq "$$rabbitmq_config_tmp"', text)
        self.assertEqual(text.count('"/run/zulip-local:rw,nosuid,nodev,noexec,size=1m,'), 3)
        self.assertNotIn("/home/memcache/memcached-sasl-db", text)
        self.assertNotIn("/tmp/zulip-redis.conf", text)
        self.assertNotIn("/etc/rabbitmq/conf.d/90-zulip-local.conf", text)
        for line in text.splitlines():
            if line.lstrip().startswith("file:"):
                self.assertTrue(
                    "/run/zulip-local/secrets/" in line
                    or "/run/zulip-local/admin/admin-password" in line
                )

    def test_policies_have_no_wildcards_or_list_capability(self) -> None:
        runtime = (PACKAGE / "openbao" / "zulip-local-runtime.hcl").read_text()
        bootstrap = (PACKAGE / "openbao" / "zulip-local-bootstrap.hcl").read_text()
        for policy in (runtime, bootstrap):
            self.assertNotIn("*", policy)
            self.assertNotIn('"list"', policy)
            self.assertIn('path "auth/token/revoke-self"', policy)
        self.assertNotIn("zulip/bot", runtime)
        self.assertNotIn("zulip/server", bootstrap)

    def test_admin_password_is_never_a_value_argument_or_environment(self) -> None:
        source = (PACKAGE / "bin" / "provision-zulip.py").read_text()
        compose = (PACKAGE / "compose.yaml").read_text()
        self.assertNotIn('"--password="', source)
        self.assertIn("--password-file=/run/secrets/admin_bootstrap_password", source)
        self.assertIn('"change_password"', source)
        self.assertIn("stdin_data=prompt_input", source)
        self.assertNotIn("ADMIN_PASSWORD:", compose)
        self.assertNotIn("ZULIP_PASSWORD:", compose)

    def test_requested_lifecycle_and_systemd_assets_exist(self) -> None:
        scripts = {
            "install.sh",
            "preflight.sh",
            "init.sh",
            "provision-openbao.py",
            "provision-zulip.py",
            "backup.sh",
            "restore.sh",
            "health.sh",
            "rollback.sh",
            "service.sh",
        }
        self.assertTrue(scripts.issubset({path.name for path in (PACKAGE / "bin").iterdir()}))
        units = {path.name for path in (PACKAGE / "systemd").iterdir()}
        self.assertIn("zulip-local.service", units)
        self.assertIn("zulip-local-secrets.service", units)
        self.assertIn("zulip-local-health.timer", units)
        self.assertIn("zulip-local-backup.timer", units)

    def test_every_start_path_waits_for_real_https_health(self) -> None:
        common = (PACKAGE / "bin" / "common.sh").read_text()
        self.assertIn("for ((attempt = 1; attempt <= 120; attempt++))", common)
        self.assertIn('"$health_script" --quiet', common)
        for name in ("service.sh", "init.sh", "restore.sh"):
            source = (PACKAGE / "bin" / name).read_text()
            self.assertIn('wait_for_zulip_health "$script_dir/health.sh"', source)

    def test_systemd_uses_encrypted_credentials_and_no_environment_file(self) -> None:
        secret_unit = (PACKAGE / "systemd" / "zulip-local-secrets.service").read_text()
        stack_unit = (PACKAGE / "systemd" / "zulip-local.service").read_text()
        self.assertIn("LoadCredentialEncrypted=zulip-local-runtime-role-id", secret_unit)
        self.assertIn("LoadCredentialEncrypted=zulip-local-runtime-secret-id", secret_unit)
        self.assertNotIn("EnvironmentFile=", secret_unit + stack_unit)
        self.assertIn("BindsTo=zulip-local-secrets.service", stack_unit)
        self.assertIn("bin/service.sh start", stack_unit)
        self.assertIn("ExecStopPost=/opt/ops-control-plane/deploy/zulip-local/bin/service.sh stop", stack_unit)
        self.assertNotIn("\nExecStop=", stack_unit)
        self.assertIn("IPAddressDeny=any", secret_unit)
        self.assertIn("CAP_CHOWN CAP_MAC_ADMIN", secret_unit)
        self.assertIn("Requires=openbao-unseal.service", secret_unit)
        health = (PACKAGE / "bin" / "health.sh").read_text()
        self.assertEqual(health.count("secret-id-accessor"), 2)
        init_unit = (PACKAGE / "systemd" / "zulip-local-init.service").read_text()
        self.assertIn("After=docker.service zulip-local-secrets.service zulip-local.service", init_unit)

    def test_backup_is_streamed_to_age_and_restore_is_explicitly_gated(self) -> None:
        backup = (PACKAGE / "bin" / "backup.sh").read_text()
        restore = (PACKAGE / "bin" / "restore.sh").read_text()
        self.assertIn("tar czf -", backup)
        self.assertIn("--exclude=./zulip-secrets.conf", backup)
        self.assertIn("--exclude=./certs/manual", backup)
        self.assertIn("age --encrypt", backup)
        self.assertNotIn("backup.tar.gz\"", backup)
        self.assertIn("RESTORE-ZULIP-LOCAL", restore)
        self.assertIn("compose down --volumes", restore)
        self.assertLess(restore.index("validate-backup-stream.py"), restore.index("compose down --volumes"))
        self.assertLess(restore.index("--validate-password-file-only"), restore.index("compose down --volumes"))
        self.assertIn("--exclude=./zulip-secrets.conf", restore)
        self.assertIn('"$script_dir/provision-zulip.py" --reset-admin-password', restore)
        self.assertIn("--deployment-lock-fd 9", restore)
        self.assertNotIn("flock --unlock", restore)
        self.assertIn("systemctl stop zulip-approval-bridge.service", restore)
        self.assertIn("systemctl start zulip-approval-bridge.service", restore)
        self.assertIn("validate_root_managed_parent_chain", restore)
        self.assertIn("Restore requires the active systemd-managed Zulip stack.", restore)
        self.assertIn("cleanup_failed_restore", restore)
        self.assertIn("systemctl stop zulip-local.service", restore)

    def test_shell_and_python_sources_parse(self) -> None:
        for path in (PACKAGE / "bin").glob("*.sh"):
            completed = subprocess.run(
                ["bash", "-n", str(path)], capture_output=True, check=False
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
        for path in list((PACKAGE / "bin").glob("*.py")) + [MODULE_PATH]:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def _archive(self, names: list[tuple[str, bytes, str]]) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, content, kind in names:
                info = tarfile.TarInfo(name)
                if kind == "file":
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
                elif kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/etc/passwd"
                    archive.addfile(info)
                elif kind == "dir":
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
        return buffer.getvalue()

    def test_backup_stream_validator_accepts_reviewed_shape(self) -> None:
        archive = self._archive(
            [
                (".", b"", "dir"),
                ("./backups/backup-2026-09-05-000000.sql", b"PGDMP-test", "file"),
            ]
        )
        completed = subprocess.run(
            [sys.executable, str(PACKAGE / "bin" / "validate-backup-stream.py")],
            input=archive,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr.decode())

    def test_backup_stream_validator_rejects_traversal_and_links(self) -> None:
        for entries in (
            [("../escape", b"x", "file"), ("backups/backup-x.sql", b"x", "file")],
            [("backups/backup-x.sql", b"x", "file"), ("uploads/link", b"", "symlink")],
            [("backups/backup-x.sql", b"x", "file")],
            [
                ("./backups/backup-x.sql", b"x", "file"),
                ("./uploads/./avatar.png", b"image", "file"),
            ],
            [
                ("./backups/backup-x.sql", b"x", "file"),
                ("./zulip-secrets.conf", b"secret", "file"),
            ],
        ):
            completed = subprocess.run(
                [sys.executable, str(PACKAGE / "bin" / "validate-backup-stream.py")],
                input=self._archive(entries),
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)

    def test_backup_stream_validator_rejects_archive_without_dump(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(PACKAGE / "bin" / "validate-backup-stream.py")],
            input=self._archive([("uploads/avatar.png", b"image", "file")]),
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)

    def test_backup_stream_validator_rejects_fake_dump(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(PACKAGE / "bin" / "validate-backup-stream.py")],
            input=self._archive(
                [("./backups/backup-2026-09-05-000000.sql", b"not-a-dump", "file")]
            ),
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(completed.returncode, 0)


if __name__ == "__main__":
    unittest.main()
