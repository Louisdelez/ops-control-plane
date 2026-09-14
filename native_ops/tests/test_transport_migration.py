"""Fixed transport identities and rollback-safe publication, without remote execution."""
import importlib.util
import importlib.machinery
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
ROOT=Path(__file__).parents[2]
def module(path):
 loader=importlib.machinery.SourceFileLoader('transport_fixture',str(path));spec=importlib.util.spec_from_loader(loader.name,loader);m=importlib.util.module_from_spec(spec);loader.exec_module(m);return m

def test_administration_uses_only_vault_runtime_and_fixed_ssh_policy(monkeypatch):
 m=module(ROOT/'native_ops/deploy/transport-migration/admin-transport.py')
 monkeypatch.setattr(m.os,'geteuid',lambda:0)
 monkeypatch.setattr(m.subprocess,'run',lambda *a,**kw:SimpleNamespace(returncode=0))
 monkeypatch.setattr(m.pwd,'getpwnam',lambda _:SimpleNamespace(pw_uid=1234))
 monkeypatch.setattr(m.Path,'lstat',lambda _:SimpleNamespace(st_mode=0o100600,st_uid=1234))
 args=m.command('prod',True)
 assert args[2]=='opsremoteadmin' and '/etc/ops-remote-admin/ssh_config' in args
 assert '/home/ops-user' not in ' '.join(args) and 'StrictHostKeyChecking=yes' in args
 assert '198.51.100.4' not in args and 'HostName=198.51.100.4' in args
 with pytest.raises(ValueError):m.command('unreviewed-host')
 with pytest.raises(ValueError):m.command('nas',True)

def test_preflight_uses_observer_command_and_only_fixed_status_input(monkeypatch):
 m=module(ROOT/'native_ops/deploy/transport-migration/preflight-worker.py');calls=[]
 monkeypatch.setattr(m,'observer_command',lambda *a:['fixture-observer'])
 class Process:
  returncode=0
  def __init__(self,args,**kw):assert args==['fixture-observer'];self.output=kw['stdout']
  def communicate(self,input,timeout):calls.append(json.loads(input));self.output.write(b'{"schema_version":1,"systemd_state":"running"}')
  def poll(self):return 0
 monkeypatch.setattr(m.subprocess,'Popen',Process)
 assert m.observe_route('prod')=={'status':'observed','systemd_state':'running'}
 assert calls==[{'action':'status'}]

def test_atomic_publication_refuses_symlink_destination(tmp_path):
 m=module(ROOT/'broker/deploy/helpers/migrate-observer-transports')
 target=tmp_path/'current';original=tmp_path/'original';original.write_bytes(b'preserve');target.symlink_to(original)
 with pytest.raises(ValueError):m.replace(target,b'new',0o600)
 assert original.read_bytes()==b'preserve'
