from pathlib import Path
import os,shutil,json
assert os.geteuid()==0
root=Path('/home/ops-user/ops-control-plane');removed=[]
for base in ['tests','native_ops','orchestrator/tests','memory/tests','broker/tests','bridges/zulip/tests']:
 for p in (root/base).rglob('__pycache__'):
  if p.is_symlink() or not p.is_dir():continue
  if any(f.is_symlink() or not f.is_file() or f.suffix!='.pyc' for f in p.iterdir()):continue
  shutil.rmtree(p);removed.append(str(p))
p=root/'artifacts/audit-cleanup-2026-09-10/root-cache-cleanup.json';p.write_text(json.dumps({'removed':removed},indent=2)+'\n');os.chown(p,1000,1000)
print('GENERATED_CACHES_REMOVED',len(removed))
