import runpy
from pathlib import Path
import pytest
LOGS=runpy.run_path(str(Path(__file__).parents[1]/'logs/collector.py'))
def test_redaction_hides_labelled_credentials_bearer_and_url_password():
 s=LOGS['redact']('password="private sample" api_key=abc123 Authorization: Bearer abc.def.ghi https://admin:private@host/\n')
 assert 'private' not in s and 'abc123' not in s and 'abc.def.ghi' not in s and '[MASQUE]' in s

def test_log_parameters_never_accept_commands_or_paths():
 base={'source':'system','service':'nginx.service','since':100,'until':200,'limit':300}
 assert LOGS['validate'](base)==base
 for changes in [{'service':'--output=export'},{'service':'../../etc/passwd'},{'service':'nginx.service;id'},{'limit':9999},{'until':1000000},{'command':'id'}]:
  with pytest.raises(ValueError):LOGS['validate']({**base,**changes})

def test_pem_and_long_tokens_are_masked_and_messages_bounded():
 s=LOGS['redact']('-----BEGIN PRIVATE KEY-----\nprivate material\n-----END PRIVATE KEY-----')
 assert 'private material' not in s
 assert len(LOGS['redact']('ordinary text '*1000))<=1024

def test_archive_keeps_manager_messages_associated_with_requested_service(tmp_path, monkeypatch):
 import importlib.util
 collector=Path(__file__).parents[1]/'logs/collector.py'
 original=Path.read_text
 def read(path,*a,**kw):
  return original(collector if str(path)=='/usr/local/libexec/ops-logs/collector.py' else path,*a,**kw)
 monkeypatch.setattr(Path,'read_text',read)
 spec=importlib.util.spec_from_file_location('logs_service_test',collector.with_name('service.py'))
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 module.STATE=tmp_path
 row={'id':'fixture-manager-event','t':150,'source':'system','service':'init.scope','level':'info','message':'Started fixture service'}
 module.fetch=lambda host,request:{'records':[row],'redacted':True,'truncated':False}
 query={'host':'dell-control','source':'system','service':'fixture.service','since':100,'until':200}
 module.handle({**query,'action':'read'})
 # The archive must work without fetching from the machine again.
 def offline(*args):raise AssertionError('Archive attempted a live read')
 module.fetch=offline
 result=module.handle({**query,'action':'archive'})
 assert result['records']==[row]
 assert not module.handle({**query,'action':'archive','service':'unrelated.service'})['records']

def test_archive_paginates_identical_timestamps_without_loss(tmp_path, monkeypatch):
 import importlib.util,sqlite3
 collector=Path(__file__).parents[1]/'logs/collector.py';original=Path.read_text
 monkeypatch.setattr(Path,'read_text',lambda path,*a,**kw:original(collector if str(path)=='/usr/local/libexec/ops-logs/collector.py' else path,*a,**kw))
 spec=importlib.util.spec_from_file_location('paginated_logs',collector.with_name('service.py'));module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);module.STATE=tmp_path
 query={'host':'dell-control','action':'archive','since':100,'until':200}
 module.handle(dict(query))
 with sqlite3.connect(tmp_path/'logs.sqlite3') as db:
  db.executemany('INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)',[('dell-control',f'{i:064x}',150,'system','fixture.service','info','fixture',151) for i in range(601)])
 seen=[];cursor=None
 for expected in [300,300,1]:
  result=module.handle({**query,**({'before':cursor} if cursor else {})});assert len(result['records'])==expected
  seen.extend(r['id'] for r in result['records']);cursor=result['next_cursor']
 assert len(set(seen))==601 and cursor is None

def test_transport_failover_is_readonly_and_only_for_network_errors(monkeypatch):
 import importlib.util
 collector=Path(__file__).parents[1]/'logs/collector.py';original=Path.read_text
 monkeypatch.setattr(Path,'read_text',lambda path,*a,**kw:original(collector if str(path)=='/usr/local/libexec/ops-logs/collector.py' else path,*a,**kw))
 spec=importlib.util.spec_from_file_location('logs_failover',collector.with_name('service.py'));module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 attempts=[]
 def route(host,request,stream=False,alternate=False):
  attempts.append((host,request,stream,alternate))
  if not alternate:raise ConnectionError('Network unreachable')
  return {'ok':True}
 module.fetch_route=route;p={'action':'stream','source':'system','cursor':{'journal':'s=a;i=1'}}
 assert module.fetch('prod',p,True)=={'ok':True}
 assert attempts==[('prod',p,True,False),('prod',p,True,True)]
 attempts.clear()
 with pytest.raises(ValueError):module.fetch('nas',p,True)
 assert len(attempts)==1
 def refused(*a,**kw):raise ValueError('Authentication failed')
 module.fetch_route=refused
 with pytest.raises(ValueError,match='Authentication'):module.fetch('prod',p,True)

def test_v2_reader_requires_successful_approval_for_the_exact_host(tmp_path,monkeypatch):
 import sqlite3,json,importlib.util
 collector=Path(__file__).parents[1]/'logs/collector.py';original=Path.read_text
 monkeypatch.setattr(Path,'read_text',lambda path,*a,**kw:original(collector if str(path)=='/usr/local/libexec/ops-logs/collector.py' else path,*a,**kw))
 spec=importlib.util.spec_from_file_location('logs_versions',collector.with_name('service.py'));module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 module.BASE=tmp_path
 (tmp_path/'stream-collector-v2.py').write_text('fixed fixture')
 module.BROKER_DATABASE=tmp_path/'broker.db'
 with sqlite3.connect(module.BROKER_DATABASE) as db:
  db.execute('CREATE TABLE actions (parameters_json TEXT,runbook_id TEXT,requested_by TEXT,status TEXT)')
  db.execute('INSERT INTO actions VALUES(?,?,?,?)',(json.dumps({'resource':'prod','bundle_sha256':module.STREAM_V2_BUNDLE}),'security.enable-logstream.v1','codex-supervised','pending_approval'))
 assert module.stream_collector('dell-control')=='stream-collector-v2.py'
 assert module.stream_collector('prod')=='stream-collector.py'
 with sqlite3.connect(module.BROKER_DATABASE) as db:db.execute("UPDATE actions SET status='succeeded'")
 assert module.stream_collector('prod')=='stream-collector-v2.py'
 assert module.stream_collector('nas')=='stream-collector.py'
