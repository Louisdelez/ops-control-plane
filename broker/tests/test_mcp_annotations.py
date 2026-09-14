import asyncio
from mcp import Client
from ops_broker.mcp_server import mcp


def test_only_observation_tools_are_declared_read_only():
    async def read():
        async with Client(mcp) as client:
            return (await client.list_tools()).tools
    tools=asyncio.run(read())
    readers={tool.name for tool in tools if tool.annotations and tool.annotations.read_only_hint is True}
    assert readers=={'list_authorized_runbooks','list_open_missions','get_mission',
        'list_actions_for_mission','list_open_incidents','get_incident','list_actions_for_incident',
        'get_action','get_mission_state','list_mission_checkpoints','list_mission_records',
        'list_model_traces','get_incident_details','get_action_artifacts','verify_audit_chain','list_memory_history','list_memory_missions'}
    assert not readers & {'request_runbook_action','execute_approved_action','create_mission',
                          'add_mission_record','set_mission_status'}
