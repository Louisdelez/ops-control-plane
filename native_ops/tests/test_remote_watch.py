import json
import runpy
from pathlib import Path
WATCH=runpy.run_path(str(Path(__file__).resolve().parents[1]/'remote_watch.py'))

def observation(resource):
 expected=WATCH['EXPECTED'][resource]
 metadata={'units':{'status':'observed','rows':[[name,'loaded','active','running'] for name in expected['units']]},'containers':{'status':'observed','rows':[[name,'image','running'] for name in expected['containers']]},'filesystems':{'status':'observed','rows':[['size','used','avail'],['100000000','10000000','90000000']]}}
 return {'status':'succeeded','result':{'return_code':0,'stdout':json.dumps({'resource':resource,'status':'observed','metadata':metadata})}}

def test_broker_success_does_not_mask_failed_services_or_disk():
 assess=WATCH['assess'];action=observation('prod');assert assess('prod',action)==[]
 data=json.loads(action['result']['stdout']);data['metadata']['containers']['rows'][0][2]='exited'
 data['metadata']['filesystems']['rows'][1]=['100000000','99999999','1']
 action['result']['stdout']=json.dumps(data)
 assert assess('prod',action)==['containers:dowze-api:exited','disk_low']
 action['result']['truncated']=True
 assert assess('prod',action)==['observation_failed']

def test_unavailable_docker_is_expected_on_non_container_hosts_only():
 for resource in ['gamebox','prod']:
  action=observation(resource);data=json.loads(action['result']['stdout']);data['metadata']['containers']={'status':'unavailable'};action['result']['stdout']=json.dumps(data)
  assert WATCH['assess'](resource,action)==([] if resource=='gamebox' else ['containers_unavailable'])
