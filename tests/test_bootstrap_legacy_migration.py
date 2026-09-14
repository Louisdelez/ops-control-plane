from copy import deepcopy
from pathlib import Path
import runpy

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_legacy_migration_and_rollback_preserve_existing_secret_ids(tmp_path, monkeypatch):
    ns = runpy.run_path(str(ROOT / 'deploy/control-plane/bin/control-plane-bootstrap'))
    env = ns['_snapshot_openbao_undo'].__globals__
    roles = {'deepseek-client': {'token_policies': ['deepseek-client'], 'token_no_default_policy': False, 'bind_secret_id': True, 'local_secret_ids': False}}
    original = deepcopy(roles)
    accessors = {'deepseek-client': ['a' * 32]}
    policies = {'deepseek-client': 'original provider policy'}
    original_policies = deepcopy(policies)
    calls = []
    encrypted = {}
    escrow = tmp_path / 'undo'
    monkeypatch.setitem(env, 'OPENBAO_UNDO_ESCROW', escrow)
    monkeypatch.setitem(env, '_root_token_from_escrow', lambda: 'synthetic-token')
    monkeypatch.setitem(env, '_set_transaction', lambda tx, **kw: tx.update(kw))
    monkeypatch.setitem(env, '_fsync_directory', lambda *a: None)
    monkeypatch.setitem(env, '_read_root_file', lambda p, *a, **k: p.read_bytes())
    def write(p, purpose, doc, **kw):
        encrypted['undo'] = deepcopy(doc)
    monkeypatch.setitem(env, '_write_encrypted_document', write)
    monkeypatch.setitem(env, '_read_encrypted_document', lambda *a: deepcopy(encrypted['undo']))
    def request(method, path, **kw):
        calls.append((method, path))
        if path.startswith('/v1/auth/approle/role/'):
            name = path.split('/')[5]
            if path.endswith('/secret-id'):
                keys = accessors.get(name, [])
                return (200, {'data': {'keys': keys}}) if keys else (404, None)
            if method == 'GET':
                return (200, {'data': deepcopy(roles[name])}) if name in roles else (404, None)
            if method == 'POST':
                assert 'local_secret_ids' not in kw['payload']
                roles[name].update(deepcopy(kw['payload'])); return 204, None
            if method == 'DELETE':
                roles.pop(name, None); accessors.pop(name, None); return 204, None
        if path.startswith('/v1/sys/policies/acl/'):
            name = path.rsplit('/', 1)[1]
            if method == 'GET':
                return (200, {'data': {'policy': policies[name]}}) if name in policies else (404, None)
            if method in ('PUT', 'POST'):
                policies[name] = kw['payload']['policy']; return 204, None
            if method == 'DELETE':
                policies.pop(name, None); return 204, None
        if path.startswith('/v1/kv-infra-shared/'):
            assert method in ('GET', 'DELETE')
            return 404, None
        raise AssertionError((method, path))
    monkeypatch.setitem(env, 'openbao_request', request)
    tx = {'release_id': 'test'}
    ns['_snapshot_openbao_undo'](tx)
    assert encrypted['undo']['roles']['deepseek-client']['accessors'] == ['a' * 32]
    tx['openbao_provisioning_started'] = True
    ns['_quarantine_legacy_openbao_roles'](tx, ROOT)
    assert roles['deepseek-client']['token_no_default_policy'] is True
    assert accessors['deepseek-client'] == ['a' * 32]
    ns['_record_openbao_poststate'](tx)
    ns['_undo_openbao_state'](tx)
    assert roles == original
    assert policies == original_policies
    assert accessors['deepseek-client'] == ['a' * 32]
    assert ('DELETE', '/v1/auth/approle/role/deepseek-client') not in calls
    assert tx['openbao_undo_complete'] is True


@pytest.mark.parametrize('safe', [False, True])
def test_hermes_preservation_refuses_unquarantined_roles(monkeypatch, safe):
    ns = runpy.run_path(str(ROOT / 'scripts/provision-hermes-openbao'))
    run = ns['_quarantine_legacy_access']
    calls = []
    def request(method, path, **kw):
        calls.append((method, path))
        if method == 'GET':
            return {'data': {'token_policies': [path.rsplit('/', 1)[1]], 'token_no_default_policy': safe}}
        return None
    monkeypatch.setitem(run.__globals__, '_request', request)
    rules = {name: ns['_EXPECTED_QUARANTINE_POLICY'] for name in ns['LEGACY_NAMES']}
    if safe:
        run('synthetic-token', rules, preserve_roles=True)
    else:
        with pytest.raises(ns['ProvisioningError'], match='not quarantined'):
            run('synthetic-token', rules, preserve_roles=True)
    assert all(method != 'DELETE' for method, _ in calls)


def test_legacy_migration_requires_durable_snapshot(monkeypatch):
    ns = runpy.run_path(str(ROOT / 'deploy/control-plane/bin/control-plane-bootstrap'))
    run = ns['_quarantine_legacy_openbao_roles']
    def unexpected(*a, **kw):
        raise AssertionError('request before snapshot')
    monkeypatch.setitem(run.__globals__, 'openbao_request', unexpected)
    with pytest.raises(ns['BootstrapError'], match='durable undo'):
        run({}, ROOT)

@pytest.mark.parametrize('status,current', [(404,None),(200,{'data':{'local_secret_ids':True}})])
def test_update_refuses_missing_role_or_immutable_drift(monkeypatch,status,current):
    ns=runpy.run_path(str(ROOT/'deploy/control-plane/bin/control-plane-bootstrap'))
    run=ns['_update_existing_approle'];calls=[]
    def request(method,path,**kw):
        calls.append(method)
        return status,current
    monkeypatch.setitem(run.__globals__,'openbao_request',request)
    with pytest.raises(ns['BootstrapError']):
        run('synthetic-token','/v1/auth/approle/role/deepseek-client',{'local_secret_ids':False})
    assert calls==['GET']


def test_role_normalization_is_limited_to_equivalent_empty_lists():
    ns=runpy.run_path(str(ROOT/'deploy/control-plane/bin/control-plane-bootstrap'))
    normalize=ns['_approle_configuration']
    assert normalize({'data':{'local_secret_ids':False,'secret_id_bound_cidrs':None}})=={'local_secret_ids':False,'secret_id_bound_cidrs':[]}
    assert normalize({'data':{'secret_id_bound_cidrs':['127.0.0.1/32']}})=={'secret_id_bound_cidrs':['127.0.0.1/32']}
