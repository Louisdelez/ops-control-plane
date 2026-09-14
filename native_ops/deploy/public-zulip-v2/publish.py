"""One approved fixed publication of zulip.example.org with audited rollback."""
from pathlib import Path
import hashlib,json,os,runpy,shlex,shutil,signal,ssl,subprocess,sys,time,urllib.request
ASSETS=Path('/usr/local/libexec/ops-zulip-public-20260912-2')
STATE=Path('/var/lib/ops-zulip-public-v2')
COMPOSE=Path('/opt/zulip/compose.override.yaml')

def override_patch(text):
    old='      SETTING_EXTERNAL_HOST: "zulip.ops.local:8443"'
    if text.count(old)!=1 or any(x in text for x in ['SETTING_ALLOWED_HOSTS','TRUST_GATEWAY_IP','SETTING_CSRF_TRUSTED_ORIGINS']):
        raise ValueError('Unexpected Zulip configuration')
    new='''      SETTING_EXTERNAL_HOST: "zulip.example.org"
      SETTING_ALLOWED_HOSTS: "['zulip.ops.local']"
      SETTING_CSRF_TRUSTED_ORIGINS: "['https://zulip.ops.local:8443', 'https://zulip.example.org']"
      TRUST_GATEWAY_IP: "True"'''
    return text.replace(old,new)

def run(args,timeout=45):
    r=subprocess.run(args,capture_output=True,timeout=timeout)
    if r.returncode:raise RuntimeError('Command failed: '+Path(args[0]).name)
    return r.stdout

def remote(role,operation,names=()):
    files={name:(ASSETS/name).read_text() for name in names}
    if role=='edge-vps' and operation=='finish':
        files['upstream-ca.crt']=Path('/etc/pki/ca-trust/source/anchors/zulip-standard-local.crt').read_text()
    payload=json.dumps({'role':role,'operation':operation,'files':files}).encode()
    command=runpy.run_path('/usr/local/libexec/ops-runbooks/remote-preflight-worker')['command'](role)
    command[-1]='sudo -n /usr/bin/python3 -c '+shlex.quote((ASSETS/'remote.py').read_text())
    r=subprocess.run(command,input=payload,capture_output=True,timeout=160)
    if r.returncode:raise RuntimeError('Remote '+role+' '+operation+' failed')
    result=json.loads(r.stdout[:4096]);assert result['status'] in {'prepared','certificate_ready','published','rolled_back'}
    return result

def userctl(*args,check=True):
    result=subprocess.run(['/usr/sbin/runuser','-u','ops-user','--','/usr/bin/env',
      'XDG_RUNTIME_DIR=/run/user/1000','/usr/bin/systemctl','--user',*args],capture_output=True,timeout=30)
    if check and result.returncode:raise RuntimeError('User service operation failed')
    return result

def compose_up():
    run(['docker','compose','--project-directory','/opt/zulip','-f','/opt/zulip/compose.yaml','-f',str(COMPOSE),
         'up','-d','--no-deps','--pull','never','zulip'],timeout=180)

def check_url(url,local=False):
    context=ssl.create_default_context(cafile='/etc/pki/ca-trust/source/anchors/zulip-standard-local.crt') if local else ssl.create_default_context()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context))
    with opener.open(url,timeout=10) as r:
        value=json.loads(r.read(131072));assert r.status==200 and value.get('result')=='success'
        assert value.get('realm_uri')=='https://zulip.example.org'
    return True

def recover():
    statefile=STATE/'state.json'
    if not statefile.exists():return
    state=json.loads(statefile.read_text())
    if state['phase'] in {'published_and_verified','rolled_back'}:return
    errors=[]
    for role in reversed(state.get('remote_started',[])):
        try:remote(role,'rollback')
        except Exception:errors.append(role)
    userctl('disable','--now','ops-zulip-tunnel.service',check=False)
    for name,backup in reversed(list(state.get('local_prior',{}).items())):
        p=Path(name);allowed={state.get('written_sha256',{}).get(name)}
        if backup:allowed.add(hashlib.sha256(Path(backup).read_bytes()).hexdigest())
        if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() not in allowed:
            errors.append('local_drift');continue
        if backup:shutil.copy2(backup,name)
        else:Path(name).unlink(missing_ok=True)
    run(['systemctl','daemon-reload']);userctl('daemon-reload')
    if str(COMPOSE) in state.get('local_prior',{}):
        try:compose_up()
        except Exception:errors.append('local_zulip')
    state.update(phase='rollback_requires_review' if errors else 'rolled_back',rollback_errors=errors)
    statefile.write_text(json.dumps(state,indent=2))
    if errors:raise RuntimeError('Rollback requires review')


def main():
    assert os.geteuid()==0
    os.umask(0o077);STATE.mkdir(mode=0o700,exist_ok=True)
    statefile=STATE/'state.json'
    if statefile.exists():raise ValueError('Existing publication transaction requires review')
    patched=override_patch(COMPOSE.read_text())
    manifest=json.loads((ASSETS/'assets.json').read_text())
    for name,digest in manifest.items():assert hashlib.sha256((ASSETS/name).read_bytes()).hexdigest()==digest
    state={'phase':'preparing','started_at':time.time(),'remote_started':[],'local_prior':{},'receipts':[]}
    def save():statefile.write_text(json.dumps(state,indent=2))
    def write(path,content,mode):
        p=Path(path)
        if str(p) not in state['local_prior']:
            if p.exists():
                if p!=COMPOSE or p.is_symlink():raise ValueError('Existing local destination')
                old=STATE/('before-'+str(len(state['local_prior'])));shutil.copy2(p,old);state['local_prior'][str(p)]=str(old)
            else:state['local_prior'][str(p)]=None
            save()
        p.parent.mkdir(parents=True,exist_ok=True)
        if str(p.parent)=='/etc/ops-zulip-public':p.parent.chmod(0o755)
        temp=p.with_name(p.name+'.ops-next')
        with temp.open('xb') as f:f.write(content.encode());f.flush();os.fsync(f.fileno())
        temp.chmod(mode);os.replace(temp,p)
        state.setdefault('written_sha256',{})[str(p)]=hashlib.sha256(p.read_bytes()).hexdigest();save()
    def terminate(signum,frame):raise RuntimeError('Publication interrupted')
    signal.signal(signal.SIGTERM,terminate)
    save()
    try:
        write('/etc/ops-zulip-public/ssh.conf',(ASSETS/'ssh.conf').read_text(),0o644)
        write('/etc/systemd/user/ops-zulip-tunnel.service',(ASSETS/'ops-zulip-tunnel.service').read_text(),0o644)
        userctl('daemon-reload');userctl('enable','--now','ops-zulip-tunnel.service')
        time.sleep(3)
        userctl('is-active','--quiet','ops-zulip-tunnel.service')
        state['remote_started'].append('nas');save()
        state['receipts'].append(remote('nas','prepare',['ops-zulip-relay.socket','ops-zulip-relay.service']));save()
        state['remote_started'].append('edge-vps');save()
        state['receipts'].append(remote('edge-vps','prepare',['zulip-http.conf']));save()
        state['phase']='migrating_canonical_host';save()
        write(COMPOSE,patched,0o600);compose_up()
        for attempt in range(24):
            try:check_url('https://zulip.ops.local:8443/api/v1/server_settings',local=True);break
            except Exception:
                if attempt==23:raise
                time.sleep(5)
        state['receipts'].append(remote('edge-vps','finish',['zulip-https.conf','renew-hook']));save()
        check_url('https://zulip.example.org/api/v1/server_settings')
        state.update(phase='published_and_verified',finished_at=time.time(),local_api_verified=True,public_tls_api_verified=True)
        save();print(json.dumps({'status':state['phase'],'url':'https://zulip.example.org','client_vpn_required':False,'state':str(statefile)}))
    except BaseException as error:
        state['failure_category']=type(error).__name__
        state['failure_http_status']=getattr(error,'code',None)
        state['phase']='rolling_back';save();errors=[]
        for role in reversed(state['remote_started']):
            try:remote(role,'rollback')
            except Exception:errors.append(role)
        userctl('disable','--now','ops-zulip-tunnel.service',check=False)
        for name,backup in reversed(list(state['local_prior'].items())):
            if backup:shutil.copy2(backup,name)
            else:Path(name).unlink(missing_ok=True)
        run(['systemctl','daemon-reload']);userctl('daemon-reload')
        if str(COMPOSE) in state['local_prior']:
            try:compose_up()
            except Exception:errors.append('local_zulip')
        state.update(phase='rollback_requires_review' if errors else 'rolled_back',rollback_errors=errors);save()
        print(json.dumps({'status':state['phase'],'errors':errors,'state':str(statefile)}));raise SystemExit(1) from None

if __name__=='__main__':
    try:
        assert os.geteuid()==0
        if sys.argv[1:]==['--recover']:recover()
        elif not sys.argv[1:]:main()
        else:raise ValueError('Invalid arguments')
    except Exception as e:print(json.dumps({'status':'failed_preflight','category':type(e).__name__}));raise SystemExit(1) from None
