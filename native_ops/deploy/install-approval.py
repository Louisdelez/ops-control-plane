"""Add native Agent + existing bridge; preserve official products and broker policy backup."""
import json,os,pwd,grp,runpy,subprocess,time
from pathlib import Path
ROOT=Path('/home/ops-user/ops-control-plane')
DEPLOY=ROOT/'native_ops/deploy'
def run(*args):subprocess.run(args,check=True,stdout=subprocess.DEVNULL)
def write(path,data,mode=0o600):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True,mode=0o755)
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,mode)
    with os.fdopen(fd,'w') as f:f.write(data);os.fchmod(f.fileno(),mode)
def main():
    assert os.geteuid()==0;os.umask(0o077)
    import yaml
    from ops_native.zulip_api import Zulip
    lib=runpy.run_path(str(DEPLOY/'provider-secrets.py'));vault=lib['Vault']()
    values=json.loads(Path('/var/lib/atlas-local-repair/credentials.json').read_bytes())
    login=vault.call('POST','auth/userpass/login/ops-user',{'password':values['openbao_louis']})
    vault.token=login['auth']['client_token'];login.clear()
    try:
        origin='https://zulip.ops.local:8443';ca='/etc/pki/ca-trust/source/anchors/zulip-standard-local.crt'
        admin=Zulip(origin,ca,'','')
        login=admin.call('POST','/api/v1/fetch_api_key',{'username':'admin@ops.local','password':values['zulip_owner']});values.clear()
        admin=Zulip(origin,ca,login['email'],login['api_key']);login.clear()
        owner=admin.call('GET','/api/v1/users/me')
        if owner['user_id']!=8 or owner.get('is_bot'):raise RuntimeError('owner mismatch')
        bots=admin.call('GET','/api/v1/bots')['bots']
        matches=[b for b in bots if b.get('username','').split('@')[0]=='ops-approvals-bot']
        if len(matches)>1:raise RuntimeError('ambiguous bot')
        bot=matches[0] if matches else admin.call('POST','/api/v1/bots',{'short_name':'ops-approvals','full_name':'Ops Approbations'})
        email=bot.get('username') or bot.get('email')
        if not email:
            members=admin.call('GET','/api/v1/users')['members']
            emails=[u['email'] for u in members if u.get('is_bot') and u['email'].split('@')[0]=='ops-approvals-bot']
            if len(emails)!=1:raise RuntimeError('bot email absent')
            email=emails[0]
        client=Zulip(origin,ca,email,bot['api_key']);me=client.call('GET','/api/v1/users/me')
        data={'ZULIP_REALM_URL':origin,'ZULIP_BOT_EMAIL':email,'ZULIP_API_KEY':bot['api_key'],'ZULIP_BOT_USER_ID':str(me['user_id']),'ZULIP_APPROVER_USER_IDS':'8','ZULIP_CA_BUNDLE':ca,'ZULIP_BRIDGE_STATE':'/var/lib/ops-native-approval/state.db','OPS_BROKER_SOCKET':'/run/ops-broker/api.sock','ALERTMANAGER_QUERY_SOCKET':'/run/zulip-alertmanager-query/api.sock'}
        subscriptions=admin.call('GET','/api/v1/users/me/subscriptions')['subscriptions']
        for purpose,name,topic in [('APPROVAL','ops-approbations','approbations'),('ALERT','ops-alertes','alertes'),('DAILY','ops-rapports','quotidien')]:
            found=[s for s in subscriptions if s['name']==name]
            if found and not found[0]['invite_only']:raise RuntimeError('channel must be private')
            admin.call('POST','/api/v1/users/me/subscriptions',{'subscriptions':[{'name':name}],'invite_only':True,'principals':[8,me['user_id']]})
            sid=admin.call('GET','/api/v1/get_stream_id',{'stream':name})['stream_id']
            data.update({f'ZULIP_{purpose}_STREAM':name,f'ZULIP_{purpose}_STREAM_ID':str(sid),f'ZULIP_{purpose}_TOPIC':topic})
        path='kv-infra-shared/data/zulip/native-approval'
        # Dedicated path: never overwrite an existing configuration silently.
        import urllib.error
        try:
            prior=vault.call('GET',path)
            if prior['data']['data']!=data:raise RuntimeError('existing approval configuration differs')
            prior.clear()
        except urllib.error.HTTPError as e:
            if e.code!=404:raise
            vault.call('POST',path,{'options':{'cas':0},'data':data})
        data.clear();bot.clear()
        role='ops-native-approval'
        vault.call('PUT','sys/policies/acl/'+role,{'policy':'path "kv-infra-shared/data/zulip/native-approval" { capabilities = ["read"] }'})
        vault.call('POST','auth/approle/role/'+role,{'token_policies':[role],'token_period':'15m','secret_id_ttl':0,'secret_id_num_uses':0,'secret_id_bound_cidrs':['127.0.0.1/32'],'token_bound_cidrs':['127.0.0.1/32']})
        files=[Path('/etc/credstore.encrypted')/(role+'-'+x) for x in ['role-id','secret-id']]
        if any(p.exists() for p in files) and not all(p.exists() for p in files):raise RuntimeError('partial credentials require reconciliation')
        if not all(p.exists() for p in files):
            rid=vault.call('GET','auth/approle/role/'+role+'/role-id')['data']['role_id']
            sid=vault.call('POST','auth/approle/role/'+role+'/secret-id',{})['data']['secret_id']
            lib['seal'](role+'-role-id',rid);lib['seal'](role+'-secret-id',sid);rid=sid=''
        policy=Path('/etc/ops-broker/actions.yaml');original=policy.read_text();metadata=policy.stat();parsed=yaml.safe_load(original)
        actors=parsed['zulip']['authorized_actor_ids']
        if actors not in [[],['zulip:8']]:raise RuntimeError('unexpected existing approvers')
        if not actors:
            backup=Path('/etc/ops-native/actions.before-native-approval.yaml')
            if not backup.exists():write(backup,original)
            parsed['zulip']['authorized_actor_ids']=['zulip:8']
            # Validate with the installed broker before atomically publishing.
            candidate=policy.with_name('actions.native-next.yaml');write(candidate,yaml.safe_dump(parsed,sort_keys=False),0o640)
            os.chown(candidate,metadata.st_uid,metadata.st_gid)
            validator=runpy.run_path(str(ROOT/'scripts/reconcile-broker-zulip-approver'))
            validator['verify_policy'].__globals__['INSTALLED_POLICY']=candidate
            validator['verify_policy'](candidate.read_bytes(),'zulip:8')
            os.replace(candidate,policy)
        directory=Path('/etc/ops-native-approval');directory.mkdir(exist_ok=True,mode=0o755);directory.chmod(0o755)
        write(directory/'agent.hcl',(DEPLOY/'approval-agent.hcl').read_text(),0o644)
        for name in ['ops-native-approval-secrets.service','ops-native-approval.service']:
            write('/etc/systemd/system/'+name,(DEPLOY/name).read_text(),0o644)
        run('/usr/bin/systemd-analyze','verify','/etc/systemd/system/ops-native-approval-secrets.service','/etc/systemd/system/ops-native-approval.service')
        # Fedora's stock proxy domain needs the reviewed NNP transition and
        # connect permission; keep it confined and restrict this unit to loopback.
        proxy_policy=directory/'ops_native_socket_proxy.cil'
        desired=(DEPLOY/'ops_native_socket_proxy.cil').read_text()
        if not proxy_policy.exists() or proxy_policy.read_text()!=desired:
            write(proxy_policy,desired,0o644)
            run('/usr/bin/semodule','-i',str(proxy_policy))
        dropin=Path('/etc/systemd/system/zulip-alertmanager-query.service.d/native-start.conf')
        write(dropin,'[Service]\nType=exec\nIPAddressDeny=any\nIPAddressAllow=localhost\n',0o644)
        run('/usr/bin/systemctl','daemon-reload')
        run('/usr/bin/systemctl','reset-failed','ops-native-approval-secrets.service')
        run('/usr/bin/systemctl','enable','ops-native-approval-secrets.service')
        run('/usr/bin/systemctl','restart','ops-native-approval-secrets.service')
        for _ in range(30):
            if Path('/run/ops-native-approval/bridge.json').exists():break
            time.sleep(1)
        else:raise RuntimeError('Agent template not ready')
        run('/usr/bin/systemctl','enable','--now','zulip-alertmanager-query.socket')
        run('/usr/bin/systemctl','restart','ops-broker.service')
        run('/usr/bin/systemctl','reset-failed','ops-native-approval.service')
        run('/usr/bin/systemctl','enable','--now','ops-native-approval.service')
        print('NATIVE_APPROVAL_BRIDGE_STARTED owner=8',flush=True)
    finally:
        values.clear()
        if vault.token:
            vault.call('POST','auth/token/revoke-self',{});vault.token=''
if __name__=='__main__':
    try:main()
    except Exception as e:
        import traceback
        print('APPROVAL_INSTALL_STOPPED',type(e).__name__,getattr(e,'code',''),[(f.name,f.lineno) for f in traceback.extract_tb(e.__traceback__)],flush=True);raise SystemExit(1)
