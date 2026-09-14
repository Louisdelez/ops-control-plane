"""Fixed, bounded journal/Docker log reader. Inputs are data, never commands."""
import datetime,hashlib,json,os,re,resource,subprocess,sys,tempfile,time
NAME=re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}$')
SECRET_LABEL=re.compile(r'(?i)((?:password|passwd|pwd|secret|token|api[_ -]?key|authorization|cookie|credential|private[_ -]?key)\s*[\"\']?\s*[:=]\s*)(?:\"[^\"]*\"|\'[^\']*\'|[^\s,;]+)')
ENTROPY=re.compile(r'(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{28,}(?![A-Za-z0-9])')
def redact(message):
 text=str(message)
 text=re.sub(r'-----BEGIN[^-]*PRIVATE KEY-----.*?(?:-----END[^-]*PRIVATE KEY-----|$)','[CLE MASQUEE]',text,flags=re.S)
 text=re.sub(r'(?i)\bBearer\s+\S+','Bearer [MASQUE]',text)
 text=re.sub(r'([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s:@]+:[^@/\s]+@',r'\1[MASQUE]@',text)
 text=SECRET_LABEL.sub(r'\1[MASQUE]',text)
 text=ENTROPY.sub('[IDENTIFIANT MASQUE]',text)
 return ''.join(c for c in text if c=='\t' or ord(c)>=32)[:1024]

import stat
from pathlib import Path
# Each request names a fixed source; no path or command is accepted from the client.
BATCH=200

def command(args):
 with tempfile.TemporaryFile() as out:
  p=subprocess.run(args,stdin=subprocess.DEVNULL,stdout=out,stderr=subprocess.DEVNULL,timeout=12,env={'PATH':'/usr/bin:/bin','LANG':'C'})
  out.seek(0);raw=out.read(2*1024*1024+1)
  if p.returncode or len(raw)>2*1024*1024:raise ValueError('Source indisponible')
  return raw

def stamp(value):return datetime.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
def event(t,source,service,message,identity,level='info'):
 return {'id':hashlib.sha256(identity.encode()).hexdigest(),'t':t,'source':source,'service':service,'level':level,'message':redact(message)}

def collect_stream(p):
 if not isinstance(p,dict) or set(p)-{'action','source','service','cursor','since'}:raise ValueError('Paramètre invalide')
 if p.get('action')=='status':
  r=subprocess.run(['/usr/bin/systemctl','is-system-running'],capture_output=True,timeout=8)
  value=r.stdout.decode('ascii').strip()
  if value not in {'initializing','starting','running','degraded','maintenance','stopping','offline','unknown'}:raise ValueError('État inconnu')
  return {'schema_version':1,'systemd_state':value}
 if p.get('action')=='sources':
  rows=[];available=True
  try:
   for line in command(['/usr/bin/docker','ps','-a','--no-trunc','--format','{{.ID}} {{.Names}}']).decode().splitlines():
    identifier,name=line.split(' ',1)
    if re.fullmatch('[a-f0-9]{64}',identifier) and NAME.fullmatch(name):rows.append({'id':identifier,'name':name})
  except (OSError,ValueError,subprocess.TimeoutExpired):available=False
  return {'schema_version':1,'containers':rows[:256],'available':available,'truncated':len(rows)>256}
 if p.get('action')!='stream' or p.get('source') not in {'system','docker'}:raise ValueError('Source invalide')
 cursor=p.get('cursor') or {};since=p.get('since',int(time.time())-3600)
 if not isinstance(cursor,dict) or len(json.dumps(cursor))>2048 or not isinstance(since,int) or not 0<=since<=time.time()+60:raise ValueError('Curseur invalide')
 records=[];gap=False;more=False
 if p['source']=='system':
  current=cursor.get('journal','')
  if not isinstance(current,str) or (current and not re.fullmatch('[A-Za-z0-9;=_-]{1,1024}',current)):raise ValueError('Curseur journal invalide')
  args=['/usr/bin/journalctl','--no-pager','--output=json','--lines=+'+str(BATCH+1)]
  try:raw=command(args+(['--after-cursor='+current] if current else ['--since=@'+str(since)]))
  except ValueError:
   if not current:raise
   gap=True;raw=command(args+['--since=@'+str(since)])
  rows=[json.loads(line) for line in raw.decode().splitlines() if line.strip()];more=len(rows)>BATCH
  new=dict(cursor)
  for row in rows[:BATCH]:
   c=row['__CURSOR'];t=int(row['__REALTIME_TIMESTAMP'])/1e6;unit=row.get('_SYSTEMD_UNIT') or row.get('SYSLOG_IDENTIFIER') or 'system'
   if not isinstance(unit,str) or not NAME.fullmatch(unit):unit='system'
   priority=int(row.get('PRIORITY',6));level='error' if priority<=3 else 'warning' if priority==4 else 'debug' if priority>=7 else 'info'
   records.append(event(t,'system',unit,row.get('MESSAGE',''),'journal:'+c,level));new={'journal':c,'last_t':t}
 else:
  identifier=p.get('service','')
  if not isinstance(identifier,str) or not re.fullmatch('[a-f0-9]{64}',identifier):raise ValueError('Conteneur invalide')
  template='[{{json .Name}},{{json .LogPath}},{{json .HostConfig.LogConfig.Type}}]'
  name,path,driver=json.loads(command(['/usr/bin/docker','inspect','--format',template,identifier]))
  name=name.lstrip('/')
  if not NAME.fullmatch(name):raise ValueError('Nom invalide')
  if driver!='json-file' or not path:return {'schema_version':1,'records':[],'cursor':cursor,'more':False,'gap':False,'redacted':True,'unsupported_driver':driver or 'unknown'}
  log=Path(path)
  if not log.is_absolute() or log.name!=identifier+'-json.log':raise ValueError('Chemin Docker non conforme')
  prior_inode=cursor.get('inode');offset=cursor.get('offset',0)
  if (prior_inode is not None and (type(prior_inode) is not int or prior_inode<=0)) or type(offset) is not int or not 0<=offset<=2**63:raise ValueError('Position invalide')
  current_stat=log.stat();selected=log
  if prior_inode and prior_inode!=current_stat.st_ino:
   rotated=[f for f in log.parent.glob(log.name+'.*') if not f.name.endswith('.gz') and not f.is_symlink() and f.stat().st_ino==prior_inode]
   if rotated:selected=rotated[0]
   else:gap=True;offset=0
  fd=os.open(selected,os.O_RDONLY|os.O_NOFOLLOW)
  with os.fdopen(fd,'rb') as f:
   st=os.fstat(f.fileno())
   if not stat.S_ISREG(st.st_mode) or st.st_uid!=0:raise ValueError('Journal Docker non conforme')
   if offset>st.st_size:gap=True;offset=0
   f.seek(offset)
   for _ in range(BATCH):
    pos=f.tell();raw=f.readline(65537)
    if not raw:break
    if len(raw)>65536:raise ValueError('Message Docker trop volumineux')
    if not raw.endswith(b'\n'):f.seek(pos);break
    row=json.loads(raw);t=stamp(row['time']);message=row.get('log','')
    level='error' if row.get('stream')=='stderr' else 'info'
    records.append(event(t,'docker',name,message,'docker:'+identifier+':'+str(st.st_ino)+':'+str(pos),level))
   offset=f.tell();more=offset<os.fstat(f.fileno()).st_size
   new={'inode':st.st_ino,'offset':offset,'last_t':records[-1]['t'] if records else cursor.get('last_t',since)}
   if selected!=log and not more:new={'inode':current_stat.st_ino,'offset':0,'last_t':new['last_t']};more=True
 return {'schema_version':1,'records':records,'cursor':new,'more':more,'gap':gap,'redacted':True,'checked_at':time.time()}

if __name__=='__main__':
 try:
  resource.setrlimit(resource.RLIMIT_FSIZE,(3*1024*1024,3*1024*1024))
  raw=sys.stdin.buffer.read(8193)
  if len(raw)>8192:raise ValueError('Requête trop longue')
  print(json.dumps(collect_stream(json.loads(raw)),separators=(',',':'),allow_nan=False))
 except Exception:print(json.dumps({'schema_version':1,'error':'Source de collecte indisponible'}));raise SystemExit(1)
