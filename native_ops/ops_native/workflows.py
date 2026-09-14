"""Durable, deterministic broker workflows. Definitions never contain shell commands."""
import argparse
import asyncio
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
from .broker import connect

DEFINITIONS = {
    'local-readiness': [
        {'id':'control','runbook':'local.health.v1','parameters':{},'after':[]},
        {'id':'vault','runbook':'local.service-status.v1',
         'parameters':{'service':'openbao.service'},'after':['control']},
    ],
    'infrastructure-readiness': [
        {'id':'control','runbook':'local.health.v1','parameters':{},'after':[]},
        *[{'id':resource,'runbook':'inventory.remote-preflight.v1',
           'parameters':{'resource':resource},'after':['control'],
           'expect':{'status':'observed','systemd_state':'running'}}
          for resource in ('edge-vps','nas','prod')],
    ],
}


def fingerprint(steps):
    return hashlib.sha256(json.dumps(steps,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def outcome(action, step):
    if action['status'] != 'succeeded':
        return 'waiting_approval' if action['status']=='pending_approval' else 'needs_review'
    result=action.get('result') or {}
    if result.get('return_code')!=0 or result.get('timed_out') or result.get('truncated'):
        return 'needs_review'
    if step.get('expect'):
        try: data=json.loads(result.get('stdout',''))
        except (ValueError,TypeError):return 'needs_review'
        if not isinstance(data,dict) or any(data.get(k)!=v for k,v in step['expect'].items()):
            return 'needs_review'
    return 'succeeded'


class Workflow:
    def __init__(self, db, broker):
        self.db,self.broker=db,broker
        db.row_factory=sqlite3.Row
        db.executescript('''
        CREATE TABLE IF NOT EXISTS workflows(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, digest TEXT NOT NULL, mission_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS workflow_steps(
          workflow_id TEXT NOT NULL, step_id TEXT NOT NULL, action_id TEXT,
          phase TEXT NOT NULL, PRIMARY KEY(workflow_id,step_id));
        ''')

    @staticmethod
    def rid(run_id, suffix):
        return str(uuid.uuid5(uuid.UUID(run_id),suffix))

    async def run(self, name, run_id, mission_id):
        run_id=str(uuid.UUID(run_id));mission_id=str(uuid.UUID(mission_id))
        steps=DEFINITIONS[name]
        mission=await self.broker.call('get_mission',mission_id=mission_id)
        if mission['project_id']!='infra-shared' or mission['status'] not in {'open','active'}:
            raise ValueError('An open infra-shared mission is required')
        digest=fingerprint(steps)
        existing=self.db.execute('SELECT * FROM workflows WHERE id=?',(run_id,)).fetchone()
        if existing and (existing['name'],existing['digest'],existing['mission_id'])!=(name,digest,mission_id):
            raise ValueError('A recorded workflow cannot be changed during resumption')
        allowed=await self.broker.call('list_authorized_runbooks')
        registry={r['id']:r for r in allowed if r['project']=='infra-shared'}
        # These installed readiness workflows are observation-only. New action
        # workflows require their own reviewed definition and approval handling.
        for step in steps:
            if registry.get(step['runbook'],{}).get('action_class')!='A':
                raise ValueError('The exact observation runbook is unavailable')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO workflows VALUES(?,?,?,?)',
                            (run_id,name,digest,mission_id))
            for step in steps:
                self.db.execute('INSERT OR IGNORE INTO workflow_steps VALUES(?,?,NULL,?)',
                                (run_id,step['id'],'pending'))
        await self.broker.call('add_mission_record',request_id=self.rid(run_id,'definition'),
            mission_id=mission_id,kind='observation',
            content='Workflow '+name+' : '+', '.join(s['id'] for s in steps),
            evidence=['workflow:'+run_id,'definition-sha256:'+digest])
        for step in steps:
            states={r['step_id']:dict(r) for r in self.db.execute(
                'SELECT * FROM workflow_steps WHERE workflow_id=?',(run_id,))}
            state=states[step['id']]
            if state['phase'] in {'succeeded','needs_review'}:continue
            if any(states[dependency]['phase']!='succeeded' for dependency in step['after']):continue
            # A durable request ID makes interrupted requests safe to reconcile
            # through the broker without creating a second action.
            with self.db:
                self.db.execute('UPDATE workflow_steps SET phase=? WHERE workflow_id=? AND step_id=?',
                                ('requesting',run_id,step['id']))
            if state['action_id']:
                action=await self.broker.call('get_action',action_id=state['action_id'])
            else:
                action=await self.broker.call('request_runbook_action',
                    request_id=self.rid(run_id,'action/'+step['id']),mission_id=mission_id,
                    runbook_id=step['runbook'],parameters=step['parameters'],
                    reason='Observation du workflow '+name+', étape '+step['id'])
            if (action.get('mission_id')!=mission_id or action.get('runbook_id')!=step['runbook']
                    or action.get('parameters')!=step['parameters']):
                raise ValueError('Broker action does not match the workflow step')
            phase=outcome(action,step)
            await self.broker.call('add_mission_record',request_id=self.rid(run_id,'result/'+step['id']),
                mission_id=mission_id,kind='observation',
                content='Workflow '+name+' / '+step['id']+' : '+phase,
                evidence=['workflow:'+run_id,action['id']])
            with self.db:
                self.db.execute('UPDATE workflow_steps SET action_id=?,phase=? WHERE workflow_id=? AND step_id=?',
                                (action['id'],phase,run_id,step['id']))
        states=[dict(r) for r in self.db.execute(
            'SELECT step_id,action_id,phase FROM workflow_steps WHERE workflow_id=? ORDER BY rowid',(run_id,))]
        complete=all(r['phase']=='succeeded' for r in states)
        return {'workflow_id':run_id,'workflow':name,'mission_id':mission_id,
                'status':'succeeded' if complete else 'needs_review','steps':states}


async def execute(args):
    folder=Path.home()/'.local/state/ops-workflows'
    folder.mkdir(parents=True,mode=0o700,exist_ok=True)
    with (folder/'workflow.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        db=sqlite3.connect(folder/'state.sqlite3')
        try:
            async with connect('/usr/bin/sudo',['-n','-u','opsbroker','-g','opsbroker','--',
                '/usr/local/libexec/ops-broker/ops-broker-mcp-codex'],'codex-supervised') as broker:
                return await Workflow(db,broker).run(args.workflow,args.run_id,args.mission_id)
        finally:db.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--workflow',required=True,choices=sorted(DEFINITIONS))
    parser.add_argument('--mission-id',required=True)
    parser.add_argument('--run-id',required=True,help='Keep this UUID to resume the same workflow')
    args=parser.parse_args();os.umask(0o077)
    try:
        result=asyncio.run(execute(args));print(json.dumps(result))
    except Exception:
        raise SystemExit('Workflow unavailable; recorded actions are preserved for review') from None
    raise SystemExit(0 if result['status']=='succeeded' else 2)


if __name__=='__main__':main()
