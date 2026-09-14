from pathlib import Path
import os,subprocess,shutil,json,time
assert os.geteuid()==0
src=Path('/home/ops-user/ops-control-plane/apps/approvals')
dst=Path('/opt/ops-approvals')
assert not dst.exists(), 'Existing installation requires versioned upgrade'
subprocess.run(['useradd','--system','--home-dir','/var/lib/ops-approvals','--shell','/usr/sbin/nologin','opsapprovals'],check=True,capture_output=True)
dst.mkdir(mode=0o755)
for name in ['server.py','push_notifications.py','export_state.py','requirements.txt']:shutil.copy2(src/name,dst/name)
shutil.copytree(src/'web',dst/'web')
subprocess.run(['/usr/bin/python3','-m','venv',str(dst/'venv')],check=True,capture_output=True)
r=subprocess.run([str(dst/'venv/bin/pip'),'install','--no-index','--find-links','/home/ops-user/ops-control-plane/artifacts/approvals-pwa-2026-09-13/wheels','--find-links','/home/ops-user/ops-control-plane/artifacts/finalisation-suite-2026-09-13/wheels','-r',str(dst/'requirements.txt')],capture_output=True)
if r.returncode:raise RuntimeError('Dependency installation failed')
for name in ['ops-approvals.service','ops-approvals-export.service']:
 p=Path('/etc/systemd/system')/name;assert not p.exists();shutil.copy2(src/'deploy'/name,p)
subprocess.run(['systemctl','daemon-reload'],check=True,capture_output=True)
subprocess.run(['systemctl','enable','--now','ops-approvals.service'],check=True,capture_output=True)
time.sleep(3)
print({'web_active':subprocess.run(['systemctl','is-active','--quiet','ops-approvals.service']).returncode==0,'export_active':subprocess.run(['systemctl','is-active','--quiet','ops-approvals-export.service']).returncode==0})
