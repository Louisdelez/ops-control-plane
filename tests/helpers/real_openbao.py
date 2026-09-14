#!/usr/bin/python3
"""Isolated, in-memory OpenBao integration test. Never connects to production."""
import copy,json,os,runpy,secrets,socket,subprocess,tempfile,time,urllib.error,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]

def scenario(local_ids, fail_after_policy=False):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    token=secrets.token_urlsafe(32)
    env={k:v for k,v in os.environ.items() if not k.startswith(('BAO_','VAULT_'))}
    env['BAO_DEV_ROOT_TOKEN_ID']=token
    server=subprocess.Popen(['/usr/bin/bao','server','-dev','-dev-no-store-token',f'-dev-listen-address=127.0.0.1:{port}'],env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(method,path,*,payload=None,allowed_statuses=frozenset({200}),**kw):
        raw=json.dumps(payload).encode() if payload is not None else None
        req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=raw,headers={'X-Vault-Token':token,'Content-Type':'application/json'},method=method)
        try:
            with opener.open(req,timeout=3) as resp:code=resp.status;body=resp.read()
        except urllib.error.HTTPError as e:code=e.code;body=e.read();e.close()
        doc=json.loads(body) if body else None
        if code not in allowed_statuses:raise RuntimeError(f'ISOLATED_API_{method}_{code}')
        return code,doc
    try:
        for _ in range(100):
            if server.poll() is not None:raise RuntimeError('ISOLATED_SERVER_EXITED')
            try:request('GET','/v1/sys/health');break
            except Exception:time.sleep(.1)
        else:raise RuntimeError('ISOLATED_SERVER_TIMEOUT')
        write=lambda path,payload:request('POST',path,payload=payload,allowed_statuses={200,204})
        write('/v1/sys/auth/approle',{'type':'approle'})
        write('/v1/sys/mounts/kv-infra-shared',{'type':'kv','options':{'version':'2'}})
        roles=('deepseek-client','hermes-coordinator','hermes-runtime')
        for role in roles:
            write('/v1/sys/policies/acl/'+role,{'policy':'path "synthetic/*" { capabilities = ["read"] }'})
            write('/v1/auth/approle/role/'+role,{'token_policies':[role],'bind_secret_id':True,'local_secret_ids':local_ids,'secret_id_num_uses':0,'secret_id_ttl':'1h','token_no_default_policy':True})
            write('/v1/auth/approle/role/'+role+'/secret-id',{})
        path='/v1/auth/approle/role/deepseek-client'
        _,observed=request('GET',path)
        code,error=request('POST',path,payload=observed['data'],allowed_statuses={400})
        assert error['errors']==['local_secret_ids can only be modified during role creation']
        print(json.dumps({'local_secret_ids':local_ids,'original_failure_reproduced':True}),flush=True)
        ns=runpy.run_path(str(ROOT/'deploy/control-plane/bin/control-plane-bootstrap'))
        state=ns['_snapshot_openbao_undo'].__globals__
        before_ids={role:request('GET','/v1/auth/approle/role/'+role+'/role-id')[1] for role in roles}
        before_accessors={role:request('LIST','/v1/auth/approle/role/'+role+'/secret-id')[1] for role in roles}
        saved={}
        with tempfile.TemporaryDirectory(prefix='atlas-isolated-test-') as raw:
            state['OPENBAO_UNDO_ESCROW']=Path(raw)/'undo'
            state['openbao_request']=request
            state['_root_token_from_escrow']=lambda:token
            state['_set_transaction']=lambda tx,**kw:tx.update(kw)
            state['_fsync_directory']=lambda *a:None
            state['_read_root_file']=lambda p,*a,**kw:p.read_bytes()
            state['_write_encrypted_document']=lambda p,name,d,**kw:saved.update(document=copy.deepcopy(d))
            state['_read_encrypted_document']=lambda *a:copy.deepcopy(saved['document'])
            tx={'release_id':'isolated-test'}
            ns['_snapshot_openbao_undo'](tx)
            before=ns['_capture_openbao_managed_state'](token)
            tx['openbao_provisioning_started']=True
            if fail_after_policy:
                def fault(method,path,**kw):
                    if method=='PUT' and path.endswith('hermes-coordinator'):raise RuntimeError('synthetic interruption')
                    return request(method,path,**kw)
                state['openbao_request']=fault
                try:ns['_quarantine_legacy_openbao_roles'](tx,ROOT)
                except RuntimeError:pass
                else:raise AssertionError('Fault did not occur')
                state['openbao_request']=request
            else:
                ns['_quarantine_legacy_openbao_roles'](tx,ROOT)
                hermes=runpy.run_path(str(ROOT/'scripts/provision-hermes-openbao'))
                def hermes_request(method,path,*,allow_not_found=False,expect_json=True,**kw):
                    code,doc=request(method,path,allowed_statuses={200,204,404} if allow_not_found else {200,204},**kw)
                    return None if code==404 else doc
                hermes['_quarantine_legacy_access'].__globals__['_request']=hermes_request
                hermes['_quarantine_legacy_access'](token,{name:hermes['_EXPECTED_QUARANTINE_POLICY'] for name in hermes['LEGACY_NAMES']},preserve_roles=True)
                hermes['_configure_runtime'](token,hermes['_EXPECTED_RUNTIME_POLICY'])
                write('/v1/kv-infra-shared/data/hermes/gateway',{'data':{'api_server_key':'synthetic-testing-value'}})
            ns['_record_openbao_poststate'](tx)
            try:
                ns['_undo_openbao_state'](tx)
            except ns['BootstrapError']:
                after=ns['_capture_openbao_managed_state'](token)
                for role in roles:
                    a=before['roles'][role]['configuration'];b=after['roles'][role]['configuration']
                    print(json.dumps({'synthetic_role':role,'differing_fields':{k:{'before':a.get(k),'after':b.get(k)} for k in set(a)|set(b) if a.get(k)!=b.get(k)}}))
                raise
            assert tx['openbao_undo_complete'] is True
            assert ns['_capture_openbao_managed_state'](token)==before
            assert {role:request('GET','/v1/auth/approle/role/'+role+'/role-id')[1]['data'] for role in roles}=={role:v['data'] for role,v in before_ids.items()}
            assert {role:request('LIST','/v1/auth/approle/role/'+role+'/secret-id')[1]['data'] for role in roles}=={role:v['data'] for role,v in before_accessors.items()}
            print(json.dumps({'local_secret_ids':local_ids,'partial_failure':fail_after_policy,'migration_rollback_verified':True,'role_ids_and_secret_ids_preserved':True}),flush=True)
    finally:
        server.terminate()
        try:server.wait(timeout=10)
        except subprocess.TimeoutExpired:server.kill();server.wait()
        token=''

if __name__=='__main__':
    for local,partial in ((False,False),(True,False),(False,True)):
        scenario(local,partial)
