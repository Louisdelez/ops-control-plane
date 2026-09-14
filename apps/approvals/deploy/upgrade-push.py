"""Owner-authorized PWA Web Push upgrade; preserve runtime and rollback on failure."""
from pathlib import Path
import os,shutil,subprocess,json,time,urllib.request
assert os.geteuid()==0
os.umask(0o077)
root=Path('/home/ops-user/ops-control-plane');src=root/'apps/approvals';dst=Path('/opt/ops-approvals')
archive=Path('/var/lib/ops-native-archives')/('approvals-webpush-'+str(time.time_ns()))
archive.mkdir(mode=0o700)
names=['server.py','requirements.txt','web']
for name in names:
 p=dst/name
 if p.is_dir():shutil.copytree(p,archive/name)
 else:shutil.copy2(p,archive/name)
shutil.copytree(dst/'venv',archive/'venv',symlinks=True)
new=not (dst/'push_notifications.py').exists()
if not new:shutil.copy2(dst/'push_notifications.py',archive/'push_notifications.py')
def run(*args):return subprocess.run(args,check=True,capture_output=True,timeout=180)
try:
 run('systemctl','stop','ops-approvals.service')
 # Runtime code must be readable by opsapprovals; state/archive remain private.
 os.umask(0o022)
 try:run(str(dst/'venv/bin/python'),'-m','pip','install','--no-index','--find-links',str(root/'artifacts/finalisation-suite-2026-09-13/wheels'),'pywebpush==2.5.0')
 finally:os.umask(0o077)
 run(str(dst/'venv/bin/python'),'-m','pip','check')
 run('/usr/sbin/runuser','-u','opsapprovals','--',str(dst/'venv/bin/python'),'-I','-c','from pywebpush import webpush; from cryptography.fernet import Fernet')
 for name in ['server.py','push_notifications.py','requirements.txt']:
  shutil.copyfile(src/name,dst/name);(dst/name).chmod(0o644)
 for p in (src/'web').iterdir():
  if p.is_file():shutil.copyfile(p,dst/'web'/p.name);(dst/'web'/p.name).chmod(0o644)
 run('systemctl','start','ops-approvals.service')
 for _ in range(30):
  try:
   with urllib.request.urlopen('http://127.0.0.1:9128/healthz',timeout=3) as response:assert json.load(response)['ok']
   break
  except Exception:time.sleep(1)
 else:raise RuntimeError('Readiness failed')
 result={'status':'installed','feature':'pwa-webpush','rollback_archive':str(archive),'checked_at':time.time(),'email_used':False,'real_iphone_receipt_verified':False}
except Exception:
 subprocess.run(['systemctl','stop','ops-approvals.service'],capture_output=True)
 shutil.rmtree(dst/'venv');shutil.copytree(archive/'venv',dst/'venv',symlinks=True)
 for name in names:
  if (archive/name).is_dir():shutil.copytree(archive/name,dst/name,dirs_exist_ok=True)
  else:shutil.copyfile(archive/name,dst/name)
 if not new:shutil.copyfile(archive/'push_notifications.py',dst/'push_notifications.py')
 subprocess.run(['systemctl','start','ops-approvals.service'],capture_output=True)
 result={'status':'rolled_back','rollback_archive':str(archive),'checked_at':time.time()}
(archive/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
raise SystemExit(0 if result['status']=='installed' else 1)
