"""Receive only a fixed ciphertext recovery seed, without replacing existing data."""
import hashlib,json,os,shutil,stat,sys,tarfile,tempfile
from pathlib import Path
NAMES={'openbao-snapshot.age','unseal-shares.age','zulip-bundle.age'}
LIMIT=32*1024*1024

def receive(stream,home):
 root=home/'ops-encrypted-backups'
 try:root.mkdir(mode=0o700)
 except FileExistsError:pass
 st=root.lstat()
 if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.geteuid() or st.st_mode&0o077:raise ValueError('Unsafe destination')
 stage=Path(tempfile.mkdtemp(prefix='.incoming-',dir=root));seen={}
 try:
  with tarfile.open(fileobj=stream,mode='r|') as bundle:
   for entry in bundle:
    if entry.name not in NAMES or entry.name in seen or not entry.isfile() or not 1<=entry.size<=LIMIT:raise ValueError('Invalid member')
    body=bundle.extractfile(entry);data=body.read(LIMIT+1)
    if len(data)!=entry.size or not data.startswith(b'age-encryption.org/v1\n'):raise ValueError('Invalid ciphertext')
    target=stage/entry.name
    with target.open('xb') as output:output.write(data);output.flush();os.fsync(output.fileno())
    target.chmod(0o600);seen[entry.name]=hashlib.sha256(data).hexdigest()
  if set(seen)!=NAMES:raise ValueError('Incomplete seed')
  identity=hashlib.sha256(json.dumps(seen,sort_keys=True).encode()).hexdigest()
  final=root/('dell-seed-'+identity)
  if final.exists() or final.is_symlink():
   st=final.lstat()
   if not stat.S_ISDIR(st.st_mode) or st.st_uid!=os.geteuid() or st.st_mode&0o077:raise ValueError('Unsafe existing seed')
   for name,digest in seen.items():
    f=final/name;st=f.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid!=os.geteuid() or st.st_mode&0o077 or st.st_size>LIMIT or hashlib.sha256(f.read_bytes()).hexdigest()!=digest:raise ValueError('Conflicting seed')
  else:os.rename(stage,final)
  return {'status':'copied_and_verified','seed_id':identity,'relative_directory':'ops-encrypted-backups/'+final.name,'sha256':seen}
 finally:
  if stage.exists():shutil.rmtree(stage)

if __name__=='__main__':
 os.umask(0o077)
 try:print(json.dumps(receive(sys.stdin.buffer,Path.home())))
 except Exception:raise SystemExit('Encrypted seed receiver refused the operation') from None
