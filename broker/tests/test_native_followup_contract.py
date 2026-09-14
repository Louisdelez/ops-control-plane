"""Real broker projections and durable pilot state, isolated from installed services."""
import asyncio
import sqlite3
import time
from conftest import request_id
from ops_native.cli_pilot import follow_approvals
from ops_native.state import State
from ops_native.worker import Worker


def test_approval_roundtrip_uses_real_summary_and_observer_permissions(harness, tmp_path):
    service = harness.service
    service.rbac._actors['codex-supervised'] = service.rbac._actors['operator']
    mission = service.create_mission(actor_id='codex-supervised', request_id=request_id(),
                                     project_id='infra', title='isolated contract test')
    mid = mission['id']
    action = service.request_action(actor_id='codex-supervised', request_id=request_id(),
        runbook_id='test.change.v1', parameters={}, reason='isolated test', mission_id=mid)
    aid = action['id']
    db = sqlite3.connect(tmp_path/'pilot.sqlite3')
    db.executescript('CREATE TABLE attempts(mission_id,tool,status,started);'
                     'CREATE TABLE approvals(action_id PRIMARY KEY,status);')
    db.execute('INSERT INTO attempts VALUES(?,?,?,?)', (mid,'codex','handed_off',time.time()))
    db.commit()

    class PilotBroker:
        async def call(self, name, **kw):
            if name == 'list_actions_for_mission':
                return service.list_actions_for_mission('codex-supervised',kw['mission_id'])
            if name == 'get_action':
                return service.get_action('codex-supervised',kw['action_id'])
            assert name == 'execute_approved_action'
            return service.execute_action(actor_id='codex-supervised',action_id=kw['action_id'])

    asyncio.run(follow_approvals(PilotBroker(),db,'codex'))
    assert harness.executor.calls == []
    # Test-only approval in the fixture database; no live approval or publication.
    service.decide_approval(actor_id='zulip:42',request_id=request_id(),action_id=aid,
        decision='approve',source='zulip',source_event_id='isolated-fixture',reason='fixture')
    asyncio.run(follow_approvals(PilotBroker(),db,'codex'))
    asyncio.run(follow_approvals(PilotBroker(),db,'codex'))
    assert len(harness.executor.calls) == 1
    assert db.execute('SELECT status FROM approvals').fetchone()[0] == 'completed'

    state = State(tmp_path/'connector.sqlite3')
    state.accept(1,'test','test');state.set(1,mission_id=mid);state.watch_approval(1,aid)
    class Observer:
        async def call(self,name,**kw):
            assert name == 'list_actions_for_mission'
            # The observer has no permission for get_action on a class C action.
            return service.list_actions_for_mission('reader',kw['mission_id'])
    class Delivery:
        calls = 0
        def send(self,*args):
            self.calls += 1
            return 42
    delivery=Delivery()
    worker=Worker(state,Observer(),None,delivery,1,operations=object())
    asyncio.run(worker.approval_followups())
    asyncio.run(worker.approval_followups())
    assert delivery.calls == 1
    assert state.approval_jobs() == []
