// Share credential validation with Atlas without starting the legacy installer.
// app.js detects an init export; this native host deliberately exposes only the
// credential validator from onboarding.js.
window.AtlasOnboarding = Object.freeze({
  validateProviderCredential: window.AtlasOnboarding.validateProviderCredential,
});

// Native deployment replaces the old bootstrap, without fabricating its status.
document.addEventListener('DOMContentLoaded',async()=>{
  const node=document.getElementById('legacy-install-status');
  try{
    const result=await window.__TAURI__.core.invoke('get_bootstrap_status');
    node.textContent=JSON.stringify(result,null,2);
  }catch(error){node.textContent=error.message||String(error);}
});

// Adapt the installed native API without claiming catalogue/runtime parity.
document.addEventListener('DOMContentLoaded',()=>{
  const nativeCall=callNative;
  callNative=async function(command,args){
    const result=await nativeCall(command,args);
    if(command==='get_runtime_snapshot' && result.native_service){
      const native=result.native_service;
      state.nativeConfigurationDeferred=native.health.mode==='deterministic-and-human-only';
      result.health={available:true,data:native.health,error_code:null};
      result.budgets={available:!!native.budgets,data:native.budgets,error_code:null};
      result.performance.error_code='native_endpoint_unavailable';
      result.provider_integrations.error_code='native_endpoint_unavailable';
    }
    if(command==='get_runtime_snapshot'){
      const list=document.getElementById('native-budget-list');
      list.replaceChildren();
      let facade;
      try{facade=await window.__TAURI__.core.invoke('get_native_budget')}catch{facade=null}
      const providers=[...(result.budgets?.data?.providers||[])];
      if(facade?.available){
        const known=new Set(providers.map(p=>p.provider));
        providers.push(...facade.providers.filter(p=>!known.has(p.provider)));
      }else{
        const notice=document.createElement('p');
        notice.textContent=facade?.reason==='not_initialized'?'Dépenses API : le registre sera créé à l’activation des fournisseurs.':'Dépenses API : lecture du registre indisponible.';
        list.append(notice);
      }
      for(const provider of providers){
        const row=document.createElement('p');
        const usd=value=>new Intl.NumberFormat('fr-FR',{style:'currency',currency:'USD',maximumFractionDigits:4}).format(value/1000000);
        row.textContent=provider.provider+'\nCe mois : '+provider.usage.monthly.calls+' appels · '+provider.usage.monthly.tokens+' tokens · '+usd(provider.usage.monthly.cost)+'\nLimite mensuelle : '+usd(provider.limits.monthly.cost_microusd);
        list.append(row);
      }
    }
    return result;
  };
});

// Presentation layer: retain Atlas calculations, filters and delegated actions.
document.addEventListener('DOMContentLoaded',()=>{
  const set=(selector,text)=>{const node=document.querySelector(selector);if(node)node.textContent=text;};
  set('.hero h1','Catalogue de modèles');
  set('.hero-copy','Compare les modèles API et trouve le bon équilibre entre capacités et coût.');
  set('#comparison-title','Scénario de comparaison');
  set('#catalogue-results-title','Modèles');
  set('#panel-routing .page-heading h1','Choisir un modèle');
  set('#panel-costs .page-heading h1','Comparer les coûts');
  set('#panel-settings .page-heading h1','Fournisseurs et accès');
  set('#panel-settings .page-heading > p:last-child','Gère tes accès API et consulte les budgets de tes services. Tu peux ajouter les clés plus tard.');
  set('[data-quick-filter="credential_resolved"]','Clé détectée');
  set('.notice--secure strong','Clés protégées dans OpenBao');
  set('.notice--secure p','Tes clés sont transmises au coffre et retirées du formulaire après l’envoi.');
  set('#tab-settings > span:last-child','Accès et réglages');
  set('#tab-routing > span:last-child','Sélection');
  const nav=document.querySelector('.tab-bar');
  const heading=document.createElement('div');heading.className='nav-heading';heading.innerHTML='<span class="nav-mark" aria-hidden="true"><img src="../assets/icons/compass.svg" alt=""></span> Atlas';
  const label=document.createElement('div');label.className='nav-label';label.textContent='GESTION DES MODÈLES';
  nav.prepend(label);nav.prepend(heading);
  const footer=document.createElement('div');footer.className='nav-footer';footer.innerHTML='<img class="ops-illustration" src="../assets/ops-connected-arches.png" alt="" width="160" height="120"><strong>À ton rythme</strong><p>Explore le catalogue avant d’ajouter tes clés API.</p><button type="button">Gérer les accès →</button>';
  footer.querySelector('button').addEventListener('click',()=>document.querySelector('#tab-settings').click());nav.append(footer);
  document.querySelector('.app-shell').prepend(nav);
  for(const [id,name] of [['tab-catalogue','layout-grid'],['tab-routing','route'],['tab-costs','chart-no-axes-combined'],['tab-settings','sliders-horizontal']]){
    const old=document.querySelector('#'+id+' .tab-icon');
    const img=document.createElement('img');img.className='nav-icon';img.src='../assets/icons/'+name+'.svg';img.alt='';img.width=20;img.height=20;old.replaceWith(img);
  }
  const installIcon=(selector,name)=>{
    const node=document.querySelector(selector);if(node){node.replaceChildren();const img=document.createElement('img');img.src='../assets/icons/'+name+'.svg';img.alt='';img.width=18;img.height=18;node.append(img);node.classList.add('svg-icon-container');}
  };
  installIcon('.scenario-icon','chart-no-axes-combined');
  installIcon('.search-icon','search');
  installIcon('.shield-icon','shield-check');
  installIcon('#close-runtime-popover','x');
  // Atlas regenerates dialogs and provider cards; decorate the resulting controls.
  const decorate=()=>{
    for(const [selector,name] of [['[data-close-dialog]','x'],['.credential-dialog-close','x'],['.credential-provider-mark','building-2']]){
      for(const node of document.querySelectorAll(selector)){
        if(node.dataset.svgReady || (selector==='[data-close-dialog]'&&!node.classList.contains('icon-button')))continue;
        node.dataset.svgReady='true';node.replaceChildren();const img=document.createElement('img');img.src='../assets/icons/'+name+'.svg';img.alt='';img.width=18;img.height=18;node.append(img);
      }
    }
  };
  new MutationObserver(decorate).observe(document.querySelector('.app-shell').parentElement,{childList:true,subtree:true});
  decorate();
  // Move historical installer details out of the primary access flow.
  const history=document.querySelector('#legacy-install-status')?.closest('details');
  if(history){history.classList.add('legacy-section');document.querySelector('#panel-settings').append(history);}
  const native=document.querySelector('#native-budget-list')?.closest('aside');
  if(native){native.classList.add('native-status-section');document.querySelector('#panel-settings').append(native);}
  const previousRuntimeStatus=renderRuntimeStatus;
  renderRuntimeStatus=function(){previousRuntimeStatus();if(state.nativeConfigurationDeferred)setRuntimeStatus('warning','API à configurer','Le service local répond. Les appels API ne sont pas activés. Les performances et les soldes détaillés ne sont pas disponibles sur ce service.');};
  modelCardMarkup=function(model){
    const account=accountForModel(model);
    const estimate=estimateForModel(model);
    const status=statusPresentation(model);
    const isReady=runtimeAvailabilityForModel(model)===true;
    const isReview=model.catalogueStatus==='review'||model.catalogueStatus==='retired';
    const statusText=isReady?'Disponible':model.catalogueStatus==='retired'?'Retiré':isReview?'À vérifier':model.deploymentDeclared?'Déploiement déclaré':'Au catalogue';
    const prefix=model.priceVerified===false?'≈ ':'';
    const badges=model.specialties.slice(0,2).map(x=>'<li>'+escapeHTML(x)+'</li>').join('');
    return `<article class="model-card" data-model-id="${escapeAttribute(model.id)}">
      <div class="model-card__body">
        <div class="model-card__identity"><span class="provider-avatar" aria-hidden="true"><img src="../assets/icons/building-2.svg" alt="" width="20" height="20"></span><div class="model-card__title"><h3>${escapeHTML(model.name)}</h3><div class="provider-name">${escapeHTML(model.providerName)}</div></div></div>
        <p class="model-card__description">${escapeHTML(model.description)}</p>
        <ul class="badge-list" aria-label="Spécialités">${badges}</ul>
        <div class="card-status ${isReady?'is-ready':isReview?'is-review':''}" title="${escapeAttribute(status.label||statusText)}">${statusText}</div>
        <div class="card-price"><div><small>Estimation / mois · scénario commun</small><strong>${estimate===null?'À vérifier':prefix+formatCurrency(estimate)}</strong></div><div class="price-input"><small>Entrée / 1 M tokens</small>${model.pricing.inputPerMillion===null?'Non renseigné':prefix+formatCurrency(model.pricing.inputPerMillion)}</div></div>
      </div>
      <div class="model-card__actions"><button class="card-button" type="button" data-model-details="${escapeAttribute(model.id)}" aria-label="Voir la fiche complète de ${escapeAttribute(model.name)}">Voir les détails <img class="inline-icon" src="../assets/icons/arrow-up-right.svg" alt="" width="14" height="14"></button>${credentialButtonMarkup(model,account,'card-button')}</div>
    </article>`;
  };
});
