from pathlib import Path
import subprocess,json,os
out=Path('/home/ops-user/ops-control-plane/artifacts/audit-cleanup-2026-09-10/python-packages.json');r={}
for raw in ['/opt/ops-broker/venv/bin/python','/opt/ops-orchestrator/venv/bin/python','/opt/ops-native/venv/bin/python','/opt/ops-control-plane/bridges/zulip/.venv/bin/python','/home/ops-user/.hermes/hermes-agent/venv/bin/python']:
 if Path(raw).exists():
  p=subprocess.run([raw,'-I','-c',"import importlib.metadata as m,json;print(json.dumps([{'name':d.metadata['Name'],'version':d.version} for d in m.distributions()]))"],capture_output=True,text=True,check=True);r[raw]=json.loads(p.stdout)
out.write_text(json.dumps(r,indent=2)+'\n');os.chown(out,1000,1000);print('PYTHON_ENVIRONMENTS',len(r),'DISTRIBUTIONS',sum(map(len,r.values())))
