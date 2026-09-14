(()=>{
 window.__opsKeyringEnabled=__OPS_ENABLED__;
 if(window.__opsCaptureInstalled)return;
 window.__opsCaptureInstalled=true;
 const capture=event=>{
  if(!window.__opsKeyringEnabled)return;
  const zulip=location.origin==='https://zulip.example.org' && location.pathname==='/login/';
  const bao=location.origin==='https://127.0.0.1:8200' && /^\/ui\/vault\/auth\/?$/.test(location.pathname);
  const deepseek=location.origin==='https://platform.deepseek.com' && location.pathname==='/sign_in';
  if(!zulip && !bao && !deepseek)return;
  if(deepseek){
   const clicked=event.target.closest?.('[role=button],button');
   if(event.type==='click' && clicked?.textContent.trim()!=='Log in')return;
   if(event.type==='keydown' && (event.key!=='Enter' || !event.target.matches('input')))return;
   const fields=[...document.querySelectorAll('input')].filter(i=>i.getClientRects().length);
   const user=fields.find(i=>i.placeholder==='Phone number / email address');
   const pass=fields.find(i=>i.type==='password' && i.placeholder==='Password');
   if(user?.value && pass?.value && user.value.length<=512 && pass.value.length<=4096)window.webkit.messageHandlers.opsCredentialCapture.postMessage(JSON.stringify({url:location.href,username:user.value,password:pass.value}));
   return;
  }
  if(event.type!=='submit')return;
  const form=event.target;
  if(!(form instanceof HTMLFormElement) || new URL(form.action,location.href).origin!==location.origin)return;
  const username=form.querySelector('input[name="username"]');
  const password=form.querySelector('input[name="password"][type="password"]');
  if(!username?.value || !password?.value || username.value.length>512 || password.value.length>4096)return;
  window.webkit.messageHandlers.opsCredentialCapture.postMessage(JSON.stringify({url:location.href,username:username.value,password:password.value}));
 };
 document.addEventListener('submit',capture,true);
 document.addEventListener('click',capture,true);
 document.addEventListener('keydown',capture,true);
})();
