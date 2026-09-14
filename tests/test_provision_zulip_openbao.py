from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "provision-zulip-openbao"


def load_script():
    loader = importlib.machinery.SourceFileLoader("provision_zulip_openbao", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ProvisionZulipOpenBaoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_script()
        self.safe_role = {
            "data": {
                "token_policies": ["zulip-bridge"],
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

    def test_added_policy_is_rejected(self):
        self.safe_role["data"]["token_policies"].append("default")
        with self.assertRaises(self.module.ProvisioningError):
            self.module._validate_role(self.safe_role)

    def test_unbounded_secret_id_uses_are_rejected(self):
        self.safe_role["data"]["secret_id_num_uses"] = 0
        with self.assertRaises(self.module.ProvisioningError):
            self.module._validate_role(self.safe_role)

    def test_non_thirty_day_secret_id_is_rejected(self):
        self.safe_role["data"]["secret_id_ttl"] = 0
        with self.assertRaises(self.module.ProvisioningError):
            self.module._validate_role(self.safe_role)

    def test_periodic_or_extra_use_token_is_rejected(self):
        self.safe_role["data"]["token_period"] = 60
        with self.assertRaises(self.module.ProvisioningError):
            self.module._validate_role(self.safe_role)
        self.safe_role["data"]["token_period"] = 0
        self.safe_role["data"]["token_num_uses"] = 3
        with self.assertRaises(self.module.ProvisioningError):
            self.module._validate_role(self.safe_role)


if __name__ == "__main__":
    unittest.main()
