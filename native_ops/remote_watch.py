"""Fixed, observation-only server watch. All remote reads go through the broker."""
import asyncio
import fcntl
import json
import os
from pathlib import Path
import time
import uuid
from ops_native.broker import connect

MISSION='b106ed2d-f46f-4b0c-9f7a-d0f594082526'
EXPECTED={
 'edge-vps':{'units':['nginx.service','ssh.service','wg-quick@wg0.service','wg-quick@wg1.service'],'containers':[]},
 'prod':{'units':['docker.service','ssh.service','wg-quick@wg0.service'],'containers':['dowze-api','dowze-web','supabase-db','traefik','prometheus','grafana']},
 'nas':{'units':['docker.service','ssh.service','wg-quick@wg0.service'],'containers':['nextcloud','nextcloud-db','nginx-proxy-manager','mailcowdockerized-postfix-mailcow-1','mailcowdockerized-dovecot-mailcow-1']},
 'gamebox':{'units':['ssh.service','velocity.service','minecraft.service','minecraft-survie.service','postgresql@17-main.service'],'containers':[]},
}

def assess(resource,action):
 problems=[]
 result=action.get('result') or {}
 if action.get('status')!='succeeded' or result.get('return_code')!=0 or result.get('timed_out') or result.get('truncated'):
  return ['observation_failed']
 try:
  observed=json.loads(result['stdout'])
  if observed.get('resource')!=resource or observed.get('status')!='observed':return ['resource_unavailable']
  metadata=observed['metadata']
  for kind,index,wanted in [('units',2,'active'),('containers',2,'running')]:
   expected=EXPECTED[resource][kind]
   if not expected:continue
   section=metadata[kind]
   if section.get('status')!='observed':problems.append(kind+'_unavailable');continue
   states={row[0]:row[index] for row in section['rows']}
   for name in expected:
    if states.get(name)!=wanted:problems.append(kind+':'+name+':'+states.get(name,'missing'))
   if kind=='units':
    for name,state in states.items():
     if state=='failed' and name not in expected:problems.append('units:'+name+':failed')
  disk=metadata['filesystems']
  if disk.get('status')!='observed':problems.append('disk_unavailable')
  else:
   for row in disk['rows'][1:]:
    size,used,available=map(int,row)
    if size<=0 or available<0:raise ValueError()
    if available<5*1024*1024 or available/size<0.10:problems.append('disk_low');break
 except (ValueError,KeyError,TypeError,IndexError):return ['invalid_observation']
 return problems

async def collect(broker,bucket):
 mission=await broker.call('get_mission',mission_id=MISSION)
 if mission['status'] not in {'open','active'}:raise ValueError('Watch mission is closed')
 books=await broker.call('list_authorized_runbooks')
 if not any(x['id']=='inventory.remote-services.v1' and x['action_class']=='A' for x in books):raise ValueError('Observation capability unavailable')
 report={'checked_at':time.time(),'mission_id':MISSION,'bucket':bucket,'resources':{}}
 for resource in EXPECTED:
  rid=uuid.uuid5(uuid.UUID(MISSION),str(bucket)+'/'+resource)
  action=await broker.call('request_runbook_action',request_id=str(rid),mission_id=MISSION,
   runbook_id='inventory.remote-services.v1',parameters={'resource':resource},
   reason='Surveillance périodique en lecture seule des services vérifiés le 11 septembre 2026.')
  if action.get('mission_id')!=MISSION or action.get('runbook_id')!='inventory.remote-services.v1' or action.get('parameters')!={'resource':resource}:raise ValueError('Mismatched broker action')
  problems=assess(resource,action)
  report['resources'][resource]={'action_id':action['id'],'status':'needs_review' if problems else 'healthy','problems':problems}
  await broker.call('add_mission_record',request_id=str(uuid.uuid5(rid,'observation')),mission_id=MISSION,kind='observation',
   content='Surveillance '+resource+' : '+('; '.join(problems) if problems else 'services attendus actifs, espace disque suffisant'),evidence=[action['id']])
 report['status']='needs_review' if any(r['problems'] for r in report['resources'].values()) else 'healthy'
 return report

async def main():
 folder=Path.home()/'.local/state/ops-remote-watch';folder.mkdir(parents=True,mode=0o700,exist_ok=True)
 with (folder/'watch.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  async with connect('/usr/bin/sudo',['-n','-u','opsbroker','-g','opsbroker','--','/usr/local/libexec/ops-broker/ops-broker-mcp-codex'],'codex-supervised') as broker:
   report=await collect(broker,int(time.time())//900)
  next_path=folder/'latest.next'
  with next_path.open('w') as f:json.dump(report,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
  os.replace(next_path,folder/'latest.json')
  print(json.dumps(report))
  return 0 if report['status']=='healthy' else 2

if __name__=='__main__':
 os.umask(0o077)
 try:code=asyncio.run(main())
 except Exception:raise SystemExit('Remote watch unavailable; existing broker evidence preserved') from None
 raise SystemExit(code)
