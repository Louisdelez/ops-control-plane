"""Launch the existing approval bridge with OpenBao Agent-rendered settings."""
import os
from pathlib import Path
from .cli import private_json

def main():
    values=private_json(Path('/run/ops-native-approval/bridge.json'))
    if not all(isinstance(k,str) and k.startswith(('ZULIP_','OPS_','ALERTMANAGER_')) and isinstance(v,str) for k,v in values.items()):
        raise SystemExit('invalid bridge configuration')
    environment={'PATH':'/usr/bin:/usr/sbin','PYTHONDONTWRITEBYTECODE':'1',**values}
    command='/opt/ops-control-plane/bridges/zulip/.venv/bin/zulip-approval-bridge'
    os.execve(command,[command],environment)

if __name__=='__main__':main()
