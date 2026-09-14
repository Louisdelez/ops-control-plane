"""Run as root with --basetemp below a private root-owned directory."""
import os
from pathlib import Path
import runpy
import pytest
ROOT=Path(__file__).resolve().parents[1]
pytestmark=pytest.mark.skipif(os.geteuid()!=0,reason='Root-owned interpreter boundary requires root fixture')

def namespace(monkeypatch,tmp_path):
    n=runpy.run_path(str(ROOT/'scripts/reconcile-broker-zulip-approver'))
    directory=tmp_path/'bin';directory.mkdir(mode=0o700)
    python=directory/'python'
    monkeypatch.setitem(n['validate_broker_python'].__globals__,'BROKER_PYTHON',python)
    return n,python

def test_accepts_real_fedora_venv_link_chain(monkeypatch,tmp_path):
    n,python=namespace(monkeypatch,tmp_path)
    python.symlink_to('python3')
    (python.parent/'python3').symlink_to('/usr/sbin/python3' if Path('/usr/sbin/python3').exists() else '/usr/bin/python3')
    n['validate_broker_python']()

@pytest.mark.parametrize('unsafe',['external_link','writable_binary','writable_parent','foreign_owner'])
def test_rejects_unsafe_interpreter(monkeypatch,tmp_path,unsafe):
    n,python=namespace(monkeypatch,tmp_path)
    if unsafe=='external_link':python.symlink_to('/tmp/python3')
    elif unsafe=='writable_binary':python.write_text('fixture');python.chmod(0o777)
    else:
        python.symlink_to('/usr/bin/python3')
        if unsafe=='writable_parent':python.parent.chmod(0o777)
        else:os.chown(python,65534,65534,follow_symlinks=False)
    with pytest.raises(n['ReconcileError']):n['validate_broker_python']()
