import asyncio
import uuid
import pytest
from ops_native.mission_tools import MissionTools
MID=str(uuid.uuid4());AID=str(uuid.uuid4())
class Broker:
 def __init__(self):self.calls=[];self.project='infra-shared';self.action_mission=MID
 async def call(self,name,**kw):
  self.calls.append((name,kw))
  if name=='get_mission':return {'id':MID,'project_id':self.project}
  if name=='get_mission_state':return {'id':MID,'project_id':self.project,'current_step':'restore',
                                     'remaining_steps':['Reprendre la vérification']}
  if name=='list_authorized_runbooks':return [{'id':'health','project':'infra-shared'},{'id':'game','project':'minecraft'}]
  if name=='list_actions_for_mission':return []
  if name=='get_action':return {'id':AID,'mission_id':self.action_mission,'status':'pending_approval'}
  if name=='request_runbook_action':return {'id':AID,'status':'pending_approval'}
  if name=='execute_approved_action':raise RuntimeError('Approval absent')

def test_only_runbooks_for_current_project_are_exposed():
 b=Broker();r=asyncio.run(MissionTools(b,MID).context());assert [x['id'] for x in r['runbooks']]==['health']
 assert r['mission']['current_step']=='restore'
 assert r['mission']['remaining_steps']==['Reprendre la vérification']
 with pytest.raises(ValueError):asyncio.run(MissionTools(b,MID).request('game',{}))
 assert not any(n=='request_runbook_action' for n,_ in b.calls)

def test_request_is_durable_and_pending_is_not_success():
 b=Broker();t=MissionTools(b,MID)
 for _ in range(2):assert asyncio.run(t.request('health',{}))['status']=='pending_approval'
 requests=[kw for n,kw in b.calls if n=='request_runbook_action']
 assert requests[0]['request_id']==requests[1]['request_id']
 assert all(r['mission_id']==MID and 'actor_id' not in r for r in requests)

def test_cross_mission_action_is_never_executed():
 b=Broker();b.action_mission=str(uuid.uuid4())
 with pytest.raises(ValueError):asyncio.run(MissionTools(b,MID).action(AID,execute=True))
 assert not any(n=='execute_approved_action' for n,_ in b.calls)

def test_actual_broker_decision_cannot_be_overridden():
 b=Broker()
 with pytest.raises(RuntimeError,match='Approval absent'):asyncio.run(MissionTools(b,MID).action(AID,execute=True))
 assert b.calls[-1]==('execute_approved_action',{'action_id':AID})

def test_approved_followup_executes_once_and_reports(tmp_path,monkeypatch):
 from ops_native.state import State
 from ops_native.worker import Worker
 import ops_native.execution
 monkeypatch.setattr(ops_native.execution,'api_allowed',lambda:True)
 s=State(tmp_path/'state.db');s.accept(1,'ops','request');s.set(1,mission_id=MID);s.watch_approval(1,AID)
 class Ops:
  calls=0
  async def action(self,mid,aid,execute=False):
   assert mid==MID and aid==AID
   if execute:self.calls+=1
   return {'id':aid,'mission_id':MID,'requested_by':'hermes-native','status':'succeeded' if execute else 'approved'}
  async def call(self,name,**kw):
   assert name=='list_actions_for_mission' and kw['mission_id']==MID
   return [await self.action(MID,AID)]
 class Zulip:
  calls=0
  def send(self,*args):self.calls+=1;return 42
 ops=Ops();z=Zulip();w=Worker(s,ops,None,z,1,operations=ops)
 asyncio.run(w.approval_followups());asyncio.run(w.approval_followups())
 assert ops.calls==1 and z.calls==1 and not s.approval_jobs()

def test_pending_approval_and_cli_mode_never_execute(tmp_path,monkeypatch):
 from ops_native.state import State
 from ops_native.worker import Worker
 import ops_native.execution
 s=State(tmp_path/'state.db');s.accept(1,'ops','request');s.set(1,mission_id=MID);s.watch_approval(1,AID)
 class Ops:
  calls=0
  async def action(self,mid,aid,execute=False):
   assert not execute;self.calls+=1
   return {'id':AID,'mission_id':MID,'requested_by':'hermes-native','status':'pending_approval'}
  async def call(self,name,**kw):
   assert name=='list_actions_for_mission' and kw['mission_id']==MID
   return [await self.action(MID,AID)]
 ops=Ops();w=Worker(s,ops,None,None,1,operations=ops)
 monkeypatch.setattr(ops_native.execution,'api_allowed',lambda:False);asyncio.run(w.approval_followups());assert ops.calls==1
 monkeypatch.setattr(ops_native.execution,'api_allowed',lambda:True);asyncio.run(w.approval_followups());assert ops.calls==2
 assert s.approval_jobs()[0]['phase']=='waiting'

def test_interrupted_execution_is_not_replayed(tmp_path):
 from ops_native.state import State
 p=tmp_path/'s.db';s=State(p);s.accept(1,'ops','request');s.set(1,mission_id=MID);s.watch_approval(1,AID)
 s.set_approval(AID,phase='executing');s.close();s=State(p)
 assert s.approval_jobs()==[]
 assert s.db.execute('SELECT phase FROM approval_followups').fetchone()[0]=='needs_review'

def test_memory_scope_is_derived_from_mission(monkeypatch):
 import ops_memory.protocol
 calls=[]
 def request(path,body,timeout):
  calls.append(body);return {'ok':True,'result':{'results':[]}}
 monkeypatch.setattr(ops_memory.protocol,'socket_request',request)
 b=Broker();b.project='minecraft'
 result=asyncio.run(MissionTools(b,MID).memory('configuration du proxy'))
 assert result['memory']['results']==[]
 assert calls[0]['actor']=='hermes-coordinator'
 assert calls[0]['payload']['project']=='minecraft'
 assert calls[0]['payload']['max_classification']=='internal'
 b.project='unknown'
 with pytest.raises(ValueError):asyncio.run(MissionTools(b,MID).memory('query'))
 assert len(calls)==1


def test_mcp_read_annotations_do_not_grant_action_tools_read_only():
 from mcp.server import MCPServer
 from mcp import Client
 from ops_native.mission_tools import register
 server=MCPServer('scope-test');register(server)
 async def read():
  async with Client(server) as client:return (await client.list_tools()).tools
 listed=asyncio.run(read())
 readonly={t.name for t in listed if t.annotations and t.annotations.read_only_hint is True}
 assert readonly=={'get_mission_operations','search_mission_memory','get_mission_action'}
 assert {'request_mission_action','execute_approved_mission_action'} <= {t.name for t in listed}-readonly


def test_final_response_with_cli_diagnostic_remains_unambiguous():
 from ops_native.worker import parse_answer,GenerationFailed
 assert parse_answer('CLI diagnostic\n{"status":"done","message":"OK"}')['message']=='OK'
 assert parse_answer('CLI diagnostic\n```json\n{"status":"done","message":"OK"}\n```')['message']=='OK'
 for raw in ['{"status":"done","message":"OK"}\ntrailing',
             '{"status":"done","message":"first"}\n{"status":"done","message":"second"}',
             'x'*513+'\n{"status":"done","message":"OK"}']:
  with pytest.raises(GenerationFailed):parse_answer(raw)
