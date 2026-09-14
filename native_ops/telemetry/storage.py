"""Durable telemetry: immutable samples, daily full-resolution partitions, no purge."""
import datetime
import hashlib
import json
import sqlite3
import zlib
from pathlib import Path

class Archive:
    def __init__(self, root):
        self.root=Path(root); self.root.mkdir(parents=True,exist_ok=True)
        self.day=None; self.db=None
    def append(self, hosts, timestamp):
        day=datetime.datetime.fromtimestamp(timestamp,datetime.UTC).strftime('%Y-%m-%d')
        if day!=self.day:
            if self.db:self.db.close()
            self.db=sqlite3.connect(self.root/(day+'.sqlite3'))
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('CREATE TABLE IF NOT EXISTS snapshots(host TEXT,t INTEGER,sha256 TEXT,payload BLOB,PRIMARY KEY(host,t))')
            self.day=day
        with self.db:
            for host in hosts:
                raw=json.dumps(host,separators=(',',':'),allow_nan=False).encode()
                self.db.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?)',(host['id'],int(timestamp*1000),hashlib.sha256(raw).hexdigest(),zlib.compress(raw,6)))
    def close(self):
        if self.db:self.db.close()

def verify(path):
    count=0
    with sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise ValueError('Corrupt archive')
        for expected,payload in db.execute('SELECT sha256,payload FROM snapshots'):
            raw=zlib.decompress(payload)
            if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('Corrupt sample')
            json.loads(raw);count+=1
    return count


FIELDS=['cpu','cores','memory_used','memory_total','storage_used','storage_total','rx','tx','read','write','processes']

def initialize(db):
    db.execute('CREATE INDEX IF NOT EXISTS samples_time ON samples(t)')
    db.execute('CREATE TABLE IF NOT EXISTS rollups(host TEXT,res INTEGER,t INTEGER,data TEXT,PRIMARY KEY(host,res,t))')
    db.execute('CREATE TABLE IF NOT EXISTS telemetry_meta(key TEXT PRIMARY KEY,value TEXT)')
    if not db.execute("SELECT 1 FROM telemetry_meta WHERE key='rollups_v1'").fetchone():
        for host,t,data in db.execute('SELECT host,t,data FROM samples ORDER BY t'):
            _rollup(db,host,t,json.loads(data))
        db.execute("INSERT INTO telemetry_meta VALUES('rollups_v1','ready')")
    db.commit()

def _merge(target,source):
    target['n']=target.get('n',0)+source['n']
    for k in FIELDS:
        if k not in source:continue
        a=target.setdefault(k,[0,0,None]);b=source[k]
        a[0]+=b[0];a[1]+=b[1];a[2]=b[2] if a[2] is None else max(a[2],b[2])

def _rollup(db,host,t,point):
    source={'n':1,**{k:[point[k],1,point[k]] for k in FIELDS if isinstance(point.get(k),(int,float))}}
    for res in [60,3600,86400]:
        bucket=int(t)//res*res
        row=db.execute('SELECT data FROM rollups WHERE host=? AND res=? AND t=?',(host,res,bucket)).fetchone()
        data=json.loads(row[0]) if row else {'n':0};_merge(data,source)
        db.execute('INSERT OR REPLACE INTO rollups VALUES(?,?,?,?)',(host,res,bucket,json.dumps(data,separators=(',',':'))))

def save_summary(db,host,point):
    cursor=db.execute('INSERT OR IGNORE INTO samples VALUES(?,?,?)',(host,point['t'],json.dumps(point)))
    if cursor.rowcount:_rollup(db,host,point['t'],point)

def ranges(db,host,now):
    result={}
    earliest=db.execute('SELECT MIN(t) FROM samples').fetchone()[0]
    for key,seconds,res in [('15m',900,60),('1h',3600,60),('24h',86400,60),('7d',604800,3600),('30d',2592000,3600),('1y',31536000,86400),('all',None,86400)]:
        start=int(now-seconds) if seconds else (earliest if earliest is not None else now)
        span=now-max(start,earliest if earliest is not None else start)
        res=60 if span<=86400 else 3600 if span<=2592000 else 86400
        width=max(res,((int(now-start)//180)//res+1)*res)
        groups={}
        for t,raw in db.execute('SELECT t,data FROM rollups WHERE host=? AND res=? AND t>=? ORDER BY t',(host,res,int(start)//res*res)):
            bucket=t//width*width;_merge(groups.setdefault(bucket,{'n':0}),json.loads(raw))
        result[key]=[dict(t=t,sample_count=r['n'],**{k:r[k][0]/r[k][1] if k in r else None for k in FIELDS},cpu_max=r.get('cpu',[0,0,None])[2],memory_peak=r.get('memory_used',[0,0,None])[2],bucket_seconds=width) for t,r in sorted(groups.items())]
    return result
