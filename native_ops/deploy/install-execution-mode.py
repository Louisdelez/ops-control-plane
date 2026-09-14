from pathlib import Path
import os,shutil,subprocess,time,json,sys
assert os.geteuid()==0
ROOT=Path('/home/ops-user/ops-control-plane')
archive=Path('/var/lib/ops-native-archives')/('execution-mode-'+str(time.time_ns()));archive.mkdir(mode=0o700,parents=True)
def install(source,target,mode):
    if target.is_symlink():raise RuntimeError('Symlink destination refused')
    if target.exists():shutil.copy2(target,archive/(str(target).strip('/').replace('/','_')))
    content=source.read_bytes();fd=os.open(str(target)+'.next',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
    with os.fdopen(fd,'wb') as f:f.write(content);f.flush();os.fsync(f.fileno())
    os.chown(str(target)+'.next',0,0);os.chmod(str(target)+'.next',mode);os.replace(str(target)+'.next',target)
# Runtime modules must be installed as a wheel, preserving package RECORD.
subprocess.run(['/opt/ops-native/venv/bin/python','-c',
    'from importlib.metadata import version; assert version("ops-native") == "0.1.8"'],check=True)
install(ROOT/'native_ops/deploy/ops-execution-mode',Path('/usr/local/libexec/ops-execution-mode'),0o755)
mode=Path('/etc/ops-execution-mode.json')
if not mode.exists():mode.write_text('{"mode":"cli"}\n');mode.chmod(0o644)
sudoers='ops-user ALL=(root) NOPASSWD: '+', '.join('/usr/local/libexec/ops-execution-mode '+x for x in ['status','cli','api','hybrid','activate','auto'])+'\n'
source=archive/'sudoers';source.write_text(sudoers);source.chmod(0o440)
subprocess.run(['/usr/sbin/visudo','-cf',str(source)],check=True,stdout=subprocess.DEVNULL)
install(source,Path('/etc/sudoers.d/ops-execution-mode'),0o440)
subprocess.run(['/usr/local/libexec/ops-execution-mode','status'],check=True)
print('EXECUTION_MODE_INSTALLED',str(archive))
