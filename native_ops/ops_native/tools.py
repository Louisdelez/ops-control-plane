"""Mission tools for native Hermes; no shell or model-supplied identity."""
import json
import os
import uuid
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from .broker import connect

mcp = MCPServer('ops-native-mission', instructions='Use only the current mission and its authorized runbooks. The broker enforces budgets and separate human approval for class C. Never claim success without action evidence.')
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)

async def health(mission_id, command, args, actor):
    mission_id = str(uuid.UUID(mission_id))
    async with connect(command, args, actor) as broker:
        mission = await broker.call('get_mission', mission_id=mission_id)
        if mission['project_id'] != 'infra-shared':
            raise ValueError('observation profile only serves infra-shared')
        allowed = await broker.call('list_authorized_runbooks')
        if not any(x['id'] == 'local.health.v1' and x['action_class'] == 'A' for x in allowed):
            raise ValueError('read-only health runbook is unavailable')
        action = await broker.call('request_runbook_action',
            request_id=str(uuid.uuid4()),
            runbook_id='local.health.v1', parameters={},
            reason='Observation demandée dans la mission native', mission_id=mission_id)
        if action['status'] != 'succeeded':
            return {'ok': False, 'action_id': action['id'], 'status': action['status']}
        return {'ok': True, 'action_id': action['id'],
                'observation': json.loads(action['result']['stdout'])}

@mcp.tool(annotations=READ)
async def get_service_health() -> dict:
    """Read current workstation health using this mission's authorized broker runbook."""
    try:
        return await health(os.environ['OPS_NATIVE_MISSION_ID'],
            '/usr/local/libexec/ops-native-broker', [], 'native-observer')
    except Exception:
        return {'ok': False, 'error': 'observation unavailable; do not claim success'}

from .mission_tools import register
register(mcp)

def main():
    mcp.run()

if __name__ == '__main__':
    main()
