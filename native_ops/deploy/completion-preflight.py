from pathlib import Path
import json,subprocess
for name in ['ops-orchestrator','ops-native']:
 p=Path('/etc')/name/'config.json'
 if p.exists():
  c=json.loads(p.read_text());print(name,'keys',sorted(c))
  if name=='ops-orchestrator':print('paths',c.get('paths'));print('catalogue_runtime',c.get('catalogue_runtime'));print('roles',sorted(c.get('roles',{})))
for unit in ['ops-orchestrator.service','ops-native.service']:
 r=subprocess.run(['systemctl','cat',unit],capture_output=True,text=True,check=True)
 print(unit,'\n'.join(x for x in r.stdout.splitlines() if x.startswith(('ExecStart=','User=','Group=','Requires=','Condition','EnvironmentFile='))))
p=Path('/opt/zulip/docker-compose.yml');print('compose_files',[str(x) for x in Path('/opt/zulip').glob('*compose*')])
print('Zulip backup schedule:')
r=subprocess.run(['docker','exec','zulip-standard-zulip-1','cat','/etc/cron.d/autobackup'],capture_output=True,text=True,check=True);print(r.stdout)
