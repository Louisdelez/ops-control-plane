import subprocess,os,json
from pathlib import Path
python='/home/ops-user/.hermes/hermes-agent/venv/bin/python'
wheels='/home/ops-user/ops-control-plane/artifacts/completion-2026-09-10/wheels'
subprocess.run(['/usr/bin/python3','-m','pip','--python',python,'install','--no-index','--find-links',wheels,'--no-deps','--upgrade','httpx2==2.12.0','httpcore2==2.12.0'],check=True,stdout=subprocess.DEVNULL)
site=Path(subprocess.run([python,'-c','import sysconfig;print(sysconfig.get_paths()["purelib"])'],check=True,capture_output=True,text=True).stdout.strip())
for name in ['httpx2','httpcore2']:
 for base in site.glob(name+'*'):
  for p in [base,*base.rglob('*')]:
   if not p.is_symlink():os.chown(p,1000,1000)
subprocess.run(['/usr/bin/python3','-m','pip','--python',python,'check'],check=True)
print('HERMES_USER_HTTP_UPDATED')
