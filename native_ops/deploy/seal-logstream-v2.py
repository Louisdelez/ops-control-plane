import ast,hashlib,json,os,shlex
from pathlib import Path
assert os.geteuid()==0
os.umask(0o077);root=Path('/home/ops-user/ops-control-plane');old=Path('/var/lib/ops-logstream-releases/4516ad2ab4a537b5620e528d671f9b587528f79446e3ad4506bad8a782c86a24/reader.py').read_text();assert hashlib.sha256(old.encode()).hexdigest()=='094744f2b8b2ab1b52ba4ae562135f149b1e60db9dfdf02ed12ec960e2445c5a'
collector=(root/'native_ops/logs/stream-collector-v2.py').read_text();tree=ast.parse(old);node=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='COLLECTORS' for t in n.targets));mapping=ast.literal_eval(node.value);assert len(mapping)==6
command='/usr/bin/python3 -I -c '+shlex.quote(collector);mapping[hashlib.sha256(command.encode()).hexdigest()]=collector
lines=old.splitlines(keepends=True);reader=''.join(lines[:node.lineno-1])+'COLLECTORS = '+repr(mapping)+'\n'+''.join(lines[node.end_lineno:])
files={'install.py':(root/'native_ops/logs/extend-stream-observer-v2.py').read_bytes(),'collector.py':collector.encode(),'reader.py':reader.encode()}
files['transport.py']=Path('/var/lib/ops-logstream-releases/4516ad2ab4a537b5620e528d671f9b587528f79446e3ad4506bad8a782c86a24/transport.py').read_bytes()
docker=(root/'native_ops/deploy/observer-role/docker-install.py').read_text().replace(repr('__SEALED_INSTALLER__'),repr(files['install.py'].decode())).replace("['/etc','/var/lib','/usr/local']","['/var/lib','/usr/local']");files['docker.py']=docker.encode()
for host in ['prod','nas','gamebox','edge-vps']:files[host+'.json']=json.dumps({'resource':host,'reader':reader,'reader_sha256':hashlib.sha256(reader.encode()).hexdigest(),'previous_reader_sha256':hashlib.sha256(old.encode()).hexdigest()},sort_keys=True).encode()
manifest={'schema_version':1,'purpose':'Continuous read-only systemd and Docker json-file logs with resumable cursors, fixed status and container listing. Batches of 200 redacted messages. Explicit source/rotation errors. Six previous fixed collectors preserved; handle bounded long Docker records and explicit malformed/oversize gaps. No application restart or deletion.','helper_sha256':hashlib.sha256((root/'broker/deploy/helpers/enable-logstream').read_bytes()).hexdigest(),'files':{k:hashlib.sha256(v).hexdigest() for k,v in files.items()}}
raw=json.dumps(manifest,sort_keys=True,indent=2).encode();sha=hashlib.sha256(raw).hexdigest();base=Path('/var/lib/ops-logstream-releases')/sha;base.mkdir(parents=True,mode=0o700,exist_ok=True)
for name,value in {**files,'manifest.json':raw}.items():
 target=base/name
 if target.exists():assert target.read_bytes()==value
 else:target.write_bytes(value);target.chmod(0o600)
report={'bundle':sha,'reader_sha256':hashlib.sha256(reader.encode()).hexdigest()};(root/'artifacts/completion-final-2026-09-14/logstream-v2-release.json').write_text(json.dumps(report));print(json.dumps(report))
report_path=root/'artifacts/completion-final-2026-09-14/logstream-v2-release.json';os.chown(report_path,1000,1000);report_path.chmod(0o600)
