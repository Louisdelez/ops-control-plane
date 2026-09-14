import subprocess,json
program='''import asyncio,json
from ops_native.broker import connect
from ops_native.mission_tools import MissionTools,COMMAND,ACTOR
async def main():
 async with connect(COMMAND,[],ACTOR) as broker:
  tools=MissionTools(broker,'11111111-1111-4111-8111-111111111111')
  result=await tools.context()
  assert result['mission']['project_id']=='infra-shared' and result['runbooks']
  assert all(r['project']=='infra-shared' for r in result['runbooks'])
  print(json.dumps({'fixed_identity':ACTOR,'mission_read':True,'runbooks':len(result['runbooks']),'mutations':0}))
asyncio.run(main())
'''
r=subprocess.run(['/usr/sbin/runuser','-u','hermesd','--','/opt/ops-native/venv/bin/python','-c',program],capture_output=True,text=True,check=True)
print(r.stdout.strip())
