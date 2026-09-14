"""Owner-authorized local backup correction, preserve old code and all archives."""
import hashlib,json,os,shutil,subprocess,time
from pathlib import Path
assert os.geteuid()==0
os.umask(0o077)
r=Path('/home/ops-user/ops-control-plane');target=Path('/usr/local/libexec/ops-telemetry/backup.py')
a=Path('/var/lib/ops-native-archives')/('telemetry-compression-'+str(time.time_ns()));a.mkdir()
source=r/'native_ops/telemetry/backup.py';compile(source.read_text(),str(source),'exec')
assert target.is_file() and not target.is_symlink()
shutil.copy2(target,a/'before.py');shutil.copy2(source,a/'candidate.py')
(a/'authorization.json').write_text(json.dumps({'authorization':'owner-local-construction-2026-09-14','sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'scope':'compress before encryption; preserve all historical formats and archives'}))
try:
 next=target.with_name('backup.next.py');shutil.copyfile(a/'candidate.py',next);next.chmod(0o644);os.replace(next,target)
 p=subprocess.run(['systemctl','start','ops-telemetry-backup.service'],capture_output=True,timeout=650)
 if p.returncode:raise ValueError('Backup failed')
 status=json.loads(Path('/var/lib/ops-telemetry-backups/latest.json').read_text())
 assert status['verified']
 result={'status':'installed_and_roundtrip_verified','archive':str(a),'new_snapshots':[{'rows':x['rows'],'encrypted_bytes':x['bytes'],'original_bytes':x.get('uncompressed_bytes'),'encoding':x.get('encoding')} for x in status['new_snapshots']]}
except BaseException:
 subprocess.run(['systemctl','stop','ops-telemetry-backup.service'],capture_output=True,timeout=30)
 shutil.copy2(a/'before.py',target)
 (a/'result.json').write_text(json.dumps({'status':'rolled_back','existing_archives_preserved':True}))
 raise
(a/'result.json').write_text(json.dumps(result))
e=r/'artifacts/completion-final-2026-09-14/telemetry-compression.json';e.write_text(json.dumps(result,indent=2));os.chown(e,1000,1000)
print(json.dumps(result))
