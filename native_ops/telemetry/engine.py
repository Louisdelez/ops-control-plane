"""Counter deltas and bounded history for the local infrastructure dashboard."""
import copy,math

def delta(now,old,seconds):
 if old is None or seconds<=0 or now<old:return None
 return (now-old)/seconds

def derive(raw,previous):
 value=copy.deepcopy(raw);elapsed=raw['uptime_seconds']-previous['uptime_seconds'] if previous and previous['boot_id']==raw['boot_id'] else 0
 usable=previous if 0<elapsed<120 else None
 ticks=value['cpu'].pop('ticks');prior=usable['cpu']['ticks'] if usable else {}
 def cpu(key):
  if key not in prior:return None
  a,b=ticks[key],prior[key];total=sum(a)-sum(b);idle=(a[3]+a[4])-(b[3]+b[4])
  return max(0,min(100,100*(total-idle)/total)) if total>0 and idle>=0 else None
 value['cpu']['usage_pct']=cpu('cpu');value['cpu']['per_core']=[cpu(k) for k in sorted(ticks,key=lambda k:int(k[3:]) if k!='cpu' else -1) if k!='cpu']
 for field,keys in [('network',['rx_bytes','tx_bytes']),('disk_io',['read_bytes','write_bytes','busy_ms'])]:
  old={n['name']:n for n in usable[field]} if usable else {}
  for item in value[field]:
   for key in keys:item[key+'_per_second']=delta(item[key],old.get(item['name'],{}).get(key),elapsed) if usable else None
 prior_proc={p['pid']:p for p in usable['processes']} if usable else {}
 for p in value['processes']:
  before=prior_proc.get(p['pid']);rate=delta(p['cpu_seconds'],before['cpu_seconds'],elapsed) if before and before['start_ticks']==p['start_ticks'] else None
  p['cpu_pct']=rate*100 if rate is not None else None
  p['memory_pct']=p['rss_bytes']/value['memory']['total_bytes']*100 if value['memory']['total_bytes'] else None
 value['processes'].sort(key=lambda p:(p['cpu_pct'] or 0,p['rss_bytes']),reverse=True)
 return value

def summary(value):
 mem=value['memory'];fs=value['filesystems'];net=[n for n in value['network'] if n['physical']];io=[d for d in value['disk_io'] if not d['name'].startswith(('dm-','md'))]
 def total(items,key):
  numbers=[p.get(key) for p in items];return sum(numbers) if numbers and all(v is not None for v in numbers) else None
 return {'t':value['sample_at'],'cpu':value['cpu']['usage_pct'],'cores':value['cpu']['cores'],'memory_used':mem['used_bytes'],'memory_total':mem['total_bytes'],'storage_used':sum(d['used_bytes'] for d in fs),'storage_total':sum(d['total_bytes'] for d in fs),'rx':total(net,'rx_bytes_per_second'),'tx':total(net,'tx_bytes_per_second'),'read':total(io,'read_bytes_per_second'),'write':total(io,'write_bytes_per_second'),'processes':value['process_count']}
