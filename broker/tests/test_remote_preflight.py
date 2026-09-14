from pathlib import Path
import json,os,runpy,time
import pytest
ROOT=Path(__file__).parents[2]
def module(name):return runpy.run_path(str(ROOT/'broker/deploy/helpers'/name))

def admin_command(monkeypatch):
 from types import SimpleNamespace
 command=module('remote-admin-transport.py')['command']
 monkeypatch.setattr(os,'geteuid',lambda:0)
 monkeypatch.setattr(Path,'lstat',lambda _:SimpleNamespace(st_mode=0o100600,st_uid=1234))
 command.__globals__['pwd']=SimpleNamespace(getpwnam=lambda _:SimpleNamespace(pw_uid=1234))
 command.__globals__['subprocess']=SimpleNamespace(run=lambda *args,**kwargs:None,DEVNULL=-3)
 return command

def test_native_ssh_is_fixed_and_noninteractive(monkeypatch):
 command=admin_command(monkeypatch)
 for resource,alias in [('edge-vps','vps'),('prod','prod'),('nas','nas'),('gamebox','gamebox')]:
  args=command(resource)
  assert args[-2:]==[alias,'/usr/bin/true']
  assert args[:4]==['/usr/sbin/runuser','-u','opsremoteadmin','--']
  assert 'StrictHostKeyChecking=yes' in args and 'BatchMode=yes' in args
  assert 'PermitLocalCommand=no' in args and 'ClearAllForwardings=yes' in args
 with pytest.raises(ValueError):command('prod; reboot')

def test_remote_output_is_never_returned_verbatim(monkeypatch):
 from types import SimpleNamespace
 fn=module('remote-preflight-worker')['observe']
 state={'text':b'{"systemd_state":"running"}','code':0}
 class Process:
  def __init__(self,args,**kwargs):
   assert args==['fixed-observer'];kwargs['stdout'].write(state['text']);self.returncode=state['code']
  def communicate(self,input,timeout):assert json.loads(input)=={'action':'status'}
  def wait(self,timeout=None):return state['code']
  def poll(self):return state['code']
 fn.__globals__['observer_command']=lambda *args:['fixed-observer']
 fn.__globals__['subprocess']=SimpleNamespace(Popen=Process,DEVNULL=-3,PIPE=-1,TimeoutExpired=TimeoutError)
 assert fn('prod')=={'status':'observed','systemd_state':'running'}
 for value in ['degraded','stopping']:
  state.update(text=json.dumps({'systemd_state':value}).encode(),code=1);assert fn('prod')['status']=='observed'
 for text,code in [(b'{"systemd_state":"running"}',255),(b'private arbitrary output',0),(b'{"systemd_state":"invented"}',0),(bytes([255]),1)]:
  state.update(text=text,code=code)
  assert fn('prod')=={'status':'unavailable','reason':'ssh_or_remote_check_failed'}

def test_report_must_be_root_owned_fresh_and_schema_bounded(tmp_path,monkeypatch):
 from types import SimpleNamespace
 fn=module('remote-preflight')['validated'];path=tmp_path/'result';started=time.time()-1
 payload={'resource':'prod','checked_at':time.time(),'status':'observed','systemd_state':'running'}
 path.write_text(json.dumps(payload))
 real=os.fstat
 def metadata(fd):
  st=real(fd);return SimpleNamespace(st_mode=st.st_mode,st_size=st.st_size,st_uid=0)
 monkeypatch.setattr(os,'fstat',metadata)
 assert fn(path,'prod',started)==payload
 for change in [{'resource':'nas'},{'checked_at':started-1},{'checked_at':time.time()+100},{'systemd_state':'invented'},{'extra':'discard me'}]:
  path.write_text(json.dumps(payload|change))
  with pytest.raises(ValueError):fn(path,'prod',started)
 path.write_text(json.dumps(payload));path.chmod(0o666)
 with pytest.raises(ValueError):fn(path,'prod',started)
 path.chmod(0o644);link=tmp_path/'link';link.symlink_to(path)
 with pytest.raises(OSError):fn(link,'prod',started)

def test_broker_only_renders_the_four_resources():
 from ops_broker.runbooks import ExecutablePolicy,RunbookRegistry
 registry=RunbookRegistry.load(ROOT/'runbooks',ExecutablePolicy.load(ROOT/'broker/config/executables.yaml'))
 item=registry.get('inventory.remote-preflight.v1')
 assert item.action_class=='A' and item.project=='infra-shared'
 assert item.render_argv(item.validate_parameters({'resource':'nas'}))[-1]=='nas'
 from ops_broker.errors import BrokerError
 with pytest.raises(BrokerError):item.validate_parameters({'resource':'unknown'})


def test_ssh_errors_are_classified_without_disclosing_their_text():
 classify=module('remote-preflight-worker')['failure_reason']
 for raw,expected in [(b'Host key verification failed.','host_key_unverified'),(b'Permission denied (publickey).','authentication_failed'),(b'No route to host','network_unreachable'),(b'Connection refused','connection_refused'),(b'Connection timed out','timeout'),(b'Could not resolve hostname example','name_resolution_failed'),(b'arbitrary remote data','ssh_or_remote_check_failed')]:
  assert classify(raw)==expected


def test_only_reviewed_gamebox_network_failure_uses_existing_alternate(monkeypatch):
 values=module('remote-preflight-worker');fn=values['observe'];calls=[]
 def route(resource,alternate=False):
  calls.append((resource,alternate))
  return {'status':'observed','systemd_state':'running'} if alternate else {'status':'unavailable','reason':'network_unreachable'}
 fn.__globals__['observe_route']=route
 assert fn('gamebox')['status']=='observed'
 assert calls==[('gamebox',False),('gamebox',True)]
 calls.clear();assert fn('prod')['status']=='observed';assert calls==[('prod',False),('prod',True)]
 command=admin_command(monkeypatch)
 assert command('gamebox',True)[-2]=='gamebox-remote'
 args=command('prod',True)
 assert 'HostName=198.51.100.4' in args and 'HostKeyAlias=192.0.2.65' in args
 assert args[args.index('-J')+1]=='vps'
 assert 'StrictHostKeyChecking=yes' in args and args[-2]=='prod'
 with pytest.raises(ValueError):command('nas',True)
 calls.clear()
 fn.__globals__['observe_route']=lambda *args:{'status':'unavailable','reason':'host_key_unverified'}
 assert fn('gamebox')['reason']=='host_key_unverified'


def test_alternate_failure_preserves_both_diagnostics():
 fn=module('remote-preflight-worker')['observe']
 def route(resource,alternate=False):
  return {'status':'unavailable','reason':'host_key_unverified' if alternate else 'network_unreachable'}
 fn.__globals__['observe_route']=route
 assert fn('gamebox')=={'status':'unavailable','reason':'network_unreachable','alternate_reason':'host_key_unverified'}

def test_location_only_matches_preexisting_native_public_host_key(monkeypatch):
 from types import SimpleNamespace
 fn=module('locate-prod-worker')['candidate'];calls=[]
 class Connection:
  def __enter__(self):return self
  def __exit__(self,*args):pass
 fn.__globals__['socket']=SimpleNamespace(create_connection=lambda address,timeout:Connection())
 def run(args,**kwargs):
  calls.append(args);return SimpleNamespace(stdout=b'192.0.2.90 ssh-ed25519 known-public-key\n')
 fn.__globals__['subprocess']=SimpleNamespace(run=run,DEVNULL=-3,PIPE=-1,TimeoutExpired=TimeoutError)
 assert fn('192.0.2.90',{b'known-public-key'})=='192.0.2.90'
 assert fn('192.0.2.90',{b'different-key'}) is None
 assert calls[0]==['/usr/bin/ssh-keyscan','-T','1','-t','ed25519','192.0.2.90']

def test_location_refuses_unknown_trust_and_unattached_lan():
 from types import SimpleNamespace
 fn=module('locate-prod-worker')['scan'];calls=[];attached=False
 def run(args,**kwargs):
  calls.append(args)
  if args[0].endswith('/ip'):return SimpleNamespace(stdout=b'[{"dst":"192.0.2.0/24","dev":"wifi"}]' if attached else b'[]')
  return SimpleNamespace(stdout=b'')
 fn.__globals__['subprocess']=SimpleNamespace(run=run,DEVNULL=-3)
 assert fn()['reason']=='lan_not_attached'
 attached=True
 assert fn()['reason']=='known_ed25519_key_absent'
 assert not any('ssh-keyscan' in args[0] for args in calls)
