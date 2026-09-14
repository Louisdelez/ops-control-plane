from pathlib import Path
import runpy
import pytest
ROOT=Path(__file__).resolve().parents[1]/'deploy/public-zulip'

@pytest.mark.parametrize('root',[ROOT,ROOT.with_name('public-zulip-v2'),ROOT.with_name('public-zulip-v3')])
def test_haproxy_route_preserves_existing_routes_and_refuses_reapplication(root):
 f=runpy.run_path(str(root/'remote.py'))['haproxy_patch']
 original='frontend sni\n    bind :::443 v4v6\n    use_backend old if old_host\n    default_backend nas\nbackend nas\n    server nas 198.51.100.2:443\n'
 result=f(original)
 assert 'use_backend old if old_host' in result and 'server nas 198.51.100.2:443' in result
 assert result.index('use_backend ops_zulip')<result.index('default_backend nas')
 for text in [result,original.replace('default_backend nas','default_backend unknown')]:
  with pytest.raises(ValueError):f(text)

@pytest.mark.parametrize('root',[ROOT,ROOT.with_name('public-zulip-v2'),ROOT.with_name('public-zulip-v3')])
def test_zulip_keeps_local_alias_and_refuses_unknown_settings(root):
 f=runpy.run_path(str(root/'publish.py'))['override_patch']
 original='services:\n  zulip:\n    environment:\n      SETTING_EXTERNAL_HOST: "zulip.ops.local:8443"\n      CERTIFICATES: "manual"\n'
 result=f(original)
 assert 'SETTING_EXTERNAL_HOST: "zulip.example.org"' in result
 assert 'SETTING_ALLOWED_HOSTS: "[\'zulip.ops.local\']"' in result
 assert 'CERTIFICATES: "manual"' in result
 for text in [result,original+'      SETTING_ALLOWED_HOSTS: "[]"\n']:
  with pytest.raises(ValueError):f(text)
