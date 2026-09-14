"""Exercise the actual fixed observation MCP without a model or message send."""
import asyncio,json,os,time
from pathlib import Path
from mcp import Client
from ops_native.tools import mcp,health
os.environ['OPS_NATIVE_MISSION_ID']='11111111-1111-4111-8111-111111111111'
async def main():
 try:
  await health(os.environ['OPS_NATIVE_MISSION_ID'],'/usr/local/libexec/ops-native-broker',[],'native-observer')
 except Exception as e:
  todo=[e];leaves=[]
  while todo:
   item=todo.pop()
   if isinstance(item,BaseExceptionGroup):todo.extend(item.exceptions)
   else:leaves.append({'type':type(item).__name__,'operation':str(item) if str(item).startswith('broker operation refused:') else 'observation_failed'})
  print(json.dumps({'errors':leaves}))
 async with Client(mcp) as client:
  result=await client.call_tool('get_service_health',{})
  value=result.structured_content
  if value is None:
   texts=[x.text for x in result.content if getattr(x,'type',None)=='text']
   try:value=json.loads(''.join(texts))
   except ValueError:value={'ok':False}
  if not isinstance(value,dict):value={'ok':False}
  print(json.dumps({'is_error':result.is_error,'fields':list(value),'text_blocks':len(result.content),'error':value.get('error')}))
  report={'checked_at':time.time(),'tool':'get_service_health','ok':value.get('ok') is True,'action_id':value.get('action_id'),'api_model_called':False,'message_sent':False}
  Path('/var/lib/hermes/native-ops/acceptance/health-tool.json').write_text(json.dumps(report))
  print(json.dumps(report));return 0 if report['ok'] else 1
if __name__=='__main__':
 try:raise SystemExit(asyncio.run(main()))
 except Exception:raise SystemExit('Observation verification failed; no raw output') from None
