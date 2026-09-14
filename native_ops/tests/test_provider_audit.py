import importlib.machinery,importlib.util,ssl
from pathlib import Path
import pytest
ROOT=Path(__file__).parents[2]
def load():
 loader=importlib.machinery.SourceFileLoader('provider_audit_test',str(ROOT/'broker/deploy/helpers/provider-access'));spec=importlib.util.spec_from_loader(loader.name,loader);m=importlib.util.module_from_spec(spec);loader.exec_module(m);return m

def test_provider_client_refuses_redirects_and_path_origin_injection():
 m=load()
 with pytest.raises(ValueError):m.NoRedirect().redirect_request(None,None,None,None,None,None)
 client=m.Client('https://api.deepseek.com','/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem')
 for path in ['https://attacker.invalid','/\nAuthorization: injected','/https://attacker.invalid']:
  with pytest.raises(ValueError):client.call(path)

def test_explicit_trust_does_not_use_keylog_or_ca_environment(tmp_path,monkeypatch):
 m=load();path=tmp_path/'keylog';monkeypatch.setenv('SSLKEYLOGFILE',str(path));monkeypatch.setenv('SSL_CERT_FILE',str(tmp_path/'absent'))
 ctx=m.context('/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem')
 assert ctx.verify_mode==ssl.CERT_REQUIRED and ctx.check_hostname and ctx.keylog_filename is None
 assert not path.exists()

def test_provider_reference_scope_is_exact():
 m=load()
 assert set(m.REFS)=={'ovh-key','ovh-secret','ovh-consumer','infomaniak','deepseek'}
 assert all('*' not in path for path in m.REFS.values())
