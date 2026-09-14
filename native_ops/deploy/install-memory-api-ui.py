"""Prepare native key entry and independent Qwen activation, without API calls."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import time

ROOT = Path('/home/ops-user/ops-control-plane')
RELEASE = 'ops-native-2026.09.12.2'


def main():
    assert os.geteuid() == 0
    os.umask(0o022)
    assert runpy.run_path(str(ROOT/'native_ops/release.py'))['verify'](ROOT)['release_id'] == RELEASE
    # No provider keys have been supplied; do not disrupt an active API workload.
    assert not json.loads(Path('/etc/ops-native/config.json').read_text()).get('generation_enabled')
    archive = Path('/var/lib/ops-native-archives')/('memory-api-ui-'+str(time.time_ns()))
    archive.mkdir(mode=0o700)
    sources = {
        '/usr/local/libexec/ops-model-key-manager': 'scripts/ops-model-key-manager',
        '/usr/local/libexec/ops-execution-mode': 'native_ops/deploy/ops-execution-mode',
        '/etc/ops-native-model/agent.hcl': 'native_ops/deploy/agent.hcl',
        '/etc/ops-native-model/config.json': 'native_ops/deploy/model-config.json',
        '/etc/systemd/system/ops-native-model.service': 'native_ops/deploy/ops-native-model.service',
    }
    prior = {}
    for name in sources:
        target = Path(name)
        assert target.is_file() and not target.is_symlink()
        saved = archive/str(len(prior))
        shutil.copy2(target, saved)
        st = target.stat()
        prior[name] = (str(saved), st.st_uid, st.st_gid, st.st_mode & 0o777)
    (archive/'prior.json').write_text(json.dumps(prior))
    def run(*args):
        return subprocess.run(args, check=True, capture_output=True, timeout=120)
    def replace(target, source, uid, gid, mode):
        p = Path(target)
        temporary = p.with_name(p.name+'.memory-api-next')
        with temporary.open('xb') as output:
            output.write(Path(source).read_bytes())
            output.flush()
            os.fsync(output.fileno())
        os.chown(temporary, uid, gid)
        os.chmod(temporary, mode)
        os.replace(temporary, p)
    identity = runpy.run_path(str(ROOT/'native_ops/deploy/prepare-qwen-access.py'))['prepare']()
    try:
        run('/usr/bin/systemctl', 'stop', 'ops-native-capabilities.timer', 'ops-native-capabilities.service',
            'ops-native-model.service', 'ops-native-secrets.service')
        for target, source in sources.items():
            _, uid, gid, mode = prior[target]
            replace(target, ROOT/source, uid, gid, mode)
        run('/usr/bin/systemctl', 'daemon-reload')
        for unit in ['ops-native-secrets.service', 'ops-native-capabilities.service']:
            state = run('/usr/bin/systemctl', 'show', unit, '-p', 'ActiveState', '--value').stdout.strip()
            if state == b'failed':
                run('/usr/bin/systemctl', 'reset-failed', unit)
        run('/usr/bin/systemctl', 'enable', '--now', 'ops-native-capabilities.timer', 'ops-memory-api-secrets.timer')
        run('/usr/bin/systemctl', 'start', 'ops-native-capabilities.service', 'ops-memory-api-secrets.service')
        assert not json.loads(Path('/etc/ops-native/config.json').read_text()).get('generation_enabled')
        for unit in ['ops-memory.service', 'ops-native.service', 'ops-broker.socket']:
            run('/usr/bin/systemctl', 'is-active', '--quiet', unit)
        app = Path('/home/ops-user/.local/lib/ops-desktop/ops-desktop')
        built = ROOT/'apps/ops-desktop/src-tauri/target/release/ops-desktop'
        assert app.read_bytes() == built.read_bytes()
        published = Path('/usr/local/share/ops-native/releases')/(RELEASE+'.json')
        assert not published.exists()
        published.write_bytes((ROOT/'native_ops/releases/current.json').read_bytes())
        published.chmod(0o444)
        receipt = {'release': RELEASE, 'desktop_version': '0.6.7', 'archive': str(archive),
                   'desktop_sha256': hashlib.sha256(app.read_bytes()).hexdigest(),
                   'identity': identity, 'required_provider_accounts': ['alibaba','siliconflow'],
                   'provider_calls': False, 'real_api_acceptance': 'waiting_for_owner_keys',
                   'whole_project_complete': False, 'installed_at': int(time.time())}
        proof = published.with_name(RELEASE+'-install.json')
        proof.write_text(json.dumps(receipt, indent=2)+'\n')
        proof.chmod(0o444)
        print(json.dumps(receipt))
    except BaseException:
        for target, metadata in prior.items():
            replace(target, *metadata)
        run('/usr/bin/systemctl', 'daemon-reload')
        run('/usr/bin/systemctl', 'start', 'ops-native-capabilities.timer')
        raise


if __name__ == '__main__':
    main()
