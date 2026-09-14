"""Real Tauri view, live metrics and local IPC boundaries; isolated browser profile."""
import base64,json,os,subprocess,tempfile,time,urllib.request,urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT.parents[1]/'artifacts/completion-final-2026-09-14';API='http://127.0.0.1:4467'
if (OUT/'ui-test.json').exists():(OUT/'ui-test.json').rename(OUT/('ui-test-previous-'+str(time.time_ns())+'.json'))
def call(method,path,data=None):
 req=urllib.request.Request(API+path,method=method,data=None if data is None else json.dumps(data).encode(),headers={'Content-Type':'application/json'})
 try:
  with urllib.request.urlopen(req,timeout=40) as f:return json.load(f)['value']
 except urllib.error.HTTPError as e:raise RuntimeError(e.read().decode()) from None
with tempfile.TemporaryDirectory(prefix='ops-telemetry-ui-') as temp:
 env=dict(os.environ,DISPLAY=':95',GDK_BACKEND='x11',WEBKIT_DISABLE_DMABUF_RENDERER='1',XDG_CONFIG_HOME=temp,XDG_DATA_HOME=temp);env.pop('WAYLAND_DISPLAY',None)
 x=subprocess.Popen(['Xvfb',':95','-screen','0','1600x1100x24','-nolisten','tcp'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);driver=None;session=None
 try:
  time.sleep(1);driver=subprocess.Popen(['/home/ops-user/.cargo/bin/tauri-driver','--port','4467','--native-port','4468'],env=env,stdout=(OUT/'webdriver.log').open('w'),stderr=subprocess.STDOUT)
  for _ in range(40):
   try:call('GET','/status');break
   except Exception:time.sleep(.25)
  session=call('POST','/session',{'capabilities':{'alwaysMatch':{'browserName':'wry','tauri:options':{'application':os.environ.get('OPS_UI_BINARY',str(ROOT/'src-tauri/target/release/ops-desktop'))}}}})['sessionId'];p='/session/'+session;time.sleep(5);views={}
  def js(code):return call('POST',p+'/execute/sync',{'script':code,'args':[]})
  def invoke(command):return call('POST',p+'/execute/async',{'script':"const done=arguments[arguments.length-1];window.__TAURI__.core.invoke(arguments[0]).then(v=>done({ok:true,value:v}),e=>done({ok:false,error:String(e)}))",'args':[command]})
  for handle in call('GET',p+'/window/handles'):
   call('POST',p+'/window',{'handle':handle});url=call('GET',p+'/url')
   if '/infrastructure/' in url:views['infrastructure']=handle
   elif '/atlas/' in url:views['atlas']=handle
   elif url.startswith('tauri:') and js('return !!document.querySelector("[data-service=infrastructure]")'):views['tabs']=handle
  assert set(views)=={'infrastructure','atlas','tabs'},views
  call('POST',p+'/window',{'handle':views['tabs']});js('document.querySelector("[data-service=infrastructure]").click();return true');time.sleep(.5)
  assert js('return document.querySelector("[aria-selected=true]").dataset.service')=='infrastructure'
  call('POST',p+'/window',{'handle':views['infrastructure']})
  live=invoke('infrastructure_snapshot');assert live['ok'],live
  for _ in range(30):
   if js('return document.querySelectorAll("#kpis .kpi").length')==4:break
   time.sleep(.2)
  assert js('return document.querySelectorAll("#fleet tr").length')==5
  assert sum(h['status']=='online' for h in live['value']['hosts'])==5
  assert js('return document.querySelectorAll(".charts .chart svg").length')==4
  checks=js("const kept=data;data=JSON.parse(JSON.stringify(data));data.hosts[0].status='unavailable';render();const stale=document.querySelector('#coverage').textContent==='4/5'&&!document.querySelector('#attention').hidden;for(const h of data.hosts)h.received_at=0;render();const offline=document.querySelector('#coverage').textContent==='0/5'&&document.querySelector('.kpi-value').textContent==='—';data=kept;render();return {stale,offline}")
  assert checks=={'stale':True,'offline':True},checks

  assert js('return document.querySelector(".infrastructure-art").complete && document.querySelector(".infrastructure-art").naturalWidth>1000')
  assert js('return document.querySelectorAll(".ops-icon").length')>30
  assert js('return getComputedStyle(document.querySelector(".ops-icon[data-icon=cpu]")).webkitMaskImage.includes("cpu.svg")')
  assert '5/5' in js('return document.querySelector("#hero-meta").textContent')
  (OUT/'infrastructure-overview.png').write_bytes(base64.b64decode(call('GET',p+'/screenshot')))
  js('document.querySelector("[data-host=prod]").click();return true');time.sleep(.3)
  assert js('return document.querySelector("#title").textContent')=='Production'
  assert js('return document.querySelector("#detail").hidden') is False
  (OUT/'infrastructure-machine.png').write_bytes(base64.b64decode(call('GET',p+'/screenshot')))
  js('document.querySelector("#process-search").value="no-match-fixture";document.querySelector("#process-search").dispatchEvent(new Event("input"));return true')
  if any(h['id']=='prod' and h.get('metrics') for h in live['value']['hosts']):assert 'Aucun processus' in js('return document.querySelector("#processes").textContent')
  js('document.querySelector("#theme").click();document.querySelectorAll("[data-period]")[2].click();return true')
  assert js('return document.body.classList.contains("light")')
  assert js('return document.querySelector(".artwork-light").naturalWidth>1000 && getComputedStyle(document.querySelector(".artwork-light")).display!=="none"')
  js("window.scrollTo(0,0);return true");time.sleep(.5)
  (OUT/'infrastructure-light.png').write_bytes(base64.b64decode(call('GET',p+'/screenshot')))
  assert not invoke('settings_state')['ok']
  call('POST',p+'/window',{'handle':views['atlas']});assert not invoke('infrastructure_snapshot')['ok']
  call('POST',p+'/window',{'handle':views['infrastructure']})
  call('POST',p+'/window/rect',{'width':900,'height':760});time.sleep(.3)
  assert js('return document.documentElement.scrollWidth<=window.innerWidth+1')
  (OUT/'infrastructure-compact-light.png').write_bytes(base64.b64decode(call('GET',p+'/screenshot')))
  call('POST',p+'/window/rect',{'width':1500,'height':1050})
  js("document.querySelector('#process-search').value='';document.querySelector('#process-search').dispatchEvent(new Event('input'));document.querySelector('#theme').click();document.querySelector('#disks').scrollIntoView({block:'center'});return true")
  assert js('return document.querySelectorAll(".apple-storage").length')>0
  assert js('return [...document.querySelectorAll(".apple-storage")].every(x=>x.getAttribute("aria-label").includes("disponibles"))')
  (OUT/'storage-apple-bars.png').write_bytes(base64.b64decode(call('GET',p+'/screenshot')))
  js("document.querySelector('#history-open').click();return true")
  for _ in range(40):
   if js('return !!historical'):break
   time.sleep(.25)
  assert js('return !!historical && historical.integrity_verified'),js('return document.querySelector("#history-label").textContent')
  assert js('return document.body.classList.contains("historical")')
  js("document.querySelector('#history-live').click();return true");time.sleep(.3)
  assert not js('return !!historical')
  js("document.querySelector('[data-infra-view=logs]').click();document.querySelector('#log-host').value='dell-control';document.querySelector('#log-host').dispatchEvent(new Event('change'));document.querySelector('#log-sources').click();return true")
  for _ in range(100):
   if js('return !logBusy'):break
   time.sleep(.2)
  assert js('return document.querySelectorAll("#log-service option").length')>1
  js("document.querySelector('#log-service').value='system|';document.querySelector('#log-read').click();return true")
  for _ in range(100):
   if js('return !logBusy'):break
   time.sleep(.2)
  assert js('return !!logData && logData.records.length>0'),js('return document.querySelector("#log-status").textContent')
  assert js('return document.querySelectorAll(".log-entry").length')>0
  js('refreshLogHealth();return true');time.sleep(.5)
  assert 'Collecte active' in js('return document.querySelector("#log-collection").textContent')
  js("document.querySelector('#log-search').value='no-match-log-fixture';document.querySelector('#log-search').dispatchEvent(new Event('input'));return true")
  assert js('return document.querySelectorAll(".log-entry").length')==0
  js("document.querySelector('#log-search').value='';document.querySelector('#log-search').dispatchEvent(new Event('input'));document.querySelector('#log-export').click();return true");time.sleep(.5)
  assert 'Export enregistré' in js('return document.querySelector("#log-status").textContent')
  exported=js('return document.querySelector("#log-status").textContent').split(' : ',1)[1]
  assert Path(exported).stat().st_mode&0o077==0
  js("document.querySelector('#log-archive').click();return true")
  for _ in range(100):
   if js('return !logBusy'):break
   time.sleep(.2)
  assert js('return !!logData && logData.archived && logData.records.length>0')
  js("document.querySelector('[data-infra-view=resources]').click();return true")
  assert not js('return !!document.querySelector("#explore") || !!document.querySelector("iframe")')
  assert not invoke('infrastructure_explore')['ok']
  before_handles=call('GET',p+'/window/handles')
  js("document.querySelector('[data-metric-group=all]').click();return true")
  for _ in range(200):
   if js('return !analysisPending && analysisGroup==="all"'):break
   time.sleep(.25)
  assert js('return document.querySelectorAll(".metric-card").length')==26
  assert '26/26' in js('return document.querySelector("#analysis-status").textContent'),js('return document.querySelector("#analysis-status").textContent')
  assert js('return analysisResults.get(3).series.length>0 && analysisResults.get(13).series.length>0')
  assert js('return analysisResults.get(3).series.every(s=>s.labels.host===selected)')
  js("document.querySelector('[data-metric-group=cpu]').click();return true")
  for _ in range(100):
   if js('return !analysisPending'):break
   time.sleep(.2)
  js("document.querySelector('#metric-plot-3').closest('.metric-card').querySelector('[data-expand]').click();document.querySelector('#analysis-panel').scrollIntoView({block:'start'});return true")
  assert js('return document.querySelector("#metric-plot-3").closest(".metric-card").classList.contains("expanded")')
  js("document.querySelector('#metric-plot-3 svg').dispatchEvent(new KeyboardEvent('keydown',{key:'End',bubbles:true}));return true")
  assert 'Production' in js('return document.querySelector("#metric-inspector-3").textContent')
  js("document.querySelector('#metric-legend-3 button').click();return true")
  assert js('return document.querySelector("#metric-legend-3 button").getAttribute("aria-pressed")')=='false'
  js("document.querySelector('#metric-legend-3 button').click();document.querySelector('#metric-plot-3').closest('.metric-card').querySelector('[data-metric-export]').click();return true")
  for _ in range(50):
   if 'Export enregistré' in js('return document.querySelector("#analysis-status").textContent'):break
   time.sleep(.1)
  export_path=js('return document.querySelector("#analysis-status").textContent').split(' : ',1)[1]
  assert Path(export_path).suffix=='.csv' and Path(export_path).stat().st_mode&0o077==0
  assert len(Path(export_path).read_text().splitlines())>2
  js("document.querySelector('#analysis-from').value=document.querySelector('#analysis-to').value;document.querySelector('#analysis-apply').click();return true")
  assert 'valide' in js('return document.querySelector("#analysis-status").textContent')
  js("document.querySelector('#analysis-live').click();return true")
  for _ in range(100):
   if js('return !analysisPending'):break
   time.sleep(.2)
  assert call('GET',p+'/window/handles')==before_handles
  js("document.querySelector('[data-period=\"1h\"]').click();return true")
  for _ in range(100):
   if js('return !analysisPending'):break
   time.sleep(.2)
  js("document.querySelector('#analysis-panel').scrollIntoView({block:'start'});return true")
  time.sleep(.5)
  (OUT/'native-analysis.png').write_bytes(base64.b64decode(call('GET',p+'/screenshot')))
  call('POST',p+'/window/rect',{'width':900,'height':760});time.sleep(.3)
  assert js('return document.documentElement.scrollWidth<=window.innerWidth+1')
  (OUT/'native-analysis-compact.png').write_bytes(base64.b64decode(call('GET',p+'/screenshot')))
  report={'status':'passed','native_tauri':True,'hosts':len(live['value']['hosts']),'online':sum(h['status']=='online' for h in live['value']['hosts']),'topbar_tab':True,'detail_navigation':True,'theme':True,'period_selection':True,'compact_layout':True,'ipc_isolated':True,'four_live_charts':True,'storage_apple_bars':True,'historical_snapshot_verified':True,'generated_hero_loaded':True,'coherent_icons_loaded':True,'native_26_metric_panels':True,'no_grafana_window_or_iframe':True,'metric_inspection_toggle_expand_export':True,'logs_read_search_archive_export':True,'retention_permanent':live['value'].get('retention_policy')=='permanent','stale_and_offline_states':True,'profile':'isolated','tested_at':time.time()};(OUT/'ui-test.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
 finally:
  if session:
   try:call('DELETE','/session/'+session)
   except Exception:pass
  if driver:driver.terminate();driver.wait(timeout=10)
  x.terminate();x.wait(timeout=10)
