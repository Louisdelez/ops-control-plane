from pathlib import Path
import json
root=Path('/var/lib/ollama')
for p in root.rglob('manifests'):
 if p.is_dir():
  for f in p.rglob('*'):
   if f.is_file():
    try:d=json.loads(f.read_text())
    except (ValueError,UnicodeError):continue
    print('MODEL_MANIFEST',str(f),'LAYERS',len(d.get('layers',[])))
for p in root.rglob('blobs'):
 if p.is_dir():print('BLOB_DIR',str(p),'FILES',sum(f.is_file() for f in p.iterdir()))
