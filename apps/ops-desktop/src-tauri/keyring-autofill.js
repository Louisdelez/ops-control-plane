(()=>{
  const c=__OPS_CREDENTIAL__;
  const isLogin=()=>location.origin===c.origin && (c.service==='deepseek'?location.pathname==='/sign_in':c.service==='zulip'?location.pathname==='/login/':/^\/ui\/vault\/auth\/?$/.test(location.pathname));
  if(!isLogin() || window.__opsKeyringFilling)return;
  const key='ops-keyring-last-attempt';
  if(Date.now()-Number(sessionStorage.getItem(key)||0)<60000)return;
  window.__opsKeyringFilling=true;
  let count=0;
  const clear=()=>{c.password='';c.username='';window.__opsKeyringFilling=false;};
  const timer=setInterval(()=>{
    if(window.__opsKeyringEnabled===false || !isLogin() || ++count>100){clearInterval(timer);clear();return;}
    if(c.service==='openbao'){
      const method=[...document.querySelectorAll('select')].find(x=>[...x.options].some(o=>o.value==='userpass'));
      if(method && method.value!=='userpass'){method.value='userpass';method.dispatchEvent(new Event('change',{bubbles:true}));return;}
    }
    const user=document.querySelector(c.service==='deepseek'?'input[placeholder="Phone number / email address"]':'input[name="username"]');
    const password=document.querySelector(c.service==='deepseek'?'input[type="password"][placeholder="Password"]':'input[name="password"][type="password"]');
    if(!user || !password)return;
    const form=password.form;
    if(c.service!=='deepseek' && (!form || user.form!==form))return;
    if(form && new URL(form.action,location.href).origin!==c.origin){clearInterval(timer);clear();return;}
    // Never replace a login that the user has started typing.
    if((user.value && user.value!==c.username) || password.value){clearInterval(timer);clear();return;}
    const setter=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
    for(const [field,value] of [[user,c.username],[password,c.password]]){setter.call(field,value);field.dispatchEvent(new Event('input',{bubbles:true}));field.dispatchEvent(new Event('change',{bubbles:true}));}
    sessionStorage.setItem(key,String(Date.now()));clearInterval(timer);clear();
    if(c.service==='deepseek'){[...document.querySelectorAll('[role=button],button')].find(b=>b.textContent.trim()==='Log in')?.click();}else{form.requestSubmit();}
  },200);
})();
