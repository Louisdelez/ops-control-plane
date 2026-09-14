"""Persist monitoring state transitions; never infer recovery from missing monitoring."""
import json,time,urllib.request
from pathlib import Path

def observe(db,now):
 db.execute('CREATE TABLE IF NOT EXISTS incidents(id INTEGER PRIMARY KEY,source TEXT,identity TEXT,host TEXT,title TEXT,severity TEXT,opened REAL,last_seen REAL,resolved REAL)')
 db.execute('CREATE UNIQUE INDEX IF NOT EXISTS open_incident ON incidents(source,identity) WHERE resolved IS NULL')
 result={'sources':{},'active':[],'history':[]}
 feeds={}
 try:
  with urllib.request.urlopen('http://127.0.0.1:9090/api/v1/alerts',timeout=2) as f:
   raw=f.read(1048577)
  if len(raw)>1048576:raise ValueError('oversize')
  v=json.loads(raw);assert v['status']=='success'
  feeds['prometheus']=[{'identity':json.dumps(a['labels'],sort_keys=True),'host':a['labels'].get('host',a['labels'].get('instance','local')),'title':a['labels'].get('alertname','Alerte'),'severity':a['labels'].get('severity','warning')} for a in v['data']['alerts'] if a['state']=='firing']
  result['sources']['prometheus']={'status':'ok','checked_at':now}
 except Exception:result['sources']['prometheus']={'status':'unavailable'}
 try:
  watch=json.loads(Path('/run/ops-telemetry/watch.json').read_text());assert now-watch['checked_at']<1200
  feeds['services']=[]
  result['services']={'checked_at':watch['checked_at'],'resources':watch['resources']}
  for host,item in watch['resources'].items():
   for issue in item.get('problems',[]):feeds['services'].append({'identity':host+':'+issue,'host':host,'title':issue,'severity':'warning'})
  result['sources']['services']={'status':'ok','checked_at':watch['checked_at']}
 except Exception:result['sources']['services']={'status':'unavailable'}
 for source,alerts in feeds.items():
  seen=set()
  for alert in alerts:
   seen.add(alert['identity'])
   row=db.execute('SELECT id FROM incidents WHERE source=? AND identity=? AND resolved IS NULL',(source,alert['identity'])).fetchone()
   if row:db.execute('UPDATE incidents SET last_seen=? WHERE id=?',(now,row[0]))
   else:db.execute('INSERT INTO incidents(source,identity,host,title,severity,opened,last_seen) VALUES(?,?,?,?,?,?,?)',(source,alert['identity'],alert['host'],alert['title'],alert['severity'],now,now))
  for id,identity in db.execute('SELECT id,identity FROM incidents WHERE source=? AND resolved IS NULL',(source,)):
   if identity not in seen:db.execute('UPDATE incidents SET resolved=? WHERE id=?',(now,id))
 cols=['id','source','host','title','severity','opened','last_seen','resolved']
 for key,where in [('active','WHERE resolved IS NULL'),('history','')]:result[key]=[dict(zip(cols,row)) for row in db.execute('SELECT '+','.join(cols)+' FROM incidents '+where+' ORDER BY opened DESC LIMIT 100')]
 db.commit();return result
