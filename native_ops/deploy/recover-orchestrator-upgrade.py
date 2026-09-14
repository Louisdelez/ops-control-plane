from pathlib import Path
import os,pwd,subprocess
u=pwd.getpwnam('opsorchestrator');p=Path('/var/lib/ops-orchestrator')
for x in [p,*p.rglob('*')]:os.chown(x,u.pw_uid,u.pw_gid)
subprocess.run(['systemctl','reset-failed','ops-orchestrator.service'],check=True)
subprocess.run(['systemctl','start','ops-orchestrator.service'],check=True)
print('ORCHESTRATOR_RESTORED')
