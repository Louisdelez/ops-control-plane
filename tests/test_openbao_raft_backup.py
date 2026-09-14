from __future__ import annotations

import importlib.machinery
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = ROOT / "openbao" / "backup" / "openbao_raft_backup.py"
PROVISIONER = ROOT / "scripts" / "provision-openbao-backup"


def load_source(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class OpenBaoProvisionerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_source("provision_openbao_backup", PROVISIONER)
        self.safe_role = {
            "data": {
                "token_policies": ["openbao-backup-runtime"],
                "bind_secret_id": True,
                "token_no_default_policy": True,
                "secret_id_num_uses": 1024,
                "token_num_uses": 2,
                "token_period": 0,
                "token_type": "service",
                "secret_id_bound_cidrs": ["127.0.0.1/32"],
                "token_bound_cidrs": ["127.0.0.1/32"],
                "secret_id_ttl": 2_592_000,
                "token_ttl": 300,
                "token_max_ttl": 300,
                "token_explicit_max_ttl": 300,
            }
        }

    def test_exact_two_use_role_is_accepted(self):
        self.module._validate_role(self.safe_role)

    def test_extra_use_or_policy_is_rejected(self):
        self.safe_role["data"]["token_num_uses"] = 3
        with self.assertRaises(self.module.ProvisioningError):
            self.module._validate_role(self.safe_role)
        self.safe_role["data"]["token_num_uses"] = 2
        self.safe_role["data"]["token_policies"].append("default")
        with self.assertRaises(self.module.ProvisioningError):
            self.module._validate_role(self.safe_role)

    def test_broader_cidr_is_rejected(self):
        self.safe_role["data"]["token_bound_cidrs"] = ["127.0.0.0/8"]
        with self.assertRaises(self.module.ProvisioningError):
            self.module._validate_role(self.safe_role)

    def test_policy_parser_accepts_only_snapshot_and_revoke(self):
        with tempfile.TemporaryDirectory() as temporary:
            policy = Path(temporary) / "policy.hcl"
            policy.write_text(
                'path "sys/storage/raft/snapshot" { capabilities = ["read"] }\n'
                'path "auth/token/revoke-self" { capabilities = ["update"] }\n',
                encoding="utf-8",
            )
            self.module.POLICY_FILE = policy
            self.module._validate_root_regular = lambda *_args, **_kwargs: None
            self.module._read_policy()
            policy.write_text(policy.read_text() + 'path "sys/storage/raft/snapshot-force" { capabilities = ["update"] }\n')
            with self.assertRaises(self.module.ProvisioningError):
                self.module._read_policy()


class OpenBaoBackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_source("openbao_raft_backup", BACKUP_SCRIPT)

    def test_age_artifact_is_inspected_without_private_identity(self):
        recipient = (ROOT / "config" / "openbao" / "recovery.age-recipient").read_text().strip()
        with tempfile.TemporaryDirectory() as temporary:
            artifact = Path(temporary) / "snapshot.age"
            completed = subprocess.run(
                ["/usr/bin/age", "--recipient", recipient, "--output", str(artifact)],
                input=b"bounded test snapshot contents\n" * 16,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode())
            os.chmod(artifact, 0o600)
            self.assertEqual(self.module._inspect_age(artifact), artifact.stat().st_size)

    def test_retention_removes_only_old_complete_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            names = [
                "openbao-raft-20260901T010101.000001Z",
                "openbao-raft-20260902T010101.000001Z",
                "openbao-raft-20260903T010101.000001Z",
            ]
            for name in names:
                folder = destination / name
                folder.mkdir(mode=0o700)
                for child in ("snapshot.age", "snapshot.age.sha256", "manifest.json"):
                    (folder / child).write_bytes(b"complete")
                    os.chmod(folder / child, 0o600)
            self.assertEqual(self.module._retention(destination, 2), 1)
            self.assertFalse((destination / names[0]).exists())
            self.assertTrue((destination / names[1]).exists())
            self.assertTrue((destination / names[2]).exists())

    def test_revocation_failure_prevents_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            self.module._safe_destination = lambda _path: None
            self.module._recipient = lambda _path: "age1" + "q" * 58
            self.module._opener = lambda _path: object()
            self.module._credential = lambda name: "credential-identifier-" + name
            self.module._login = lambda *_args: "token-runtime-identifier-12345"

            def stream(_opener, _token, _recipient, output, _maximum, _timeout):
                output.write_bytes(b"age-encryption.org/v1\n" + b"x" * 256)
                os.chmod(output, 0o600)
                return 128

            self.module._stream_snapshot = stream
            self.module._json_request = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                self.module.BackupError("revoke failed")
            )
            config = {
                "destination": destination,
                "recipient_file": Path("/unused"),
                "ca_file": Path("/unused"),
                "role_name": "openbao-backup-role-id",
                "secret_name": "openbao-backup-secret-id",
                "timeout": 10.0,
                "max_snapshot_bytes": 4096,
                "retention_count": 2,
            }
            with self.assertRaises(self.module.BackupError):
                self.module.create_backup(config)
            self.assertEqual(list(destination.iterdir()), [])


class OpenBaoBackupAssetTests(unittest.TestCase):
    def test_runtime_policy_is_exact_and_restore_is_absent(self):
        policy = (ROOT / "config" / "openbao" / "policies" / "openbao-backup-runtime.hcl").read_text()
        self.assertEqual(policy.count('path "'), 2)
        self.assertIn('path "sys/storage/raft/snapshot"', policy)
        self.assertIn('path "auth/token/revoke-self"', policy)
        self.assertNotIn("snapshot-force", policy)
        self.assertNotIn("restore", policy.lower())

    def test_unit_uses_dedicated_identity_and_encrypted_credentials(self):
        unit = (ROOT / "systemd" / "openbao-raft-backup.service").read_text()
        self.assertIn("User=openbao-backup", unit)
        self.assertIn("Group=openbao-backup", unit)
        self.assertEqual(unit.count("LoadCredentialEncrypted="), 2)
        self.assertIn("OnSuccess=openbao-raft-backup-metric-success.service", unit)
        self.assertIn("OnFailure=openbao-raft-backup-metric-failure.service", unit)
        self.assertIn("IPAddressDeny=any", unit)
        self.assertIn("IPAddressAllow=127.0.0.1", unit)

        for name in ("success", "failure"):
            metric_unit = (
                ROOT / "systemd" / f"openbao-raft-backup-metric-{name}.service"
            ).read_text()
            self.assertIn("CapabilityBoundingSet=CAP_DAC_READ_SEARCH", metric_unit)
            self.assertIn("InaccessiblePaths=-/etc/credstore.encrypted", metric_unit)

    def test_runtime_accepts_only_root_owned_systemd_credentials(self):
        source = BACKUP_SCRIPT.read_text()
        credential_source = source.split("def _credential", 1)[1].split("def _opener", 1)[0]
        self.assertIn("directory_info.st_uid != 0", credential_source)
        self.assertIn("info.st_uid != 0", credential_source)
        self.assertNotIn("os.geteuid()", credential_source)

    def test_provisioner_uses_host_tpm2_and_never_force_restore(self):
        source = PROVISIONER.read_text()
        self.assertIn('"--with-key=host+tpm2"', source)
        self.assertIn('"token_num_uses": 2', source)
        self.assertIn('"/v1/sys/storage/raft/snapshot"', source)
        self.assertIn('"/v1/auth/token/revoke-self"', source)
        self.assertNotIn("snapshot restore", source)
        self.assertNotIn("-force", source)
        self.assertIn('not 1 <= auth["lease_duration"] <= 300', source)

    def test_alerts_cover_failure_and_staleness(self):
        alerts = (ROOT / "monitoring" / "alerts.yaml").read_text()
        self.assertEqual(alerts.count("alert: DellOpsOpenBaoBackupVerifyFailed"), 1)
        self.assertEqual(alerts.count("alert: DellOpsOpenBaoBackupStale"), 1)
        self.assertIn("ops_openbao_backup_last_verify_ok == 0", alerts)
        self.assertIn("ops_openbao_backup_last_success_timestamp_seconds > 108000", alerts)

    def test_installer_rejects_managed_symlinks_and_uses_local_admin_dirs(self):
        installer = (ROOT / "scripts" / "install-openbao-backup.sh").read_text()
        self.assertIn("reject_symlink", installer)
        self.assertIn("/etc/sysusers.d/openbao-backup.conf", installer)
        self.assertIn("/etc/tmpfiles.d/openbao-backup.conf", installer)
        self.assertNotIn("/usr/lib/sysusers.d/openbao-backup.conf", installer)
        self.assertNotIn("/usr/lib/tmpfiles.d/openbao-backup.conf", installer)


if __name__ == "__main__":
    unittest.main()
