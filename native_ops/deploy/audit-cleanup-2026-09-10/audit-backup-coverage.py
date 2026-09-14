from pathlib import Path
import os,json,subprocess,hashlib,base64
out=Path('/home/ops-user/ops-control-plane/artifacts/audit-cleanup-2026-09-10')
r={}
for p in [Path('/etc/cron.d'),Path('/etc/systemd/system')]:
 r[str(p)]=[f.name for f in p.iterdir() if any(x in f.name.lower() for x in ['backup','zulip','native'])]
r['zulip_backup_paths']={str(p):{'exists':p.exists(),'directory':p.is_dir()} for p in [Path('/opt/zulip/backups'),Path('/var/backups/zulip'),Path('/var/lib/ops-native-backups'),Path('/var/backups')]}
r['private_credential_modes']={}
for p in [Path('/var/lib/atlas-local-repair/credentials.json'),Path('/home/ops-user/Atlas-identifiants/installation.txt')]:
 if p.exists():r['private_credential_modes'][str(p)]={'uid':p.stat().st_uid,'mode':oct(p.stat().st_mode&0o777)}
code="""import importlib.metadata as m,hashlib,base64,json
out=[]
for p in m.distribution('ops-native').files:
 if str(p).startswith('ops_native/') and str(p).endswith('.py') and p.hash:
  actual=base64.urlsafe_b64encode(hashlib.sha256(p.locate().read_bytes()).digest()).decode().rstrip('=')
  if actual!=p.hash.value:out.append(str(p))
print(json.dumps(out))"""
p=subprocess.run(['/opt/ops-native/venv/bin/python','-I','-c',code],capture_output=True,text=True,check=True)
r['native_wheel_record_mismatches']=json.loads(p.stdout)
p=out/'backup-packaging.json';p.write_text(json.dumps(r,indent=2)+'\n');os.chown(p,1000,1000)
print(json.dumps(r,indent=2))
