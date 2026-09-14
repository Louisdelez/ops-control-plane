"""Read-only readiness evidence across boots; never marks a mission complete."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid

from ops_memory.protocol import socket_request

SYSTEM_UNITS = ('openbao.service', 'ops-broker.service', 'ops-memory.service',
                'ops-native.service', 'ops-native-model.service', 'ops-native-secrets.service')
USER_UNITS = ('ops-cli-pilot.service', 'ops-memory-sync.timer')
MISSION = '11111111-1111-4111-8111-111111111111'


def unit_states(units, user=False):
    result = {}
    for unit in units:
        command = ['systemctl'] + (['--user'] if user else [])
        response = subprocess.run(command+['show', unit, '--property=ActiveState', '--value'],
                                  capture_output=True, text=True, timeout=10)
        state = response.stdout.strip()
        result[unit] = state if response.returncode == 0 and state in {
            'active', 'inactive', 'failed', 'activating', 'deactivating'} else 'unavailable'
    return result


def memory_call(operation, payload):
    return socket_request(Path('/run/ops-memory/ops-memory.sock'),
        {'version': 1, 'request_id': str(uuid.uuid4()), 'actor': 'codex-supervised',
         'operation': operation, 'payload': payload}, timeout=20)


def collect(folder):
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    uuid.UUID(boot)
    baseline_path = folder/'baseline-boot-id'
    if not baseline_path.exists():
        with baseline_path.open('x') as stream:
            stream.write(boot+'\n')
    baseline = baseline_path.read_text().strip()
    report = {'checked_at': time.time(), 'boot_id': boot, 'new_boot_observed': boot != baseline,
              'system_units': unit_states(SYSTEM_UNITS), 'user_units': unit_states(USER_UNITS, True),
              'memory_healthy': False, 'mission_snapshot_readable': False,
              'scope': 'service health and durable mission snapshot; not full project acceptance'}
    try:
        response = memory_call('health', {})
        report['memory_healthy'] = response.get('ok') is True and response.get('result', {}).get('status') == 'ok'
        database = Path.home()/'.local/state/ops-memory-sync/state.sqlite3'
        with sqlite3.connect('file:'+str(database)+'?mode=ro', uri=True) as db:
            row = db.execute('SELECT memory_id FROM mission_states WHERE source=?',
                             ('ops-broker://missions/'+MISSION+'/mission/'+MISSION,)).fetchone()
        if row:
            response = memory_call('get', {'id': row[0]})
            record = response.get('result', {})
            report['mission_snapshot_readable'] = response.get('ok') is True and record.get('id') == row[0]
            report['mission_memory_id'] = row[0]
    except Exception:
        # No provider bodies, memory text or arbitrary exception messages in reports.
        report['memory_check_error'] = True
    report['status'] = 'healthy' if (report['memory_healthy'] and report['mission_snapshot_readable']
        and all(value == 'active' for value in report['system_units'].values())
        and all(value == 'active' for value in report['user_units'].values())) else 'needs_review'
    return report


def main():
    os.umask(0o077)
    folder = Path.home()/'.local/state/ops-continuity'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = collect(folder)
    content = json.dumps(report, indent=2)+'\n'
    temporary = folder/'latest.next'
    temporary.write_text(content)
    temporary.replace(folder/'latest.json')
    # Retain first observation of each boot as well as the latest status.
    first = folder/('boot-'+report['boot_id']+'.json')
    if not first.exists():
        with first.open('x') as stream:
            stream.write(content)
    print(json.dumps(report))
    return 0 if report['status'] == 'healthy' else 1


if __name__ == '__main__':
    raise SystemExit(main())
