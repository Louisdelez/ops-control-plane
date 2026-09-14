from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "provision-memory-openbao"


def _load():
    loader = importlib.machinery.SourceFileLoader("provision_memory_openbao", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _role(token_cidr: str) -> dict[str, object]:
    return {
        "data": {
            "token_policies": ["ops-memory-runtime"],
            "bind_secret_id": True,
            "token_no_default_policy": True,
            "secret_id_num_uses": 1024,
            "token_num_uses": 2,
            "token_period": 0,
            "token_type": "service",
            "secret_id_ttl": 2_592_000,
            "token_ttl": 60,
            "token_max_ttl": 120,
            "token_explicit_max_ttl": 120,
            "secret_id_bound_cidrs": ["127.0.0.1/32"],
            "token_bound_cidrs": [token_cidr],
        }
    }


def test_role_accepts_openbao_canonical_loopback_host() -> None:
    module = _load()
    module._validate_role(_role("127.0.0.1"))


def test_role_rejects_a_broader_loopback_network() -> None:
    module = _load()
    with pytest.raises(module.ProvisioningError, match="token_bound_cidrs"):
        module._validate_role(_role("127.0.0.0/8"))
