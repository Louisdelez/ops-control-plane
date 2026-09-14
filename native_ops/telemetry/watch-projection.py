"""Copy existing observer status, no additional remote command or privilege."""
import json,os
from pathlib import Path
source=Path('/home/ops-user/.local/state/ops-remote-watch/latest.json')
v=json.loads(source.read_text())
assert set(v['resources'])=={'nas','prod','edge-vps','gamebox'}
value={'checked_at':v['checked_at'],'resources':{k:{'status':x['status'],'problems':x['problems'],'action_id':x['action_id']} for k,x in v['resources'].items()}}
p=Path('/run/ops-telemetry/watch.next');p.write_text(json.dumps(value));p.chmod(0o640);os.chown(p,0,1000);p.replace(p.with_name('watch.json'))

# Confirm exact archive members in the most recent bundle against BOTH remote receipts.
try:
 import hashlib,time
 state=Path('/var/lib/ops-telemetry');backups=Path('/var/lib/ops-telemetry-backups')
 manifest=json.loads(Path('/var/lib/ops-recovery-bundles/latest.json').read_text())
 receipts={h:json.loads((Path('/var/lib/ops-periodic-recovery')/(h+'.latest.json')).read_text()) for h in ['nas','edge-vps']}
 confirmed=all(x['sha256']==manifest['sha256'] and x['status']=='copied_and_verified' for x in receipts.values())
 ledger_path=state/'replication-ledger.json';ledger=json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
 if confirmed:
  for member in manifest['members']:
   if not member.startswith('telemetry/') or not member.endswith('.sqlite3.age'):continue
   name=Path(member).name
   item=json.loads((backups/name.replace('.sqlite3.age','.json')).read_text())
   ledger[name]={'sha256':item['sha256'],'bundle_sha256':manifest['sha256'],'checked_at':min(x['checked_at'] for x in receipts.values()),'destinations':['nas','edge-vps']}
  tmp=state/'replication-ledger.next';tmp.write_text(json.dumps(ledger));tmp.chmod(0o640);os.chown(tmp,0,1000);tmp.replace(ledger_path)
 missing=[]
 for item in backups.glob('*.json'):
  if item.name=='latest.json':continue
  entry=json.loads(item.read_text())
  if ledger.get(entry['file'],{}).get('sha256')!=entry['sha256']:missing.append(entry['file'])
 status={'verified':confirmed,'checked_at':min(x['checked_at'] for x in receipts.values()) if confirmed else None,'pending_archives':len(missing),'replicated_archives':len(ledger),'destinations':{h:{'checked_at':r['checked_at'],'verified':r['sha256']==manifest['sha256']} for h,r in receipts.items()},'automatic_deletion':False}
 tmp=state/'offsite-status.next';tmp.write_text(json.dumps(status));tmp.chmod(0o640);os.chown(tmp,0,1000);tmp.replace(state/'offsite-status.json')
except (OSError,ValueError,KeyError):pass  # Preserve last verified receipt; UI and alert check its date.
