"""Remote metadata cannot become an arbitrary SSH operation or disclose raw output."""
import runpy
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
WORKER=runpy.run_path(str(ROOT/'broker/deploy/helpers/remote-services-worker'))

def test_fixed_readonly_collector_and_strict_destination():
 command=WORKER['command']('prod')
 assert 'StrictHostKeyChecking=yes' in command
 assert command[-2]=='prod'
 assert command[-1].startswith('/usr/bin/python3 -I -c ')
 with pytest.raises(KeyError):WORKER['command']('prod; reboot')
 collector=WORKER['COLLECTOR']
 assert 'docker' in collector and 'ps' in collector
 assert 'inspect' not in collector and 'environ' not in collector


def test_metadata_rejects_extra_fields_and_control_characters():
 validate=WORKER['validate_metadata']
 payload={k:{'status':'unavailable'} for k in ['hostname','units','containers','filesystems']}
 assert validate(payload)==payload
 assert validate(payload|{'units':{'status':'observed','rows':[[r'systemd-fsck@dev-disk-by\x2duuid.service','loaded','active','exited']]}})
 for bad in [payload|{'credentials':'secret'},payload|{'units':{'status':'observed','rows':[['x\nsecret']]}},payload|{'containers':{'status':'observed','rows':[['x']]*257}},payload|{'hostname':{'status':'observed','rows':[['x'],'raw']}}]:
  with pytest.raises(ValueError):validate(bad)


def test_collector_only_returns_selected_unit_metadata():
 from unittest.mock import patch
 from types import SimpleNamespace
 import json
 values=[]
 def run(args,**kwargs):
  if 'systemctl' in args[0]:return SimpleNamespace(returncode=0,stdout=b'ssh.service loaded active running PRIVATE DESCRIPTION\n')
  if 'docker' in args[0]:return SimpleNamespace(returncode=1,stdout=b'private error')
  return SimpleNamespace(returncode=0,stdout=b'host\n')
 with patch('subprocess.run',run),patch('builtins.print',lambda s:values.append(s)):
  exec(WORKER['COLLECTOR'],{})
 result=json.loads(values[0])
 assert result['units']['rows']==[['ssh.service','loaded','active','running']]
 assert result['containers']=={'status':'unavailable'}
 assert 'PRIVATE' not in values[0] and 'private error' not in values[0]
 assert WORKER['validate_metadata'](result)==result


def test_registry_only_allows_reviewed_resources():
 from ops_broker.runbooks import ExecutablePolicy,RunbookRegistry
 from ops_broker.errors import BrokerError
 registry=RunbookRegistry.load(ROOT/'runbooks',ExecutablePolicy.load(ROOT/'broker/config/executables.yaml'))
 item=registry.get('inventory.remote-services.v1')
 assert item.action_class=='A'
 with pytest.raises(BrokerError):item.validate_parameters({'resource':'unknown'})
 assert item.render_argv(item.validate_parameters({'resource':'prod'}))[-1]=='prod'
