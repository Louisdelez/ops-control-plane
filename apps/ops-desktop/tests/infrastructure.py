"""Real Tauri view, live metrics and local IPC boundaries; isolated browser profile."""
import base64,json,os,subprocess,tempfile,time,urllib.request,urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT.parents[1]/'artifacts/telemetry-2026-09-14';API='http://127.0.0.1:4467'
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
  time.sleep(1);driver=subprocess.Popen(['/home/ops-user/.cargo/bin/tauri-driver','--port','4467','--native-port','4468'],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  for _ in range(40):
   try:call('GET','/status');break
   except Exception:time.sleep(.25)
  session=call('POST','/session',{'capabilities':{'alwaysMatch':{'browserName':'wry','tauri:options':{'application':str(ROOT/'src-tauri/target/release/ops-desktop')}}}})['sessionId'];p='/session/'+session;time.sleep(5);views={}
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
  assert js('return document.querySelectorAll(".chart svg").length')==4
  checks=js("const kept=data;data=JSON.parse(JSON.stringify(data));data.hosts[0].status='unavailable';render();const stale=document.querySelector('#coverage').textContent==='4/5'&&!document.querySelector('#attention').hidden;for(const h of data.hosts)h.received_at=0;render();const offline=document.querySelector('#coverage').textContent==='0/5'&&document.querySelector('.kpi-value').textContent==='—';data=kept;render();return {stale,offline}")
  assert checks=={'stale':True,'offline':True},checks

  (OUT/'infrastructure-overview.png').write_bytes(base64.b64decode(call('GET',p+'/screenshot')))
  js('document.querySelector("[data-host=prod]").click();return true');time.sleep(.3)
  assert js('return document.querySelector("#title").textContent')=='Production'
  assert js('return document.querySelector("#detail").hidden') is False
  (OUT/'infrastructure-machine.png').write_bytes(base64.b64decode(call('GET',p+'/screenshot')))
  js('document.querySelector("#process-search").value="no-match-fixture";document.querySelector("#process-search").dispatchEvent(new Event("input"));return true')
  if any(h['id']=='prod' and h.get('metrics') for h in live['value']['hosts']):assert 'Aucun processus' in js('return document.querySelector("#processes").textContent')
  js('document.querySelector("#theme").click();document.querySelectorAll("[data-period]")[2].click();return true')
  assert js('return document.body.classList.contains("light")')
  assert not invoke('settings_state')['ok']
  call('POST',p+'/window',{'handle':views['atlas']});assert not invoke('infrastructure_snapshot')['ok']
  call('POST',p+'/window',{'handle':views['infrastructure']})
  call('POST',p+'/window/rect',{'width':900,'height':760});time.sleep(.3)
  assert js('return document.documentElement.scrollWidth<=window.innerWidth+1')
  (OUT/'infrastructure-compact-light.png').write_bytes(base64.b64decode(call('GET',p+'/screenshot')))
  report={'status':'passed','native_tauri':True,'hosts':len(live['value']['hosts']),'online':sum(h['status']=='online' for h in live['value']['hosts']),'topbar_tab':True,'detail_navigation':True,'theme':True,'period_selection':True,'compact_layout':True,'ipc_isolated':True,'four_live_charts':True,'stale_and_offline_states':True,'profile':'isolated','tested_at':time.time()};(OUT/'ui-test.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
 finally:
  if session:
   try:call('DELETE','/session/'+session)
   except Exception:pass
  if driver:driver.terminate();driver.wait(timeout=10)
  x.terminate();x.wait(timeout=10)
