import sqlite3
from ops_native.dispatch import select_backend
from ops_native.cli_pilot import source_for, command, clean_environment
from ops_native.state import State
import pytest

@pytest.mark.parametrize('content,mode,expected',[
 ('Observe','cli','codex'),('Observe','hybrid','api'),('!claude Observe','hybrid','claude'),
 ('!api Observe','cli','manual'),('!codex Observe','api','manual'),('Observe','invalid','manual')])
def test_backend_never_crosses_mode(content,mode,expected):
 assert select_backend(content,mode)==expected

def test_route_persists_and_old_state_migrates(tmp_path):
 p=tmp_path/'s.db';s=State(p);s.accept(1,'Ops','!codex hello');s.set(1,backend='codex');s.close()
 s=State(p);assert s.get(1)['backend']=='codex'
 s.close()

def test_source_requires_dispatch_and_no_prior_handoff():
 source={'kind':'observation','evidence':['zulip-source:42']}
 route={'kind':'observation','evidence':['zulip-dispatch:42:codex']}
 assert source_for([source],'codex') is None
 assert source_for([source,route],'claude') is None
 assert source_for([source,route],'codex')==42
 assert source_for([source,route,{'kind':'handoff','evidence':['zulip-response:42']}],'codex') is None
 assert source_for([source,route,{'kind':'observation','evidence':['zulip-dispatch:42:api']}],'codex') is None

def test_native_command_and_environment_are_bounded(monkeypatch):
 monkeypatch.setenv('OPENAI_API_KEY','synthetic-key');monkeypatch.setenv('ANTHROPIC_AUTH_TOKEN','synthetic-token')
 assert 'OPENAI_API_KEY' not in clean_environment() and 'ANTHROPIC_AUTH_TOKEN' not in clean_environment()
 a=command('codex');assert 'read-only' in a and 'features.shell_tool=false' in a
 assert 'forced_login_method="chatgpt"' in a and a[-1]=='-'
 assert '--permission-mode' in command('claude')
 with pytest.raises(ValueError):command('sh')

def test_existing_schema_preserves_jobs(tmp_path):
 p=tmp_path/'legacy.db'
 db=sqlite3.connect(p)
 db.executescript("CREATE TABLE jobs(source_id INTEGER PRIMARY KEY,topic TEXT,content TEXT,mission_id TEXT,phase TEXT,response TEXT,delivery_id INTEGER,error TEXT); INSERT INTO jobs VALUES(7,'ops','existing','mission','prepared',NULL,NULL,NULL);")
 db.close();s=State(p)
 assert s.get(7)['content']=='existing' and s.get(7)['backend'] is None
 s.set(7,backend='claude');assert s.get(7)['phase']=='prepared';s.close()

def test_project_is_explicit_and_persistent(tmp_path):
 from ops_native.dispatch import select_project
 assert select_project('[minecraft] Vérification')=='minecraft'
 assert select_project('Vérification')=='infra-shared'
 with pytest.raises(ValueError):select_project('[unknown] demande')
 s=State(tmp_path/'projects.db');s.accept(12,'[minecraft] test','Demande',project_id='minecraft')
 s.close();s=State(tmp_path/'projects.db');assert s.get(12)['project_id']=='minecraft';s.close()

def test_cli_approval_only_for_own_accepted_work_and_never_replayed(tmp_path):
 import asyncio,time
 from ops_native.cli_pilot import follow_approvals
 db=sqlite3.connect(tmp_path/'pilot.db')
 db.executescript('CREATE TABLE attempts(mission_id TEXT,tool TEXT,status TEXT,started REAL);CREATE TABLE approvals(action_id TEXT PRIMARY KEY,status TEXT);')
 db.execute('INSERT INTO attempts VALUES(?,?,?,?)',('mission','codex','handed_off',time.time()));db.commit()
 class Broker:
  calls=0
  async def call(self,name,**kw):
   if name=='list_actions_for_mission':return [
    {'id':'own','requested_by':'codex-supervised','status':'approved'},
    {'id':'other','requested_by':'claude-supervised','status':'approved'},
    {'id':'pending','requested_by':'codex-supervised','status':'pending_approval'}]
   if name=='get_action':return {'id':kw['action_id'],'mission_id':'mission',
    'requested_by':'codex-supervised' if kw['action_id']=='own' else 'claude-supervised','status':'approved'}
   assert kw['action_id']=='own';self.calls+=1;return {'status':'succeeded'}
 b=Broker();asyncio.run(follow_approvals(b,db,'codex'));asyncio.run(follow_approvals(b,db,'codex'))
 assert b.calls==1

def test_automatic_backend_waits_for_account_and_uses_connected_choice():
 assert select_backend('Observe','cli',[]) is None
 assert select_backend('Observe','cli',['claude'])=='claude'
 assert select_backend('Observe','cli',['claude','codex'])=='codex'
 assert select_backend('!claude Observe','cli',['codex'])=='claude'
 assert select_backend('Observe','hybrid',['codex'])=='api'

def test_cli_cycle_claims_matching_requests_once(tmp_path,monkeypatch):
 import asyncio
 import ops_native.cli_pilot as module
 db=sqlite3.connect(tmp_path/'attempts.db')
 db.executescript('CREATE TABLE attempts(source_id INTEGER PRIMARY KEY,mission_id,tool,started,status);CREATE TABLE approvals(action_id TEXT PRIMARY KEY,status);')
 calls=[]
 async def invoke(tool,mid,sid):calls.append((tool,mid,sid));return True
 monkeypatch.setattr(module,'invoke_cli',invoke)
 class Broker:
  async def call(self,name,**kw):
   if name=='list_open_missions':return [{'id':'m'}] if kw['project_id']=='infra-shared' else []
   if name=='list_actions_for_mission':return []
   if name=='list_mission_records':
    values=[{'kind':'observation','evidence':['zulip-source:123']},{'kind':'observation','evidence':['zulip-dispatch:123:claude']}]
    if calls:values.append({'kind':'handoff','evidence':['zulip-response:123']})
    return values
 b=Broker()
 asyncio.run(module.cycle('codex',db,b));assert not calls
 asyncio.run(module.cycle('claude',db,b));asyncio.run(module.cycle('claude',db,b))
 assert calls==[('claude','m',123)]
 assert db.execute('SELECT status FROM attempts').fetchone()[0]=='handed_off'

def test_missing_native_binary_does_not_block_other_account(monkeypatch):
 import asyncio
 import ops_native.cli_pilot as module
 class Process:
  returncode=0
  async def communicate(self):return b'{"loggedIn":true}',b''
 async def spawn(*args,**kwargs):
  if 'codex' in args[0]:raise FileNotFoundError()
  return Process()
 monkeypatch.setattr(module.asyncio,'create_subprocess_exec',spawn)
 assert asyncio.run(module.accounts())=={'codex':False,'claude':True}

def test_account_projection_refuses_untrusted_or_expired_file(tmp_path,monkeypatch):
 from ops_native.dispatch import available_cli
 from pathlib import Path
 from types import SimpleNamespace
 import time
 p=tmp_path/'accounts';p.write_text('{"codex":true,"claude":false}')
 metadata=dict(st_uid=0,st_mode=0o100644,st_mtime=time.time()-1,st_size=p.stat().st_size)
 monkeypatch.setattr(Path,'lstat',lambda self:SimpleNamespace(**metadata))
 assert available_cli(p)==('codex',)
 for field,value in [('st_uid',1000),('st_mode',0o100666),('st_mode',0o120777),('st_mtime',time.time()-181),('st_mtime',time.time()+100),('st_size',4097)]:
  old=metadata[field];metadata[field]=value
  assert available_cli(p)==()
  metadata[field]=old
