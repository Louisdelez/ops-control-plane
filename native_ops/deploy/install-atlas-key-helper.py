from pathlib import Path
import os,hashlib,shutil,time
assert os.geteuid()==0
source=Path('/home/ops-user/ops-control-plane/scripts/ops-model-key-manager')
target=Path('/usr/local/libexec/ops-model-key-manager')
payload=source.read_bytes()
compile(payload,str(target),'exec')
if target.exists() or target.is_symlink():
 if target.is_symlink():raise RuntimeError('Unexpected helper symlink')
 if target.read_bytes()!=payload:
  archive=Path('/var/lib/ops-native-archives')/('atlas-key-helper-'+str(time.time_ns()))
  archive.mkdir(parents=True,mode=0o700)
  shutil.copy2(target,archive/target.name)
tmp=target.with_name(target.name+'.next')
if tmp.exists() or tmp.is_symlink():raise RuntimeError('Unexpected staging path')
fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o755)
with os.fdopen(fd,'wb') as f:f.write(payload);f.flush();os.fsync(f.fileno())
os.chown(tmp,0,0);os.chmod(tmp,0o755);os.replace(tmp,target)
print('NATIVE_KEY_STORAGE_HELPER_INSTALLED',hashlib.sha256(payload).hexdigest())
