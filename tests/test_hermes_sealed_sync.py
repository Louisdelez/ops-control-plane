import copy
from pathlib import Path
import runpy
import pytest

@pytest.mark.parametrize('variant', ['project_only', 'dependency', 'extra', 'version', 'environment', 'not_check'])
def test_only_same_sealed_project_rebuild_is_allowed(variant):
    root = Path('/var/lib/hermes/hermes-agent')
    report = {'schema': {'version': 'preview'}, 'target': 'project', 'dry_run': True,
              'project': {'path': str(root)}, 'lock': {'path': str(root / 'uv.lock'), 'action': 'check'},
              'sync': {'action': 'check', 'environment': {'path': str(root / 'venv')}, 'changes': [
                  {'name': 'hermes-agent', 'version': '0.21.0', 'action': 'uninstalled'},
                  {'name': 'hermes-agent', 'action': 'installed'}]}}
    if variant == 'dependency':
        report['sync']['changes'][1]['name'] = 'requests'
    elif variant == 'extra':
        report['sync']['changes'].append({'name': 'unexpected', 'action': 'uninstalled'})
    elif variant == 'version':
        report['sync']['changes'][0]['version'] = '0.22.0'
    elif variant == 'environment':
        report['sync']['environment']['path'] = '/other/venv'
    elif variant == 'not_check':
        report['dry_run'] = False
    ns = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/verify-hermes-sync-check.py'))
    if variant == 'project_only':
        ns['validate_report'](report, root)
    else:
        with pytest.raises(ValueError):
            ns['validate_report'](report, root)
