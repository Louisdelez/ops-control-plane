#!/usr/bin/python3 -I
"""Read one fixed remote systemd state through the forced observer account."""
import json,os,runpy,shlex,signal,subprocess,sys,tempfile,time
from pathlib import Path
ALIASES={'edge-vps':'vps','prod':'prod','nas':'nas','gamebox':'gamebox'}
STATES={'initializing','starting','running','degraded','maintenance','stopping','offline','unknown'}
DIRECTORY=Path('/run/ops-remote-preflight')
def command(resource,alternate=False):
 # Compatibility for already-reviewed seed/publication helpers; no personal key.
 return runpy.run_path('/usr/local/libexec/ops-runbooks/remote-admin-transport.py')['command'](resource,alternate)
def observer_command(resource,alternate=False):
 worker=runpy.run_path('/usr/local/libexec/ops-runbooks/remote-details-worker')
 args=worker['command'](resource,alternate)
 if args[2]!='opsreader':raise ValueError('Observer identity required')
 collector=Path('/usr/local/libexec/ops-logs/stream-collector.py').read_text()
 args[-1]='/usr/bin/python3 -I -c '+shlex.quote(collector)
 return args
def failure_reason(raw):
 text=raw.lower()
 for markers,reason in [
  ((b'host key verification failed',b'remote host identification has changed'),'host_key_unverified'),
  ((b'permission denied',b'no supported authentication methods'),'authentication_failed'),
  ((b'no route to host',b'network is unreachable'),'network_unreachable'),
  ((b'connection refused',),'connection_refused'),
  ((b'connection timed out',b'operation timed out'),'timeout'),
  ((b'could not resolve hostname',),'name_resolution_failed')]:
  if any(marker in text for marker in markers):return reason
 return 'ssh_or_remote_check_failed'

def observe_route(resource,alternate=False):
 # Output is bounded on disk and never returned verbatim; no remote content is logged.
 with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
  process=subprocess.Popen(observer_command(resource,alternate),stdin=subprocess.PIPE,stdout=output,stderr=errors,start_new_session=True)
  try:
   try:process.communicate(input=b'{"action":"status"}',timeout=10);code=process.returncode
   except subprocess.TimeoutExpired:return {'status':'unavailable','reason':'timeout'}
   output.seek(0);raw=output.read(256)
   try:state=json.loads(raw)['systemd_state']
   except (UnicodeDecodeError,ValueError,KeyError,TypeError):state=''
   if code in {0,1} and state in STATES:return {'status':'observed','systemd_state':state}
   errors.seek(0)
   return {'status':'unavailable','reason':failure_reason(errors.read(8192))}
  finally:
   if process.poll() is None:
    try:os.killpg(process.pid,signal.SIGKILL)
    except ProcessLookupError:pass
    process.wait()
def observe(resource):
 result=observe_route(resource)
 if resource in {'gamebox','prod'} and result.get('reason') in {'network_unreachable','connection_refused','timeout'}:
  alternate=observe_route(resource,True)
  if alternate['status']=='observed':return alternate
  result['alternate_reason']=alternate['reason']
 return result

def main():
 if os.geteuid()!=0 or len(sys.argv)!=2 or sys.argv[1] not in ALIASES:raise SystemExit(2)
 resource=sys.argv[1];started=time.time()
 result={'resource':resource,'checked_at':started,**observe(resource)}
 DIRECTORY.mkdir(mode=0o755,exist_ok=True)
 fd,name=tempfile.mkstemp(prefix='.'+resource+'-',dir=DIRECTORY)
 try:
  with os.fdopen(fd,'w') as f:json.dump(result,f);f.write('\n');f.flush();os.fsync(f.fileno())
  os.chmod(name,0o644);os.replace(name,DIRECTORY/(resource+'.json'))
 finally:
  if os.path.exists(name):os.unlink(name)
if __name__=='__main__':main()
