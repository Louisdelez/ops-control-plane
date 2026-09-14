"""Pagination, retry and rotation contract for the fixed remote log collector."""
import importlib.util
import json
from pathlib import Path
import pytest

@pytest.fixture(params=['stream-collector.py','stream-collector-v2.py'])
def stream(request):
 spec=importlib.util.spec_from_file_location('log_stream_test',Path(__file__).parents[1]/'logs'/request.param)
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 return module

def journal(count):
 return b'\n'.join(json.dumps({'__CURSOR':f's=abc;i={i:x}', '__REALTIME_TIMESTAMP':str((100+i)*1000000),'MESSAGE':f'event {i}','_SYSTEMD_UNIT':'fixture.service'}).encode() for i in range(count))

def test_journal_pagination_does_not_skip_lookahead(stream):
 calls=[]
 def read(args):calls.append(args);return journal(201)
 stream.command=read
 result=stream.collect_stream({'action':'stream','source':'system','since':100})
 assert len(result['records'])==200 and result['more']
 assert result['cursor']['journal']=='s=abc;i=c7'
 assert '--lines=+201' in calls[0]
 stream.command=lambda args:journal(1)
 stream.collect_stream({'action':'stream','source':'system','since':100,'cursor':result['cursor']})

def test_retry_uses_stable_ids_and_reports_missing_cursor(stream):
 stream.command=lambda args:journal(2)
 p={'action':'stream','source':'system','since':100}
 first=stream.collect_stream(p);second=stream.collect_stream(p)
 assert first['records']==second['records']
 def rotated(args):
  if any(a.startswith('--after-cursor=') for a in args):raise ValueError('vacuumed')
  return journal(2)
 stream.command=rotated
 assert stream.collect_stream({**p,'cursor':first['cursor']})['gap']

def docker_fixture(stream,tmp_path,monkeypatch):
 identifier='a'*64;path=tmp_path/(identifier+'-json.log')
 def line(n):return json.dumps({'time':f'2026-09-14T10:00:{n:02d}Z','log':f'event {n}\n','stream':'stdout'}).encode()+b'\n'
 path.write_bytes(line(1)+line(2))
 stream.command=lambda args:json.dumps(['/fixture',str(path),'json-file']).encode()
 # Production requires root ownership; local fixtures use the current test uid.
 original=stream.os.fstat
 def stat(fd):
  value=original(fd)
  from types import SimpleNamespace
  return SimpleNamespace(st_mode=value.st_mode,st_ino=value.st_ino,st_uid=0,st_size=value.st_size)
 monkeypatch.setattr(stream.os,'fstat',stat)
 return identifier,path,line

def test_docker_restarts_and_uncompressed_rotation(stream,tmp_path,monkeypatch):
 identifier,path,line=docker_fixture(stream,tmp_path,monkeypatch)
 p={'action':'stream','source':'docker','service':identifier}
 first=stream.collect_stream(p)
 assert len(first['records'])==2
 assert not stream.collect_stream({**p,'cursor':first['cursor']})['records']
 with path.open('ab') as f:f.write(line(3))
 path.rename(path.with_suffix('.log.1'));path.write_bytes(line(4))
 remaining=stream.collect_stream({**p,'cursor':first['cursor']})
 assert [r['message'] for r in remaining['records']]==['event 3']
 assert remaining['more'] and not remaining['gap']
 new=stream.collect_stream({**p,'cursor':remaining['cursor']})
 assert [r['message'] for r in new['records']]==['event 4']

def test_partial_docker_line_does_not_advance_cursor(stream,tmp_path,monkeypatch):
 identifier,path,line=docker_fixture(stream,tmp_path,monkeypatch)
 first=stream.collect_stream({'action':'stream','source':'docker','service':identifier})
 with path.open('ab') as f:f.write(line(3)[:-1])
 next=stream.collect_stream({'action':'stream','source':'docker','service':identifier,'cursor':first['cursor']})
 assert not next['records'] and next['cursor']==first['cursor']
 with path.open('ab') as f:f.write(b'\n')
 final=stream.collect_stream({'action':'stream','source':'docker','service':identifier,'cursor':next['cursor']})
 assert len(final['records'])==1

@pytest.mark.parametrize('changes',[{'command':'id'},{'source':'/etc/shadow'},{'source':'docker','service':'--help'},{'cursor':{'journal':'$(id)'}},{'cursor':'bad'},{'since':-1}])
def test_rejects_untrusted_command_path_and_cursor(stream,changes):
 with pytest.raises(ValueError):stream.collect_stream({'action':'stream','source':'system','since':100,**changes})

def test_unavailable_docker_is_not_reported_as_empty_healthy_inventory(stream):
 def fail(args):raise ValueError('offline')
 stream.command=fail
 result=stream.collect_stream({'action':'sources'})
 assert result['available'] is False

@pytest.fixture
def store(tmp_path):
 spec=importlib.util.spec_from_file_location('stream_store_test',Path(__file__).parents[1]/'logs/stream-service.py')
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);module.STATE=tmp_path
 return module

def batch():
 return {'schema_version':1,'redacted':True,'records':[{'id':'a'*64,'t':150,'source':'system','service':'fixture.service','level':'info','message':'fixture'}],'cursor':{'journal':'s=a;i=1','last_t':150},'gap':False,'more':False}

def test_committed_cursor_and_logs_survive_process_reopen(store):
 value=batch();store.commit('dell-control','system','',{},100,value)
 assert store.position('dell-control','system','')==(value['cursor'],100)
 with store.database() as db:assert db.execute('SELECT count(*) FROM entries').fetchone()[0]==1
 # An uncertain delivery replay must not create a duplicate message.
 store.commit('dell-control','system','',value['cursor'],100,value)
 with store.database() as db:assert db.execute('SELECT count(*) FROM entries').fetchone()[0]==1

def test_invalid_batch_or_stale_writer_advances_nothing(store):
 value=batch();value['records'].append({**value['records'][0],'message':'x'*1025})
 with pytest.raises(ValueError):store.commit('dell-control','system','',{},100,value)
 assert store.position('dell-control','system','')[0]=={}
 with store.database() as db:assert db.execute('SELECT count(*) FROM entries').fetchone()[0]==0
 value=batch();store.commit('dell-control','system','',{},100,value)
 with pytest.raises(ValueError):store.commit('dell-control','system','',{},100,{**value,'cursor':{'journal':'s=a;i=2'}})
 assert store.position('dell-control','system','')[0]==value['cursor']

def test_gap_remains_durable_after_later_success(store):
 value=batch();store.commit('dell-control','system','',{},100,{**value,'gap':True})
 store.commit('dell-control','system','',value['cursor'],100,value)
 with store.database() as db:assert db.execute('SELECT count(*) FROM stream_gaps').fetchone()[0]==1

def test_remote_extension_sudo_grant_matches_every_sealed_runbook_invocation():
 import yaml
 root=Path(__file__).parents[2]
 book=root/'runbooks/local/enable-logstream.yaml'
 if not book.exists():book=book.with_suffix('.yaml.example')
 value=yaml.safe_load(book.read_text())
 grants=(root/'broker/deploy/sudoers/ops-broker-enable-logstream').read_text().splitlines()
 for resource in value['parameters']['resource']['values']:
  for bundle in value['parameters']['bundle_sha256']['values']:
   command=' '.join(x.format(resource=resource,bundle_sha256=bundle) for x in value['command']['argv'][2:])
   assert 'opsbroker ALL=(root) NOPASSWD: '+command in grants

def test_retired_source_keeps_archive_cursor_and_records_unfinished_backlog(store):
 value=batch();value['records'][0].update(source='docker',service='fixture')
 store.commit('dell-control','docker','container-id',{},100,{**value,'more':True})
 store.retire_removed_sources('dell-control',set())
 store.retire_removed_sources('dell-control',set())
 with store.database() as db:
  assert db.execute('SELECT count(*) FROM entries').fetchone()[0]==1
  assert db.execute('SELECT count(*) FROM stream_cursors').fetchone()[0]==1
  assert db.execute('SELECT status FROM stream_health').fetchone()[0]=='retired'
  assert db.execute('SELECT count(*) FROM stream_gaps').fetchone()[0]==1
 store.commit('dell-control','docker','container-id',value['cursor'],100,value)
 with store.database() as db:assert db.execute('SELECT status FROM stream_health').fetchone()[0]=='collecting'
