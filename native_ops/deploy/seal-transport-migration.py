"""Seal a reviewable transport migration; do not touch active identities or schedules."""
import hashlib,json,os,runpy
from pathlib import Path
assert os.geteuid()==0
os.umask(0o077);root=Path('/home/ops-user/ops-control-plane');helper=root/'broker/deploy/helpers/migrate-observer-transports';scope=runpy.run_path(str(helper))['TARGETS']
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
files={n:(root/'native_ops/deploy/transport-migration'/n).read_bytes() for n in scope}
manifest={'schema_version':1,'purpose':'Retire personal SSH transport from preflight and deterministic legacy callers; use forced observer for status and OpenBao administrative identity for existing encrypted copies. Archive previous files/activations, keep schedules, verify NAS and VPS copies, roll back local changes on failure. Requires all four approved logstream extensions to have succeeded.','helper_sha256':digest(helper),'files':{n:hashlib.sha256(v).hexdigest() for n,v in files.items()},'previous':{n:digest(Path(p)) for n,p in scope.items()},'activation':{h:digest(Path('/var/lib/ops-periodic-recovery')/(h+'.enabled.json')) for h in ['nas','edge-vps']}}
raw=json.dumps(manifest,sort_keys=True,indent=2).encode();bundle=hashlib.sha256(raw).hexdigest();dest=Path('/var/lib/ops-transport-releases')/bundle;dest.mkdir(parents=True,mode=0o700)
for n,value in {**files,'manifest.json':raw}.items():(dest/n).write_bytes(value);(dest/n).chmod(0o600)
report={'bundle_sha256':bundle,'files':len(files),'remote_changes':False,'preconditions':['four logstream extensions succeeded','actual class C approval']}
p=root/'artifacts/completion-final-2026-09-14/transport-migration-release.json';p.write_text(json.dumps(report,indent=2));os.chown(p,1000,1000);p.chmod(0o600);print(json.dumps(report))
