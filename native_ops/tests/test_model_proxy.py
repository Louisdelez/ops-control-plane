"""Real loopback HTTP transport, synthetic facade; no provider request."""
import http.client,json,threading,uuid
from types import SimpleNamespace
from ops_native.model_proxy import Handler,identity
from ops_orchestrator.hermes_facade import ThreadingHermesFacadeServer,FacadeResponse

def test_http_scope_selects_pool_and_preserves_mission_budget_identity(monkeypatch):
    monkeypatch.setattr("ops_native.model_proxy.api_allowed",lambda: True)
    calls=[]
    class Facade:
        limits=SimpleNamespace(max_request_bytes=65536)
        def __init__(self,name):self.name=name
        def authorized(self,token):return token=='Bearer synthetic'
        def dispatch(self,token,body):
            calls.append((self.name,identity()))
            return FacadeResponse(200,'application/json',b'{}')
    server=ThreadingHermesFacadeServer(('127.0.0.1',0),Facade('standard'))
    server.advanced=Facade('advanced');server.RequestHandlerClass=Handler
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    mid=str(uuid.uuid4())
    def request(scope=None,level='standard',auth='Bearer synthetic'):
        conn=http.client.HTTPConnection(*server.server_address,timeout=5)
        headers={'Content-Type':'application/json','Authorization':auth,'X-Ops-Reasoning':level}
        if scope is not None:headers['X-Ops-Mission-ID']=scope
        try:
            conn.request('POST','/v1/chat/completions',b'{}',headers)
            response=conn.getresponse();response.read();return response.status
        finally:conn.close()
    try:
        assert request()==400
        assert request('invalid')==400
        assert request(mid,'unbounded')==400
        assert request(mid,auth='Bearer wrong')==401
        assert calls==[]
        monkeypatch.setattr('ops_native.model_proxy.api_allowed',lambda: False)
        assert request(mid)==403
        assert calls==[]
        monkeypatch.setattr('ops_native.model_proxy.api_allowed',lambda: True)
        assert request(mid)==200
        assert request(mid,'advanced')==200
        assert request(mid)==200
        assert [name for name,pair in calls]==['standard','advanced','standard']
        assert {pair[1] for name,pair in calls}=={'native/mission/'+mid}
        assert len({pair[0] for name,pair in calls})==3
    finally:
        server.shutdown();server.server_close();thread.join(5)
