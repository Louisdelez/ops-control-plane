// Google sign-in must start in the system browser, with that browser's cookies.
window.addEventListener('click',event=>{
 if(!event.isTrusted)return;
 const button=event.target.closest?.('button,[role=button]');
 if(!button || !/^(log in with|sign in with|continue with|se connecter avec)\s+google$/i.test(button.textContent.trim()))return;
 if(!window.webkit?.messageHandlers?.opsProviderBrowser)return;
 event.preventDefault();event.stopImmediatePropagation();
 window.webkit.messageHandlers.opsProviderBrowser.postMessage('open');
},true);
