from pathlib import Path
import json,subprocess,os
out=Path('/home/ops-user/ops-control-plane/artifacts/audit-cleanup-2026-09-10/python-packages.json');r=json.loads(out.read_text())
paths=[]
for root in ['/opt/ops-memory','/opt/hermes','/var/lib/hermes/hermes-agent','/opt/ops-control-plane']:
 p=Path(root)
 if p.exists():
  for pattern in ['*-venv/bin/python','venv/bin/python','.venv/bin/python','*/venv/bin/python','*/.venv/bin/python','bin/python']:
   paths.extend(p.glob(pattern))
for p in paths:
 if str(p) in r or not p.exists():continue
 q=subprocess.run([str(p),'-I','-c',"import importlib.metadata as m,json;print(json.dumps([{'name':d.metadata['Name'],'version':d.version} for d in m.distributions()]))"],capture_output=True,text=True,check=True)
 r[str(p)]=json.loads(q.stdout);print('ADDED_ENV',str(p))
out.write_text(json.dumps(r,indent=2)+'\n');os.chown(out,1000,1000)
