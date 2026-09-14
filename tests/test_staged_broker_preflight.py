"""Exercise the installer's actual Python preflight with the worker's staging record."""
import ast
import json
from pathlib import Path
import runpy
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'deploy/control-plane/bin/control-plane-deployment-worker'

@pytest.mark.parametrize('tamper', [False, True])
def test_installer_accepts_actual_staging_marker(tmp_path, monkeypatch, tamper):
    manifest = json.loads((ROOT / 'deploy/control-plane/release-manifest.v1.json').read_text())
    tree = ast.parse(WORKER.read_text())
    stage = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'stage_release')
    marker_call = next(node for node in ast.walk(stage) if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Name) and node.func.id == 'atomic_json'
                       and isinstance(node.args[1], ast.Dict))
    marker = eval(compile(ast.Expression(marker_call.args[1]), '<actual-stage-record>', 'eval'), {
        'manifest': manifest, 'actual_tree_digest': manifest['source']['tree_sha256'],
        'artifact_digest': manifest['artifacts']['atlas_rpm']['sha256'],
        'hermes_digest': manifest['artifacts']['hermes_source_archive']['sha256']})
    if tamper:
        marker['hermes_source_archive_sha256'] = '0' * 64
    staged = tmp_path / 'release'
    manifest_path = staged / 'tree/deploy/control-plane/release-manifest.v1.json'
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(json.dumps(manifest))
    (staged / '.deployment-release.json').write_text(json.dumps(marker))
    checks = []
    worker = {'validate_manifest': lambda doc: checks.append('manifest'),
              'source_tree_digest': lambda root, roots: manifest['source']['tree_sha256'],
              'validate_component_declarations': lambda root, doc: checks.append('components'),
              'validate_atlas_rpm': lambda path, artifact: checks.append('artifact')}
    monkeypatch.setattr(runpy, 'run_path', lambda path: worker)
    monkeypatch.setattr(sys, 'argv', ['-', str(WORKER), str(manifest_path)])
    script = (ROOT / 'scripts/install-broker.sh').read_text().split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    if tamper:
        with pytest.raises(SystemExit, match='marker differs'):
            exec(compile(script, '<actual-installer-preflight>', 'exec'), {})
        assert checks == ['manifest']
    else:
        exec(compile(script, '<actual-installer-preflight>', 'exec'), {})
        assert checks == ['manifest', 'components', 'artifact']
