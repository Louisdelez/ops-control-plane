import asyncio
from types import SimpleNamespace
import pytest
from ops_native.state import State
from ops_native.worker import Worker, GenerationFailed
from ops_native.broker import Broker, BrokerError

class FakeBroker:
    def __init__(self, verified=True):self.calls=[];self.verified=verified
    async def call(self,name,**kw):
        self.calls.append((name,kw))
        if name=='create_mission':return {'id':'3ac02e32-fc06-4003-b3cd-28f4a66f08dd'}
        if name=='list_actions_for_mission':return [{'id':'health-action','runbook_id':'local.health.v1','status':'succeeded'}] if self.verified else []
        return {}
class FakeHermes:
    def __init__(self):self.calls=0
    async def explain(self,*args):self.calls+=1;return '{"status":"done","message":"OpenBao répond."}'
class FakeZulip:
    def __init__(self,fail=False):self.calls=0;self.fail=fail
    def send(self,*args):
        self.calls+=1
        if self.fail:raise TimeoutError()
        return 123

def test_duplicate_input_and_recovery(tmp_path):
    p=tmp_path/'state.db';s=State(p)
    s.accept(1,'pilot','Vérifie OpenBao');s.accept(1,'pilot','different')
    assert len(s.pending())==1 and s.get(1)['content']=='Vérifie OpenBao'
    s.set(1,phase='generating');s.close()
    s=State(p)
    assert s.get(1)['phase']=='needs_review' and s.pending()==[]

def test_observation_and_delivery_are_recorded(tmp_path):
    s=State(tmp_path/'s.db');s.accept(1,'pilot','État du poste ?')
    b,h,z=FakeBroker(),FakeHermes(),FakeZulip()
    asyncio.run(Worker(s,b,h,z,1).process(s.get(1)))
    assert s.get(1)['phase']=='delivered'
    assert z.calls==1 and h.calls==1
    assert any(n=='add_mission_record' for n,k in b.calls)
    assert not any(n=='set_mission_status' for n,k in b.calls)

def test_missing_real_observation_cannot_be_published(tmp_path):
    s=State(tmp_path/'s.db');s.accept(1,'pilot','État ?')
    z=FakeZulip()
    asyncio.run(Worker(s,FakeBroker(False),FakeHermes(),z,1).process(s.get(1)))
    assert s.get(1)['phase']=='needs_review' and z.calls==0

def test_ambiguous_post_is_not_replayed_or_regenerated(tmp_path):
    s=State(tmp_path/'s.db');s.accept(1,'pilot','État ?')
    b,h,z=FakeBroker(),FakeHermes(),FakeZulip(True);w=Worker(s,b,h,z,1)
    with pytest.raises(TimeoutError):asyncio.run(w.process(s.get(1)))
    assert s.get(1)['phase']=='sending'
    asyncio.run(w.process(s.get(1)))
    assert z.calls==1 and h.calls==1 and s.get(1)['phase']=='needs_review'

def test_actor_cannot_be_injected():
    with pytest.raises(BrokerError):asyncio.run(Broker(None,'native-observer').call('get_mission',actor_id='codex-supervised'))

def test_private_state_refuses_symlink(tmp_path):
    target=tmp_path/'original';target.write_text('preserve')
    link=tmp_path/'s.db';link.symlink_to(target)
    with pytest.raises(OSError):State(link)
    assert target.read_text()=='preserve'

def test_realistic_systemd_credential_modes(tmp_path):
    from ops_native.cli import private_json
    p=tmp_path/'credential';p.write_text('{"key":"synthetic"}')
    for mode in [0o400,0o440,0o600]:
        p.chmod(mode)
        assert private_json(p)=={'key':'synthetic'}
    p.chmod(0o640)
    with pytest.raises(ValueError):private_json(p)

def test_installer_preserves_requested_mode_under_private_umask(tmp_path):
    import os,runpy,stat
    from pathlib import Path
    installer=runpy.run_path(str(Path(__file__).resolve().parents[1]/'deploy/install.py'))
    previous=os.umask(0o077)
    try:
        target=tmp_path/'script'
        installer['write'](target,'test',0o755)
        assert stat.S_IMODE(target.stat().st_mode)==0o755
    finally:os.umask(previous)

@pytest.mark.parametrize('final_status,phase,published',[('done','delivered',1),('needs_reasoning','needs_review',0)])
def test_reasoning_escalates_once_for_same_mission(tmp_path,final_status,phase,published):
    import json
    class Escalating:
        def __init__(self):self.calls=[]
        async def explain(self,mid,content,reasoning='standard'):
            self.calls.append((mid,reasoning))
            return json.dumps({'status':'needs_reasoning' if reasoning=='standard' else final_status,'message':'Analyse.'})
    s=State(tmp_path/'s.db');s.accept(1,'pilot','État ?')
    h,z=Escalating(),FakeZulip()
    asyncio.run(Worker(s,FakeBroker(),h,z,1).process(s.get(1)))
    assert s.get(1)['phase']==phase and z.calls==published
    assert [level for mid,level in h.calls]==['standard','advanced']
    assert len({mid for mid,level in h.calls})==1

@pytest.mark.parametrize('raw',['Réponse sans JSON','{"status":"done","message":""}','{"status":"retry","message":"x"}'])
def test_invalid_model_answer_is_rejected(raw):
    from ops_native.worker import parse_answer
    with pytest.raises(GenerationFailed):parse_answer(raw)

@pytest.mark.parametrize('records,expected',[
    ([], 'prepared'),
    ([{'kind':'analysis','content':'No implicit publication.'}], 'prepared'),
    ([{'kind':'handoff','content':'Contrôle vérifié.'}], 'ready'),
    ([{'kind':'handoff','content':'A'},{'kind':'handoff','content':'B'}], 'needs_review'),
])
def test_explicit_supervised_handoff(tmp_path,records,expected):
    mid='3ac02e32-fc06-4003-b3cd-28f4a66f08dd'
    class HandoffBroker(FakeBroker):
        async def call(self,name,**kw):
            if name=='list_mission_records':
                return [dict(r,mission_id=mid,evidence=['zulip-response:1']) for r in records]
            return await super().call(name,**kw)
    s=State(tmp_path/'s.db');s.accept(1,'pilot','État ?');s.set(1,mission_id=mid,phase='prepared')
    h,z=FakeHermes(),FakeZulip();w=Worker(s,HandoffBroker(),h,z,1)
    asyncio.run(w.accept_handoff(s.get(1)))
    assert s.get(1)['phase']==expected and h.calls==0
    if expected=='ready':
        asyncio.run(w.process(s.get(1)))
        assert s.get(1)['phase']=='delivered' and z.calls==1 and h.calls==0
        asyncio.run(w.process(s.get(1)))
        assert z.calls==1

@pytest.mark.parametrize('field,value',[('mission_id','wrong'),('evidence',['zulip-response:2'])])
def test_handoff_cannot_cross_source_or_mission(tmp_path,field,value):
    mid='3ac02e32-fc06-4003-b3cd-28f4a66f08dd'
    class B(FakeBroker):
        async def call(self,name,**kw):
            return [dict({'mission_id':mid,'kind':'handoff','content':'x','evidence':['zulip-response:1']},**{field:value})]
    s=State(tmp_path/'s.db');s.accept(1,'pilot','État ?');s.set(1,mission_id=mid,phase='prepared')
    w=Worker(s,B(),FakeHermes(),FakeZulip(),1)
    assert asyncio.run(w.accept_handoff(s.get(1))) is False
    assert s.get(1)['phase']=='prepared'
