"""Publish verified runtime/source evidence without replacing historical releases."""
from pathlib import Path
import hashlib,json,os,runpy,subprocess,time

ROOT=Path('/home/ops-user/ops-control-plane')
OUT=ROOT/'artifacts/continuation-2026-09-11'
RELEASE='ops-native-2026.09.11.11'

def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    assert os.geteuid()==0
    os.umask(0o077)
    release=runpy.run_path(str(ROOT/'native_ops/release.py'))['verify'](ROOT)
    assert release['release_id']==RELEASE
    previous=Path('/usr/local/share/ops-native/releases/ops-native-2026.09.11.10-install.json')
    assert json.loads(previous.read_text())['status']=='installed'
    for python,source,module in [('/opt/ops-native/venv/bin/python','native_ops/ops_native','ops_native'),
                                 ('/opt/ops-broker/venv/bin/python','broker/src/ops_broker','ops_broker')]:
        code='from pathlib import Path;import '+module+';target=Path('+module+'.__file__).parent;source=Path('+repr(str(ROOT/source))+');assert all((target/f.name).read_bytes()==f.read_bytes() for f in source.glob("*.py"))'
        subprocess.run([python,'-I','-c',code],check=True,capture_output=True)
    assert all((Path('/opt/ops-memory/src/ops_memory')/p.name).read_bytes()==p.read_bytes() for p in (ROOT/'memory/src/ops_memory').glob('*.py'))
    binary=Path('/home/ops-user/.local/lib/ops-desktop/ops-desktop')
    assert digest(binary)==digest(ROOT/'apps/ops-desktop/src-tauri/target/release/ops-desktop')
    team=json.loads((OUT/'team-verified-tool-results.json').read_text())
    assert len(team)==8 and all(row['success'] for row in team)
    web=json.loads((OUT/'desktop-webviews-retry/tabs-results.json').read_text())
    assert web['state_preserved'] is True and web['resize_verified'] is True
    for service in ['ops-broker.service','ops-native.service','ops-native-approval.service','ops-memory.service','openbao.service']:
        subprocess.run(['systemctl','is-active','--quiet',service],check=True)
    target=Path('/usr/local/share/ops-native/releases')/(RELEASE+'.json')
    assert not target.exists()
    with target.open('xb') as f:f.write((ROOT/'native_ops/releases/current.json').read_bytes())
    target.chmod(0o444)
    result={'release':RELEASE,'status':'verified','kind':'acceptance_and_desktop_security_update',
            'runtime_package_install_release':'ops-native-2026.09.11.10',
            'desktop_installed_version':'0.6.5','desktop_existing_sessions_restarted':False,
            'hermes_mcp_registration_sha256':digest(Path('/home/ops-user/.hermes/hermes-agent/tools/mcp_tool_registration.py')),'desktop_sha256':digest(binary),'manifest_sha256':digest(target),
            'native_team_read_only_acceptance':True,'team_full_autonomy':False,
            'memory_isolated_restore':True,'production_reachable':False,
            'whole_project_complete':False,'verified_at':int(time.time())}
    proof=target.with_name(RELEASE+'-install.json')
    with proof.open('x') as f:json.dump(result,f,indent=2);f.write('\n')
    proof.chmod(0o444)
    print(json.dumps(result))

if __name__=='__main__':main()
