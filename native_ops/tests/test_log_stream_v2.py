import importlib.util,json
from pathlib import Path
from test_log_stream import docker_fixture

def load():
 spec=importlib.util.spec_from_file_location('log_stream_v2_test',Path(__file__).parents[1]/'logs/stream-collector-v2.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def test_large_valid_line_no_longer_blocks_following_messages(tmp_path,monkeypatch):
 m=load();identifier,path,line=docker_fixture(m,tmp_path,monkeypatch)
 large=json.dumps({'time':'2026-09-14T10:00:00Z','log':'synthetic event '*10000,'stream':'stdout'}).encode()+b'\n'
 path.write_bytes(large+line(2))
 result=m.collect_stream({'action':'stream','source':'docker','service':identifier})
 assert len(result['records'])==2 and result['records'][1]['message']=='event 2'
 assert not result['gap'] and not result['more'] and len(result['records'][0]['message'])<=1024

def test_oversize_and_malformed_lines_are_explicit_and_do_not_block(tmp_path,monkeypatch):
 m=load();identifier,path,line=docker_fixture(m,tmp_path,monkeypatch)
 path.write_bytes(b'x'*(3*1024*1024)+b'\nnot-json\n'+line(2))
 result=m.collect_stream({'action':'stream','source':'docker','service':identifier})
 assert result['gap'] and not result['more']
 assert [r['level'] for r in result['records']]==['warning','warning','info']
 assert result['records'][-1]['message']=='event 2' and not result['cursor']['discarding']

def test_huge_line_continues_bounded_discard_after_restart(tmp_path,monkeypatch):
 m=load();identifier,path,line=docker_fixture(m,tmp_path,monkeypatch)
 path.write_bytes(b'x'*(12*1024*1024)+b'\n'+line(2))
 p={'action':'stream','source':'docker','service':identifier};first=m.collect_stream(p)
 assert first['more'] and first['cursor']['discarding'] and len(first['records'])==1
 final=m.collect_stream(dict(p,cursor=first['cursor']))
 assert not final['more'] and final['records'][-1]['message']=='event 2'
