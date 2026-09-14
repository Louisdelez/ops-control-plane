const $=id=>document.getElementById(id);let category='pending',items=new Map(),next=0,busy=false,online=false,expanded=new Set();
const categories=[['pending','◉','En attente','Un regard, une décision. Tu gardes la main.'],['accepted','✓','Acceptées','Les décisions que tu as validées.'],['rejected','×','Refusées','Les demandes que tu as déclinées.'],['later','◷','À traiter plus tard','Mises de côté, à reprendre quand tu veux.'],['expired','⌛','Expirées','Ces demandes nécessitent une nouvelle autorisation.']];
async function api(path,options={}){const r=await fetch('/api/'+path,{...options,headers:{'Content-Type':'application/json',...options.headers}});const data=await r.json();if(!r.ok){if(r.status===401)showLogin();throw Error(typeof data.detail==='string'?data.detail:'Impossible de traiter cette demande.');}return data;}
function showLogin(){items.clear();$('feed').replaceChildren();$('workspace').hidden=true;$('login').hidden=false;}
function notice(text){$('notice').textContent=text;$('notice').hidden=!text;}
function menu(open){$('sidebar').classList.toggle('open',open);document.body.classList.toggle('sidebar-open',open);$('shade').hidden=!open;$('menu').setAttribute('aria-expanded',String(open));if(open)$('close-menu').focus();else $('menu').focus();}
$('menu').onclick=()=>menu(!$('sidebar').classList.contains('open'));$('close-menu').onclick=$('shade').onclick=()=>menu(false);document.addEventListener('keydown',e=>{if(e.key==='Escape')menu(false);});
function node(tag,cls,text){const el=document.createElement(tag);if(cls)el.className=cls;if(text!==undefined)el.textContent=text;return el;}
function render(){const selected=categories.find(x=>x[0]===category);$('title').textContent=selected[2];$('subtitle').textContent=selected[3];$('categories').replaceChildren();for(const [key,icon,label] of categories){const btn=node('button',category===key?'active':'');btn.append(node('span','',icon),node('span','',label),node('span','',String([...items.values()].filter(x=>x.status===key).length)));btn.onclick=()=>{category=key;render();menu(false);};$('categories').append(btn);}
const current=[...items.values()].filter(x=>x.status===category).sort((a,b)=>b.id-a.id);$('count').textContent=current.length;$('feed').replaceChildren();if(!current.length){const el=node('div','empty');el.append(node('span','','✓'),node('h2','',category==='pending'?'Tout est à jour':'Rien ici pour le moment'),node('p','',category==='pending'?'Les prochaines demandes apparaîtront ici.':'Tes décisions restent accessibles dans ces catégories.'));$('feed').append(el);}
for(const item of current){const card=node('article','card');const meta=node('div','meta');meta.append(node('span','badge',selected[2]),node('time','',new Date(item.created_at).toLocaleString('fr-CH',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})));card.append(meta);const text=node('p','message'+(expanded.has(item.id)?' expanded':''),item.message);card.append(text);const expand=node('button','expand',expanded.has(item.id)?'Voir moins ↑':'Voir plus ↓');expand.setAttribute('aria-expanded',String(expanded.has(item.id)));expand.onclick=()=>{expanded.has(item.id)?expanded.delete(item.id):expanded.add(item.id);render();};card.append(expand);if(['pending','later'].includes(item.status)){const actions=node('div','actions');for(const [decision,label,cls] of [['approve','✓ Accepter','accept'],['reject','× Refuser','reject'],[category==='later'?'resume':'later',category==='later'?'Reprendre':'◷ Plus tard','later']]){const b=node('button',cls,label);b.disabled=busy||!online;b.onclick=()=>decide(item.id,decision);actions.append(b);}card.append(actions);}
$('feed').append(card);requestAnimationFrame(()=>{if(!expanded.has(item.id))expand.hidden=text.scrollWidth<=text.clientWidth;});}
$('more').hidden=!next;$('more').disabled=busy;
}
async function load(older=false){if(busy)return;try{const data=await api('feed'+(older&&next?'?before='+next:''));online=true;$('sync').textContent='À jour';$('name').textContent=data.name;$('login').hidden=true;$('workspace').hidden=false;initPush();for(const i of data.items)items.set(i.id,i);if(older||items.size<=100)next=data.next;render();}catch(e){online=false;$('sync').textContent='Hors connexion';notice(e.message);if($('workspace').hidden&&$('login').hidden)showLogin();if(!$('login').hidden)$('login-error').textContent=e.message;render();}}
async function decide(id,decision){if(busy||!online)return;busy=true;render();notice('');try{const result=await api('items/'+id+'/decision',{method:'POST',body:JSON.stringify({decision})});if(result.confirmed&&items.has(id))items.get(id).status=result.status;notice(result.confirmed?'Décision synchronisée avec Zulip.':result.message);}catch(e){notice(e.message);}finally{busy=false;await load();}}
$('login-form').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button');button.disabled=true;$('login-error').textContent='';try{await api('login',{method:'POST',body:JSON.stringify({username:$('username').value,password:$('password').value})});$('password').value='';notice('');pushReady=false;await load();}catch(error){$('login-error').textContent=error.message;}finally{button.disabled=false;}};
$('logout').onclick=async()=>{try{await api('logout',{method:'POST'});showLogin();}catch(e){notice(e.message);}};$('more').onclick=()=>load(true);window.addEventListener('online',()=>load());window.addEventListener('offline',()=>{online=false;$('sync').textContent='Hors connexion';notice('Reconnecte-toi à Internet pour prendre une décision.');render();});document.addEventListener('visibilitychange',()=>{if(!document.hidden)load();});setInterval(()=>{if(!document.hidden&&!$('workspace').hidden)load();},5000);
if('serviceWorker' in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});load();

let pushReady=false,pushRegistration=null,pushSubscription=null,pushKey=null;
function pushLabel(text){$('push-status').textContent=text;}
async function initPush(){
 if(pushReady)return;pushReady=true;
 const ios=/iPad|iPhone|iPod/.test(navigator.userAgent)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
 if(ios&&!navigator.standalone&&!matchMedia('(display-mode: standalone)').matches){pushLabel('Sur iPhone : dans Safari, Partager → Sur l’écran d’accueil. Ouvre ensuite l’application depuis son icône.');return;}
 if(!('serviceWorker' in navigator)||!('PushManager' in window)||!('Notification' in window)){pushLabel('Les notifications ne sont pas disponibles dans ce navigateur.');return;}
 try{
  pushRegistration=await navigator.serviceWorker.ready;
  pushKey=(await api('push')).public_key;
  pushSubscription=await pushRegistration.pushManager.getSubscription();
  if(pushSubscription?.options.applicationServerKey){const stored=btoa(String.fromCharCode(...new Uint8Array(pushSubscription.options.applicationServerKey))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');if(stored!==pushKey){await pushSubscription.unsubscribe();pushSubscription=null;}}
  if(pushSubscription&&Notification.permission==='granted')await api('push/subscribe',{method:'POST',body:JSON.stringify({subscription:pushSubscription.toJSON()})});
  else pushSubscription=null;
  updatePush();
 }catch{pushReady=false;pushLabel('Notifications momentanément indisponibles.');}
}
function updatePush(){const blocked=Notification.permission==='denied';$('push-toggle').disabled=blocked;$('push-toggle').textContent=pushSubscription?'Désactiver les notifications':'Activer les notifications';$('push-test').hidden=!pushSubscription;pushLabel(blocked?'Notifications bloquées. Autorise-les dans les réglages de cet appareil.':pushSubscription?'Activées sur cet appareil.':'Une alerte pour chaque nouvelle demande.');}
$('push-toggle').onclick=async()=>{
 $('push-toggle').disabled=true;
 try{
  if(pushSubscription){await api('push/unsubscribe',{method:'POST',body:JSON.stringify({endpoint:pushSubscription.endpoint})});await pushSubscription.unsubscribe();pushSubscription=null;}
  else{
   // Called directly from the user's gesture, as required by iOS.
   const permission=await Notification.requestPermission();
   if(permission!=='granted'){updatePush();return;}
   const raw=atob(pushKey.replace(/-/g,'+').replace(/_/g,'/'));
   pushSubscription=await pushRegistration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:Uint8Array.from(raw,c=>c.charCodeAt(0))});
   try{await api('push/subscribe',{method:'POST',body:JSON.stringify({subscription:pushSubscription.toJSON()})});}
   catch(error){await pushSubscription.unsubscribe();pushSubscription=null;throw error;}
  }
  updatePush();
 }catch(error){$('push-toggle').disabled=false;pushLabel(error.message||'Impossible d’activer les notifications.');}
};
$('push-test').onclick=async()=>{if(!pushSubscription)return;$('push-test').disabled=true;try{const result=await api('push/test',{method:'POST',body:JSON.stringify({endpoint:pushSubscription.endpoint})});pushLabel(result.message);}catch(error){pushLabel(error.message);}finally{$('push-test').disabled=false;}};
