import subprocess
from pathlib import Path
for container,command in [('zulip-standard-zulip-1','pg_dump'),('zulip-standard-database-1','pg_restore')]:
 print(subprocess.run(['docker','exec',container,command,'--version'],capture_output=True,text=True,check=True).stdout.strip())
p=Path('/var/lib/ops-zulip-backups/last-error.private')
if p.exists():
 for line in p.read_text().splitlines():
  if line.startswith('pg_restore: error:'):print(line[:250])
