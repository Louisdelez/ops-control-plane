"""Native DB dump, encrypted data bundle and isolated PostgreSQL restore check."""
from pathlib import Path
import os,subprocess,json,time,tempfile,tarfile,hashlib,fcntl
assert os.geteuid()==0
os.umask(0o077)
root=Path('/var/lib/ops-zulip-backups');root.mkdir(mode=0o700,exist_ok=True)
container='zulip-standard-zulip-1'
def run(*args,**kw):
 r=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**kw)
 if r.returncode:
  (root/'last-error.private').write_bytes(r.stderr)
  raise RuntimeError('Backup subcommand failed; private diagnostic retained')
 return r
with (root/'backup.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime());name='ops-zulip-restore-'+stamp.lower()
 with tempfile.TemporaryDirectory(prefix='zulip-backup-',dir='/var/lib/ops-zulip-backups') as raw:
  temp=Path(raw)
  run('docker','exec',container,'sh','-c','umask 077; exec /sbin/entrypoint.sh app:backup')
  dump=run('docker','exec',container,'python3','-c',"from pathlib import Path;print(max(Path('/data/backups').glob('backup-*.sql'),key=lambda p:p.stat().st_mtime).name)").stdout.decode().strip()
  assert '/' not in dump and dump.startswith('backup-') and dump.endswith('.sql')
  run('docker','cp',container+':/data/backups/'+dump,str(temp/'database.dump'))
  with (temp/'zulip-data.tar').open('wb') as output:
   subprocess.run(['docker','exec',container,'tar','-C','/data','--exclude=./backups','--exclude=./logs','-cf','-','.'],check=True,stdout=output,stderr=subprocess.DEVNULL)
  image=run('docker','inspect','--format','{{.Image}}','zulip-standard-database-1').stdout.decode().strip()
  started=False
  try:
   run('docker','run','-d','--name',name,'--network','none','--memory','512m','--cpus','1','--pids-limit','100','--tmpfs','/var/lib/postgresql/data:rw,size=1024m','-e','POSTGRES_HOST_AUTH_METHOD=trust','-e','POSTGRES_USER=zulip','-e','POSTGRES_DB=zulip',image);started=True
   for _ in range(45):
    try:run('docker','exec',name,'pg_isready','-U','zulip','-d','zulip');break
    except RuntimeError:time.sleep(1)
   else:raise RuntimeError('Isolated database did not become ready')
   with (temp/'database.dump').open('rb') as source:
    run('docker','exec','-i',name,'pg_restore','--clean','--if-exists','--exit-on-error','--no-owner','--no-privileges','-U','zulip','-d','zulip',stdin=source)
   counts=run('docker','exec',name,'psql','-U','zulip','-d','zulip','-tAc','SELECT (SELECT count(*) FROM zerver_realm), (SELECT count(*) FROM zerver_userprofile), (SELECT count(*) FROM zerver_message)').stdout.decode().strip()
   realms,users,messages=map(int,counts.split('|'));assert realms>=1 and users>=1
  finally:
   if started:run('docker','rm','-f','-v',name)
  with tarfile.open(temp/'bundle.tar','w') as bundle:
   for filename in ['database.dump','zulip-data.tar']:bundle.add(temp/filename,arcname=filename)
  target=root/(stamp+'.tar.age')
  run('/usr/bin/age','-R','/etc/openbao-backup/recovery.age-recipient','-o',str(target),str(temp/'bundle.tar'))
  result={'status':'ok','created_at':int(time.time()),'encrypted_bundle':str(target),'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'database_restore_verified':True,'restored_counts':{'realms':realms,'users':users,'messages':messages},'data_files_included':True,'encrypted_recovery_verified':False}
  (root/'latest.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
