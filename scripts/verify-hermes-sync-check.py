#!/usr/bin/python3
"""Accept only uv's rebuild of the already verified, sealed editable project."""
import json
from pathlib import Path
import sys


def validate_report(report, root):
    if report.get('schema') != {'version': 'preview'} or report.get('target') != 'project' or report.get('dry_run') is not True:
        raise ValueError('Unexpected uv check report')
    if report.get('project', {}).get('path') != str(root) or report.get('lock') != {'path': str(root / 'uv.lock'), 'action': 'check'}:
        raise ValueError('Unexpected uv check project')
    sync = report.get('sync', {})
    if sync.get('action') != 'check' or sync.get('environment', {}).get('path') != str(root / 'venv'):
        raise ValueError('Unexpected uv check environment')
    if sync.get('changes') != [
        {'name': 'hermes-agent', 'version': '0.21.0', 'action': 'uninstalled'},
        {'name': 'hermes-agent', 'action': 'installed'},
    ]:
        raise ValueError('Locked dependencies require changes')


def main():
    root = Path(sys.argv[1])
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        raise ValueError('uv check report exceeds bound')
    validate_report(json.loads(raw), root)
    candidates = list((root / 'venv/lib').glob('python3.*/site-packages/hermes_agent-0.21.0.dist-info'))
    if len(candidates) != 1 or candidates[0].is_symlink():
        raise ValueError('Installed Hermes distribution differs')
    metadata = candidates[0]
    if json.loads((metadata / 'direct_url.json').read_text()) != {'url': root.as_uri(), 'dir_info': {'editable': True}}:
        raise ValueError('Installed Hermes source reference differs')
    lines = (metadata / 'METADATA').read_text().splitlines()
    if 'Name: hermes-agent' not in lines or 'Version: 0.21.0' not in lines:
        raise ValueError('Installed Hermes version differs')
    print('Locked dependencies unchanged; sealed editable source remains the verified 0.21.0 project.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError):
        raise SystemExit('Hermes sealed-runtime check failed')
