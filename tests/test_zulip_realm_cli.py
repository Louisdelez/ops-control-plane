import argparse,runpy
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]

def test_create_realm_optional_positionals_parse_with_official_cli_contract(monkeypatch):
    n=runpy.run_path(str(ROOT/'deploy/zulip-local/bin/provision-zulip.py'))
    observed=[]
    def command(*args,**kwargs):
        start=args.index('Ops')
        assert args[start:start+3]==('Ops','admin@ops.local','Ops Administrator')
        parser=argparse.ArgumentParser()
        parser.add_argument('realm_name')
        parser.add_argument('email',nargs='?')
        parser.add_argument('full_name',nargs='?')
        parser.add_argument('--string-id')
        parser.add_argument('--password-file')
        observed.append(parser.parse_args(args[args.index('create_realm')+1:]))
        return SimpleNamespace(returncode=0)
    monkeypatch.setitem(n['create_realm'].__globals__,'compose_command',command)
    n['create_realm']({'ZULIP_LOCAL_REALM_NAME':'Ops','ZULIP_LOCAL_ADMIN_EMAIL':'admin@ops.local','ZULIP_LOCAL_ADMIN_NAME':'Ops Administrator'})
    assert observed[0].email=='admin@ops.local'
    assert observed[0].full_name=='Ops Administrator'
    assert observed[0].string_id==''
    assert observed[0].password_file=='/run/secrets/admin_bootstrap_password'

def test_quiesce_skips_absent_units_during_interrupted_restoration(monkeypatch):
    n=runpy.run_path(str(ROOT/'deploy/control-plane/bin/control-plane-bootstrap'))
    calls=[]
    monkeypatch.setitem(n['_quiesce_broker'].__globals__,'_systemctl_state',lambda *a:False)
    worker={'_unit_is_loaded':lambda name:name=='ops-broker.socket','run_command':lambda name,argv,**kw:calls.append(argv)}
    n['_quiesce_broker'](worker)
    assert calls==[['/usr/bin/systemctl','stop','ops-broker.socket']]
