"""Continuous observation with atomic archive/cursor commits and durable gap reports."""
import concurrent.futures
import json
import math
import os
from pathlib import Path
import runpy
import sqlite3
import threading
import time

STATE=Path('/var/lib/ops-telemetry')
HOSTS=('dell-control','edge-vps','nas','gamebox','prod')
STOP=threading.Event()

def database():
 db=sqlite3.connect(STATE/'logs.sqlite3',timeout=30)
 db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
 db.execute('CREATE TABLE IF NOT EXISTS entries(host TEXT,id TEXT,t REAL,source TEXT,service TEXT,level TEXT,message TEXT,received REAL,PRIMARY KEY(host,id))')
 db.execute('CREATE INDEX IF NOT EXISTS logs_time ON entries(host,t)')
 db.execute('CREATE TABLE IF NOT EXISTS stream_cursors(host TEXT,source TEXT,service TEXT,cursor TEXT,since INTEGER,checked REAL,PRIMARY KEY(host,source,service))')
 db.execute('CREATE TABLE IF NOT EXISTS stream_health(host TEXT,source TEXT,service TEXT,checked REAL,last_success REAL,status TEXT,count INTEGER,backlog INTEGER,PRIMARY KEY(host,source,service))')
 db.execute('CREATE TABLE IF NOT EXISTS stream_gaps(id INTEGER PRIMARY KEY,host TEXT,source TEXT,service TEXT,detected REAL,reason TEXT)')
 return db

def position(host,source,service):
 with database() as db:
  row=db.execute('SELECT cursor,since FROM stream_cursors WHERE host=? AND source=? AND service=?',(host,source,service)).fetchone()
 return (json.loads(row[0]),row[1]) if row else ({},int(time.time())-3600)

def commit(host,source,service,previous,since,value):
 """An interruption before commit replays the same IDs; an invalid batch advances nothing."""
 rows=value.get('records');cursor=value.get('cursor')
 if value.get('schema_version')!=1 or value.get('redacted') is not True or not isinstance(rows,list) or len(rows)>200 or not isinstance(cursor,dict):raise ValueError('Invalid batch')
 if value.get('unsupported_driver'):raise ValueError('Unsupported Docker log driver')
 serialized=json.dumps(cursor,sort_keys=True,allow_nan=False)
 if len(serialized)>2048:raise ValueError('Invalid cursor')
 for row in rows:
  if not isinstance(row,dict) or set(row)!={'id','t','source','service','level','message'}:raise ValueError('Invalid event')
  if not isinstance(row['id'],str) or len(row['id'])!=64 or any(c not in '0123456789abcdef' for c in row['id']):raise ValueError('Invalid identity')
  if not isinstance(row['t'],(int,float)) or not math.isfinite(row['t']) or row['t']<0:raise ValueError('Invalid timestamp')
  if row['source']!=source or not isinstance(row['service'],str) or len(row['service'])>128 or row['level'] not in {'debug','info','warning','error'}:raise ValueError('Invalid source')
  if not isinstance(row['message'],str) or len(row['message'])>1024:raise ValueError('Invalid message')
 now=time.time()
 with database() as db:
  db.execute('BEGIN IMMEDIATE')
  existing=db.execute('SELECT cursor FROM stream_cursors WHERE host=? AND source=? AND service=?',(host,source,service)).fetchone()
  if (json.loads(existing[0]) if existing else {})!=previous:raise ValueError('Concurrent cursor update')
  db.executemany('INSERT OR IGNORE INTO entries VALUES(?,?,?,?,?,?,?,?)',[(host,r['id'],r['t'],r['source'],r['service'],r['level'],r['message'],now) for r in rows])
  db.execute('INSERT OR REPLACE INTO stream_cursors VALUES(?,?,?,?,?,?)',(host,source,service,serialized,since,now))
  if value.get('gap'):db.execute('INSERT INTO stream_gaps(host,source,service,detected,reason) VALUES(?,?,?,?,?)',(host,source,service,now,'Source cursor or rotation no longer available'))
  db.execute('INSERT OR REPLACE INTO stream_health VALUES(?,?,?,?,?,?,?,?)',(host,source,service,now,now,'collecting',len(rows),int(bool(value.get('more')))))
 return len(rows)

def failed(host,source,service,status='unavailable'):
 with database() as db:
  db.execute('INSERT INTO stream_health VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(host,source,service) DO UPDATE SET checked=excluded.checked,status=excluded.status',(host,source,service,time.time(),None,status,0,0))

def collect_one(fetch,host,source,service=''):
 cursor,since=position(host,source,service)
 request={'action':'stream','source':source,'cursor':cursor,'since':max(since,int(cursor.get('last_t',since))-1)}
 if service:request['service']=service
 value=fetch(host,request,stream=True)
 if value.get('unsupported_driver'):
  failed(host,source,service,'unsupported_driver');return False
 commit(host,source,service,cursor,since,value)
 return bool(value.get('more'))

def activated(host):
 if host=='dell-control':return True
 # A pending/expired request must not create repeated refused SSH sessions.
 with sqlite3.connect('file:/var/lib/ops-broker/state.db?mode=ro',uri=True,timeout=5) as db:
  rows=db.execute("SELECT parameters_json FROM actions WHERE runbook_id='security.enable-logstream.v1' AND requested_by='codex-supervised' AND status='succeeded'").fetchall()
 return any(json.loads(r[0])=={'resource':host,'bundle_sha256':'4516ad2ab4a537b5620e528d671f9b587528f79446e3ad4506bad8a782c86a24'} for r in rows)

def worker(host):
 fetch=runpy.run_path('/usr/local/libexec/ops-logs/service.py')['fetch']
 sources=[];last_discovery=0
 while not STOP.is_set():
  backlog=False
  try:ready=activated(host)
  except Exception:ready=False
  if not ready:
   failed(host,'system','','activation_pending');failed(host,'docker','','activation_pending');STOP.wait(15);continue
  if time.monotonic()-last_discovery>60:
   try:
    value=fetch(host,{'action':'sources'},stream=True)
    if not value.get('available'):
     failed(host,'docker','', 'discovery_unavailable')
    else:
     sources=[row['id'] for row in value['containers']]
     failed(host,'docker','', 'source_limit' if value.get('truncated') else 'discovered')
    last_discovery=time.monotonic()
   except Exception:failed(host,'docker','','discovery_unavailable');last_discovery=time.monotonic()
  for source,service in [('system','')]+[('docker',s) for s in sources]:
   if STOP.is_set():break
   try:
    for _ in range(4):
     more=collect_one(fetch,host,source,service)
     if not more:break
    backlog=backlog or more
   except Exception:failed(host,source,service)
  STOP.wait(1 if backlog else 15)

if __name__=='__main__':
 import signal
 os.umask(0o027)
 signal.signal(signal.SIGTERM,lambda *_:STOP.set());signal.signal(signal.SIGINT,lambda *_:STOP.set())
 with database():pass
 with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
  futures=[pool.submit(worker,h) for h in HOSTS]
  # Fatal local storage errors terminate the unit visibly instead of silently losing a worker.
  while not STOP.wait(5):
   if any(f.done() for f in futures):
    STOP.set()
    for f in futures:
     if f.done():f.result()
    raise RuntimeError('Collection worker stopped')
