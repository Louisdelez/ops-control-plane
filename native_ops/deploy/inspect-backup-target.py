import subprocess
from pathlib import Path
for name in ['zulip-standard-database-1','zulip-standard-postgresql-1']:
 r=subprocess.run(['docker','inspect','--format','{{.Config.Image}}',name],capture_output=True,text=True)
 if r.returncode==0:print(name,r.stdout.strip())
for p in ['/etc/openbao-backup/recovery.age-recipient','/usr/bin/age']:
 print(p,Path(p).is_file())
r=subprocess.run(['docker','ps','--format','{{.Names}} {{.Image}}'],capture_output=True,text=True,check=True);print(r.stdout)
