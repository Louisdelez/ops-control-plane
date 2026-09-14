#!/usr/bin/python3
"""Online SQLite backup and isolated restore verification for the two connectors."""
from pathlib import Path
import datetime,hashlib,json,os,sqlite3,tempfile,shutil,stat
SOURCES={'pilot':Path('/var/lib/hermes/native-ops/connector/delivery.sqlite3'),'approvals':Path('/var/lib/ops-native-approval/state.db')}
ROOT=Path('/var/lib/ops-native-backups')
def check(db):
    with sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True) as c:
        if c.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise RuntimeError('SQLite integrity failure')
        return {name:c.execute('SELECT COUNT(*) FROM "'+name.replace('"','""')+'"').fetchone()[0] for (name,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()}
def main():
    if os.geteuid()!=0:raise RuntimeError('root required')
    os.umask(0o077);ROOT.mkdir(exist_ok=True,mode=0o700)
    st=ROOT.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid!=0 or st.st_mode&0o077:raise RuntimeError('private backup directory required')
    if shutil.disk_usage(ROOT).free<2*1024**3:raise RuntimeError('insufficient free space for backup')
    stamp=datetime.datetime.now(datetime.UTC).strftime('%Y%m%dT%H%M%S.%fZ')
    folder=ROOT/stamp;folder.mkdir(mode=0o700)
    report={'created_at':stamp,'restore_test':'isolated SQLite copy','databases':{}}
    for name,source in SOURCES.items():
        meta=source.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_size>1024**3:raise RuntimeError('unexpected source database')
        destination=folder/(name+'.sqlite3')
        with sqlite3.connect('file:'+str(source)+'?mode=ro',uri=True) as original,sqlite3.connect(destination) as backup:
            original.backup(backup)
        tables=check(destination)
        with tempfile.TemporaryDirectory(prefix='restore-',dir=ROOT) as raw:
            restored=Path(raw)/'restored.sqlite3';shutil.copyfile(destination,restored)
            if check(restored)!=tables:raise RuntimeError('restore verification differs')
        report['databases'][name]={'sha256':hashlib.sha256(destination.read_bytes()).hexdigest(),'tables':tables}
    (folder/'manifest.json').write_text(json.dumps(report,indent=2)+'\n')
    (ROOT/'latest.json').write_text(json.dumps({'snapshot':stamp,'verified':True})+'\n')
    print(json.dumps({'snapshot':stamp,'databases':list(report['databases']),'restore_verified':True}))
if __name__=='__main__':main()
