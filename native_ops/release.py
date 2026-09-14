"""Content inventory for the native release, independent of sealed legacy releases."""
import argparse
import hashlib
import json
import re
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=Path('native_ops/releases/current.json')
EXCLUDED={'target','node_modules','.git','__pycache__','.pytest_cache','build','.venv','venv'}

def inventory(root, roots):
    result={}
    for raw in roots:
        base=root/raw
        if not base.exists():raise ValueError('Missing release root: '+raw)
        for path in sorted(base.rglob('*') if base.is_dir() else [base]):
            relative=path.relative_to(root)
            if relative==MANIFEST or any(p in EXCLUDED or p.endswith('.egg-info') for p in relative.parts):continue
            if path.is_symlink():
                result[str(relative)]={'symlink':path.readlink().as_posix()}
            elif path.is_file() and path.suffix not in {'.pyc','.pyo'}:
                result[str(relative)]={'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    return result

def verify(root=ROOT):
    release=json.loads((root/MANIFEST).read_text())
    observed=inventory(root,release['roots'])
    if observed!=release['files']:
        changed=sorted(k for k in observed.keys()|release['files'].keys() if observed.get(k)!=release['files'].get(k))
        raise ValueError('Native release differs: '+', '.join(changed[:15]))
    return release

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--write',action='store_true')
    parser.add_argument('--release-id',help='New immutable release identifier, required when writing')
    args=parser.parse_args()
    if args.write:
        if not args.release_id or not re.fullmatch(r'ops-native-[0-9.]+',args.release_id):
            parser.error('--write requires --release-id ops-native-YYYY.MM.DD.N')
        legacy=json.loads((ROOT/'deploy/control-plane/release-manifest.v1.json').read_text())
        roots=sorted(set(legacy['source']['roots'])|{'native_ops','apps/ops-desktop','apps/model-manager','apps/approvals','memory','inventory'})
        payload={'schema_version':1,'release_id':args.release_id,'roots':roots,
                 'legacy_manifest_sha256':hashlib.sha256((ROOT/'deploy/control-plane/release-manifest.v1.json').read_bytes()).hexdigest(),
                 'files':inventory(ROOT,roots)}
        (ROOT/MANIFEST).parent.mkdir(parents=True,exist_ok=True)
        (ROOT/MANIFEST).write_text(json.dumps(payload,indent=2,sort_keys=True)+'\n')
    value=verify();print(value['release_id'],len(value['files']),'files verified')
