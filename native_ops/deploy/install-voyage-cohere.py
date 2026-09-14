"""Owner-authorized local provider migration; no inference and no provider-key input."""
import hashlib,json,os,runpy,shutil,subprocess,sys,time,tomllib
from pathlib import Path
ROOT=Path('/home/ops-user/ops-control-plane')
RELEASE='ops-native-2026.09.12.4'

def main():
    assert os.geteuid()==0
    os.umask(0o022)
    assert runpy.run_path(str(ROOT/'native_ops/release.py'))['verify'](ROOT)['release_id']==RELEASE
    config=Path('/etc/ops-memory/memory.toml');before=config.read_text()
    assert all(not tomllib.loads(before)[s]['api']['enabled'] for s in ['embedding','reranker']), 'Active provider migration needs review'
    archive=Path('/var/lib/ops-native-archives')/('voyage-cohere-'+str(time.time_ns()))
    archive.mkdir(mode=0o700)
    native=Path('/opt/ops-native/venv/lib64/python3.14/site-packages/ops_native')
    sources={
        str(native/name): ROOT/'native_ops/ops_native'/name
        for name in ['memory_capabilities.py','memory_secrets.py','memory_models.py']}
    sources.update({str(Path('/opt/ops-memory/src/ops_memory')/name):ROOT/'memory/src/ops_memory'/name
        for name in ['embedding.py','config.py','semantic_index.py','service.py']})
    sources.update({'/usr/local/libexec/ops-model-key-manager':ROOT/'scripts/ops-model-key-manager',
                    '/usr/local/bin/ops-memory-models':ROOT/'native_ops/deploy/ops-memory-models'})
    prior={}
    for target in [*sources,str(config)]:
        p=Path(target);assert not p.is_symlink()
        if p.exists():
            st=p.stat();saved=archive/str(len(prior));shutil.copy2(p,saved)
            prior[target]=(str(saved),st.st_uid,st.st_gid,st.st_mode&0o777)
        else:prior[target]=None
    (archive/'prior.json').write_text(json.dumps(prior))
    def run(*args):return subprocess.run(args,check=True,capture_output=True,timeout=90)
    def write(p,raw,uid=0,gid=0,mode=0o644):
        p=Path(p);tmp=p.with_name(p.name+'.voyage-next')
        with tmp.open('xb') as f:f.write(raw);f.flush();os.fsync(f.fileno())
        os.chown(tmp,uid,gid);os.chmod(tmp,mode);os.replace(tmp,p)
    vault=runpy.run_path(str(ROOT/'native_ops/deploy/provider-secrets.py'))['Vault']()
    cred=Path('/var/lib/atlas-local-repair/credentials.json');st=cred.lstat()
    assert st.st_uid==0 and not st.st_mode&0o077 and not cred.is_symlink()
    values=json.loads(cred.read_bytes());password=values['openbao_louis'];values.clear()
    login=vault.call('POST','auth/userpass/login/ops-user',{'password':password});password=''
    vault.token=login['auth']['client_token'];login.clear()
    policy_path='sys/policies/acl/ops-memory-api'
    old_policy=vault.call('GET',policy_path)['data']['policy']
    (archive/'prior-policy.txt').write_text(old_policy)
    policy_changed=False
    try:
        # Owner key-entry permissions already cover provider references; verify only.
        paths=['kv-infra-shared/data/llm/providers/'+p for p in ['voyage','cohere']]
        for path in paths:
            caps=vault.call('POST','sys/capabilities-self',{'paths':[path]})
            allowed=caps.get(path,caps.get('capabilities',[]))
            assert 'root' in allowed or {'create','update'} <= set(allowed)
        run('/usr/bin/systemctl','stop','ops-memory-api-secrets.timer','ops-memory-api-secrets.service',
            'ops-native-capabilities.timer','ops-native-capabilities.service')
        for target,source in sources.items():
            previous=prior[target]
            mode=previous[3] if previous else (0o755 if target.startswith('/usr/local/') else 0o644)
            write(target,source.read_bytes(),*(previous[1:3] if previous else (0,0)),mode)
        sys.path.insert(0,str(ROOT/'native_ops'))
        from ops_native.memory_models import render
        rendered=render(before,'voyage-4-lite','rerank-v4.0-fast',migrate=True)
        _,uid,gid,mode=prior[str(config)];write(config,rendered.encode(),uid,gid,mode)
        policy='\n'.join('path "'+p+'" { capabilities = ["read"] }' for p in paths)
        vault.call('PUT',policy_path,{'policy':policy});policy_changed=True
        run('/usr/bin/systemctl','restart','ops-memory.service')
        run('/usr/bin/systemctl','start','ops-memory-api-secrets.service')
        current=tomllib.loads(config.read_text())
        assert all(not current[s]['api']['enabled'] for s in ['embedding','reranker']), 'Unexpected provider key appeared'
        run('/usr/bin/systemctl','start','ops-memory-api-secrets.timer','ops-native-capabilities.timer')
        run('/usr/bin/systemctl','is-active','--quiet','ops-memory.service')
        output=run('/usr/local/bin/ops-memory-models').stdout
        result=json.loads(output)
        receipt={'release':RELEASE,'archive':str(archive),'models':result,'provider_calls':False,
                 'real_api_acceptance':'awaiting_owner_keys','installed_at':int(time.time())}
        published=Path('/usr/local/share/ops-native/releases')/(RELEASE+'.json')
        assert not published.exists()
        write(published,(ROOT/'native_ops/releases/current.json').read_bytes(),mode=0o444)
        write(published.with_name(RELEASE+'-install.json'),(json.dumps(receipt,indent=2)+'\n').encode(),mode=0o444)
        print(json.dumps(receipt))
    except BaseException:
        for target,previous in prior.items():
            if previous:
                saved,uid,gid,mode=previous;write(target,Path(saved).read_bytes(),uid,gid,mode)
            else:Path(target).unlink(missing_ok=True)
        if policy_changed:vault.call('PUT',policy_path,{'policy':old_policy})
        run('/usr/bin/systemctl','restart','ops-memory.service')
        run('/usr/bin/systemctl','start','ops-memory-api-secrets.timer','ops-native-capabilities.timer')
        raise
    finally:
        vault.call('POST','auth/token/revoke-self',{});vault.token=''

if __name__=='__main__':main()
