"""Real, ephemeral OpenBao API checks; never connects to the workstation vault."""
import os
from pathlib import Path
import runpy
import secrets
import shutil
import socket
import subprocess
import time
import tempfile
from types import SimpleNamespace
import urllib.request
import pytest

ROOT = Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('script', ['deploy/zulip-local/bin/provision-openbao.py', 'scripts/provision-zulip-openbao'])
@pytest.mark.parametrize('field', ['secret_id_bound_cidrs', 'token_bound_cidrs'])
@pytest.mark.parametrize('scope', [['127.0.0.1'], ['127.0.0.1/32'], ['127.0.0.0/8'], ['0.0.0.0/0'], [], None, ['127.0.0.1','192.0.2.1']])
def test_only_exact_loopback_scope_is_accepted(script, field, scope):
    n = runpy.run_path(str(ROOT / script))
    local = script.startswith('deploy/')
    policy = 'zulip-local-runtime' if local else 'zulip-bridge'
    data = {'bind_secret_id': True, 'token_no_default_policy': True, 'secret_id_num_uses': 1024, 'token_num_uses': 2, 'token_period': 0, 'token_type': 'service', 'secret_id_ttl': 2592000, 'token_ttl': 120 if local else 60, 'token_max_ttl': 120, 'token_explicit_max_ttl': 120, 'token_policies': [policy], 'secret_id_bound_cidrs': ['127.0.0.1/32'], 'token_bound_cidrs': ['127.0.0.1/32']}
    data[field] = scope
    validate = (lambda: n['validate_role_document']({'data':data},policy,2)) if local else (lambda: n['_validate_role']({'data':data}))
    if scope in (['127.0.0.1'], ['127.0.0.1/32']):
        validate()
    else:
        with pytest.raises(n['ZulipLocalError' if local else 'ProvisioningError']): validate()

@pytest.mark.skipif(shutil.which('bao') is None, reason='Real OpenBao binary required')
def test_real_openbao_all_provisioner_roles_and_scoped_reads(monkeypatch, tmp_path):
    local = runpy.run_path(str(ROOT/'deploy/zulip-local/bin/provision-openbao.py'))
    sock = socket.socket(); sock.bind(('127.0.0.1',0)); port = sock.getsockname()[1]; sock.close()
    token = secrets.token_hex(32)
    env = dict(os.environ, BAO_DEV_ROOT_TOKEN_ID=token)
    process = subprocess.Popen([shutil.which('bao'), 'server', '-dev', f'-dev-listen-address=127.0.0.1:{port}'], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        origin = f'http://127.0.0.1:{port}'
        client = local['OpenBaoClient'](opener)
        monkeypatch.setitem(client.request.__globals__, 'OPENBAO_ORIGIN', origin)
        for _ in range(100):
            try: client.request('GET','/v1/sys/health'); break
            except local['ZulipLocalError']: time.sleep(.1)
        else: pytest.fail('Isolated OpenBao did not start')
        def write(path, payload):
            return client.request('POST',path,token=token,payload=payload,allowed_statuses=frozenset({200,204}))
        write('/v1/sys/auth/approle', {'type':'approle'})
        write('/v1/sys/mounts/kv-infra-shared', {'type':'kv','options':{'version':'2'}})
        monkeypatch.setitem(local['generate_tls'].__globals__, 'tempfile', SimpleNamespace(mkdtemp=lambda **kw: tempfile.mkdtemp(prefix=kw['prefix'], dir=tmp_path)))
        server = local['new_server_document']()
        client.write_kv(local['SERVER_KV_API_PATH'], server, token, cas=0)
        monkeypatch.setitem(local['issue_role'].__globals__, 'CREDSTORE', tmp_path)
        # The root audit additionally exercises host+TPM encryption. Ordinary
        # tests cover real API issuance/read/revoke without needing root/TPM.
        if os.geteuid() != 0:
            monkeypatch.setitem(local['issue_role'].__globals__, 'encrypt_credential', lambda name, value: b'synthetic-encryption-fixture')
        for name, spec in local['ROLE_SPECS'].items():
            local['install_policy'](client,token,name)
            encrypted, old = local['issue_role'](client,token,name,spec)
            assert len(encrypted)==3 and old is None
            if os.geteuid()==0:
                for name, blob in encrypted.items():
                    path=tmp_path/name;path.write_bytes(blob);path.chmod(0o600)
                    assert local['decrypt_systemd_credential'](path,name)
            print('REAL_LOCAL_ROLE_ISSUE_READ_REVOKE_PASSED',name)
        facade=secrets.token_hex(32)
        client.write_kv('/v1/kv-infra-shared/data/hermes/gateway',{'api_server_key':secrets.token_hex(32),'orchestrator_api_key':facade},token,cas=0)
        for short in ('orchestrator','hermes','zulip'):
            n=runpy.run_path(str(ROOT/f'scripts/provision-{short}-openbao'))
            monkeypatch.setitem(n['_request'].__globals__,'BAO_ORIGIN',origin)
            monkeypatch.setitem(n['_request'].__globals__,'_opener',lambda:opener)
            role=n['ROLE_NAME']
            if short=='zulip':
                policy=(ROOT/'config/openbao/policies/zulip-bridge.hcl').read_text()
                write('/v1/sys/policies/acl/'+role, {'policy':policy})
                payload=local['role_payload'](role,2);payload['token_ttl']='60s'
                write('/v1/auth/approle/role/'+role,payload)
                fixture=runpy.run_path(str(ROOT/'tests/test_zulip_openbao_launcher.py'))['valid_secret']()
                client.write_kv(n['KV_PATH'],fixture,token,cas=0)
            else:
                policy=(ROOT/f'config/openbao/policies/{n["POLICY_NAME"]}.hcl').read_text()
                n['_configure_runtime'](token,policy)
            _,doc=client.request('GET','/v1/auth/approle/role/'+role,token=token)
            n['_validate_role'](doc)
            _,rid=client.request('GET','/v1/auth/approle/role/'+role+'/role-id',token=token)
            _,sid=write('/v1/auth/approle/role/'+role+'/secret-id',{})
            args=(rid['data']['role_id'],sid['data']['secret_id'])
            if short=='orchestrator': n['_test_runtime'](*args,{},facade)
            elif short=='hermes': n['_test_runtime_credentials'](*args)
            else:
                launcher=SimpleNamespace(**runpy.run_path(str(ROOT/'scripts/zulip-openbao-launcher')))
                n['_test_runtime_credentials'](*args,launcher)
            print('REAL_ROLE_AUTH_READ_REVOKE_PASSED',role)
    finally:
        process.terminate();process.wait(timeout=10)


def test_generated_server_secrets_accept_urlsafe_leading_punctuation(monkeypatch):
    n=runpy.run_path(str(ROOT/'deploy/zulip-local/bin/provision-openbao.py'))
    g=n['new_server_document'].__globals__
    monkeypatch.setitem(g,'generate_tls',lambda:('fixture-ca','fixture-cert','fixture-key'))
    monkeypatch.setitem(g,'secrets',SimpleNamespace(token_hex=lambda size:'a'*(size*2),token_urlsafe=lambda size:'-_'+'x'*size))
    secret_validator=n['validate_server_secret'].__globals__['_secret_text']
    def validate(data):
        for name,value in data.items():
            if not name.startswith('tls_'):secret_validator(value)
        return data
    monkeypatch.setitem(g,'validate_server_secret',validate)
    result=n['new_server_document']()
    assert result['postgres_password'].startswith('A-_')
