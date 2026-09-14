"""Prepare Qwen's OpenBao service identity without any provider key or call."""
import json
import os
from pathlib import Path
import runpy
import urllib.error

ROOT = Path('/home/ops-user/ops-control-plane')


def prepare():
    assert os.geteuid() == 0
    library = runpy.run_path(str(ROOT/'native_ops/deploy/provider-secrets.py'))
    store = Path('/etc/credstore.encrypted')
    names = ['ops-native-api-role-id', 'ops-native-api-secret-id']
    if all((store/name).is_file() and not (store/name).is_symlink() for name in names):
        return {'identity': 'already_prepared', 'provider_calls': False}
    if any((store/name).exists() for name in names):
        raise RuntimeError('Partial service credentials require reconciliation')
    prepared = Path('/var/lib/atlas-local-repair/credentials.json')
    st = prepared.lstat()
    assert not prepared.is_symlink() and st.st_uid == 0 and not st.st_mode & 0o077
    credentials = json.loads(prepared.read_bytes())
    vault = library['Vault']()
    login = vault.call('POST', 'auth/userpass/login/ops-user', {'password': credentials['openbao_louis']})
    credentials.clear()
    vault.token = login['auth']['client_token']
    login.clear()
    try:
        for path in ['sys/policies/acl/ops-native-api', 'auth/approle/role/ops-native-api']:
            try:
                vault.call('GET', path)
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
            else:
                raise RuntimeError('Existing service identity requires reconciliation')
        vault.call('PUT', 'sys/policies/acl/ops-native-api', {
            'policy': 'path "kv-infra-shared/data/llm/qwen" { capabilities = ["read"] }'})
        vault.call('POST', 'auth/approle/role/ops-native-api', {
            'token_policies': ['ops-native-api'], 'token_period': '15m',
            'secret_id_ttl': 0, 'secret_id_num_uses': 0,
            'token_bound_cidrs': ['127.0.0.1/32'], 'secret_id_bound_cidrs': ['127.0.0.1/32']})
        role = vault.call('GET', 'auth/approle/role/ops-native-api/role-id')['data']['role_id']
        secret = vault.call('POST', 'auth/approle/role/ops-native-api/secret-id', {})['data']['secret_id']
        library['seal'](names[0], role)
        library['seal'](names[1], secret)
        role = secret = ''
        return {'identity': 'prepared', 'provider_calls': False}
    finally:
        vault.call('POST', 'auth/token/revoke-self', {})
        vault.token = ''


if __name__ == '__main__':
    try:
        print(json.dumps(prepare()))
    except Exception:
        raise SystemExit('Qwen identity preparation failed; no provider was contacted') from None
