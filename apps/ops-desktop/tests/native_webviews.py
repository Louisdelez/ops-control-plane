"""Exercise the real release binary and native web pages via WebKit WebDriver.
No login secrets or API model requests are used.
"""
import base64,json,os,subprocess,tempfile,time,urllib.request,urllib.error
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=Path('/home/ops-user/standard-install-review/native-integration/desktop-evidence')
OUT.mkdir(exist_ok=True)
API='http://127.0.0.1:4447'
def call(method,path,data=None):
    req=urllib.request.Request(API+path,method=method,data=None if data is None else json.dumps(data).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.urlopen(req,timeout=40) as r:return json.load(r)['value']
    except urllib.error.HTTPError as exc:
        raise RuntimeError(exc.read().decode()) from exc
def main():
    env=dict(os.environ,DISPLAY=':97',GDK_BACKEND='x11',WEBKIT_DISABLE_DMABUF_RENDERER='1')
    env.pop('WAYLAND_DISPLAY',None)
    xvfb=subprocess.Popen(['Xvfb',':97','-screen','0','1440x1000x24','-nolisten','tcp'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    session=None;driver=None;report=[]
    try:
        time.sleep(1)
        driver=subprocess.Popen(['/home/ops-user/.cargo/bin/tauri-driver','--port','4447','--native-port','4448'],env=env,stdout=(OUT/'webdriver.log').open('w'),stderr=subprocess.STDOUT)
        for _ in range(30):
            try:call('GET','/status');break
            except Exception:time.sleep(.3)
        value=call('POST','/session',{'capabilities':{'alwaysMatch':{'browserName':'wry','tauri:options':{'application':str(ROOT/'src-tauri/target/release/ops-desktop')}}}})
        session=value['sessionId'];prefix='/session/'+session
        def js(script):return call('POST',prefix+'/execute/sync',{'script':script,'args':[]})
        def screenshot(name):(OUT/(name+'.png')).write_bytes(base64.b64decode(call('GET',prefix+'/screenshot')))
        time.sleep(5)
        handles=call('GET',prefix+'/window/handles')
        print('WEBVIEWS',handles,flush=True)
        views={}
        for handle in handles:
            call('POST',prefix+'/window',{'handle':handle})
            url=call('GET',prefix+'/url')
            print('VIEW',handle,url,flush=True)
            if url.startswith('tauri:') and '/atlas/' in url:views['atlas']=handle
            elif url.startswith('tauri:') and '/terminals/' in url:views['terminals']=handle
            elif url.startswith('tauri:'):views['tabs']=handle
            elif 'zulip.ops.local' in url:views['zulip']=handle
            elif ':8200/' in url:views['openbao']=handle
            elif ':9119/' in url:views['hermes']=handle
        assert len(views)==6, views
        def switch(name):call('POST',prefix+'/window',{'handle':views[name]})
        switch('tabs')
        assert js('return [...document.images].every(img=>img.complete && img.naturalWidth>0)')
        assert js('return document.querySelectorAll("[role=tab]").length')==5
        wid=subprocess.check_output(['xdotool','search','--name','^Ops$'],env=env,text=True).splitlines()[-1]
        switch('atlas')
        for _ in range(60):
            if js('return document.querySelectorAll(".model-card").length')>0:break
            time.sleep(.3)
        assert js('return document.querySelectorAll(".model-card").length')>0,js('return document.body.innerText')[:500]
        assert js('return [...document.images].every(img=>img.complete && img.naturalWidth>0)')
        assert js('return document.querySelectorAll(".nav-icon").length')==4
        assert js('return document.querySelector(".ops-illustration").naturalWidth')>500
        def invoke(command,args=None):
            return call('POST',prefix+'/execute/async',{'script':'const done=arguments[arguments.length-1];window.__TAURI__.core.invoke(arguments[0],arguments[1]).then(x=>done({ok:true,value:x})).catch(e=>done({ok:false,error:e}))','args':[command,args or {}]})
        catalogue=invoke('get_catalogue');assert catalogue['ok'],catalogue
        public=catalogue['value'];print('ATLAS_CATALOGUE_FIELDS',list(public),flush=True)
        snapshot=invoke('get_runtime_snapshot');assert snapshot['ok'],snapshot
        (OUT/'atlas-runtime.json').write_text(json.dumps(snapshot['value'],indent=2)+'\n')
        assert snapshot['value']['overall_available'] or snapshot['value']['native_service'],snapshot
        print('ATLAS_NATIVE_SERVICE_READ_VERIFIED',flush=True)
        # Select a real card identifier from the embedded catalogue, never a guessed alias.
        cards=public.get('cards',public.get('models',[]))
        for card in cards:
            cost=invoke('simulate_cost',{'request':{'card_id':card['card_id'],'input_tokens':10000000,'output_tokens':2000000}})
            if cost['ok'] and cost['value']['available']:break
        assert cost['ok'] and cost['value']['available'],cost
        preview=invoke('preview_candidates',{'request':{'limit':3}});assert preview['ok'],preview
        assert not invoke('begin_bootstrap')['ok']
        assert not invoke('save_provider_credential',{'providerAccountId':'invalid','openBaoPassword':'','apiKey':''})['ok']
        for panel in ['routing','costs','settings','catalogue']:
            js('document.querySelector("[data-tab='+panel+']").click();return true')
            assert js('return !document.querySelector("#panel-'+panel+'").hidden')
            time.sleep(.3)
            screenshot('redesign-'+panel)
        js('document.querySelector("[data-model-details]").click();return true')
        assert js('return document.querySelector("#model-dialog").open') is True
        screenshot('redesign-detail')
        js('document.querySelector("[data-close-dialog]").click();return true')
        js('document.querySelector("[data-configure-account]").click();return true')
        assert js('return document.querySelector("#credential-dialog").open') is True
        time.sleep(.3)
        screenshot('redesign-credentials')
        js('document.querySelector(".credential-dialog-close").click();return true')
        js('const input=document.querySelector("#catalogue-search");input.value="deepseek";input.dispatchEvent(new Event("input",{bubbles:true}));window.__atlasKept=true;return true')
        screenshot('atlas')
        subprocess.run(['/usr/bin/python3','-c','import gi;gi.require_version("Gdk","3.0");from gi.repository import Gdk;w=Gdk.get_default_root_window();Gdk.pixbuf_get_from_window(w,0,0,1320,860).savev("'+str(OUT/'tabs-atlas.png')+'","png",[],[])'],env=env,check=True)
        assert js('return document.querySelectorAll("#native-budget-list p").length')>0
        assert js('return document.querySelector(".app-shell").getBoundingClientRect().width')>1200
        for index,service in enumerate(['zulip','openbao','hermes','zulip']):
            switch('tabs')
            box=js('const r=document.querySelector("[data-service='+service+']").getBoundingClientRect(); return {x:r.x+r.width/2,y:r.y+r.height/2}')
            subprocess.run(['xdotool','windowfocus',wid,'mousemove','--window',wid,str(round(box['x'])),str(round(box['y'])),'click','1'],env=env,check=True)
            time.sleep(1)
            assert js('return document.querySelector("[aria-selected=true]").dataset.service')==service,(service,js('return document.body.innerText'))
            switch(service)
            for _ in range(50):
                if js('return document.body.innerText.length')>100:break
                time.sleep(.3)
            assert js('return document.body.innerText.length')>100
            if index==0:js('window.__opsPersistence="kept"; document.querySelector("input[type=email]").value="onglet-test"; return true')
            if index==3:
                assert js('return window.__opsPersistence')=='kept'
                assert js('return document.querySelector("input[type=email]").value')=='onglet-test'
                js('document.querySelector("input[type=email]").value=""; return true')
            denied=call('POST',prefix+'/execute/async',{'script':'const done=arguments[arguments.length-1]; if(!window.__TAURI__){done(true)}else{window.__TAURI__.core.invoke("open_service",{service:"zulip"}).then(()=>done(false)).catch(()=>done(true))}','args':[]})
            assert denied is True
            assert not invoke('get_catalogue')['ok']
            assert not invoke('terminal_start',{'tool':'codex','action':'version','cols':80,'rows':24})['ok']
            assert not invoke('get_native_budget')['ok']
            assert not invoke('execution_mode',{'mode':'api'})['ok']
            # Capture the entire native window, including its permanent tab strip.
            subprocess.run(['/usr/bin/python3','-c','import gi;gi.require_version("Gdk","3.0");from gi.repository import Gdk;w=Gdk.get_default_root_window();Gdk.pixbuf_get_from_window(w,0,0,1320,860).savev("'+str(OUT/('tabs-'+service+'.png'))+'","png",[],[])'],env=env,check=True)
            report.append({'service':service,'url':call('GET',prefix+'/url'),'tab_bar_visible':True,'shell_ipc_denied':True})
        switch('tabs')
        assert not invoke('execution_status')['ok']
        assert invoke('open_service',{'service':'terminals'})['ok']
        switch('terminals')
        time.sleep(1)
        assert js('return typeof Terminal')=='function'
        assert invoke('execution_status')['value']['mode']=='cli'
        assert not invoke('terminal_start',{'tool':'bash','action':'start','cols':80,'rows':24})['ok']
        for tool in ['codex','claude']:
            started=invoke('terminal_start',{'tool':tool,'action':'version','cols':80,'rows':24});assert started['ok'],started
            ident=started['value'];output=b''
            assert invoke('terminal_resize',{'id':ident,'cols':100,'rows':30})['ok']
            assert not invoke('execution_mode',{'mode':'api'})['ok']
            for _ in range(80):
                result=invoke('terminal_poll',{'id':ident});assert result['ok'],result
                output+=bytes(result['value']['data'])
                if result['value']['exited'] and not result['value']['data']:break
                time.sleep(.15)
            assert (b'codex-cli' if tool=='codex' else b'Claude Code') in output,(tool,output[:200])
            assert invoke('terminal_close',{'id':ident})['ok']
        assert js('return !document.querySelector("aside, #mode, #tool")')
        pilot=invoke('terminal_start',{'tool':'claude','action':'pilot','cols':80,'rows':24})
        assert pilot['ok'],pilot
        output=b''
        for _ in range(100):
            poll=invoke('terminal_poll',{'id':pilot['value']});assert poll['ok']
            output+=bytes(poll['value']['data'])
            if poll['value']['exited'] and not poll['value']['data']:break
            time.sleep(.15)
        assert b'Connecte ton compte' in output,output[:300]
        assert invoke('terminal_close',{'id':pilot['value']})['ok']
        # Launch two actual native TUIs through the + menu; no model prompt.
        for tool in ['codex','claude']:
            js('document.querySelector("#add").click();return true')
            assert js('return document.querySelector("#add").getAttribute("aria-expanded")')=='true'
            js("document.querySelector('[data-tool=" + tool + "]').click();return true")
            expected=1 if tool=='codex' else 2
            for _ in range(80):
                if js('return sessions.size')==expected:break
                time.sleep(.2)
            assert js('return sessions.size')==expected,js('return document.querySelector("#error").textContent')
        time.sleep(4)
        boxes=js('return [...document.querySelectorAll(".terminal-pane")].map(x=>{const r=x.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height}})')
        assert boxes[0]['x'] < boxes[1]['x'] and boxes[0]['y']==boxes[1]['y']
        assert boxes[0]['h']>500 and boxes[0]['w']>=320
        screenshot('terminals-columns')
        live_id=js('return [...sessions.keys()][0]')
        resources=subprocess.check_output(['systemctl','--user','show','ops-terminal-'+live_id+'.scope','-p','MemoryMax','-p','TasksMax'],env=env,text=True)
        assert 'MemoryMax=1572864000' in resources and 'TasksMax=128' in resources,resources
        assert invoke('terminal_write',{'id':live_id,'data':'\x1b'})['ok']
        switch('tabs');assert invoke('open_service',{'service':'atlas'})['ok']
        assert invoke('open_service',{'service':'terminals'})['ok']
        switch('terminals');assert js('return sessions.size')==2
        subprocess.run(['xdotool','windowsize',wid,'1000','700'],env=env,check=True)
        time.sleep(1)
        assert js('return [...sessions.values()].every(s=>s.term.cols>=20 && s.term.rows>=5)')
        # Four simultaneous panes, fifth refused, including on a narrow window.
        for expected in [3,4]:
            js('launch("codex","version");return true')
            for _ in range(60):
                if js('return sessions.size')==expected:break
                time.sleep(.1)
            assert js('return sessions.size')==expected
        assert js('return document.querySelector("#add").disabled')
        assert not invoke('terminal_start',{'tool':'codex','action':'version','cols':80,'rows':24})['ok']
        screenshot('terminals-four-columns')
        for expected in [3,2,1,0]:
            js('document.querySelector(".pane-close").click();return true')
            for _ in range(60):
                if js('return sessions.size')==expected:break
                time.sleep(.1)
            assert js('return sessions.size')==expected
        assert not js('return document.querySelector("#empty").hidden')
        assert not js('return document.querySelector("#add").disabled')
        assert invoke('execution_mode',{'mode':'api'})['ok']
        started=invoke('terminal_start',{'tool':'codex','action':'version','cols':80,'rows':24})
        assert started['ok'],started
        assert invoke('terminal_close',{'id':started['value']})['ok']
        assert invoke('execution_status')['value']['mode']=='hybrid'
        assert invoke('execution_mode',{'mode':'cli'})['ok']
        print('REAL_CLI_COLUMNS_PLUS_CLOSE_RESIZE_AND_AUTOMATIC_COEXISTENCE_VERIFIED',flush=True)
        switch('atlas')
        assert js('return window.__atlasKept') is True
        assert js('return document.querySelector("#catalogue-search").value')=='deepseek'
        (OUT/'atlas-results.json').write_text(json.dumps({'catalogue_cards':len(cards),'cost':cost['value'],'preview_verified':True,'four_panels_verified':True,'native_bootstrap_denied':True,'state_preserved':True},indent=2)+'\n')
        subprocess.run(['xdotool','windowsize',wid,'1000','700'],env=env,check=True)
        time.sleep(1)
        switch('tabs');assert js('return window.innerWidth')==1000
        assert js('return window.innerHeight')==56
        switch('tabs')
        box=js('const r=document.querySelector("[data-service=atlas]").getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}')
        subprocess.run(['xdotool','mousemove','--window',wid,str(round(box['x'])),str(round(box['y'])),'click','1'],env=env,check=True)
        time.sleep(.3)
        assert js('return [...document.querySelectorAll("[role=tab]")].filter(x=>x.tabIndex===0).length')==1
        switch('atlas');assert js('return document.documentElement.scrollWidth<=window.innerWidth')
        screenshot('redesign-compact')
        switch('tabs');assert invoke('open_service',{'service':'zulip'})['ok']
        time.sleep(.3)
        switch('zulip');assert js('return window.innerWidth')==1000
        assert js('return window.innerHeight')==644
        (OUT/'tabs-results.json').write_text(json.dumps({'views':report,'state_preserved':True,'resize_verified':True},indent=2)+'\n')
        print('PERMANENT_TABS_STATE_AND_RESIZE_VERIFIED',flush=True)
    finally:
        if session:
            try:call('DELETE','/session/'+session)
            except Exception:pass
        if driver:driver.terminate();driver.wait(timeout=10)
        xvfb.terminate();xvfb.wait(timeout=10)
if __name__=='__main__':main()
