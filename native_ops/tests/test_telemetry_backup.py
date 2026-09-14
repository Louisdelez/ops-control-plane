"""Compression must preserve SQLite bytes and enforce the declared restore size."""
import gzip,importlib.util,sqlite3
from pathlib import Path
import pytest
spec=importlib.util.spec_from_file_location('telemetry_backup',Path(__file__).parents[1]/'telemetry/backup.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
def test_compressed_database_roundtrip(tmp_path):
 source=tmp_path/'original'
 with sqlite3.connect(source) as db:
  db.execute('CREATE TABLE entries (message TEXT)')
  db.executemany('INSERT INTO entries VALUES (?)',[('synthetic event',)]*1000)
 archive=tmp_path/'compressed';archive.write_bytes(gzip.compress(source.read_bytes()))
 target=tmp_path/'restored';m.expand(archive,target,source.stat().st_size)
 assert m.sha(source)==m.sha(target) and m.check(target)==1000
 assert archive.stat().st_size<source.stat().st_size
@pytest.mark.parametrize('declared',[1,999999,0,True,5*1024**3])
def test_corrupt_or_unbounded_manifest_refused(tmp_path,declared):
 archive=tmp_path/'compressed';archive.write_bytes(gzip.compress(b'x'*1024))
 with pytest.raises(ValueError):m.expand(archive,tmp_path/'out',declared)
def test_corrupt_gzip_refused(tmp_path):
 archive=tmp_path/'compressed';archive.write_bytes(b'bad archive')
 with pytest.raises((OSError,ValueError)):m.expand(archive,tmp_path/'out',1024)
