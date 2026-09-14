"""Prometheus exposition of approved observation data; bounded labels, no process args."""
import json,math,time

def exposition(hosts,now):
 lines=[]
 def emit(name,value,**labels):
  if not isinstance(value,(int,float)) or not math.isfinite(value):return
  labels=','.join(k+'='+json.dumps(str(v),ensure_ascii=True) for k,v in labels.items())
  lines.append('ops_infra_'+name+'{'+labels+'} '+str(float(value)))
 for h in hosts:
  host=h['id'];fresh=h['status']=='online' and now-(h.get('received_at') or 0)<50
  emit('up',int(fresh),host=host);emit('last_received_timestamp_seconds',h.get('received_at'),host=host)
  if not fresh or not h.get('metrics'):continue
  m=h['metrics'];emit('cpu_usage_percent',m['cpu'].get('usage_pct'),host=host);emit('cpu_cores',m['cpu']['cores'],host=host)
  for i,v in enumerate(m['cpu']['per_core']):emit('cpu_core_usage_percent',v,host=host,core=i)
  for i,v in enumerate(m['cpu']['load']):emit('load',v,host=host,minutes=[1,5,15][i])
  emit('uptime_seconds',m['uptime_seconds'],host=host);emit('process_count',m['process_count'],host=host)
  for k,v in m['memory'].items():emit('memory_'+k,v,host=host)
  for d in m['filesystems']:
   for k in ['total_bytes','used_bytes','available_bytes','inodes_total','inodes_free']:emit('filesystem_'+k,d.get(k),host=host,mount=d['mount'],device=d['device'],filesystem=d['filesystem'])
  for n in m['network']:
   for k in ['rx_bytes_per_second','tx_bytes_per_second','rx_errors','tx_errors','rx_dropped','tx_dropped','speed_mbps']:emit('network_'+k,n.get(k),host=host,interface=n['name'],physical=int(n['physical']))
  for d in m['disk_io']:
   for k in ['read_bytes_per_second','write_bytes_per_second','busy_ms_per_second']:emit('disk_'+k,d.get(k),host=host,device=d['name'])
  for t in m['temperatures']:emit('temperature_celsius',t['celsius'],host=host,sensor=t['name'])
  for g in m['gpus']:
   for k in ['usage_pct','memory_used_mib','memory_total_mib','power_w']:emit('gpu_'+k,g.get(k),host=host,gpu=g['name'])
 lines.append('ops_infra_collection_timestamp_seconds '+str(now))
 return '\n'.join(lines)+'\n'
