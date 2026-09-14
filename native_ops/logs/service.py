"""Owner-only Unix API for bounded remote log queries and durable redacted archive."""
import json,os,pwd,runpy,shlex,signal,socket,socketserver,sqlite3,struct,subprocess,tempfile,threading,time
from pathlib import Path
HOSTS={'dell-control','nas','prod','edge-vps','gamebox'}
BASE=Path('/usr/local/libexec/ops-logs');SOURCE=(BASE/'collector.py').read_text();STATE=Path('/var/lib/ops-telemetry')
LIMIT=1024*1024
STREAM_V2_BUNDLE='71e1061ab888119ccb0149bda359fe0edc551be72346a5aff2e3b9925bcf1ad2'
BROKER_DATABASE=Path('/var/lib/ops-broker/state.db')

def stream_collector(host):
 if not (BASE/'stream-collector-v2.py').is_file():return 'stream-collector.py'
 if host=='dell-control':return 'stream-collector-v2.py'
 # Publish locally first; switch each remote only after its actual approved
 # extension succeeded. Pending approvals leave its existing reader in use.
 try:
  with sqlite3.connect('file:'+str(BROKER_DATABASE)+'?mode=ro',uri=True,timeout=5) as db:
   rows=db.execute("SELECT parameters_json FROM actions WHERE runbook_id='security.enable-logstream.v1' AND requested_by='codex-supervised' AND status='succeeded'").fetchall()
  if any(json.loads(r[0])=={'resource':host,'bundle_sha256':STREAM_V2_BUNDLE} for r in rows):return 'stream-collector-v2.py'
 except (OSError,ValueError,sqlite3.Error):pass
 return 'stream-collector.py'

def fetch_route(host,request,stream=False,alternate=False):
 if host not in HOSTS:raise ValueError('Machine invalide')
 payload=json.dumps(request).encode()
 collector=stream_collector(host) if stream else 'collector.py'
 source=(BASE/collector).read_text() if stream else SOURCE
 if host=='dell-control':
  command=['/usr/bin/python3','-I',str(BASE/collector)];identity={}
 else:
  worker=runpy.run_path('/usr/local/libexec/ops-runbooks/remote-details-worker');command=worker['command'](host,alternate);assert command[2]=='opsreader';command=command[4:];command[-1]='/usr/bin/python3 -I -c '+shlex.quote(source)
  account=pwd.getpwnam('opsreader');identity={'user':account.pw_uid,'group':account.pw_gid,'extra_groups':[]}
 with tempfile.TemporaryFile() as out,tempfile.TemporaryFile() as err:
  p=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=out,stderr=err,start_new_session=True,**identity)
  try:p.communicate(input=payload,timeout=35)
  except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait();raise ConnectionError('Délai de lecture dépassé')
  out.seek(0);raw=out.read(LIMIT+1)
  err.seek(0);failure=err.read(8192).lower()
  if p.returncode==255 and any(marker in failure for marker in [b'timed out',b'no route',b'unreachable',b'connection refused']):raise ConnectionError('Route indisponible')
  if p.returncode or len(raw)>LIMIT:raise ValueError('Accès aux journaux indisponible ou non activé')
  value=json.loads(raw)
  if value.get('schema_version')!=1 or value.get('error'):raise ValueError('Source de journaux indisponible')
  return value

def fetch(host,request,stream=False):
 try:return fetch_route(host,request,stream=stream)
 except ConnectionError:
  if host not in {'prod','gamebox'}:raise ValueError('Route de collecte indisponible') from None
  try:return fetch_route(host,request,stream=stream,alternate=True)
  except ConnectionError:raise ValueError('Routes de collecte indisponibles') from None

def handle(request):
 if not isinstance(request,dict):raise ValueError('Demande invalide')
 host=request.pop('host',None)
 if host not in HOSTS:raise ValueError('Machine invalide')
 action=request.get('action','read')
 with sqlite3.connect(STATE/'logs.sqlite3',timeout=5) as db:
  db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
  db.execute('CREATE TABLE IF NOT EXISTS entries(host TEXT,id TEXT,t REAL,source TEXT,service TEXT,level TEXT,message TEXT,received REAL,PRIMARY KEY(host,id))')
  db.execute('CREATE INDEX IF NOT EXISTS logs_time ON entries(host,t)')
  db.execute('CREATE TABLE IF NOT EXISTS entry_scopes(host TEXT,id TEXT,source TEXT,service TEXT,PRIMARY KEY(host,id,source,service))')
  db.execute('CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY,host TEXT,source TEXT,service TEXT,since INTEGER,until INTEGER,received REAL,count INTEGER,truncated INTEGER)')
  if action=='health':
   if set(request)!={'action'}:raise ValueError('Paramètre invalide')
   if not db.execute("SELECT 1 FROM sqlite_master WHERE name='stream_health'").fetchone():return {'schema_version':1,'host':host,'status':'not_installed','sources':[],'gaps':0}
   rows=db.execute('SELECT source,service,checked,last_success,status,count,backlog FROM stream_health WHERE host=? ORDER BY source,service',(host,)).fetchall()
   sources=[dict(zip(['source','service','checked','last_success','status','count','backlog'],r)) for r in rows]
   retired=[row for row in sources if row['status']=='retired']
   sources=[row for row in sources if row['status']!='retired']
   now=time.time()
   for row in sources:
    if now-row['checked']>180:row['status']='stale'
   gaps=db.execute('SELECT count(*) FROM stream_gaps WHERE host=?',(host,)).fetchone()[0]
   return {'schema_version':1,'host':host,'sources':sources,'retired_sources':retired,'gaps':gaps,'checked_at':now,'status':'collecting' if sources and all(r['status'] in {'collecting','discovered'} for r in sources) else 'attention'}
  if action=='archive':
   if set(request)-{'action','since','until','service','source','limit','before'}:raise ValueError('Paramètre invalide')
   since,until=request.get('since'),request.get('until')
   if not isinstance(since,int) or not isinstance(until,int) or not 0<=since<until:raise ValueError('Période invalide')
   params=[host,since,until];where='host=? AND t>=? AND t<=?'
   if request.get('source'):where+=' AND source=?';params.append(request['source'])
   if request.get('service'):
    where+=' AND (service=? OR EXISTS(SELECT 1 FROM entry_scopes s WHERE s.host=entries.host AND s.id=entries.id AND s.source=entries.source AND s.service=?))'
    params.extend([request['service'],request['service']])
   before=request.get('before')
   if before is not None:
    if not isinstance(before,list) or len(before)!=2 or not isinstance(before[0],(int,float)) or not isinstance(before[1],str) or len(before[1])>128:raise ValueError('Page invalide')
    where+=' AND (t<? OR (t=? AND id<?))';params.extend([before[0],before[0],before[1]])
   rows=db.execute('SELECT id,t,source,service,level,message FROM entries WHERE '+where+' ORDER BY t DESC,id DESC LIMIT 301',params).fetchall()
   return {'schema_version':1,'host':host,'records':[dict(zip(['id','t','source','service','level','message'],r)) for r in rows[:300]],'truncated':len(rows)>300,'next_cursor':[rows[299][1],rows[299][0]] if len(rows)>300 else None,'archived':True,'redacted':True,'checked_at':time.time()}
  value=fetch(host,request)
  if action=='read':
   rows=value.get('records',[])
   if not isinstance(rows,list) or len(rows)>300 or not value.get('redacted'):raise ValueError('Réponse invalide')
   for row in rows:
    if not isinstance(row.get('message'),str) or len(row['message'])>1024:raise ValueError('Message invalide')
    db.execute('INSERT OR IGNORE INTO entries VALUES(?,?,?,?,?,?,?,?)',(host,row['id'],row['t'],row['source'],row['service'],row['level'],row['message'],time.time()))
    # journalctl --unit includes manager messages whose emitting unit is init.scope.
    # Preserve both the emitter and membership in the requested service journal.
    if request.get('service'):db.execute('INSERT OR IGNORE INTO entry_scopes VALUES(?,?,?,?)',(host,row['id'],row['source'],request['service']))
   db.execute('INSERT INTO requests(host,source,service,since,until,received,count,truncated) VALUES(?,?,?,?,?,?,?,?)',(host,request['source'],request.get('service',''),request['since'],request['until'],time.time(),len(rows),int(value.get('truncated',False))))
  return {**value,'host':host,'archived':action=='read'}

semaphore=threading.BoundedSemaphore(4)
class Handler(socketserver.StreamRequestHandler):
 def handle(self):
  self.connection.settimeout(5)
  _,uid,_=struct.unpack('3i',self.connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
  if uid not in {0,1000}:return
  if not semaphore.acquire(blocking=False):self.wfile.write(b'{"error":"Lectures occupees"}\n');return
  try:
   raw=self.rfile.readline(4097)
   if len(raw)>4096:raise ValueError('Demande trop volumineuse')
   result=handle(json.loads(raw))
  except Exception as e:result={'error':str(e) if isinstance(e,ValueError) else 'Journaux indisponibles'}
  finally:semaphore.release()
  data=json.dumps(result,separators=(',',':'),allow_nan=False).encode()
  if len(data)>LIMIT:data=b'{"error":"Resultat trop volumineux"}'
  self.wfile.write(data+b'\n')
class Server(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
 daemon_threads=True
if __name__=='__main__':
 os.umask(0o027);path=Path('/run/ops-logs/query.sock');path.unlink(missing_ok=True)
 with Server(str(path),Handler) as server:path.chmod(0o660);server.serve_forever()
