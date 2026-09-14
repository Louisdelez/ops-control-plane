"""Provision new official user profiles; native shared OAuth remains untouched."""
from pathlib import Path
import hashlib,json,os,runpy,shutil,subprocess,time,tomllib

ROOT=Path('/home/ops-user/ops-control-plane')

def run(*args):return subprocess.run(args,check=True,capture_output=True,timeout=60)

def user(*args):
    return run('/usr/sbin/runuser','-u','ops-user','--','/usr/bin/env',
               'HOME=/home/ops-user','PATH=/home/ops-user/.local/bin:/usr/bin:/bin',*args)

def main():
    assert os.geteuid()==0
    os.umask(0o077)
    module=runpy.run_path(str(ROOT/'native_ops/team_profiles.py'))
    profiles=module['PROFILES']
    model=tomllib.loads(Path('/home/ops-user/.codex/config.toml').read_text())['model']
    home=Path('/home/ops-user/.hermes/profiles')
    assert all(not (home/name).exists() for name in profiles),'An existing profile needs explicit reconciliation'
    archive=Path('/var/lib/ops-native-archives')/('subscription-team-'+str(time.time_ns()))
    archive.mkdir(parents=True,mode=0o700)
    rule=Path('/etc/sudoers.d/ops-subscription-team')
    assert not rule.exists()
    candidate=archive/'sudoers';candidate.write_text(module['sudoers']());candidate.chmod(0o440)
    run('/usr/sbin/visudo','-cf',str(candidate))
    shutil.copyfile(candidate,rule);rule.chmod(0o440)
    created=[]
    try:
        for name,entry in profiles.items():
            user('/home/ops-user/.local/bin/hermes','profile','create',name,'--no-alias','--no-skills',
                 '--description',entry[3]);created.append(name)
            # Render YAML under the user's Hermes interpreter (the host root
            # Python need not have PyYAML installed). No credential is loaded.
            code='''import runpy,yaml,json,sys
from pathlib import Path
module=runpy.run_path(sys.argv[1]);config,soul,description=module['render'](sys.argv[2],sys.argv[3],Path(sys.argv[4]))
home=Path.home()/'.hermes/profiles'/sys.argv[2]
(home/'config.yaml').write_text(yaml.safe_dump(config,sort_keys=False,allow_unicode=True))
(home/'config.yaml').chmod(0o600)
(home/'SOUL.md').write_text(soul);(home/'SOUL.md').chmod(0o600)
(home/'workspace').mkdir(mode=0o700,exist_ok=True)
'''
            user('/home/ops-user/.hermes/hermes-agent/venv/bin/python','-c',code,
                 str(ROOT/'native_ops/team_profiles.py'),name,model,str(ROOT))
            status=user('/home/ops-user/.local/bin/hermes','-p',name,'auth','status','openai-codex')
            assert b'logged in' in status.stdout
            # Only installed, reviewed skills are copied; no generated skill is promoted.
            skillroot=home/name/'skills'
            for skill in ['incident-triage','backup-verification','shared-infra-change','deploy-release','ops-deterministic-workflows']:
                target=skillroot/skill
                shutil.copytree(ROOT/'config/hermes/skills'/skill,target)
                for p in [target,*target.rglob('*')]:os.chown(p,1000,1000);p.chmod(0o700 if p.is_dir() else 0o600)
        result={'profiles':created,'native_provider':'openai-codex','model':model,
                'shared_native_login_verified':True,'credentials_copied':False,
                'specialist_tool_execution_verified':False,'archive':str(archive),'created_at':int(time.time())}
        proof=Path('/usr/local/share/ops-native/releases/ops-subscription-team-2026.09.11.json')
        with proof.open('x') as f:json.dump(result,f,indent=2);f.write('\n')
        proof.chmod(0o444);print(json.dumps(result))
    except BaseException:
        rule.rename(archive/'disabled-sudoers')
        # Keep any partial profile state for recovery, but revoke the newly
        # granted MCP commands before propagating the installation failure.
        (archive/'partial-profiles.json').write_text(json.dumps(created))
        raise

if __name__=='__main__':main()
