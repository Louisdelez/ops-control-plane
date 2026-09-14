"""Fixed observer identities, one collection per host per 15 seconds; no UI shell."""
import concurrent.futures,json,os,shutil,pwd,runpy,shlex,signal,sqlite3,subprocess,tempfile,time
from pathlib import Path
from engine import derive,summary
from collector import collect
from storage import Archive,ranges,initialize,save_summary
from exporter import exposition
from operations import observe
BASE=Path('/usr/local/libexec/ops-telemetry');RUNTIME=Path('/run/ops-telemetry');STATE=Path('/var/lib/ops-telemetry')
HOSTS={'dell-control':('Dell','Poste de contrôle'),'edge-vps':('VPS','Passerelle publique'),'prod':('Production','Applications'),'nas':('NAS','Stockage et services'),'gamebox':('Gamebox','Jeux et applications')}
SOURCE=(BASE/'collector.py').read_text()

def fetch(resource):
 if resource=='dell-control':return collect()
 worker=runpy.run_path('/usr/local/libexec/ops-runbooks/remote-details-worker')
 for alternate in [False,True] if resource in {'prod','gamebox'} else [False]:
  args=worker['command'](resource,alternate);assert args[2]=='opsreader'
  args[-1]='/usr/bin/python3 -I -c '+shlex.quote(SOURCE)
  with tempfile.TemporaryFile() as out,tempfile.TemporaryFile() as err:
   account=pwd.getpwnam('opsreader')
   p=subprocess.Popen(args[4:],user=account.pw_uid,group=account.pw_gid,extra_groups=[],stdin=subprocess.DEVNULL,stdout=out,stderr=err,start_new_session=True)
   try:p.wait(timeout=22)
   except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait();raise ValueError('timeout')
   out.seek(0);data=out.read(262145);err.seek(0);error=err.read(8192)
   if p.returncode==0 and len(data)<=262144:
    value=json.loads(data);assert value['schema_version']==1 and 0<value['cpu']['cores']<=8192 and 0<len(value['processes'])<=200;return value
   if p.returncode==64:raise ValueError('collector_not_enabled')
   reason=worker['failure_reason'](error)
   if reason=='ssh_or_remote_check_failed':
    markers=[x for x in ['Permission denied','Traceback','NameError','FileNotFoundError','ValueError','SyntaxError','Operation not permitted','runuser:','failed to execute','No such file'] if x.lower().encode() in error.lower()]
    raise ValueError('remote_failure:'+','.join(markers))
   if alternate or reason not in {'network_unreachable','connection_refused','timeout'}:raise ValueError(reason)
 raise ValueError('unavailable')

def main():
 os.umask(0o027);RUNTIME.mkdir(exist_ok=True);STATE.mkdir(exist_ok=True)
 db=sqlite3.connect(STATE/'history.sqlite3');db.execute('PRAGMA journal_mode=WAL');db.execute('CREATE TABLE IF NOT EXISTS samples(host TEXT,t INTEGER,data TEXT,PRIMARY KEY(host,t))');db.commit();initialize(db)
 archive=Archive(STATE/'full');previous={};latest={};last_saved={};pool=concurrent.futures.ThreadPoolExecutor(max_workers=5)
 while True:
  start=time.monotonic();futures={pool.submit(fetch,h):h for h in HOSTS};now=time.time()
  for future in concurrent.futures.as_completed(futures):
   host=futures[future];label,role=HOSTS[host]
   try:
    raw=future.result();value=derive(raw,previous.get(host));previous[host]=raw
    latest[host]={'id':host,'label':label,'role':role,'status':'online','received_at':time.time(),'metrics':value}
    if True:  # Persist every completed collection, not only minute summaries.
     point=summary(value);point['t']=int(time.time());save_summary(db,host,point);last_saved[host]=time.time()
   except Exception as e:
    known=str(e) if str(e) in {'collector_not_enabled','authentication_failed','host_key_unverified','timeout','connection_refused','network_unreachable'} else 'unavailable'
    old=latest.get(host,{});latest[host]={'id':host,'label':label,'role':role,'status':'unavailable','reason':known,'diagnostic_category':type(e).__name__,'diagnostic_errno':getattr(e,'errno',None),'diagnostic_file':str(getattr(e,'filename','')) if getattr(e,'filename','') in ['/usr/bin/env','/usr/bin/ssh'] else None,'diagnostic_markers':str(e) if str(e).startswith('remote_failure:') else None,'received_at':old.get('received_at'),'metrics':old.get('metrics')}
  now=time.time();db.commit()  # Owner requires permanent retention; never prune measurements.
  operations=observe(db,now)
  backup={}
  try:backup=json.loads((STATE/'backup-status.json').read_text())
  except (OSError,ValueError):pass
  offsite={}
  try:offsite=json.loads((STATE/'offsite-status.json').read_text())
  except (OSError,ValueError):pass
  backup['offsite']=offsite
  capacity=shutil.disk_usage(STATE)
  archive.append(list(latest.values()),now)
  metrics=Path('/var/lib/node-exporter/textfile/ops-infrastructure.prom');next_metrics=metrics.with_suffix('.next');next_metrics.write_text(exposition(list(latest.values()),now)+'ops_infra_backup_timestamp_seconds '+str(backup.get('checked_at',0))+'\nops_infra_archive_available_bytes '+str(capacity.free)+'\nops_infra_replication_timestamp_seconds '+str(offsite.get('checked_at') or 0)+'\n');next_metrics.chmod(0o644);os.replace(next_metrics,metrics)
  hosts=[{**value,'history':ranges(db,host,now)} for host,value in latest.items()]
  output={'schema_version':1,'generated_at':now,'interval_seconds':15,'retention_days':None,'retention_policy':'permanent','hosts':hosts,'archive':{'policy':'permanent','full_resolution_seconds':15,'oldest_summary':db.execute('SELECT MIN(t) FROM samples').fetchone()[0],'summary_count':db.execute('SELECT COUNT(*) FROM samples').fetchone()[0],'backup':backup,'available_bytes':capacity.free},'operations':operations}
  temp=RUNTIME/'dashboard.next';temp.write_text(json.dumps(output,separators=(',',':'),allow_nan=False));temp.chmod(0o640);os.replace(temp,RUNTIME/'dashboard.json')
  time.sleep(max(1,15-(time.monotonic()-start)))
if __name__=='__main__':main()
