"""Remove only explicitly inventoried, downloadable Ollama weights and expired authorization."""
from pathlib import Path
import os,json,shutil,subprocess,time,hashlib,tarfile
assert os.geteuid()==0
ROOT=Path('/var/lib/ollama/models');OUT=Path('/home/ops-user/ops-control-plane/artifacts/audit-cleanup-2026-09-10')
archive=Path('/var/lib/ops-native-archives')/('cleanup-'+str(time.time_ns()));archive.mkdir(mode=0o700,parents=True)
report={'archive':str(archive),'removed_downloads':[],'changes':[]}
# A recovery inventory contains public model metadata, never the Ollama account key.
models={};blobs=set()
for p in (ROOT/'manifests').rglob('*'):
 if p.is_file():
  rel=p.relative_to(ROOT/'manifests').as_posix()
  if rel not in {'registry.ollama.ai/library/qwen3/0.6b','registry.ollama.ai/library/qwen3/1.7b','registry.ollama.ai/library/qwen3/4b','registry.ollama.ai/library/llama3.2/3b'}:raise RuntimeError('Unexpected model; preserve it')
  d=json.loads(p.read_text());models[rel]=d
  for layer in [d['config'],*d['layers']]:blobs.add(layer['digest'].replace(':','-'))
with tarfile.open(archive/'ollama-metadata.tar.gz','w:gz') as t:
 t.add(ROOT/'manifests',arcname='manifests')
 for name in sorted(blobs):
  p=ROOT/'blobs'/name
  if p.is_file() and p.stat().st_size<1048576:t.add(p,arcname='blobs/'+name)
(archive/'ollama-models.json').write_text(json.dumps(models,indent=2)+'\n')
shutil.copy2('/etc/systemd/system/ollama.service',archive/'ollama.service')
subprocess.run(['systemctl','disable','--now','ollama.service'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
assert subprocess.run(['systemctl','is-active','--quiet','ollama.service']).returncode!=0
for name in sorted(blobs):
 p=ROOT/'blobs'/name
 if p.is_symlink() or not p.is_file():raise RuntimeError('Unexpected blob')
 if p.stat().st_size>=1048576:
  # Verify the immutable downloaded object against its content address before removal.
  digest=hashlib.sha256()
  with p.open('rb') as f:
   while chunk:=f.read(8*1024*1024):digest.update(chunk)
  if name!='sha256-'+digest.hexdigest():raise RuntimeError('Model digest mismatch')
  report['removed_downloads'].append({'path':str(p),'bytes':p.stat().st_size,'sha256':digest.hexdigest()});p.unlink()
for rel in models:(ROOT/'manifests'/rel).unlink()
report['changes'].append('Ollama disabled; four downloaded models removed; metadata archived')
old=Path('/etc/sudoers.d/atlas-local-repair')
if old.exists():
 helper=Path('/usr/local/libexec/atlas-local-repair').read_text()
 if 'time.time()>1788989820' not in helper or time.time()<=1788989820:raise RuntimeError('Not the audited expired authorization')
 shutil.copy2(old,archive/old.name);old.unlink();report['changes'].append('Expired atlas-local-repair sudo rule removed')
# Only remove the retired Ollama expectation; preserve every remote target setting.
p=Path('/etc/ops-v1/inventory.json');data=json.loads(p.read_text());shutil.copy2(p,archive/'inventory-before.json')
for item in data['resources']:
 if item['id']=='dell-control':item['expected_services']=[s for s in item['expected_services'] if s!='ollama.service']
t=p.with_suffix('.cleanup-next');t.write_text(json.dumps(data,indent=2)+'\n');os.chmod(t,p.stat().st_mode&0o777);os.chown(t,p.stat().st_uid,p.stat().st_gid);os.replace(t,p)
report['changes'].append('Retired Ollama expectation removed from local inventory')
subprocess.run(['systemctl','reset-failed','ops-orchestrator-secrets.service'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
subprocess.run(['/usr/sbin/visudo','-cf','/etc/sudoers'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
report['bytes_removed']=sum(x['bytes'] for x in report['removed_downloads'])
p=OUT/'root-cleanup.json';p.write_text(json.dumps(report,indent=2)+'\n');os.chown(p,1000,1000)
print('RETIRED_LOCAL_CLEANUP',report['bytes_removed'],'bytes',str(archive))
