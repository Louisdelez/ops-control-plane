"""Isolated fresh-node restore drill. Normal restore is the default.

The isolated-force option is prepared for explicit procedure review only:
the current local recovery runbook prohibits its execution without escalation.
No request can reach the host OpenBao: require a loopback-only network namespace.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

BASE='http://127.0.0.1:18420/v1/'
IDENTITY='/home/ops-user/Documents/Recuperation-Ops-2026-09-11/PRIVE/cle-age.txt'
REPORT=Path('/var/lib/ops-bao-restore-check')


def api(path, payload=None, token=None, raw=None):
    headers={'Content-Type':'application/json'}
    if token:headers['X-Vault-Token']=token
    data=raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
    request=urllib.request.Request(BASE+path,data=data,headers=headers)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request,timeout=10) as response:
            return response.status,json.loads(response.read(2*1024*1024) or b'{}')
    except urllib.error.HTTPError as error:
        return error.code,json.loads(error.read(2*1024*1024) or b'{}')


def wait_ready(path='sys/init', token=None):
    for _ in range(40):
        try:
            code,value=api(path,token=token)
            if code==200:return value
        except (OSError,ValueError):pass
        time.sleep(.5)
    raise ValueError('Isolated instance did not become ready')


def main(force, snapshot_dir=None, shares_file=None):
    if os.geteuid()!=0 or {name for _,name in socket.if_nameindex()}!={'lo'}:
        raise ValueError('Isolated loopback namespace required')
    if os.readlink('/proc/self/ns/net')==os.readlink('/proc/1/ns/net'):
        raise ValueError('Host network namespace forbidden')
    os.umask(0o077)
    REPORT.mkdir(mode=0o700,exist_ok=True)
    snapshot=Path(snapshot_dir) if snapshot_dir else max(Path('/var/backups/openbao').glob('openbao-raft-*'),key=lambda p:p.stat().st_mtime)
    shares_path=Path(shares_file) if shares_file else Path('/usr/local/share/ops-native/recovery-seed/unseal-shares.age')
    for p in ([snapshot] if snapshot_dir else [])+([shares_path] if shares_file else []):
        if p.resolve()!=p.absolute() or p.stat().st_uid!=0 or p.stat().st_mode & 0o022:
            raise ValueError('Unsafe recovery input')
    manifest=json.loads((snapshot/'manifest.json').read_text())
    with (snapshot/'snapshot.age').open('rb') as source:
        digest=hashlib.file_digest(source,'sha256').hexdigest()
    if digest!=manifest['artifact']['sha256'] or time.time()-snapshot.stat().st_mtime>30*3600:
        raise ValueError('Snapshot provenance check failed')
    report={'checked_at':time.time(),'snapshot_id':snapshot.name,'snapshot_sha256':digest,
            'network_isolated':True,'host_vault_contacted':False,'force_requested':force,
            'full_restore_verified':False}
    runtime=Path('/run/ops-bao-restore-check')
    runtime.mkdir(mode=0o700,exist_ok=True)
    if runtime.resolve()!=runtime or runtime.stat().st_uid!=0 or runtime.stat().st_mode & 0o077:
        raise ValueError('Private restore runtime required')
    with tempfile.TemporaryDirectory(prefix='drill-',dir='/run/ops-bao-restore-check') as raw:
        work=Path(raw);(work/'raft').mkdir(mode=0o700)
        config=work/'server.hcl'
        config.write_text('disable_mlock = true\n'
            'api_addr = "http://127.0.0.1:18420"\ncluster_addr = "https://127.0.0.1:18421"\n'
            'listener "tcp" { address = "127.0.0.1:18420"\n tls_disable = true }\n'
            'storage "raft" { path = "'+str(work/'raft')+'"\n node_id = "isolated-recovery-drill"\n performance_multiplier = 1 }\n')
        with subprocess.Popen(['/usr/bin/bao','server','-config='+str(config)],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            env={'PATH':'/usr/bin:/bin','HOME':str(work),'TMPDIR':str(work)}) as server:
            try:
                report['stage']='initialize'
                wait_ready()
                code,initialized=api('sys/init',{'secret_shares':1,'secret_threshold':1})
                if code!=200:raise ValueError('Isolated initialization failed')
                code,_=api('sys/unseal',{'key':initialized['keys_base64'][0]})
                if code!=200:raise ValueError('Isolated unseal failed')
                wait_ready('sys/health')
                decrypted=subprocess.run(['/usr/bin/age','-d','-i',IDENTITY,str(snapshot/'snapshot.age')],
                    check=True,capture_output=True,timeout=30).stdout
                endpoint='sys/storage/raft/snapshot-force' if force else 'sys/storage/raft/snapshot'
                report['stage']='restore_snapshot'
                code,response=api(endpoint,token=initialized['root_token'],raw=decrypted)
                del decrypted
                report['restore_http_status']=code
                if code not in (200,204):
                    message=str(response).lower()
                    report['seal_mismatch_reported']=any(word in message for word in ('shamir','seal','decrypt'))
                    report['failure_categories']=[label for text,label in (
                        ('read-only','filesystem_read_only'),('permission denied','permission_denied'),
                        ('shamir','seal_mismatch'),('decrypt','decryption_mismatch'),
                        ('checksum','snapshot_checksum'),('unexpected eof','snapshot_truncated')) if text in message]
                    report['status']='procedure_review_required' if not force and report['seal_mismatch_reported'] else 'restore_failed'
                else:
                    # Restart the disposable node to reload restored seal metadata.
                    server.terminate();server.wait(timeout=10)
                    server=subprocess.Popen(['/usr/bin/bao','server','-config='+str(config)],
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                        env={'PATH':'/usr/bin:/bin','HOME':str(work),'TMPDIR':str(work)})
                    wait_ready()
                    # Use only the recovered shares inside this isolated process.
                    shares=json.loads(subprocess.run(['/usr/bin/age','-d','-i',IDENTITY,
                        str(shares_path)],
                        check=True,capture_output=True,timeout=30).stdout)
                    time.sleep(1)
                    for share in shares['unseal_keys_b64']:
                        api('sys/unseal',{'key':share})
                    del shares
                    report['stage']='recovered_health'
                    health=wait_ready('sys/health')
                    report['stage']='owner_authentication'
                    password=json.loads(Path('/var/lib/atlas-local-repair/credentials.json').read_text())['openbao_louis']
                    code,login=api('auth/userpass/login/ops-user',{'password':password});del password
                    if code!=200:raise ValueError('Recovered owner authentication failed')
                    token=login['auth']['client_token']
                    code,mounts=api('sys/mounts',token=token)
                    report['owner_authentication_verified']=True
                    report['expected_mount_present']=code==200 and 'kv-infra-shared/' in mounts.get('data',mounts)
                    report['full_restore_verified']=report['expected_mount_present'] and health.get('sealed') is False
                    report['status']='restored' if report['full_restore_verified'] else 'restore_failed'
                    api('auth/token/revoke-self',{},token=token)
            except Exception as error:
                report['status']='verification_failed'
                report['exception_type']=type(error).__name__
                try:
                    code,seal=api('sys/seal-status')
                    report['seal_state']={k:seal.get(k) for k in ('sealed','t','n','progress','initialized')}
                except Exception:pass
            finally:
                server.terminate()
                try:server.wait(timeout=10)
                except subprocess.TimeoutExpired:server.kill();server.wait()
    destination=REPORT/('forced.json' if force else 'normal.json')
    if destination.exists():
        shutil.copyfile(destination,REPORT/(destination.stem+'-previous-'+str(time.time_ns())+'.json'))
    destination.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))
    return 0 if report.get('full_restore_verified') else 2


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--isolated-force',action='store_true')
    parser.add_argument('--snapshot-dir',type=Path)
    parser.add_argument('--shares-file',type=Path)
    args=parser.parse_args()
    try:raise SystemExit(main(args.isolated_force,args.snapshot_dir,args.shares_file))
    except Exception:raise SystemExit('Isolated OpenBao recovery verification failed; no secret output') from None
