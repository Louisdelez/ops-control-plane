import ast,hashlib,json,os,shlex
from pathlib import Path
assert os.geteuid()==0
os.umask(0o077);root=Path('/home/ops-user/ops-control-plane');old=Path('/var/lib/ops-logs-releases/d9732d5fc3333368b671baf57b2e99fb509cc2b8c115578a123464574022397b/reader.py').read_text();assert hashlib.sha256(old.encode()).hexdigest()=='5b763596fce3a79787b5e8bd2d9dadfbd3a20d1e7fa1db7c470e36b0948e69ca'
collector=(root/'native_ops/logs/stream-collector.py').read_text();tree=ast.parse(old);node=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='COLLECTORS' for t in n.targets));mapping=ast.literal_eval(node.value);assert len(mapping)==5
command='/usr/bin/python3 -I -c '+shlex.quote(collector);mapping[hashlib.sha256(command.encode()).hexdigest()]=collector
lines=old.splitlines(keepends=True);reader=''.join(lines[:node.lineno-1])+'COLLECTORS = '+repr(mapping)+'\n'+''.join(lines[node.end_lineno:])
files={'install.py':(root/'native_ops/logs/extend-stream-observer.py').read_bytes(),'collector.py':collector.encode(),'reader.py':reader.encode()}
files['transport.py']=Path('/var/lib/ops-logs-releases/d9732d5fc3333368b671baf57b2e99fb509cc2b8c115578a123464574022397b/transport.py').read_bytes()
docker=(root/'native_ops/deploy/observer-role/docker-install.py').read_text().replace(repr('__SEALED_INSTALLER__'),repr(files['install.py'].decode())).replace("['/etc','/var/lib','/usr/local']","['/var/lib','/usr/local']");files['docker.py']=docker.encode()
for host in ['prod','nas','gamebox','edge-vps']:files[host+'.json']=json.dumps({'resource':host,'reader':reader,'reader_sha256':hashlib.sha256(reader.encode()).hexdigest(),'previous_reader_sha256':hashlib.sha256(old.encode()).hexdigest()},sort_keys=True).encode()
manifest={'schema_version':1,'purpose':'Continuous read-only systemd and Docker json-file logs with resumable cursors, fixed status and container listing. Batches of 200 redacted messages. Explicit source/rotation errors. Five previous fixed collectors preserved. No application restart or deletion.','helper_sha256':hashlib.sha256((root/'broker/deploy/helpers/enable-logstream').read_bytes()).hexdigest(),'files':{k:hashlib.sha256(v).hexdigest() for k,v in files.items()}}
raw=json.dumps(manifest,sort_keys=True,indent=2).encode();sha=hashlib.sha256(raw).hexdigest();base=Path('/var/lib/ops-logstream-releases')/sha;base.mkdir(parents=True,mode=0o700,exist_ok=True)
for name,value in {**files,'manifest.json':raw}.items():
 target=base/name
 if target.exists():assert target.read_bytes()==value
 else:target.write_bytes(value);target.chmod(0o600)
report={'bundle':sha,'reader_sha256':hashlib.sha256(reader.encode()).hexdigest()};(root/'artifacts/completion-final-2026-09-14/logstream-release.json').write_text(json.dumps(report));print(json.dumps(report))
report_path=root/'artifacts/completion-final-2026-09-14/logstream-release.json';os.chown(report_path,1000,1000);report_path.chmod(0o600)
