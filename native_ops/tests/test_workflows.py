import asyncio
import json
import sqlite3
import uuid
import pytest
from ops_native.workflows import Workflow, DEFINITIONS, outcome

MID=str(uuid.uuid4());RID=str(uuid.uuid4())

class Broker:
    def __init__(self):
        self.actions={};self.records={};self.fail_record_once=False;self.unavailable=set()
    async def call(self,name,**kw):
        if name=='get_mission':return {'id':MID,'project_id':'infra-shared','status':'open'}
        if name=='list_authorized_runbooks':return [
            {'id':x,'project':'infra-shared','action_class':'A'}
            for x in {s['runbook'] for steps in DEFINITIONS.values() for s in steps}]
        if name=='request_runbook_action':
            if kw['request_id'] not in self.actions:
                observed='unavailable' if kw['parameters'].get('resource') in self.unavailable else 'observed'
                self.actions[kw['request_id']]={'id':str(uuid.uuid4()),'status':'succeeded',
                    'mission_id':MID,'runbook_id':kw['runbook_id'],'parameters':kw['parameters'],
                    'result':{'return_code':0,'timed_out':False,'truncated':False,
                              'stdout':json.dumps({'status':observed,'systemd_state':'running'})}}
            return self.actions[kw['request_id']]
        if name=='get_action':return next(a for a in self.actions.values() if a['id']==kw['action_id'])
        if name=='add_mission_record':
            if self.fail_record_once and self.actions:
                self.fail_record_once=False;raise ConnectionError('interruption')
            self.records.setdefault(kw['request_id'],kw)
            return {'id':kw['request_id']}
        raise AssertionError(name)


def test_resume_after_broker_completion_does_not_repeat_effect(tmp_path):
    path=tmp_path/'state.sqlite3';b=Broker();b.fail_record_once=True
    with sqlite3.connect(path) as db:
        with pytest.raises(ConnectionError):asyncio.run(Workflow(db,b).run('local-readiness',RID,MID))
    assert len(b.actions)==1
    with sqlite3.connect(path) as db:
        result=asyncio.run(Workflow(db,b).run('local-readiness',RID,MID))
        assert result['status']=='succeeded'
        assert asyncio.run(Workflow(db,b).run('local-readiness',RID,MID))==result
    assert len(b.actions)==2
    assert len(b.records)==3


def test_unavailable_target_is_not_reported_as_success(tmp_path):
    b=Broker();b.unavailable={'prod'}
    with sqlite3.connect(tmp_path/'state.sqlite3') as db:
        result=asyncio.run(Workflow(db,b).run('infrastructure-readiness',RID,MID))
    assert result['status']=='needs_review'
    assert {s['step_id']:s['phase'] for s in result['steps']}=={
        'control':'succeeded','edge-vps':'succeeded','nas':'succeeded','prod':'needs_review'}


def test_different_definition_cannot_reuse_run_id(tmp_path):
    b=Broker()
    with sqlite3.connect(tmp_path/'state.sqlite3') as db:
        workflow=Workflow(db,b)
        asyncio.run(workflow.run('local-readiness',RID,MID))
        with pytest.raises(ValueError,match='cannot be changed'):
            asyncio.run(workflow.run('infrastructure-readiness',RID,MID))


@pytest.mark.parametrize('result',[
    {'return_code':1}, {'return_code':0,'truncated':True},
    {'return_code':0,'timed_out':True},
    {'return_code':0,'stdout':'invalid'},
    {'return_code':0,'stdout':'[]'},
    {'return_code':0,'stdout':'{"status":"unavailable"}'},
])
def test_incomplete_or_negative_observations_fail_closed(result):
    assert outcome({'status':'succeeded','result':result},{'expect':{'status':'observed'}})=='needs_review'


def test_dependencies_stop_but_no_action_is_undone(tmp_path):
    class Failing(Broker):
        async def call(self,name,**kw):
            result=await super().call(name,**kw)
            if name=='request_runbook_action':result['result']['return_code']=1
            return result
    b=Failing()
    with sqlite3.connect(tmp_path/'state.sqlite3') as db:
        result=asyncio.run(Workflow(db,b).run('local-readiness',RID,MID))
    assert len(b.actions)==1
    assert [s['phase'] for s in result['steps']]==['needs_review','pending']
