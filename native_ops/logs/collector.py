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

def run(args):
 with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as error:
  p=subprocess.run(args,stdin=subprocess.DEVNULL,stdout=output,stderr=error,timeout=15,env={'PATH':'/usr/bin:/bin','LANG':'C','SYSTEMD_COLORS':'0'})
  if p.returncode:raise ValueError('Source de journaux indisponible')
  output.seek(0);raw=output.read(8*1024*1024+1)
  if len(raw)>8*1024*1024:raise ValueError('Réduisez la période demandée')
  return raw.decode('utf-8','replace')

def validate(p):
 if not isinstance(p,dict) or set(p)-{'action','source','service','since','until','limit'}:raise ValueError('Paramètres invalides')
 action=p.get('action','read')
 if action not in {'sources','read'}:raise ValueError('Action invalide')
 if action=='sources':return p
 if p.get('source') not in {'system','docker'}:raise ValueError('Source invalide')
 service=p.get('service','')
 if not isinstance(service,str) or (service and not NAME.fullmatch(service)):raise ValueError('Service invalide')
 if p['source']=='docker' and not service:raise ValueError('Choisissez un conteneur')
 if p['source']=='system' and service and not service.endswith('.service'):raise ValueError('Unité invalide')
 since,until=p.get('since'),p.get('until')
 if not isinstance(since,int) or not isinstance(until,int) or not 0<=since<until<=time.time()+60 or until-since>86400:raise ValueError('La période doit être comprise entre une seconde et24heures')
 if not isinstance(p.get('limit',300),int) or not 1<=p.get('limit',300)<=300:raise ValueError('Limite invalide')
 return p

def collect(p):
 p=validate(p)
 if p.get('action')=='sources':
  sources=[]
  try:
   raw=run(['/usr/bin/systemctl','list-units','--type=service','--all','--plain','--no-legend','--no-pager'])
   for line in raw.splitlines():
    name=line.lstrip('● ').split()[0] if line.strip() else ''
    if NAME.fullmatch(name) and name.endswith('.service'):sources.append({'source':'system','service':name})
  except (ValueError,subprocess.TimeoutExpired):pass
  try:
   for name in run(['/usr/bin/docker','ps','-a','--format','{{.Names}}']).splitlines():
    if NAME.fullmatch(name):sources.append({'source':'docker','service':name})
  except (ValueError,OSError,subprocess.TimeoutExpired):pass
  return {'schema_version':1,'sources':sources[:512],'checked_at':time.time()}
 limit=p.get('limit',300);records=[]
 if p['source']=='system':
  args=['/usr/bin/journalctl','--no-pager','--output=json','--reverse','-n',str(limit+1),'--since=@'+str(p['since']),'--until=@'+str(p['until'])]
  if p.get('service'):args+=['--unit='+p['service']]
  for line in run(args).splitlines():
   try:
    item=json.loads(line);ts=int(item['__REALTIME_TIMESTAMP'])/1000000
    service=item.get('_SYSTEMD_UNIT') or item.get('SYSLOG_IDENTIFIER') or 'system';message=redact(item.get('MESSAGE',''))
    if not NAME.fullmatch(service):service='system'
    priority=int(item.get('PRIORITY',6));level='error' if priority<=3 else 'warning' if priority==4 else 'debug' if priority>=7 else 'info'
    records.append({'t':ts,'service':service,'level':level,'message':message})
   except (KeyError,ValueError,TypeError):continue
 else:
  args=['/usr/bin/docker','logs','--timestamps','--since',str(p['since']),'--until',str(p['until']),'--tail',str(limit+1),p['service']]
  # Docker uses stderr for container stderr. Preserve both streams, never expose CLI errors.
  with tempfile.TemporaryFile() as output:
   proc=subprocess.run(args,stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,timeout=15,env={'PATH':'/usr/bin:/bin','LANG':'C'})
   if proc.returncode:raise ValueError('Journaux du conteneur indisponibles')
   output.seek(0);raw=output.read(8*1024*1024+1)
   if len(raw)>8*1024*1024:raise ValueError('Réduisez la période demandée')
  for line in raw.decode('utf-8','replace').splitlines():
   try:
    stamp,message=line.split(' ',1);ts=datetime.datetime.fromisoformat(stamp.replace('Z','+00:00')).timestamp()
    level='error' if re.search(r'\b(error|fatal|panic|exception)\b',message,re.I) else 'warning' if re.search(r'\b(warn|warning)\b',message,re.I) else 'info'
    records.append({'t':ts,'service':p['service'],'level':level,'message':redact(message)})
   except (ValueError,TypeError):continue
 for row in records:
  row['source']=p['source'];row['id']=hashlib.sha256(json.dumps(row,sort_keys=True).encode()).hexdigest()
 records.sort(key=lambda r:r['t'],reverse=True)
 return {'schema_version':1,'records':records[:limit],'truncated':len(records)>limit,'checked_at':time.time(),'redacted':True,'scope':{'source':p['source'],'service':p.get('service',''),'since':p['since'],'until':p['until']}}
if __name__=='__main__':
 try:
  resource.setrlimit(resource.RLIMIT_FSIZE,(8*1024*1024,8*1024*1024))
  raw=sys.stdin.buffer.read(4097)
  if len(raw)>4096:raise ValueError('Demande trop volumineuse')
  print(json.dumps(collect(json.loads(raw)),separators=(',',':'),allow_nan=False))
 except Exception:print(json.dumps({'schema_version':1,'error':'Lecture des journaux indisponible ou demande invalide'}));raise SystemExit(1)
