from pathlib import Path
import runpy
import pytest
ROOT=Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('directory', ['/usr/bin','/usr/sbin'])
def test_provisioner_accepts_fedora_merged_bin_path(monkeypatch,directory):
    n=runpy.run_path(str(ROOT/'deploy/zulip-local/bin/provision-openbao.py'))
    f=n['check_sources'];checked=[]
    monkeypatch.setitem(f.__globals__,'require_root_file',checked.append)
    monkeypatch.setenv('PATH',directory+':/usr/bin')
    f()
    assert Path('/usr/bin/systemd-creds') in checked


def test_provisioner_rejects_shadow_credentials_program(monkeypatch,tmp_path):
    n=runpy.run_path(str(ROOT/'deploy/zulip-local/bin/provision-openbao.py'))
    f=n['check_sources'];monkeypatch.setitem(f.__globals__,'require_root_file',lambda p:None)
    fake=tmp_path/'systemd-creds';fake.write_text('#!/bin/sh\nexit 0\n');fake.chmod(0o755)
    monkeypatch.setenv('PATH',str(tmp_path)+':/usr/bin')
    with pytest.raises(n['ZulipLocalError'],match='unavailable'):f()
