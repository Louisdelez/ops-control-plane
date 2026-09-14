"""Fixed server-side publication transaction; inputs are root-owned local assets."""
from pathlib import Path
import hashlib,json,os,shutil,subprocess,sys,time,socket

EXPECTED_HAPROXY='6d7bd245765b065756053c0d3612def35c653bf72db0082298dc3ac880ce572f'
BASE=Path('/var/lib/ops-zulip-public-v3')
RULE=['allow','in','on','wg0','proto','tcp','from','198.51.100.1','to','198.51.100.2','port','18443','comment','ops-zulip-v3']

def haproxy_patch(text):
    if 'ops_zulip' in text:raise ValueError('Zulip route already exists')
    lines=text.splitlines(keepends=True)
    indices=[i for i,l in enumerate(lines) if l.strip()=='default_backend nas']
    if len(indices)!=1:raise ValueError('Unexpected ingress topology')
    i=indices[0]
    lines.insert(i,'    acl h_ops_zulip req.ssl_sni -i zulip.example.org\n    use_backend ops_zulip if h_ops_zulip\n')
    return ''.join(lines)+'\nbackend ops_zulip\n    mode tcp\n    server zulip 127.0.0.1:8546 send-proxy-v2\n'

def run(args,timeout=30):
    r=subprocess.run(args,capture_output=True,timeout=timeout,umask=0o022)
    if r.returncode:raise RuntimeError('Command failed: '+Path(args[0]).name)
    return r.stdout

def main(payload):
    if os.geteuid()!=0:raise ValueError('Root required')
    role=payload['role'];operation=payload['operation']
    if role not in {'nas','edge-vps'} or operation not in {'prepare','finish','rollback'}:raise ValueError('Invalid operation')
    os.umask(0o077);BASE.mkdir(mode=0o700,exist_ok=True)
    statefile=BASE/'state.json'
    if operation=='prepare':
        previous=Path('/var/lib/ops-zulip-public-v2/state.json')
        if not previous.exists() or json.loads(previous.read_text()).get('phase')!='rolled_back':
            raise ValueError('Prior transaction must be rolled back')
        if statefile.exists():raise ValueError('Existing transaction requires review')
        state={'role':role,'started_at':time.time(),'prior':{},'phase':'preparing'}
        statefile.write_text(json.dumps(state))
    else:
        state=json.loads(statefile.read_text());assert state['role']==role
    def save():statefile.write_text(json.dumps(state))
    def write(name,content,mode=0o644):
        p=Path(name)
        if name not in state['prior']:
            if p.exists() or p.is_symlink():
                if name!='/etc/haproxy/haproxy.cfg' or p.is_symlink():raise ValueError('Destination already exists')
                backup=BASE/('before-'+str(len(state['prior'])));shutil.copy2(p,backup);state['prior'][name]=str(backup)
            else:state['prior'][name]=None
            save()
        p.parent.mkdir(parents=True,exist_ok=True)
        staged=p.with_name(p.name+'.ops-next')
        with staged.open('xb') as f:f.write(content.encode());f.flush();os.fsync(f.fileno())
        staged.chmod(mode);os.replace(staged,p)
        state.setdefault('written_sha256',{})[name]=hashlib.sha256(p.read_bytes()).hexdigest();save()
    if operation=='rollback':
        if state['phase']=='rolled_back':return {'status':'rolled_back','role':role}
        if role=='nas':
            if state.get('firewall_rule_attempted'):
                run(['/usr/sbin/ufw','--force','delete',*RULE])
                state['firewall_rule_attempted']=False;save()
            subprocess.run(['systemctl','disable','--now','ops-zulip-relay.socket'],capture_output=True)
            subprocess.run(['systemctl','stop','ops-zulip-relay.service'],capture_output=True)
        for name,backup in reversed(list(state['prior'].items())):
            p=Path(name);expected=state.get('written_sha256',{}).get(name)
            allowed={expected}
            if backup:allowed.add(hashlib.sha256(Path(backup).read_bytes()).hexdigest())
            if p.exists() and hashlib.sha256(p.read_bytes()).hexdigest() not in allowed:raise ValueError('Rollback drift requires review')
            if backup:shutil.copy2(backup,name)
            else:Path(name).unlink(missing_ok=True)
        run(['systemctl','daemon-reload'])
        if role=='edge-vps':
            run(['/usr/sbin/nginx','-t']);run(['systemctl','reload','nginx'])
            run(['/usr/sbin/haproxy','-c','-f','/etc/haproxy/haproxy.cfg']);run(['systemctl','reload','haproxy'])
        state['phase']='rolled_back';save();return {'status':'rolled_back','role':role}
    files=payload['files']
    if not isinstance(files,dict) or sum(len(v) for v in files.values())>65536:raise ValueError('Invalid assets')
    if role=='nas':
        assert operation=='prepare' and set(files)=={'ops-zulip-relay.socket','ops-zulip-relay.service'}
        status=run(['/usr/sbin/ufw','status']).decode()
        if 'Status: active' not in status or '18443' in status:raise ValueError('Unexpected NAS firewall state')
        for name,content in files.items():write('/etc/systemd/system/'+name,content)
        state['firewall_rule_attempted']=True;save()
        run(['/usr/sbin/ufw',*RULE])
        if '18443' not in run(['/usr/sbin/ufw','status']).decode():raise ValueError('Firewall rule missing')
        run(['systemctl','daemon-reload']);run(['systemctl','enable','--now','ops-zulip-relay.socket'])
        run(['systemctl','is-active','--quiet','ops-zulip-relay.socket'])
        with socket.create_connection(('127.0.0.1',17443),timeout=10) as connection:
            pass
        state['phase']='prepared';save()
    elif operation=='prepare':
        previous=Path('/var/lib/ops-zulip-public-v2/state.json')
        if not previous.exists() or json.loads(previous.read_text()).get('phase')!='rolled_back':
            raise ValueError('Prior transaction must be rolled back')
        assert set(files)=={'zulip-http.conf'}
        assert hashlib.sha256(Path('/etc/haproxy/haproxy.cfg').read_bytes()).hexdigest()==EXPECTED_HAPROXY
        assert any(Path('/etc/letsencrypt/accounts/acme-v02.api.letsencrypt.org/directory').glob('*/regr.json'))
        webroot=Path('/var/lib/ops-zulip-acme')
        assert not webroot.is_symlink()
        webroot.mkdir(mode=0o755,exist_ok=True);webroot.chmod(0o755)
        write('/etc/nginx/conf.d/ops-zulip-http.conf',files['zulip-http.conf'])
        run(['/usr/sbin/nginx','-t']);run(['systemctl','reload','nginx'])
        run(['/usr/bin/certbot','certonly','--non-interactive','--webroot','-w','/var/lib/ops-zulip-acme',
             '--cert-name','ops-zulip-delez-ovh','-d','zulip.example.org'],timeout=120)
        state['phase']='certificate_ready';save()
    else:
        assert state['phase']=='certificate_ready' and set(files)=={'zulip-https.conf','renew-hook','upstream-ca.crt'}
        assert hashlib.sha256(Path('/etc/haproxy/haproxy.cfg').read_bytes()).hexdigest()==EXPECTED_HAPROXY
        write('/etc/nginx/ops-zulip-upstream-ca.crt',files['upstream-ca.crt'])
        write('/etc/nginx/conf.d/ops-zulip-https.conf',files['zulip-https.conf'])
        write('/etc/letsencrypt/renewal-hooks/deploy/ops-zulip-nginx',files['renew-hook'],0o755)
        run(['/usr/sbin/nginx','-t']);run(['systemctl','reload','nginx'])
        write('/etc/haproxy/haproxy.cfg',haproxy_patch(Path('/etc/haproxy/haproxy.cfg').read_text()))
        run(['/usr/sbin/haproxy','-c','-f','/etc/haproxy/haproxy.cfg']);run(['systemctl','reload','haproxy'])
        state['phase']='published';save()
    return {'role':role,'status':state['phase'],'archive':str(BASE),'deleted_user_data':False}

if __name__=='__main__':
    try:
        raw=sys.stdin.buffer.read(131073)
        if len(raw)>131072:raise ValueError('Input limit')
        print(json.dumps(main(json.loads(raw))))
    except Exception as e:
        print(json.dumps({'status':'failed','category':type(e).__name__}));raise SystemExit(1) from None
