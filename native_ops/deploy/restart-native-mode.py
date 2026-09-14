import subprocess,time,json
from pathlib import Path
subprocess.run(['/usr/bin/systemctl','reset-failed','ops-native.service'],check=True)
subprocess.run(['/usr/bin/systemctl','start','ops-native.service'],check=True)
time.sleep(6)
print('NATIVE_SERVICE',subprocess.check_output(['/usr/bin/systemctl','is-active','ops-native.service'],text=True).strip())
print('NATIVE_STATUS',Path('/var/lib/hermes/native-ops/connector/status.json').read_text())
