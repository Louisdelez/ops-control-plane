"""Owner-authorized local install; preserve daemon sources and roll back on failure."""
import hashlib,json,os,shutil,sqlite3,subprocess,time
from pathlib import Path
assert os.geteuid()==0
os.umask(0o027)
root=Path('/home/ops-user/ops-control-plane');src=root/'native_ops/logs';base=Path('/usr/local/libexec/ops-logs')
bundle='71e1061ab888119ccb0149bda359fe0edc551be72346a5aff2e3b9925bcf1ad2'
sealed=Path('/var/lib/ops-logstream-releases')/bundle/'collector.py'
assert sealed.read_bytes()==(src/'stream-collector-v2.py').read_bytes()
a=Path('/var/lib/ops-native-archives')/('log-stream-'+str(time.time_ns()));a.mkdir(mode=0o700)
files={src/n:base/n for n in ['service.py','stream-service.py']}
files[src/'stream-collector-v2.py']=base/'stream-collector-v2.py'
files[src/'ops-log-stream.service']=Path('/etc/systemd/system/ops-log-stream.service')
prior={};digests={}
for i,(source,target) in enumerate(files.items()):
 if source.suffix=='.py':compile(source.read_text(),str(source),'exec')
 assert not source.is_symlink() and not target.is_symlink()
 staged=a/('candidate-'+str(i));shutil.copyfile(source,staged)
 digests[str(target)]=hashlib.sha256(staged.read_bytes()).hexdigest()
 if target.exists():
  previous=a/('previous-'+str(i));shutil.copy2(target,previous);prior[str(target)]=str(previous)
 else:prior[str(target)]=None
(a/'authorization.json').write_text(json.dumps({'authorization':'direct-owner-local-construction-2026-09-14','files':digests,'prior':prior}))
def run(*args):return subprocess.run(args,check=True,capture_output=True,timeout=40)
try:
 run('systemctl','stop','ops-log-stream.service')
 for i,target in enumerate(files.values()):
  next=target.with_name(target.name+'.next');shutil.copyfile(a/('candidate-'+str(i)),next);next.chmod(0o644);os.replace(next,target)
 run('systemctl','daemon-reload');run('systemctl','restart','ops-logs.service');run('systemctl','enable','--now','ops-log-stream.service')
 run('systemctl','is-active','--quiet','ops-logs.service','ops-log-stream.service')
 result={'status':'installed','archive':str(a),'files':digests}
except BaseException:
 subprocess.run(['systemctl','disable','--now','ops-log-stream.service'],capture_output=True,timeout=40)
 for name,previous in prior.items():
  if previous:shutil.copy2(previous,name)
  else:Path(name).unlink(missing_ok=True)
 run('systemctl','daemon-reload');run('systemctl','restart','ops-logs.service','ops-log-stream.service')
 (a/'result.json').write_text(json.dumps({'status':'rolled_back'}));raise
(a/'result.json').write_text(json.dumps(result))
evidence=root/'artifacts/completion-final-2026-09-14/logstream-local-install.json';evidence.write_text(json.dumps(result,indent=2));os.chown(evidence,1000,1000);evidence.chmod(0o600)
print(json.dumps(result))
