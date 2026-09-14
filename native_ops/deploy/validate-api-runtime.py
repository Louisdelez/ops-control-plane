import subprocess,os
from pathlib import Path
assert os.geteuid()==0
code="from pathlib import Path; from ops_orchestrator.config import load_config; from ops_native.model_proxy import Handler; c=load_config(Path('/etc/ops-native-model/config.json')); print('INSTALLED_API_CONFIG_AND_ADAPTER_IMPORT_VERIFIED')"
r=subprocess.run(['/usr/sbin/runuser','-u','opsnativeai','--','/opt/ops-native/venv/bin/python','-I','-c',code],capture_output=True,text=True)
if r.returncode:
 print('API_CONFIG_VALIDATION_FAILED',r.stderr[-1800:]);raise SystemExit(1)
print(r.stdout.strip())
r=subprocess.run(['/usr/bin/systemd-analyze','verify','/etc/systemd/system/ops-native-model.service','/etc/systemd/system/ops-native-secrets.service'],capture_output=True,text=True)
print('API_UNITS_VERIFIED',r.returncode==0)
if r.returncode:print(r.stderr[-1000:]);raise SystemExit(1)
