"""Bounded historical snapshot reader. No SQL, path or command accepted from the UI."""
import datetime,hashlib,json,os,sqlite3,stat,sys,time,zlib
from pathlib import Path
HOSTS={'dell-control','nas','prod','edge-vps','gamebox'}
def query(root,host,timestamp):
 if host not in HOSTS or not isinstance(timestamp,int) or not 0<timestamp<=time.time()+60:raise ValueError('Invalid historical request')
 day=datetime.datetime.fromtimestamp(timestamp,datetime.UTC).strftime('%Y-%m-%d')
 p=Path(root)/(day+'.sqlite3')
 if not p.exists():raise ValueError('Aucune archive pour cette date.')
 st=p.lstat()
 if not stat.S_ISREG(st.st_mode) or st.st_mode&0o022:raise ValueError('Archive non conforme')
 with sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True,timeout=2) as db:
  row=db.execute('SELECT t,sha256,payload FROM snapshots WHERE host=? AND t<=? AND t>=? ORDER BY t DESC LIMIT 1',(host,timestamp*1000,(timestamp-60)*1000)).fetchone()
 if not row:raise ValueError('Aucun relevé dans la minute précédant cette date.')
 if len(row[2])>1048576:raise ValueError('Archive trop volumineuse')
 dec=zlib.decompressobj();raw=dec.decompress(row[2],1048577)
 if len(raw)>1048576 or not dec.eof or hashlib.sha256(raw).hexdigest()!=row[1]:raise ValueError('Vérification de l’archive échouée')
 value=json.loads(raw);assert value['id']==host
 return {'archived_at':row[0]/1000,'host':value,'integrity_verified':True}
if __name__=='__main__':
 try:
  assert len(sys.argv)==3
  print(json.dumps({'ok':True,**query('/var/lib/ops-telemetry/full',sys.argv[1],int(sys.argv[2]))},separators=(',',':')))
 except Exception as e:
  message=str(e) if isinstance(e,ValueError) else 'Archive indisponible'
  print(json.dumps({'ok':False,'error':message}));raise SystemExit(1)
