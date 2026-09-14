import asyncio,json,uuid
from contextlib import asynccontextmanager
from ops_native import tools

def test_health_never_reuses_another_actor_or_old_observation(monkeypatch):
    requests=[]
    class Broker:
        async def call(self,name,**kw):
            if name=='get_mission':return {'project_id':'infra-shared'}
            if name=='list_authorized_runbooks':return [{'id':'local.health.v1','action_class':'A'}]
            assert name=='request_runbook_action';requests.append(kw)
            return {'id':kw['request_id'],'status':'succeeded','result':{'stdout':json.dumps({'sample':len(requests)})}}
    @asynccontextmanager
    async def connect(command,args,actor):
        assert actor=='native-observer';yield Broker()
    monkeypatch.setattr(tools,'connect',connect)
    async def run():
        a=await tools.health('11111111-1111-4111-8111-111111111111','fixture',[],'native-observer')
        b=await tools.health('11111111-1111-4111-8111-111111111111','fixture',[],'native-observer')
        assert a['observation']['sample']==1 and b['observation']['sample']==2
    asyncio.run(run())
    assert len({r['request_id'] for r in requests})==2
    assert all(uuid.UUID(r['request_id']).version==4 for r in requests)
