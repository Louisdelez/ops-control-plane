import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def configure(monkeypatch, payload):
    ns = runpy.run_path(str(ROOT/'deploy/control-plane/bin/control-plane-bootstrap'))
    env = ns['_decrypt_credential'].__globals__
    monkeypatch.setitem(env, '_validate_encrypted_file', lambda *a, **k: None)
    original = env['subprocess']
    fake = SimpleNamespace(**vars(original))
    fake.run = lambda *a, **k: SimpleNamespace(returncode=0, stdout=payload)
    monkeypatch.setitem(env, 'subprocess', fake)
    return ns


def test_document_reader_preserves_policy_whitespace(monkeypatch):
    document = {'schema_version': 1, 'policies': {'legacy': 'path "secret/*" {\n  capabilities = ["read"]\n}\n'}}
    payload = json.dumps(document).encode()
    ns = configure(monkeypatch, payload)
    assert ns['_read_encrypted_document'](Path('/unused'), 'undo') == document
    assert bytes(ns['_decrypt_credential']('undo', Path('/unused'), document=True)) == payload


@pytest.mark.parametrize('payload', [b'a' * 16 + b' b', b'a' * 16 + b'\nb'])
def test_recovery_shares_still_reject_embedded_whitespace(monkeypatch, payload):
    ns = configure(monkeypatch, payload)
    with pytest.raises(ns['BootstrapError']) as error:
        ns['_decrypt_credential']('share', Path('/unused'))
    assert error.value.code == 'credential_invalid'


def test_document_write_verification_reads_whitespace_and_publishes(tmp_path, monkeypatch):
    ns = configure(monkeypatch, b'not used here')
    env = ns['_write_encrypted_document'].__globals__
    final = tmp_path/'undo.cred'
    staging = tmp_path/'undo.staging'
    monkeypatch.setitem(env, '_escrow_staging_path', lambda p: staging)
    monkeypatch.setitem(env, '_reject_unexpected_escrow_staging', lambda: None)
    monkeypatch.setitem(env, '_deployment_pass_fds', lambda: ())
    stored = {}
    def run(command, **kwargs):
        if command[1] == 'encrypt':
            stored['payload'] = bytes(kwargs['input'])
            staging.write_bytes(b'fake ciphertext')
            return SimpleNamespace(returncode=0)
        assert command[1] == 'decrypt'
        return SimpleNamespace(returncode=0, stdout=stored['payload'])
    env['subprocess'].run = run
    doc = {'schema_version': 1, 'policies': {'role': 'path "secret/*" { capabilities = ["read"] }'}}
    ns['_write_encrypted_document'](final, 'undo', doc, create=True)
    assert final.exists() and not staging.exists()
    assert ns['_read_encrypted_document'](final, 'undo') == doc
