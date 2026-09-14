import asyncio
import importlib.util
from pathlib import Path
import sqlite3

spec = importlib.util.spec_from_file_location('memory_sync', Path(__file__).parents[1]/'memory_sync.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)
MID = '11111111-1111-4111-8111-111111111111'
RID = '75aea141-2759-47f4-86e8-380fa2780918'


def test_outputs_and_raw_messages_never_collected():
    action = {'id': RID, 'status': 'succeeded', 'result': {'stdout': 'private output'}, 'parameters': {'hidden': 'value'}}
    p = sync.payload('infra-shared', MID, action, 'action')
    assert 'private output' not in p['content'] and 'hidden' not in p['content']
    assert p['reviewed'] is False and p['classification'] == 'restricted'
    assert sync.payload('infra-shared', MID, {'id': RID, 'content': 'raw request', 'evidence': ['zulip-source:1']}, 'record') is None
    assert sync.payload('infra-shared', MID, {'id': RID, 'content': 'mot de passe sensible'}, 'record') is None


def test_failed_write_retried_and_success_deduplicated():
    class Broker:
        async def call(self, name, **args):
            if name == 'list_memory_history':
                return {'records': [], 'next_cursor': args['after']}
            if name == 'list_memory_missions':
                return {'missions': [{'id': MID, 'project_id': 'infra-shared'}] if args['project_id'] == 'infra-shared' else [], 'next_cursor': 1}
            if name == 'get_mission_state':
                return {'id': MID, 'project_id': 'infra-shared', 'summary': 'Mission en cours'}
            return [{'id': RID, 'status': 'succeeded'}] if name == 'list_actions_for_mission' else []
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE missions (id TEXT PRIMARY KEY, project TEXT)')
    db.execute('CREATE TABLE captured (source TEXT PRIMARY KEY, memory_id TEXT)')
    def fail(_):
        raise RuntimeError('unavailable')
    assert asyncio.run(sync.sync(Broker(), db, fail))['failed'] == 2
    assert db.execute('SELECT count(*) FROM captured').fetchone()[0] == 0
    assert asyncio.run(sync.sync(Broker(), db, lambda _: RID))['stored'] == 2
    assert asyncio.run(sync.sync(Broker(), db, fail))['failed'] == 0


def test_mission_checkpoint_recovery_versions_and_secret_exclusion(tmp_path):
    path = tmp_path/'sync.sqlite3'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE captured (source TEXT PRIMARY KEY, memory_id TEXT)')
    values = []
    def ingest(value):
        values.append(value)
        return str(__import__('uuid').uuid4())
    state = {'id': MID, 'project_id': 'infra-shared', 'objective': 'Terminer Ops',
             'summary': 'API validées', 'current_step': 'restauration',
             'remaining_steps': ['Tester la reprise'], 'resources': ['excluded raw field']}
    first = sync.payload('infra-shared', MID, state, 'mission')
    assert sync.capture_mission(db, first, ingest)
    db.close()
    db = sqlite3.connect(path)
    assert not sync.capture_mission(db, first, ingest)
    second = sync.payload('infra-shared', MID, {**state, 'current_step': 'reprise'}, 'mission')
    assert sync.capture_mission(db, second, ingest)
    assert values[1]['supersedes_id']
    assert sync.capture_mission(db, first, ingest)  # returning to an earlier phase is a new version
    assert values[2]['source_ref'] != values[0]['source_ref']
    assert 'Tester la reprise' in values[0]['content']
    assert 'excluded raw field' not in values[0]['content']
    assert sync.payload('infra-shared', MID, {**state, 'summary': 'mot de passe sensible'}, 'mission') is None
    import pytest
    with pytest.raises(ValueError):
        sync.payload('minecraft', MID, state, 'mission')


def test_project_permissions_preserved():
    p = sync.payload('minecraft', MID, {'id': RID, 'status': 'succeeded'}, 'action')
    assert p['project'] == 'minecraft'
    assert p['allowed_roles'] == ['hermes-coordinator', 'minecraft-ops']


def test_api_outage_does_not_advance_index_cursor(monkeypatch):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE captured (source TEXT PRIMARY KEY, memory_id TEXT)')
    db.execute('INSERT INTO captured VALUES (?,?)', ('source', RID))
    requests = []
    def request(path, value, **kwargs):
        requests.append(value)
        return {'ok': True, 'result': {'id': RID}} if value['operation'] == 'get' else {'ok': False}
    monkeypatch.setattr(sync, 'socket_request', request)
    assert sync.index_pending(db) == 'unavailable'
    assert db.execute('SELECT count(*) FROM index_cursor').fetchone()[0] == 0
    assert requests[-1]['payload'] == {'ids': [RID]}


def test_api_index_skips_superseded_records(monkeypatch):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE captured (source TEXT PRIMARY KEY, memory_id TEXT)')
    db.execute('INSERT INTO captured VALUES (?,?)', ('source', RID))
    def request(path, value, **kwargs):
        assert value['operation'] == 'get'
        return {'ok': True, 'result': {'id': RID, 'superseded_by_id': MID}}
    monkeypatch.setattr(sync, 'socket_request', request)
    assert sync.index_pending(db) == 'no_current_records'
    assert db.execute('SELECT position FROM index_cursor').fetchone()[0] == 1


def test_one_pending_embedding_does_not_starve_later_documents(monkeypatch):
    db = sqlite3.connect(':memory:')
    db.execute('CREATE TABLE captured (source TEXT PRIMARY KEY, memory_id TEXT)')
    db.execute('INSERT INTO captured VALUES (?,?)', ('source', RID))
    def request(path, value, **kwargs):
        return {'ok': True, 'result': {'id': RID} if value['operation'] == 'get' else {'pending': [RID], 'indexed': []}}
    monkeypatch.setattr(sync, 'socket_request', request)
    assert sync.index_pending(db) == 'pending'
    assert db.execute('SELECT position FROM index_cursor').fetchone()[0] == 1

def test_budget_pause_does_not_poll_or_block_capture(monkeypatch):
    import time
    db=sqlite3.connect(':memory:')
    db.execute('CREATE TABLE index_backoff (id INTEGER PRIMARY KEY,retry_after REAL)')
    db.execute('INSERT INTO index_backoff VALUES (1,?)',(time.time()+60,))
    monkeypatch.setattr(sync,'socket_request',lambda *a,**k: (_ for _ in ()).throw(AssertionError('unexpected provider poll')))
    assert sync.index_pending(db)=='budget_wait'
