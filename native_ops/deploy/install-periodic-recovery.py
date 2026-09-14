"""Install an inert Class C capability; only actual approvals enable destinations."""
from pathlib import Path
import hashlib,json,os,shutil,subprocess,time
import yaml
from ops_broker.policy import RBACPolicy
from ops_broker.runbooks import ExecutablePolicy,RunbookRegistry

ROOT=Path('/home/ops-user/ops-control-plane')

def run(*argv):
    return subprocess.run(argv,check=True,capture_output=True,timeout=60)

def main():
    if os.geteuid()!=0:raise ValueError('Administrator required')
    run('/usr/sbin/visudo','-cf',str(ROOT/'broker/deploy/sudoers/ops-broker-periodic-recovery'))
    archive=Path('/var/lib/ops-native-archives')/('periodic-recovery-'+str(time.time_ns()))
    archive.mkdir(mode=0o700)
    files={
        Path('/usr/local/libexec/ops-runbooks/remote-admin-transport.py'):(ROOT/'broker/deploy/helpers/remote-admin-transport.py',0o644),
        Path('/usr/local/libexec/ops-runbooks/periodic-recovery-enable'):(ROOT/'broker/deploy/helpers/periodic-recovery-enable',0o755),
        Path('/usr/local/libexec/ops-runbooks/periodic-recovery-copy'):(ROOT/'broker/deploy/helpers/periodic-recovery-copy',0o755),
        Path('/usr/local/libexec/ops-runbooks/receive-recovery-bundle.py'):(ROOT/'broker/deploy/recovery/receive-recovery-bundle.py',0o644),
        Path('/etc/sudoers.d/ops-broker-periodic-recovery'):(ROOT/'broker/deploy/sudoers/ops-broker-periodic-recovery',0o440),
        Path('/opt/ops-broker/runbooks/local/periodic-recovery-enable.yaml'):(ROOT/'runbooks/local/periodic-recovery-enable.yaml',0o644),
    }
    for name in ('ops-periodic-recovery@.service','ops-periodic-recovery@.timer'):
        files[Path('/etc/systemd/system')/name]=(ROOT/'native_ops/deploy'/name,0o644)
    if any(p.exists() for p in files):raise ValueError('Existing capability requires review')
    rbac=yaml.safe_load(Path('/etc/ops-broker/rbac.yaml').read_text())
    rbac['roles']['supervised-operator']['runbooks'].append('backup.periodic-recovery-enable.v1')
    RBACPolicy.from_data(rbac)
    executables=yaml.safe_load(Path('/etc/ops-broker/executables.yaml').read_text())
    executables['sudo']['allowed_helpers'].append('/usr/local/libexec/ops-runbooks/periodic-recovery-enable')
    for name,value in [('rbac',rbac),('executables',executables)]:
        path=archive/(name+'.next.yaml');path.write_text(yaml.safe_dump(value,sort_keys=False))
        files[Path('/etc/ops-broker')/(name+'.yaml')]=(path,0o640)
    before={}
    for index,path in enumerate(files):
        if path.is_symlink():raise ValueError('Unexpected destination symlink')
        if path.exists():
            meta=path.stat();saved=archive/('before-'+str(index));shutil.copy2(path,saved)
            before[str(path)]=(str(saved),meta.st_uid,meta.st_gid,meta.st_mode&0o777)
        else:before[str(path)]=None
    (archive/'before.json').write_text(json.dumps(before))
    try:
        for path,(source,mode) in files.items():
            group=path.stat().st_gid if path.exists() else 0
            temporary=path.with_name(path.name+'.periodic-next')
            with temporary.open('xb') as f:f.write(source.read_bytes());f.flush();os.fsync(f.fileno())
            os.chown(temporary,0,group);temporary.chmod(mode);temporary.replace(path)
        run('/usr/sbin/visudo','-c')
        registry=RunbookRegistry.load(Path('/opt/ops-broker/runbooks'),ExecutablePolicy.load(Path('/etc/ops-broker/executables.yaml')))
        assert registry.get('backup.periodic-recovery-enable.v1').action_class=='C'
        run('/usr/bin/systemctl','daemon-reload')
        # Both destination timers deliberately remain disabled; no remote command.
        for target in ('nas','edge-vps'):
            result=subprocess.run(['systemctl','is-enabled','ops-periodic-recovery@'+target+'.timer'],capture_output=True,text=True)
            if result.stdout.strip()!='disabled':raise ValueError('Unexpected enabled destination')
        result={'status':'installed_inert','archive':str(archive),'runbook':'backup.periodic-recovery-enable.v1',
                'remote_changes':False,'destination_timers_enabled':False,
                'files':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
        (archive/'result.json').write_text(json.dumps(result,indent=2)+'\n')
        print(json.dumps(result))
    except BaseException:
        for raw,prior in before.items():
            path=Path(raw)
            if prior is None:path.unlink(missing_ok=True)
            else:
                saved,uid,gid,mode=prior;shutil.copyfile(saved,path);os.chown(path,uid,gid);path.chmod(mode)
        run('/usr/bin/systemctl','daemon-reload')
        raise

if __name__=='__main__':main()
