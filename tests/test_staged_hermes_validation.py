"""Exercise Hermes validation using exactly the source closure shipped to installers."""
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import sys


def test_hermes_validator_works_from_reviewed_source_closure(tmp_path):
    root = Path(__file__).resolve().parents[1]
    worker = runpy.run_path(str(root / 'deploy/control-plane/bin/control-plane-deployment-worker'))
    manifest = json.loads((root / 'deploy/control-plane/release-manifest.v1.json').read_text())
    for source, relative in worker['iter_source_paths'](root, manifest['source']['roots']):
        target = tmp_path / str(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    before = worker['source_tree_digest'](tmp_path, manifest['source']['roots'])
    result = subprocess.run([sys.executable, str(tmp_path / 'scripts/validate-hermes-config.py')], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'Hermes configuration OK' in result.stdout
    assert worker['source_tree_digest'](tmp_path, manifest['source']['roots']) == before
