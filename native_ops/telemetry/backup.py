"""Consistent daily encrypted telemetry snapshots with an isolated round-trip check.
No backup or measurement is deleted. Closed daily partitions are copied once;
the open partition and summary index receive a dated daily recovery point.
"""
import datetime,gzip,hashlib,json,os,shutil,sqlite3,subprocess,tempfile,time
from pathlib import Path
STATE=Path('/var/lib/ops-telemetry');BACKUPS=Path('/var/lib/ops-telemetry-backups')
RECIPIENT=Path('/etc/openbao-backup/recovery.age-recipient')
IDENTITY=Path('/home/ops-user/Documents/Recuperation-Ops-2026-09-11/PRIVE/cle-age.txt')
def sha(p):
 with p.open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def expand(source,target,expected_bytes):
 if type(expected_bytes) is not int or not 0<expected_bytes<=4*1024**3:raise ValueError('Invalid snapshot size')
 count=0
 with gzip.open(source,'rb') as incoming,target.open('xb') as out:
  while chunk:=incoming.read(min(1024*1024,expected_bytes-count+1)):
   count+=len(chunk)
   if count>expected_bytes:raise ValueError('Expanded snapshot exceeds manifest')
   out.write(chunk)
 if count!=expected_bytes:raise ValueError('Incomplete snapshot')
def check(p):
 with sqlite3.connect('file:'+str(p)+'?mode=ro',uri=True) as db:
  if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise ValueError('Invalid SQLite backup')
  tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
  table=next(t for t in ['snapshots','samples','entries'] if t in tables)
  return db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
def main():
 assert os.geteuid()==0
 os.umask(0o077);BACKUPS.mkdir(mode=0o700,exist_ok=True)
 today=datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d');stamp=datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%SZ');results=[]
 sources=[STATE/'history.sqlite3']
 if (STATE/'logs.sqlite3').exists():sources.append(STATE/'logs.sqlite3')
 for source in [*sources,*sorted((STATE/'full').glob('????-??-??.sqlite3'))]:
  sealed=source.parent.name=='full' and source.stem<today
  name=source.stem+('-sealed' if sealed else '-'+stamp)
  manifest=BACKUPS/(name+'.json')
  if sealed and manifest.exists():
   prior=json.loads(manifest.read_text())
   if sha(BACKUPS/prior['file'])!=prior['sha256']:raise ValueError('Existing encrypted archive differs')
   continue
  with tempfile.TemporaryDirectory(prefix='.backup-',dir=BACKUPS) as raw:
   work=Path(raw);snapshot=work/'snapshot.sqlite3'
   with sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True) as original,sqlite3.connect(snapshot) as backup:original.backup(backup)
   count=check(snapshot)
   size=snapshot.stat().st_size
   if not 0<size<=4*1024**3:raise ValueError('Snapshot exceeds restore bound')
   compressed=work/'snapshot.sqlite3.gz'
   with snapshot.open('rb') as incoming,gzip.open(compressed,'wb',compresslevel=6) as out:shutil.copyfileobj(incoming,out,1024*1024)
   output=work/'snapshot.sqlite3.age'
   subprocess.run(['/usr/bin/age','-R',str(RECIPIENT),'-o',str(output),str(compressed)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=180)
   restored=work/'restored.sqlite3'
   restored_compressed=work/'restored.sqlite3.gz'
   subprocess.run(['/usr/bin/age','-d','-i',str(IDENTITY),'-o',str(restored_compressed),str(output)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=180)
   expand(restored_compressed,restored,size)
   if sha(snapshot)!=sha(restored) or check(restored)!=count:raise ValueError('Restore differs')
   filename=name+'.sqlite3.age';destination=BACKUPS/filename
   if destination.exists():raise ValueError('Existing backup preserved')
   output.replace(destination)
   report={'file':filename,'sha256':sha(destination),'bytes':destination.stat().st_size,'rows':count,'source':str(source),'created_at':time.time(),'verified':True,'sealed_partition':sealed,'encoding':'gzip','uncompressed_bytes':size,'uncompressed_sha256':sha(snapshot)}
   manifest.write_text(json.dumps(report,indent=2)+'\n');results.append(report)
 report={'verified':True,'checked_at':time.time(),'archives':len(list(BACKUPS.glob('*.sqlite3.age'))),'new_snapshots':results,'automatic_deletion':False,'offsite_verified':False}
 (BACKUPS/'latest.json').write_text(json.dumps(report,indent=2)+'\n')
 # Public-to-owner status only; encrypted backup files remain root-private.
 status=STATE/'backup-status.next';status.write_text(json.dumps({k:v for k,v in report.items() if k!='new_snapshots'}));status.chmod(0o640);os.chown(status,0,1000);status.replace(STATE/'backup-status.json')
 print(json.dumps({'verified':True,'archives':report['archives'],'new':len(results)}))
if __name__=='__main__':main()
