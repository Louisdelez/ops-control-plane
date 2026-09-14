'use strict';
let logData=null,logBusy=false,logArchiveRequest=null;
function logTime(date){const d=new Date(date);d.setMinutes(d.getMinutes()-d.getTimezoneOffset());return d.toISOString().slice(0,16);}
$('#log-until').value=logTime(Date.now());$('#log-since').value=logTime(Date.now()-3600000);
function filteredLogs(){const query=$('#log-search').value.toLocaleLowerCase('fr'),level=$('#log-level').value;return (logData?.records||[]).filter(r=>(level==='all'||r.level===level)&&[r.message,r.service].some(v=>String(v).toLocaleLowerCase('fr').includes(query)));}
function renderLogs(){
 const rows=filteredLogs();$('#log-results').innerHTML=rows.map(r=>`<article class="log-entry"><time>${esc(new Date(r.t*1000).toLocaleString('fr-CH'))}</time><span class="log-level ${esc(r.level)}">${esc({error:'Erreur',warning:'Attention',info:'Information',debug:'Débogage'}[r.level]||r.level)}</span><span class="log-service">${esc(r.service)}<br>${esc(r.source)}</span><pre>${esc(r.message)}</pre></article>`).join('')||'<p>Aucun message correspondant à cette recherche.</p>';
 $('#log-export').disabled=!rows.length;
}
function resetLogs(){if(logBusy)return;logData=null;logArchiveRequest=null;$('#log-next').hidden=true;refreshLogHealth();$('#log-service').innerHTML='<option value="system|">Système · toutes les unités</option>';$('#log-status').textContent='Chargez les services de cette machine ou récupérez son journal système.';renderLogs();}
async function logRequest(action,nextPage=false){
 if(logBusy)return;logBusy=true;
 const controls=['log-host','log-service','log-sources','log-read','log-archive','log-since','log-until','log-next'];controls.forEach(id=>$('#'+id).disabled=true);
 $('#log-status').textContent='Lecture en cours…';
 try{
  const host=$('#log-host').value,[source,service]=$('#log-service').value.split('|');
  let request=action==='sources'?{host,action}:{host,action,source,service,since:Math.floor(new Date($('#log-since').value).getTime()/1000),until:Math.floor(new Date($('#log-until').value).getTime()/1000),limit:300};
  if(nextPage&&logArchiveRequest&&logData?.next_cursor)request={...logArchiveRequest,before:logData.next_cursor};
  const result=await window.__TAURI__.core.invoke('infrastructure_logs',{request});
  if(action==='sources'){
   $('#log-service').innerHTML='<option value="system|">Système · toutes les unités</option>'+(result.sources||[]).map(s=>`<option value="${esc(s.source+'|'+s.service)}">${s.source==='docker'?'Conteneur':'Service'} · ${esc(s.service)}</option>`).join('');
   $('#log-status').textContent=`${(result.sources||[]).length} services et conteneurs disponibles.`;
  }else{
   logData=result;logArchiveRequest=action==='archive'?request:null;$('#log-next').hidden=!result.next_cursor;renderLogs();$('#log-status').textContent=`${names[result.host]?.[0]||result.host} · ${result.records.length} messages · ${action==='archive'?'archive locale':'récupérés et archivés'} · ${result.truncated?(action==='archive'?'D’autres messages sont disponibles avec Messages plus anciens.':'Lecture directe limitée : consultez les archives ou réduisez la période.'):'masquage automatique appliqué'}`;
  }
 }catch(e){$('#log-status').textContent=String(e);}
 finally{logBusy=false;controls.forEach(id=>$('#'+id).disabled=false);}
}
$('#log-next').addEventListener('click',()=>logRequest('archive',true));
$('#log-host').addEventListener('change',resetLogs);
$('#log-sources').addEventListener('click',()=>logRequest('sources'));
$('#log-read').addEventListener('click',()=>logRequest('read'));
$('#log-archive').addEventListener('click',()=>logRequest('archive'));
$('#log-search').addEventListener('input',renderLogs);$('#log-level').addEventListener('change',renderLogs);
$('#log-export').addEventListener('click',async()=>{if(!logData)return;try{const path=await window.__TAURI__.core.invoke('infrastructure_export_logs',{payload:{host:logData.host,records:filteredLogs()}});$('#log-status').textContent='Export enregistré : '+path;}catch(e){$('#log-status').textContent=String(e);}});
document.addEventListener('click',e=>{
 const tab=e.target.closest('[data-infra-view]');if(!tab)return;
 const logs=tab.dataset.infraView==='logs';if(logs)refreshLogHealth();document.body.classList.toggle('logs-view',logs);$('#logs-panel').hidden=!logs;
 document.querySelectorAll('[data-infra-view]').forEach(b=>{b.classList.toggle('active',b===tab);b.setAttribute('aria-pressed',String(b===tab));});
 if(logs&&selected!=='all'&&!logBusy&&$('#log-host').value!==selected){$('#log-host').value=selected;resetLogs();}
});

let logHealthBusy=false;
async function refreshLogHealth(){
 if(logHealthBusy)return;logHealthBusy=true;const host=$('#log-host').value;
 try{
  const r=await window.__TAURI__.core.invoke('infrastructure_logs',{request:{host,action:'health'}});
  if(host!==$('#log-host').value)return;
  const sources=(r.sources||[]).filter(s=>s.source==='system'||s.service);
  const ok=sources.filter(s=>s.status==='collecting').length,late=sources.filter(s=>s.backlog).length;
  $('#log-collection').textContent=`${names[host][0]} · ${r.status==='collecting'?'Collecte active':'Collecte à vérifier'} · ${ok}/${sources.length} sources collectées${late?' · '+late+' en rattrapage':''}${r.gaps?' · '+r.gaps+' interruption(s) d’archive signalée(s)':''} · conservation sans purge automatique`;
 }catch(e){$('#log-collection').textContent='État de la collecte indisponible : '+String(e);}
 finally{logHealthBusy=false;}
}
setInterval(()=>{if(!$('#logs-panel').hidden)refreshLogHealth();},15000);
refreshLogHealth();
