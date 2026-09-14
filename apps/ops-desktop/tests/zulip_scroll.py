"""Native-wheel regression against the installed local Zulip; no messages sent.
Optional OPS_SCROLL_CREDENTIAL_FILE is consumed privately, never logged.
"""
import json,os,subprocess,time,urllib.request,urllib.error
from pathlib import Path
OUT=Path(os.environ.get('OPS_SCROLL_EVIDENCE','/tmp/ops-zulip-scroll-evidence'));OUT.mkdir(parents=True,exist_ok=True);API='http://127.0.0.1:4457';session=None;driver=None

def call(method,path,data=None):
 req=urllib.request.Request(API+path,method=method,data=None if data is None else json.dumps(data).encode(),headers={'Content-Type':'application/json'})
 try:
  with urllib.request.urlopen(req,timeout=30) as r:return json.load(r)['value']
 except Exception:raise RuntimeError('WebDriver request failed: '+path.split('/')[-1]) from None

def main():
 global session,driver
 env=dict(os.environ,DISPLAY=':96',GDK_BACKEND='x11',WEBKIT_DISABLE_DMABUF_RENDERER='1');env.pop('WAYLAND_DISPLAY',None)
 xvfb=subprocess.Popen(['Xvfb',':96','-screen','0','1440x1000x24','-nolisten','tcp'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
 try:
  time.sleep(1)
  driver=subprocess.Popen(['/home/ops-user/.cargo/bin/tauri-driver','--port','4457','--native-port','4458'],env=env,stdout=(OUT/'driver.log').open('w'),stderr=subprocess.STDOUT)
  for _ in range(30):
   try:call('GET','/status');break
   except Exception:time.sleep(.3)
  value=call('POST','/session',{'capabilities':{'alwaysMatch':{'browserName':'wry','tauri:options':{'application':'/home/ops-user/ops-control-plane/apps/ops-desktop/src-tauri/target/release/ops-desktop'}}}})
  session=value['sessionId'];prefix='/session/'+session
  def js(script,args=[]):return call('POST',prefix+'/execute/sync',{'script':script,'args':args})
  time.sleep(4);views={}
  for h in call('GET',prefix+'/window/handles'):
   call('POST',prefix+'/window',{'handle':h});url=call('GET',prefix+'/url')
   if 'zulip.ops.local' in url:views['zulip']=h
   elif url.startswith('tauri:') and '/atlas/' not in url and '/terminals/' not in url:views['tabs']=h
  call('POST',prefix+'/window',{'handle':views['tabs']})
  js('document.querySelector("[data-service=zulip]").click()');time.sleep(1)
  call('POST',prefix+'/window',{'handle':views['zulip']})
  if js('return !!document.querySelector("input[type=password]")'):
   raw=Path(os.environ['OPS_SCROLL_CREDENTIAL_FILE']).read_text();password=raw.split('Mot de passe : ',1)[1].splitlines()[0];raw=''
   js('let u=document.querySelector("input[type=email],input[name=username]");let p=document.querySelector("input[type=password]");u.value=arguments[0];p.value=arguments[1];u.dispatchEvent(new Event("input",{bubbles:true}));p.dispatchEvent(new Event("input",{bubbles:true}));p.form.requestSubmit();return true',['admin@ops.local',password]);password=''
  for _ in range(60):
   if js('return !!document.querySelector("#message_feed_container")'):break
   time.sleep(.5)
  js('location.hash="#narrow/channel/5-ops-approbations";return true')
  for _ in range(60):
   if js('return document.scrollingElement.scrollHeight > innerHeight+300'):break
   time.sleep(.5)
  wid=subprocess.check_output(['xdotool','search','--name','^Ops$'],env=env,text=True).splitlines()[-1]
  def wheel(button,x=600,y=450):
   subprocess.run(['xdotool','windowfocus',wid,'mousemove','--window',wid,str(x),str(y),'click','--repeat','6','--delay','90',str(button)],env=env,check=True)
   time.sleep(1)
  def check_scroll(label):
   assert js('return getComputedStyle(document.documentElement).overscrollBehavior')=='auto'
   js('window.scrollTo(0,Math.min(700,document.scrollingElement.scrollHeight-innerHeight-100));return true');time.sleep(.5)
   before=js('return scrollY');assert before>100
   wheel(4);up=js('return scrollY');assert up<before-50,(label,before,up)
   wheel(5);down=js('return scrollY');assert down>up+50,(label,up,down)
   return {'phase':label,'before':before,'up':up,'down':down}
  report=[check_scroll('initial')]
  call('POST',prefix+'/window',{'handle':views['tabs']});js('document.querySelector("[data-service=atlas]").click()');time.sleep(.3)
  call('POST',prefix+'/window',{'handle':views['tabs']});js('document.querySelector("[data-service=zulip]").click()');time.sleep(.3)
  call('POST',prefix+'/window',{'handle':views['zulip']});report.append(check_scroll('tab_return'))
  call('POST',prefix+'/refresh',{});time.sleep(6)
  for _ in range(60):
   if js('return document.scrollingElement.scrollHeight > innerHeight+300'):break
   time.sleep(.5)
  report.append(check_scroll('reload'))
  # Native wheel must still scroll a nested pane without moving the message feed.
  js('let p=document.createElement("div");p.id="ops-scroll-test-pane";p.style.cssText="position:fixed;z-index:999999;left:400px;top:180px;width:300px;height:200px;overflow:auto;overscroll-behavior:contain;background:white";let c=document.createElement("div");c.style.height="2000px";p.append(c);document.body.append(p);return true')
  root_before=js('return scrollY');wheel(5,500,300)
  nested=js('return {pane:document.querySelector("#ops-scroll-test-pane").scrollTop,root:scrollY}')
  assert nested['pane']>50 and nested['root']==root_before,nested
  js('document.querySelector("#ops-scroll-test-pane").remove()')
  report.append({'phase':'nested_pane','position':nested['pane'],'root_unchanged':True})
  (OUT/'scroll-results.json').write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report),flush=True)

 finally:
  if session:
   try:call('DELETE','/session/'+session)
   except Exception:pass
  if driver:driver.terminate()
  xvfb.terminate()
if __name__=='__main__':main()
