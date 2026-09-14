"""Update only the audited Python packages; retain each exact prior package."""
from pathlib import Path
import os,subprocess,json,shutil,time
assert os.geteuid()==0
root=Path('/home/ops-user/ops-control-plane');wheels=root/'artifacts/completion-2026-09-10/wheels'
archive=Path('/var/lib/ops-native-archives')/('python-security-'+str(time.time_ns()));archive.mkdir(mode=0o700)
targets={}
versions={'pip':'26.2.1','setuptools':'83.0.0','httpx2':'2.12.0','httpcore2':'2.12.0'}
for item in json.loads((root/'artifacts/audit-cleanup-2026-09-10/advisories.json').read_text())['matches']:
 if item['ecosystem']=='PyPI' and item['name'] in versions:
  for env in item['environments']:targets.setdefault(env,set()).add(item['name'])
results=[]
for number,(python,names) in enumerate(targets.items()):
 r=subprocess.run([python,'-c','import sysconfig; print(sysconfig.get_paths()["purelib"])'],capture_output=True,text=True,check=True)
 site=Path(r.stdout.strip());backup=archive/str(number);backup.mkdir(mode=0o700)
 files=[]
 for p in site.iterdir():
  if any(p.name==name or p.name.startswith(name+'-') and p.name.endswith('.dist-info') for name in names) or ('setuptools' in names and p.name in ['_distutils_hack','distutils-precedence.pth','pkg_resources']):
   files.append(p.name)
   if p.is_dir():shutil.copytree(p,backup/p.name,symlinks=True)
   else:shutil.copy2(p,backup/p.name)
 before=subprocess.run([python,'-m','pip','check'],capture_output=True,text=True)
 r=subprocess.run([python,'-m','pip','install','--no-index','--find-links',str(wheels),'--no-deps','--upgrade',*[n+'=='+versions[n] for n in sorted(names)]],capture_output=True,text=True)
 after=subprocess.run([python,'-m','pip','check'],capture_output=True,text=True)
 if r.returncode or (after.returncode and after.stdout!=before.stdout):
  # Restore only the selected distributions, not unrelated packages.
  for p in list(site.iterdir()):
   if any(p.name==name or p.name.startswith(name+'-') and p.name.endswith('.dist-info') for name in names):
    if p.is_dir():shutil.rmtree(p)
    else:p.unlink()
  for name in files:
   src=backup/name;dst=site/name
   if src.is_dir():shutil.copytree(src,dst,dirs_exist_ok=True,symlinks=True)
   else:shutil.copy2(src,dst)
  results.append({'python':python,'status':'rolled_back','dependency_check':after.stdout.strip()[:500]})
 else:
  # Owner's native venv remains owned by the owner after root-assisted upgrade.
  uid=1000 if python.startswith('/home/ops-user/') else 0
  if uid:
   for p in [site,*site.rglob('*')]:
    if not p.is_symlink():os.chown(p,1000,1000)
  results.append({'python':python,'status':'updated','versions':{n:versions[n] for n in names},'pip_check':after.returncode==0})
(archive/'result.json').write_text(json.dumps(results,indent=2));print(json.dumps({'archive':str(archive),'environments':results}))
