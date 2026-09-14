"""Restore the encrypted recovery bundle into disposable services, never live paths."""
from pathlib import Path,PurePosixPath
import hashlib,importlib.util,json,os,sqlite3,subprocess,sys,tarfile,tempfile,time,uuid
BASE=Path('/var/lib/ops-recovery-drills')
SOURCE=Path('/home/ops-user/ops-control-plane')
IDENTITY=Path('/home/ops-user/Documents/Recuperation-Ops-2026-09-11/PRIVE/cle-age.txt')
EXPECTED={'pwa/session.key','pwa/webpush.pem','pwa/sessions.sqlite3','broker/state.db','clients/cli-pilot.sqlite3','clients/memory-sync.sqlite3','clients/workflows.sqlite3','memory/manifest.json','memory/memory.sqlite3','memory/qdrant.snapshot','native/approvals.sqlite3','native/manifest.json','native/pilot.sqlite3','openbao/manifest.json','openbao/snapshot.age','openbao/unseal-shares.age','zulip/bundle.tar.age'}

def extract(archive,target,expected):
    with tarfile.open(archive,'r:') as tar:
        members=tar.getmembers()
        if len(members)!=len(expected) or {m.name for m in members}!=expected:raise ValueError('Unexpected archive members')
        if sum(m.size for m in members)>2*1024**3:raise ValueError('Oversized archive')
        for m in members:
            p=PurePosixPath(m.name)
            if not m.isfile() or p.is_absolute() or '..' in p.parts or m.size>512*1024**2:raise ValueError('Unsafe archive member')
            destination=target/m.name;destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with tar.extractfile(m) as incoming,destination.open('xb') as out:
                import shutil
                shutil.copyfileobj(incoming,out,1024*1024)
            destination.chmod(0o600)

def run(*args,stdin=None,timeout=180):
    p=subprocess.run(args,stdin=stdin,capture_output=True,timeout=timeout)
    if p.returncode:raise RuntimeError('Disposable restore command failed')
    return p.stdout

def decrypt(source,target):
    with target.open('xb') as out:
        p=subprocess.run(['/usr/bin/age','-d','-i',str(IDENTITY),str(source)],stdout=out,stderr=subprocess.DEVNULL,timeout=180)
    if p.returncode:raise ValueError('Recovery decryption failed')

def sql_check(path):
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise ValueError('Invalid restored database')
        return len(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall())

def main():
    assert os.geteuid()==0
    os.umask(0o077);BASE.mkdir(mode=0o700,exist_ok=True)
    report={'checked_at':time.time(),'status':'running','host_services_modified':False,'full_machine_restore_verified':False}
    stage='decrypt_bundle'
    try:
        meta=json.loads(Path('/var/lib/ops-recovery-bundles/latest.json').read_text())
        assert time.time()-meta['created_at']<30*3600
        name=meta['bundle'];assert Path(name).name==name
        encrypted=Path('/var/lib/ops-recovery-bundles')/name
        with encrypted.open('rb') as f:assert hashlib.file_digest(f,'sha256').hexdigest()==meta['sha256']
        report['bundle_sha256']=meta['sha256']
        with tempfile.TemporaryDirectory(prefix='drill-',dir=BASE) as raw:
            work=Path(raw);decrypt(encrypted,work/'bundle.tar');extract(work/'bundle.tar',work/'components',EXPECTED);parts=work/'components'
            stage='sqlite';report['sqlite_tables']={name:sql_check(parts/name) for name in EXPECTED if name.endswith(('.db','.sqlite3'))}
            stage='qdrant'
            path=SOURCE/'memory/backup/ops_memory_backup.py';spec=importlib.util.spec_from_file_location('bundle_memory_restore',path);module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
            memory_id=json.loads((parts/'memory/manifest.json').read_text())['backup_id']
            assert module.BACKUP_RE.fullmatch(memory_id)
            memory_dir=parts/memory_id;(parts/'memory').rename(memory_dir)
            result=module.restore_test(module.BackupConfig.load(Path('/etc/ops-memory/backup.toml')),memory_dir)
            report['memory']={k:result[k] for k in ('status','sqlite_memories','qdrant_points','environment')}
            stage='zulip_decrypt';decrypt(parts/'zulip/bundle.tar.age',work/'zulip.tar');extract(work/'zulip.tar',work/'zulip',{'database.dump','zulip-data.tar'})
            stage='zulip_database'
            image=run('docker','inspect','--format','{{.Image}}','zulip-standard-database-1').decode().strip()
            container='ops-bundle-restore-'+uuid.uuid4().hex[:12];started=False
            try:
                run('docker','run','-d','--name',container,'--network','none','--memory','512m','--cpus','1','--pids-limit','100','--tmpfs','/var/lib/postgresql/data:rw,size=1024m','-e','POSTGRES_HOST_AUTH_METHOD=trust','-e','POSTGRES_USER=zulip','-e','POSTGRES_DB=zulip',image);started=True
                for _ in range(45):
                    try:run('docker','exec',container,'pg_isready','-U','zulip','-d','zulip');break
                    except RuntimeError:time.sleep(1)
                else:raise RuntimeError('Disposable database timeout')
                with (work/'zulip/database.dump').open('rb') as f:run('docker','exec','-i',container,'pg_restore','--clean','--if-exists','--exit-on-error','--no-owner','--no-privileges','-U','zulip','-d','zulip',stdin=f)
                counts=run('docker','exec',container,'psql','-U','zulip','-d','zulip','-tAc','SELECT (SELECT count(*) FROM zerver_realm), (SELECT count(*) FROM zerver_userprofile), (SELECT count(*) FROM zerver_message)').decode().strip()
                realms,users,messages=map(int,counts.split('|'));assert realms and users
                with tarfile.open(work/'zulip/zulip-data.tar') as tar:
                    members=tar.getmembers();assert members
                report['zulip']={'restored':True,'realms':realms,'users':users,'messages':messages,'data_archive_readable':True}
            finally:
                if started:run('docker','rm','-f','-v',container)
            stage='openbao'
            # Network namespace contains only loopback: force restore cannot reach the host vault.
            args=['/usr/bin/unshare','--net','/bin/sh','-c','/usr/sbin/ip link set lo up && exec "$@"','restore','/usr/bin/python3',str(SOURCE/'native_ops/deploy/verify-openbao-recovery.py'),'--isolated-force','--snapshot-dir',str(parts/'openbao'),'--shares-file',str(parts/'openbao/unseal-shares.age')]
            value=json.loads(run(*args,timeout=180).decode())
            assert value['full_restore_verified'] and value['network_isolated'] and not value['host_vault_contacted']
            report['openbao']={'restored':True,'owner_authentication_verified':value['owner_authentication_verified'],'network_isolated':True}
        report.update(status='verified',application_data_restore_verified=True)
    except Exception as exc:report.update(status='failed',failed_stage=stage,exception_type=type(exc).__name__)
    report['finished_at']=time.time();destination=BASE/('proof-'+str(time.time_ns())+'.json');destination.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report));return 0 if report['status']=='verified' else 1
if __name__=='__main__':raise SystemExit(main())
