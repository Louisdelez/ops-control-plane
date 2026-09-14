"""Add one fixed telemetry collector, preserving every old forced command."""
import ast,fcntl,hashlib,json,os,shutil,sys,time
from pathlib import Path
HELPER=Path('/usr/local/libexec/ops-observer-read');AUDIT=Path('/var/lib/ops-observer-install')
def mapping(source):
 return next(ast.literal_eval(n.value) for n in ast.parse(source).body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='COLLECTORS' for t in n.targets))
def main(p):
 assert os.geteuid()==0 and p['resource'] in {'prod','nas','gamebox','edge-vps'}
 os.umask(0o077)
 with (AUDIT/'install.lock').open('a') as lock:
  fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
  old=HELPER.read_text();new=p['reader'];assert hashlib.sha256(new.encode()).hexdigest()==p['reader_sha256']
  before,after=mapping(old),mapping(new)
  assert all(after.get(k)==v for k,v in before.items()) and len(after)==5
  compile(new,'fixed-read','exec')
  if old==new:return {'status':'already_installed','account':'ops-observer','logs':True}
  assert hashlib.sha256(old.encode()).hexdigest()==p['previous_reader_sha256'] and len(before)==4
  archive=AUDIT/('logs-'+str(time.time_ns()));archive.mkdir(mode=0o700);shutil.copy2(HELPER,archive/'reader.before.py');shutil.copy2(AUDIT/'current.json',archive/'current.before.json')
  try:
   temporary=HELPER.with_name('ops-observer-read.next');temporary.write_text(new);temporary.chmod(0o755);os.chown(temporary,0,0);os.replace(temporary,HELPER)
   state=json.loads((AUDIT/'current.json').read_text());state.update(reader_sha256=p['reader_sha256'],telemetry=True);(AUDIT/'current.json').write_text(json.dumps(state))
   result={'status':'installed','account':'ops-observer','logs':True,'previous_collectors_preserved':True,'reader_sha256':p['reader_sha256']};(archive/'result.json').write_text(json.dumps(result));return result
  except BaseException:shutil.copy2(archive/'reader.before.py',HELPER);shutil.copy2(archive/'current.before.json',AUDIT/'current.json');raise
if __name__=='__main__':
 try:
  raw=sys.stdin.buffer.read(131073);assert len(raw)<=131072;print(json.dumps(main(json.loads(raw))))
 except Exception as e:print(json.dumps({'status':'failed','category':type(e).__name__}));raise SystemExit(1)
