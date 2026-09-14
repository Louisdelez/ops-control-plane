"""Small MCP client; callers cannot provide another actor in a tool payload."""
from contextlib import asynccontextmanager
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

class BrokerError(RuntimeError):
    pass

class Broker:
    def __init__(self, session, actor):
        self.session, self.actor = session, actor

    async def call(self, name, **arguments):
        if 'actor_id' in arguments:
            raise BrokerError('caller cannot override the service identity')
        result = await self.session.call_tool(
            name, {'actor_id': self.actor, **arguments},
            read_timeout_seconds=60)
        body = result.structured_content
        if result.is_error or not isinstance(body, dict) or body.get('ok') is not True:
            # Deliberately omit arbitrary remote exception bodies.
            raise BrokerError('broker operation refused: ' + name)
        return body.get('data')

@asynccontextmanager
async def connect(command, args, actor):
    async with stdio_client(StdioServerParameters(command=command, args=args)) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            yield Broker(session, actor)
