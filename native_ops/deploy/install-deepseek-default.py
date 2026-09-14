"""Owner-authorized local provider switch with archive and rollback, no API calls."""
import json,os,runpy,shutil,subprocess,time
from pathlib import Path
ROOT=Path('/home/ops-user/ops-control-plane')
def run(*args):
    subprocess.run(args,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=60)
def main():
    assert os.geteuid()==0
    os.umask(0o077)
    archive=Path('/var/lib/ops-native-archives')/('deepseek-default-'+str(time.time_ns()))
    archive.mkdir(parents=True,mode=0o700)
    mapping={
      '/etc/ops-native-model/config.json':'native_ops/deploy/model-config.json',
      '/etc/ops-native-model/agent.hcl':'native_ops/deploy/agent.hcl',
      '/etc/systemd/system/ops-native-model.service':'native_ops/deploy/ops-native-model.service',
      '/usr/local/libexec/ops-execution-mode':'native_ops/deploy/ops-execution-mode',
      '/opt/ops-native/venv/lib64/python3.14/site-packages/ops_native/model_proxy.py':'native_ops/ops_native/model_proxy.py',
    }
    for i,target in enumerate(mapping):
        p=Path(target);assert p.is_file() and not p.is_symlink()
        shutil.copy2(p,archive/str(i))
    vault=runpy.run_path(str(ROOT/'native_ops/deploy/provider-secrets.py'))['Vault']()
    values=json.loads(Path('/var/lib/atlas-local-repair/credentials.json').read_bytes())
    login=vault.call('POST','auth/userpass/login/ops-user',{'password':values['openbao_louis']})
    values.clear();vault.token=login['auth']['client_token'];login.clear()
    policy=vault.call('GET','sys/policies/acl/ops-native-api')['data']['policy']
    (archive/'policy.hcl').write_text(policy)
    try:
        run('systemctl','stop','ops-native-model.service','ops-native-secrets.service')
        for target,source in mapping.items():
            p=Path(target);st=p.stat();temp=p.with_name(p.name+'.deepseek-next')
            with temp.open('xb') as f:f.write((ROOT/source).read_bytes())
            os.chmod(temp,st.st_mode & 0o777);os.chown(temp,st.st_uid,st.st_gid);os.replace(temp,p)
        vault.call('PUT','sys/policies/acl/ops-native-api',{'policy':'path "kv-infra-shared/data/llm/deepseek" { capabilities = ["read"] }'})
        run('systemctl','daemon-reload')
        run('systemctl','restart','ops-native-secrets.service')
        # The normal capability detector activates generation only once the owner key exists.
        run('/usr/local/libexec/ops-execution-mode','auto')
        config=json.loads(Path('/etc/ops-native-model/config.json').read_text())
        enabled=[p['model'] for ps in config['roles'].values() for p in ps if p['enabled']]
        assert enabled==['deepseek-flash']
        receipt={'default_model':'deepseek-flash','enabled_models':enabled,'provider_calls':False,'archive':str(archive),'deepseek_key_ready':Path('/run/ops-native-model/deepseek').is_file() and Path('/run/ops-native-model/deepseek').stat().st_size>0}
        (archive/'receipt.json').write_text(json.dumps(receipt,indent=2)+'\n')
        print(json.dumps(receipt))
    except Exception:
        for i,target in enumerate(mapping):shutil.copy2(archive/str(i),target)
        vault.call('PUT','sys/policies/acl/ops-native-api',{'policy':policy})
        run('systemctl','daemon-reload');run('systemctl','restart','ops-native-secrets.service')
        run('/usr/local/libexec/ops-execution-mode','auto')
        raise RuntimeError('Switch rolled back') from None
    finally:
        vault.call('POST','auth/token/revoke-self',{});vault.token=''
if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('DeepSeek switch failed; review local archive') from None
