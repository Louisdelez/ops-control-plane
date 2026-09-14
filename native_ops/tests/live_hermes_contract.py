"""Real official Hermes + real broker; synthetic model endpoint, no paid inference.

Run explicitly; this is a transport test, never completion evidence for AI quality.
"""
import json,os,subprocess,tempfile,threading,sys,pwd
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import yaml

ROOT=Path('/home/ops-user/ops-control-plane')
MISSION='11111111-1111-4111-8111-111111111111'
requests=[]
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_POST(self):
        raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
        body=json.loads(raw)
        has_result=any(m.get('role')=='tool' for m in body['messages'])
        names=[t['function']['name'] for t in body.get('tools',[])]
        requests.append({'tool_names':names,'has_tool_result':has_result,'mission_scope_ok':self.headers.get('X-Ops-Mission-ID')==MISSION,'reasoning_scope_ok':self.headers.get('X-Ops-Reasoning')=='standard'})
        if not requests[-1]['mission_scope_ok'] or not requests[-1]['reasoning_scope_ok']:
            # Native auxiliary probes have no mission: the real facade refuses them before egress.
            self.send_error(400);return
        if has_result:
            msg={'role':'assistant','content':'RECETTE SYNTHÉTIQUE : le contrôle réel du broker a été reçu. Aucun modèle API réel testé.'}
            reason='stop'
        else:
            matches=[name for name in names if name.endswith('get_service_health')]
            if len(matches)!=1:
                self.send_error(400);return
            msg={'role':'assistant','content':None,'tool_calls':[{'id':'call_native_health','type':'function','function':{'name':matches[0],'arguments':'{}'}}]}
            reason='tool_calls'
        response={'id':'synthetic-test','object':'chat.completion','created':0,'model':'qwen-coordinator',
            'choices':[{'index':0,'message':msg,'finish_reason':reason}],
            'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}}
        if body.get('stream'):
            chunks=[]
            delta={'role':'assistant'}
            if msg.get('tool_calls'):
                delta['tool_calls']=[{'index':0,**msg['tool_calls'][0]}]
            else:delta['content']=msg['content']
            for value,finish in [(delta,None),({},reason)]:
                chunks.append('data: '+json.dumps({'id':'synthetic-test','object':'chat.completion.chunk','created':0,'model':'qwen-coordinator','choices':[{'index':0,'delta':value,'finish_reason':finish}]})+'\n\n')
            data=(''.join(chunks)+'data: [DONE]\n\n').encode();kind='text/event-stream'
        else:data=json.dumps(response).encode();kind='application/json'
        self.send_response(200);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)

def main():
    service='--service-runtime' in sys.argv
    if service and os.geteuid()!=0:raise SystemExit('service identity test needs administrator')
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    with tempfile.TemporaryDirectory(prefix='ops-native-contract-') as raw:
        home=Path(raw)
        adapter=home/'tools.py'
        adapter.write_text("import sys\nsys.path.insert(0,"+repr(str(ROOT/'native_ops'))+")\nfrom mcp.server import MCPServer\nfrom mcp.types import ToolAnnotations\nfrom ops_native.tools import health\nm=MCPServer('native-contract')\n@m.tool(annotations=ToolAnnotations(readOnlyHint=True))\nasync def get_service_health():\n return await health("+repr(MISSION)+",'/usr/bin/sudo',['-n','-u','opsbroker','-g','opsbroker','--','/usr/local/libexec/ops-broker/ops-broker-mcp-codex'],'codex-supervised')\nm.run()\n")
        cfg=yaml.safe_load((ROOT/'native_ops/deploy/hermes.yaml').read_text())
        cfg['providers']['ops-native']['api']=f'http://127.0.0.1:{server.server_port}/v1'
        cfg['mcp_servers']={'ops-native':{'command':str(ROOT/'broker/.venv/bin/python'),'args':[str(adapter)],'tools':{'include':['get_service_health'],'resources':False,'prompts':False},'trust':'untrusted'}}
        if service:
            cfg['mcp_servers']['ops-native'].update(command='/opt/ops-native/venv/bin/ops-native-tools',args=[],env={'OPS_NATIVE_MISSION_ID':MISSION})
        (home/'config.yaml').write_text(yaml.safe_dump(cfg))
        (home/'SOUL.md').write_text('Test de transport. Utilise le contrôle de santé puis rapporte son résultat.\n')
        env=dict(os.environ,HERMES_HOME=raw,OPS_NATIVE_MISSION_ID=MISSION,OPS_NATIVE_REASONING='standard',OPS_NATIVE_FACADE_TOKEN='synthetic-test-token',HERMES_NO_UPDATE_CHECK='1')
        env.pop('PYTHONPATH',None)
        command=['/home/ops-user/.local/bin/hermes']
        if service:
            account=pwd.getpwnam('hermesd')
            for f in [home,*home.iterdir()]:os.chown(f,account.pw_uid,account.pw_gid)
            command=['/usr/sbin/runuser','-u','hermesd','--','/var/lib/hermes/hermes-agent/venv/bin/python','-m','hermes_cli.main']
        result=subprocess.run([*command,'chat','--query-file','-','--oneshot','-Q','--max-turns','3','--run-budget','45'],input='Effectue le contrôle de santé avec get_service_health.',capture_output=True,text=True,cwd=raw,env=env,timeout=65)
        observed=any(r['has_tool_result'] for r in requests)
        report={'service_runtime':service,'official_hermes_exit':result.returncode,'model_endpoint':'synthetic loopback fixture','paid_inference':False,'real_broker_tool_result_seen':observed,'requests':requests}
        out=Path('/home/ops-user/standard-install-review/native-integration/'+('hermes-service-transport-test.json' if service else 'hermes-transport-test.json'));out.write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report))
        if not observed:
            print('DIAGNOSTIC',result.stderr[-1500:],result.stdout[-1500:])
        assert result.returncode==0 and observed
        assert all(r['mission_scope_ok'] and r['reasoning_scope_ok'] for r in requests if r['tool_names'] or r['has_tool_result'])
    server.shutdown()
if __name__=='__main__':main()
