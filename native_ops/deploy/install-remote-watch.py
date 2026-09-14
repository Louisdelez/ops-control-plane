"""Owner-authorized local install, archive and rollback; no direct remote command."""
from pathlib import Path
import hashlib,json,os,runpy,shutil,subprocess,time
ROOT=Path('/home/ops-user/ops-control-plane');RELEASE='ops-native-2026.09.11.16'
assert os.geteuid()==0
assert runpy.run_path(str(ROOT/'native_ops/release.py'))['verify'](ROOT)['release_id']==RELEASE
archive=Path('/var/lib/ops-native-archives')/('remote-watch-'+str(time.time_ns()));archive.mkdir(parents=True,mode=0o700)
def run(*args):return subprocess.run(args,check=True,capture_output=True,timeout=120)
def user(*args):return run('/usr/sbin/runuser','-u','ops-user','--','/usr/bin/env','XDG_RUNTIME_DIR=/run/user/1000','DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus',*args)
files={Path('/usr/local/libexec/ops-native/remote-watch.py'):(ROOT/'native_ops/remote_watch.py',0o644)}
for name in ['ops-remote-watch.service','ops-remote-watch.timer']:files[Path('/etc/systemd/user')/name]=(ROOT/'native_ops/systemd/user'/name,0o644)
prior={}
for index,p in enumerate(files):
 assert not p.is_symlink()
 p.parent.mkdir(parents=True,exist_ok=True)
 if p.exists():
  saved=archive/('before-'+str(index));shutil.copy2(p,saved);st=p.stat();prior[str(p)]=(str(saved),st.st_uid,st.st_gid,st.st_mode&0o777)
 else:prior[str(p)]=None
(archive/'prior.json').write_text(json.dumps(prior))
def replace(p,source,uid,gid,mode):
 next_path=p.with_name(p.name+'.watch-next')
 with next_path.open('xb') as f:f.write(Path(source).read_bytes());f.flush();os.fsync(f.fileno())
 os.chown(next_path,uid,gid);os.chmod(next_path,mode);os.replace(next_path,p)
was_enabled=False
try:
 was_enabled=user('/usr/bin/systemctl','--user','is-enabled','ops-remote-watch.timer').stdout.strip()==b'enabled'
except subprocess.CalledProcessError:pass
try:
 for p,(source,mode) in files.items():replace(p,source,0,0,mode)
 user('/usr/bin/systemctl','--user','daemon-reload')
 user('/usr/bin/systemctl','--user','start','ops-remote-watch.service')
 user('/usr/bin/systemctl','--user','enable','--now','ops-remote-watch.timer')
 user('/usr/bin/systemctl','--user','is-active','--quiet','ops-remote-watch.timer')
 latest=json.loads(Path('/home/ops-user/.local/state/ops-remote-watch/latest.json').read_text())
 assert latest['checked_at']>=time.time()-120 and latest['status']=='healthy'
 published=Path('/usr/local/share/ops-native/releases')/(RELEASE+'.json');assert not published.exists()
 published.write_bytes((ROOT/'native_ops/releases/current.json').read_bytes());published.chmod(0o444)
 result={'release':RELEASE,'status':'installed','kind':'deterministic_remote_watch','interval_minutes':15,'archive':str(archive),'first_check':latest,'manifest_sha256':hashlib.sha256(published.read_bytes()).hexdigest(),'whole_project_complete':False}
 proof=published.with_name(RELEASE+'-install.json');proof.write_text(json.dumps(result,indent=2)+'\n');proof.chmod(0o444)
 (archive/'result.json').write_text(json.dumps(result));print(json.dumps(result))
except BaseException:
 if not was_enabled:
  try:user('/usr/bin/systemctl','--user','disable','--now','ops-remote-watch.timer')
  except subprocess.CalledProcessError:pass
 for name,info in prior.items():
  p=Path(name)
  if info is None:p.unlink(missing_ok=True)
  else:replace(p,*info)
 user('/usr/bin/systemctl','--user','daemon-reload')
 raise
