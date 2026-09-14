import asyncio
from types import SimpleNamespace
import pytest
from ops_native.broker import Broker
from ops_native.zulip_api import Zulip

class Session:
    async def call_tool(self,name,args,**kw):
        assert type(kw['read_timeout_seconds']) in {int,float}
        assert args['actor_id']=='native-observer'
        return SimpleNamespace(is_error=False,structured_content={'ok':True,'data':{'id':'a'}})

def test_mcp_timeout_uses_current_sdk_numeric_contract():
    assert asyncio.run(Broker(Session(),'native-observer').call('get_mission',mission_id='a')) == {'id':'a'}

def test_forged_or_deactivated_zulip_actor_is_refused():
    client=object.__new__(Zulip)
    msg={'type':'stream','stream_id':3,'sender_id':7}
    client.call=lambda *a: {'user':{'user_id':7,'is_bot':False,'is_active':False}}
    assert not client.trusted_message(msg,3,[7])
    client.call=lambda *a: {'user':{'user_id':7,'is_bot':True,'is_active':True}}
    assert not client.trusted_message(msg,3,[7])
    client.call=lambda *a: {'user':{'user_id':7,'is_bot':False,'is_active':True}}
    assert client.trusted_message(msg,3,[7])
    assert not client.trusted_message(msg,4,[7])
    assert not client.trusted_message(msg,3,[8])

def test_zulip_channel_narrow_preserves_numeric_identity():
    z=object.__new__(Zulip);captured=[]
    def call(method,path,fields):
        captured.append(fields);return {'messages':[]}
    z.call=call
    assert z.messages(4,'newest',1,0)==[]
    assert captured[0]['narrow']==[{'operator':'channel','operand':4}]
