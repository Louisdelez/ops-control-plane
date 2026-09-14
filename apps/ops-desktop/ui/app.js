const tabs=[...document.querySelectorAll('[role=tab]')];
let switching=false;
for(const tab of tabs)tab.tabIndex=tab.getAttribute('aria-selected')==='true'?0:-1;
async function select(button){
  if(switching)return;
  switching=true;
  const error=document.querySelector('#error');error.textContent='';
  try{
    await window.__TAURI__.core.invoke('open_service',{service:button.dataset.service});
    document.querySelector('#settings').setAttribute('aria-pressed','false');
    document.querySelector('#reload').hidden=!['zulip','approvals','openbao','hermes','deepseek','jina'].includes(button.dataset.service);
    document.querySelector('#browser').hidden=!['deepseek','jina'].includes(button.dataset.service);
    button.scrollIntoView({block:'nearest',inline:'nearest'});
    for(const tab of tabs){const active=tab===button;tab.setAttribute('aria-selected',String(active));tab.tabIndex=active?0:-1;}
  }catch(e){error.textContent='Ouverture impossible : '+String(e);}
  finally{switching=false;}
}
for(const button of tabs){
  button.addEventListener('click',()=>select(button));
  button.addEventListener('keydown',event=>{
    const visible=tabs.filter(t=>!t.hidden);let index=visible.indexOf(button);
    if(event.key==='ArrowRight')index=(index+1)%visible.length;
    else if(event.key==='ArrowLeft')index=(index+visible.length-1)%visible.length;
    else if(event.key==='Home')index=0;
    else if(event.key==='End')index=visible.length-1;
    else return;
    event.preventDefault();visible[index].focus();select(visible[index]);
  });
}

document.querySelector('#reload').addEventListener('click',async()=>{
  const button=document.querySelector('#reload');
  const service=tabs.find(tab=>tab.getAttribute('aria-selected')==='true')?.dataset.service;
  button.disabled=true;
  try {await window.__TAURI__.core.invoke('reload_service',{service});}
  catch(e){document.querySelector('#error').textContent='Rechargement impossible : '+String(e);}
  finally{button.disabled=false;}
});

document.querySelector('#settings').addEventListener('click',async()=>{
 const button=document.querySelector('#settings');button.disabled=true;
 try{await window.__TAURI__.core.invoke('open_settings');button.setAttribute('aria-pressed','true');document.querySelector('#reload').hidden=true;document.querySelector('#browser').hidden=true;for(const tab of tabs){tab.setAttribute('aria-selected','false');tab.tabIndex=0;}}
 catch(e){document.querySelector('#error').textContent=String(e);}
 finally{button.disabled=false;}
});

window.opsExternalAuthNotice=()=>{document.querySelector('#error').textContent='Connexion à poursuivre dans le navigateur.';};
document.querySelector('#browser').addEventListener('click',async()=>{
 const service=tabs.find(t=>t.getAttribute('aria-selected')==='true')?.dataset.service;
 try{await window.__TAURI__.core.invoke('provider_browser',{service});}catch(e){document.querySelector('#error').textContent=String(e);}
});
async function refreshProviders(){
 try{const state=await window.__TAURI__.core.invoke('provider_tabs');for(const tab of tabs.filter(t=>t.classList.contains('provider-tab'))){const hide=!state.visible.includes(tab.dataset.service);if(hide && tab.getAttribute('aria-selected')==='true')await select(tabs[0]);tab.hidden=hide;}}
 catch(e){document.querySelector('#error').textContent='Onglets fournisseurs indisponibles.';}
}
refreshProviders();setInterval(refreshProviders,3000);
