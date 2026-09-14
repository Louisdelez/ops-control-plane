"""Mission-scoped operations for the API Hermes profile.

The model never supplies an actor or a mission. The broker remains responsible
for runbook validation, budgets and separate human approval of class C actions.
"""
import json
import asyncio
import os
import uuid
from mcp.types import ToolAnnotations
from .broker import connect
from .dispatch import PROJECTS
from .execution import api_allowed

ACTOR='hermes-native'
COMMAND='/usr/local/libexec/ops-native-api-broker'

class MissionTools:
    def __init__(self,broker,mission_id):
        self.broker=broker;self.mission_id=str(uuid.UUID(mission_id))

    async def context(self):
        mission=await self.broker.call('get_mission_state',mission_id=self.mission_id)
        if mission['project_id'] not in PROJECTS:raise ValueError('Project unavailable')
        runbooks=await self.broker.call('list_authorized_runbooks')
        return {'mission':mission,'runbooks':[r for r in runbooks if r.get('project')==mission['project_id']],
                'actions':await self.broker.call('list_actions_for_mission',mission_id=self.mission_id,limit=50)}

    async def memory(self,query):
        if not isinstance(query,str) or not 1<=len(query)<=4000:raise ValueError('Invalid memory query')
        mission=await self.broker.call('get_mission',mission_id=self.mission_id)
        if mission['project_id'] not in PROJECTS:raise ValueError('Project unavailable')
        from pathlib import Path
        from ops_memory.protocol import socket_request
        response=await asyncio.to_thread(socket_request,Path('/run/ops-memory/ops-memory.sock'),{
            'version':1,'request_id':str(uuid.uuid4()),'operation':'search','actor':'hermes-coordinator',
            'payload':{'query':query,'project':mission['project_id'],'environment':'production',
                'max_classification':'internal','top_k':5,'candidate_limit':20,'allow_api':True,'important':True}},15.0)
        if response.get('ok') is not True:raise ValueError('Memory unavailable')
        return {'memory':response['result'],'authority':'advisory; verify current state with broker runbooks'}

    async def request(self,runbook_id,parameters):
        if not isinstance(parameters,dict) or len(json.dumps(parameters))>8192:raise ValueError('Invalid parameters')
        context=await self.context()
        if not any(r['id']==runbook_id for r in context['runbooks']):raise ValueError('Runbook unavailable in this mission')
        request_id=str(uuid.uuid5(uuid.UUID(self.mission_id),'native-api/'+runbook_id+'/'+json.dumps(parameters,sort_keys=True,separators=(',',':'))))
        return await self.broker.call('request_runbook_action',request_id=request_id,
            runbook_id=runbook_id,parameters=parameters,mission_id=self.mission_id,
            reason='Demande de la mission via le profil Hermes API natif')

    async def action(self,action_id,execute=False):
        action_id=str(uuid.UUID(action_id))
        action=await self.broker.call('get_action',action_id=action_id)
        if action.get('mission_id')!=self.mission_id:raise ValueError('Action outside this mission')
        if execute:
            # The broker checks the actual approval, its expiry and the exact
            # parameter digest. A model's statement cannot grant approval.
            action=await self.broker.call('execute_approved_action',action_id=action_id)
        return action

async def invoke(operation,**arguments):
    if not api_allowed():return {'ok':False,'error':'API disabled by execution mode'}
    try:
        async with connect(COMMAND,[],ACTOR) as broker:
            tools=MissionTools(broker,os.environ['OPS_NATIVE_MISSION_ID'])
            result=await getattr(tools,operation)(**arguments)
            return {'ok':True,'data':result}
    except Exception:
        return {'ok':False,'error':'Operation refused or unavailable; do not claim success'}

def register(mcp):
    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def get_mission_operations() -> dict:
        """Read this mission, its actions and the exact broker-authorized runbooks."""
        return await invoke('context')

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def search_mission_memory(query: str) -> dict:
        """Retrieve at most five memories from this mission's project. Verify current facts."""
        return await invoke('memory',query=query)

    @mcp.tool()
    async def request_mission_action(runbook_id: str, parameters: dict) -> dict:
        """Request an authorized action in this mission. Class C awaits human approval."""
        return await invoke('request',runbook_id=runbook_id,parameters=parameters)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))
    async def get_mission_action(action_id: str) -> dict:
        """Read an action belonging to this mission."""
        return await invoke('action',action_id=action_id)

    @mcp.tool()
    async def execute_approved_mission_action(action_id: str) -> dict:
        """Execute only an action in this mission with a valid broker-recorded approval."""
        return await invoke('action',action_id=action_id,execute=True)


class Operations:
    async def action(self,mission_id,action_id,execute=False):
        if not api_allowed():raise ValueError('API mode is disabled')
        async with connect(COMMAND,[],ACTOR) as broker:
            return await MissionTools(broker,mission_id).action(action_id,execute=execute)
