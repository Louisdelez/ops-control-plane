import subprocess,json,os
from pathlib import Path
code="""from pathlib import Path
import json
r={}
for raw in ['/data/backups','/home/zulip/backups','/var/lib/zulip/backups']:
 p=Path(raw);r[raw]={'exists':p.exists(),'files':sum(1 for f in p.rglob('*') if f.is_file()) if p.exists() else 0}
r['cron_files_mentioning_backup']=[]
for folder in ['/etc/cron.d','/etc/cron.daily','/var/spool/cron/crontabs']:
 p=Path(folder)
 if p.exists():
  for f in p.iterdir():
   if f.is_file() and 'backup' in f.read_text(errors='ignore').lower():r['cron_files_mentioning_backup'].append(str(f))
print(json.dumps(r))"""
p=subprocess.run(['docker','exec','zulip-standard-zulip-1','python3','-c',code],capture_output=True,text=True,check=True)
r=json.loads(p.stdout);target=Path('/home/ops-user/ops-control-plane/artifacts/audit-cleanup-2026-09-10/zulip-backups.json');target.write_text(json.dumps(r,indent=2)+'\n');os.chown(target,1000,1000);print(json.dumps(r))
