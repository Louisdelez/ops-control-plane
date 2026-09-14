from pathlib import Path
import json,subprocess
for name in ['atlas-local-repair','ops-native-project']:
 p=Path('/etc/sudoers.d')/name
 print('SUDOERS',name,p.read_text() if p.exists() else 'absent')
 p=Path('/usr/local/libexec')/name
 if p.exists():
  for line in p.read_text().splitlines():
   if any(k in line.lower() for k in ['expires','expiry','deadline','time.time','allowed','root','prefix','finish']):print(name,line[:200])
for path in ['/var/lib/ollama','/usr/share/ollama','/home/ops-user/.ollama','/var/lib/ops-native-archives','/var/lib/atlas-local-repair']:
 p=Path(path)
 if p.exists():
  r=subprocess.run(['du','-sh',path],capture_output=True,text=True);print('SIZE',r.stdout.strip())
