import json,subprocess,urllib.error
from pathlib import Path
import pytest
from ops_native import memory_secrets as module

def test_missing_optional_reference_still_revokes_token(monkeypatch):
 monkeypatch.setattr(module,'protected',lambda *args:b'fixture-approle')
 calls=[]
 class Vault:
  def call(self,method,path,data=None,token=''):
   calls.append(path)
   if path=='auth/approle/login':return {'auth':{'client_token':'fixture-token'}}
   if method=='GET':raise urllib.error.HTTPError('https://127.0.0.1',404,'absent',{},None)
   return {}
 assert module.resolve(Vault(),Path('/synthetic')) is None
 assert calls[-1]=='auth/token/revoke-self'

def test_provider_value_is_not_returned_when_revocation_fails(monkeypatch):
 monkeypatch.setattr(module,'protected',lambda *args:b'fixture-approle')
 class Vault:
  def call(self,method,path,data=None,token=''):
   if path=='auth/approle/login':return {'auth':{'client_token':'fixture-token'}}
   if method=='GET':return {'data':{'data':{'api_key':'synthetic-provider-token'}}}
   raise RuntimeError('revocation failed')
 with pytest.raises(RuntimeError):module.resolve(Vault(),Path('/synthetic'))

def test_failed_second_encryption_preserves_both_prior_credentials(tmp_path,monkeypatch):
 store=tmp_path/'store';runtime=tmp_path/'runtime';store.mkdir();runtime.mkdir()
 for name in module.NAMES:(store/name).write_bytes(b'prior-ciphertext')
 monkeypatch.setattr(module,'protected',lambda path,*args:path.read_bytes())
 calls=[]
 def encrypt(args,**kw):
  calls.append(args)
  assert 'synthetic-key' not in ' '.join(args)
  assert kw['input']==b'synthetic-key'
  if len(calls)==2:raise subprocess.CalledProcessError(1,args)
  Path(args[-1]).write_bytes(b'new-ciphertext')
 monkeypatch.setattr(module.subprocess,'run',encrypt)
 with pytest.raises(subprocess.CalledProcessError):module.install('synthetic-key',store,runtime)
 assert all((store/name).read_bytes()==b'prior-ciphertext' for name in module.NAMES)
 assert not (runtime/'credential-digest').exists()

def test_unchanged_key_does_not_restart_or_reencrypt(tmp_path,monkeypatch):
 store=tmp_path/'store';runtime=tmp_path/'runtime';store.mkdir();runtime.mkdir()
 monkeypatch.setattr(module,'protected',lambda path,*args:path.read_bytes())
 calls=[]
 def encrypt(args,**kw):calls.append(args);Path(args[-1]).write_bytes(b'ciphertext')
 monkeypatch.setattr(module.subprocess,'run',encrypt)
 assert module.install('synthetic-key',store,runtime)
 assert not module.install('synthetic-key',store,runtime)
 assert len(calls)==2

def test_managed_absence_disables_preexisting_encrypted_keys(tmp_path):
 import runpy
 from ops_native.memory_capabilities import desired
 root=Path(__file__).parents[2]
 render=runpy.run_path(str(root/'native_ops/deploy/prepare-memory-api.py'))['render']
 text=render((root/'memory/config/memory.toml').read_text(),json.loads((root/'memory/config/providers-api.json').read_text()))
 for name in module.NAMES:(tmp_path/name).write_bytes(b'encrypted-fixture')
 enabled,_=desired(text,tmp_path,True)
 disabled,changed=desired(enabled,tmp_path,False)
 import tomllib
 assert changed
 assert not tomllib.loads(disabled)['embedding']['api']['enabled']
 assert not tomllib.loads(disabled)['reranker']['api']['enabled']
