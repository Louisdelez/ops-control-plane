import copy,runpy
from pathlib import Path
ROOT=Path(__file__).parents[2]
engine=runpy.run_path(str(ROOT/'native_ops/telemetry/engine.py'))
def sample():
 return {'boot_id':'one','uptime_seconds':10,'sample_at':10,'cpu':{'cores':1,'ticks':{'cpu':[10,0,0,90,0,0,0,0],'cpu0':[10,0,0,90,0,0,0,0]}},'memory':{'total_bytes':100,'used_bytes':30},'filesystems':[],'network':[{'name':'eth0','physical':True,'rx_bytes':100,'tx_bytes':20}], 'disk_io':[],'process_count':1,'processes':[{'pid':1,'start_ticks':1,'cpu_seconds':1,'rss_bytes':20}]}
def test_counter_rates_cpu_and_pid_reuse():
 before=sample();after=copy.deepcopy(before);after['uptime_seconds']=20;after['cpu']['ticks']['cpu']=[60,0,0,140,0,0,0,0];after['network'][0]['rx_bytes']=200;after['processes'][0]['cpu_seconds']=6
 out=engine['derive'](after,before)
 assert out['cpu']['usage_pct']==50 and out['network'][0]['rx_bytes_per_second']==10 and out['processes'][0]['cpu_pct']==50
 assert 'ticks' in after['cpu']
 after['processes'][0]['start_ticks']=5
 assert engine['derive'](after,before)['processes'][0]['cpu_pct'] is None

def test_first_sample_reboot_and_reset_never_fabricate_zero():
 a=sample();assert engine['derive'](a,None)['cpu']['usage_pct'] is None
 b=copy.deepcopy(a);b['boot_id']='two';b['uptime_seconds']=20
 assert engine['derive'](b,a)['network'][0]['rx_bytes_per_second'] is None
 assert engine['delta'](2,10,5) is None

def test_collector_output_excludes_sensitive_process_fields():
 c=runpy.run_path(str(ROOT/'native_ops/telemetry/collector.py'));v=c['collect']()
 assert v['cpu']['cores']>0 and v['memory']['total_bytes']>0
 assert len(v['processes'])<=200
 assert all(set(p)=={'pid','name','state','cpu_seconds','rss_bytes','threads','start_ticks'} for p in v['processes'])

def test_storage_rates_do_not_double_count_device_mapper():
 v=engine['derive'](sample(),None)
 v['disk_io']=[{'name':'sda','read_bytes_per_second':10,'write_bytes_per_second':20},{'name':'dm-0','read_bytes_per_second':10,'write_bytes_per_second':20}]
 result=engine['summary'](v)
 assert result['read']==10 and result['write']==20

def test_archive_retains_old_full_samples_and_detects_corruption(tmp_path):
 import sqlite3
 storage=runpy.run_path(str(ROOT/'native_ops/telemetry/storage.py'))
 a=storage['Archive'](tmp_path)
 old={'id':'dell-control','metrics':{'processes':[{'pid':2,'rss_bytes':100}]}}
 a.append([old],1);a.append([old],2000000000);a.close()
 files=sorted(tmp_path.glob('*.sqlite3'));assert len(files)==2
 assert all(storage['verify'](p)==1 for p in files)
 with sqlite3.connect(files[0]) as db:db.execute("UPDATE snapshots SET sha256='invalid'")
 import pytest
 with pytest.raises(ValueError):storage['verify'](files[0])

def test_history_query_bounds_output_without_deleting_old_samples():
 import sqlite3
 storage=runpy.run_path(str(ROOT/'native_ops/telemetry/storage.py'))
 with sqlite3.connect(':memory:') as db:
  db.execute('CREATE TABLE samples(host TEXT,t INTEGER,data TEXT)')
  db.executemany('INSERT INTO samples VALUES(?,?,?)',[('h',i*86400,'{"cpu":50,"cores":2}') for i in range(1000)])
  storage['initialize'](db)
  result=storage['ranges'](db,'h',1000*86400)
  assert 1<len(result['all'])<=182 and result['all'][0]['cpu']==50
  assert db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]==1000

def test_incident_lifecycle_preserves_history_and_does_not_resolve_on_failure(monkeypatch):
 import sqlite3,io,json,urllib.request
 op=runpy.run_path(str(ROOT/'native_ops/telemetry/operations.py'))
 db=sqlite3.connect(':memory:')
 response={'status':'success','data':{'alerts':[{'state':'firing','labels':{'alertname':'DiskLow','host':'nas','severity':'warning'}}]}}
 monkeypatch.setattr(urllib.request,'urlopen',lambda *a,**kw:io.BytesIO(json.dumps(response).encode()))
 assert len(op['observe'](db,100)['active'])==1
 assert len(op['observe'](db,120)['history'])==1
 def fail(*a,**kw):raise OSError()
 monkeypatch.setattr(urllib.request,'urlopen',fail)
 assert len(op['observe'](db,140)['active'])==1
 response['data']['alerts']=[]
 monkeypatch.setattr(urllib.request,'urlopen',lambda *a,**kw:io.BytesIO(json.dumps(response).encode()))
 out=op['observe'](db,160)
 assert not out['active'] and out['history'][0]['resolved']==160

def test_metrics_do_not_publish_stale_values_or_process_names():
 exporter=runpy.run_path(str(ROOT/'native_ops/telemetry/exporter.py'))
 txt=exporter['exposition']([{'id':'nas','status':'unavailable','received_at':1,'metrics':{'processes':[{'name':'private'}]}}],100)
 assert 'ops_infra_up{host="nas"} 0.0' in txt and 'private' not in txt and 'cpu_usage' not in txt

def test_historical_snapshot_is_exact_bounded_and_integrity_checked(tmp_path):
 import pytest
 storage=runpy.run_path(str(ROOT/'native_ops/telemetry/storage.py'));q=runpy.run_path(str(ROOT/'native_ops/telemetry/query.py'))
 a=storage['Archive'](tmp_path);a.append([{'id':'nas','metrics':{'processes':[{'pid':42}]}}],100);a.close()
 assert q['query'](tmp_path,'nas',110)['host']['metrics']['processes'][0]['pid']==42
 with pytest.raises(ValueError):q['query'](tmp_path,'nas',200)
 with pytest.raises(ValueError):q['query'](tmp_path,'../../etc/passwd',110)

def test_rollups_preserve_peaks_and_idempotent_counts():
 import sqlite3
 storage=runpy.run_path(str(ROOT/'native_ops/telemetry/storage.py'))
 with sqlite3.connect(':memory:') as db:
  db.execute('CREATE TABLE samples(host TEXT,t INTEGER,data TEXT,PRIMARY KEY(host,t))');storage['initialize'](db)
  storage['save_summary'](db,'nas',{'t':100,'cpu':10});storage['save_summary'](db,'nas',{'t':110,'cpu':90});storage['save_summary'](db,'nas',{'t':110,'cpu':90})
  p=storage['ranges'](db,'nas',120)['all'][0]
  assert p['sample_count']==2 and p['cpu']==50 and p['cpu_max']==90
