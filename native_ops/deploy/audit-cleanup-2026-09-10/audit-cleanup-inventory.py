import os,json,subprocess,hashlib,stat,sys
from pathlib import Path
OUT=Path('/home/ops-user/ops-control-plane/artifacts/audit-cleanup-2026-09-10')
def run(args):
 p=subprocess.run(args,capture_output=True,text=True,timeout=30);return {'returncode':p.returncode,'output':p.stdout.strip()}
r={}
units=run(['systemctl','list-unit-files','--no-legend','--no-pager'])['output'].splitlines()
names=[x.split()[0] for x in units if any(x.startswith(k) for k in ['ops-','zulip','hermes','openbao','ollama'])]
r['units']={n:run(['systemctl','show',n,'-p','ActiveState','-p','SubState','-p','UnitFileState','-p','LoadState','-p','Result','-p','FragmentPath'])['output'] for n in names}
r['failed_units']=run(['systemctl','--failed','--no-legend','--no-pager'])
r['docker_containers']=run(['docker','ps','-a','--format','{{.Names}}\t{{.Image}}\t{{.Status}}'])
r['docker_usage']=run(['docker','system','df'])
r['docker_dangling']=run(['docker','images','--filter','dangling=true','--format','{{.ID}} {{.Size}}'])
r['encrypted_storage']=run(['lsblk','-o','NAME,TYPE,FSTYPE,MOUNTPOINTS'])
r['disk']=run(['df','-B1','/home'])
r['source_installed']={}
for root in ['/opt/ops-orchestrator/venv','/opt/ops-native/venv','/opt/ops-broker/venv']:
 p=Path(root)/'bin/python'
 if p.exists():r['source_installed'][root]=run([str(p),'-I','-c',"import importlib.metadata as m,json;print(json.dumps({d.metadata['Name']:d.version for d in m.distributions() if d.metadata['Name'].startswith(('ops-','mcp'))}))"])
r['historical_paths']={str(p):{'directory':p.is_dir(),'symlink':p.is_symlink()} for root in ['/opt','/usr/local/lib/ops-control-plane','/var/lib/ops-native-archives'] if Path(root).exists() for p in Path(root).iterdir()}
r['public_listener_addresses']=run(['ss','-ltnH'])
r['legacy_launchers']=[str(p) for root in ['/usr/share/applications','/usr/local/share/applications','/home/ops-user/.local/share/applications'] if Path(root).exists() for p in Path(root).glob('*.desktop') if any(x in p.name.lower() for x in ['atlas','ops','hermes','zulip'])]
r['audit_directory_modes']={str(p):oct(stat.S_IMODE(p.stat().st_mode)) for p in Path('/home/ops-user/Documents').glob('audit-acces*')}
r['sudoers_names']=[p.name for p in Path('/etc/sudoers.d').iterdir()]
r['polkit_names']=[p.name for folder in ['/etc/polkit-1/rules.d','/usr/share/polkit-1/actions'] for p in Path(folder).glob('*') if any(k in p.name for k in ['atlas','ops','repair'])]
p=OUT/'host-after.json'
if p.exists():raise RuntimeError('Evidence file already exists')
p.write_text(json.dumps(r,indent=2)+'\n');os.chown(p,1000,1000)
print('AUDIT_INVENTORY_SAVED',str(p))
print('FAILED_UNITS',r['failed_units']['output'])
print('CONTAINERS',r['docker_containers']['output'])
print('DOCKER_USAGE',r['docker_usage']['output'])
print('LEGACY_LAUNCHERS',r['legacy_launchers'])
