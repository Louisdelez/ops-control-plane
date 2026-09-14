'use strict';
const invoke=(command,args={})=>window.__TAURI__.core.invoke(command,args);
const $=id=>document.getElementById(id),sessions=new Map();let active=null,busy=false;
const error=e=>{$('error').textContent=String(e)};
function menu(open){$('launcher').hidden=!open;$('add').setAttribute('aria-expanded',String(open));if(open)$('launcher').querySelector('button').focus()}
$('add').onclick=()=>menu($('launcher').hidden);
document.addEventListener('click',e=>{if(!e.target.closest('.new-terminal'))menu(false)});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('launcher').hidden){menu(false);$('add').focus()}});
$('launcher').onkeydown=e=>{const buttons=[...$('launcher').querySelectorAll('button')];if(['ArrowDown','ArrowUp'].includes(e.key)){e.preventDefault();buttons[(buttons.indexOf(document.activeElement)+(e.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length].focus()}};
function update(){const n=sessions.size;$('count').textContent=n+' / 4';$('empty').hidden=n>0;$('add').disabled=busy||n>=4;$('session-status').textContent=n?n+' terminal'+(n>1?'s':'')+' ouvert'+(n>1?'s':''):'Prêt'}
function select(id,focus=true){active=id;for(const [key,s]of sessions)s.pane.classList.toggle('active',key===id);if(focus)sessions.get(id)?.term.focus()}
function resize(s){if(!s.id||s.closing)return;s.fit.fit();if(!s.finished)invoke('terminal_resize',{id:s.id,cols:Math.max(20,Math.min(500,s.term.cols)),rows:Math.max(5,Math.min(200,s.term.rows))}).catch(error)}
let resizeFrame;function layout(){cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(()=>{for(const s of sessions.values())resize(s)})}
async function closeSession(s){if(s.closing)return;s.closing=true;s.close.disabled=true;try{await invoke('terminal_close',{id:s.id});s.term.dispose();s.pane.remove();sessions.delete(s.id);if(active===s.id)select([...sessions.keys()].at(-1)||null);update();layout()}catch(e){s.closing=false;s.close.disabled=false;error(e);poll(s)}}
async function poll(s){if(s.closing)return;try{const result=await invoke('terminal_poll',{id:s.id});if(s.closing)return;if(result.data.length)await new Promise(resolve=>s.term.write(new Uint8Array(result.data),resolve));if(result.exited&&!result.data.length){s.finished=true;s.status.textContent='Terminé';return}setTimeout(()=>poll(s),60)}catch(e){if(!s.closing){s.status.textContent='Erreur';error(e)}}}
async function launch(tool,action='start'){
 if(busy||sessions.size>=4)return;busy=true;menu(false);update();$('error').textContent='';let s;
 try{
 const pane=document.createElement('section');pane.className='terminal-pane';pane.setAttribute('aria-label',(tool==='codex'?'Codex':'Claude Code')+' — terminal');
 const header=document.createElement('div');header.className='pane-header';const title=document.createElement('span');title.className='pane-title';title.textContent=tool==='codex'?'Codex':'Claude Code';const status=document.createElement('span');status.className='pane-status';status.textContent='Démarrage…';const close=document.createElement('button');close.className='pane-close';close.textContent='×';close.setAttribute('aria-label','Fermer le terminal '+title.textContent);close.title='Fermer ce terminal';header.append(title,status,close);const panel=document.createElement('div');panel.className='terminal-panel';pane.append(header,panel);$('terminals').append(pane);$('empty').hidden=true;
 const term=new Terminal({cursorBlink:true,fontSize:13,fontFamily:'monospace',scrollback:3000,allowProposedApi:false,theme:{background:'#111a17',foreground:'#dce5e1',cursor:'#b2dab8',selectionBackground:'#405b4b'}});const fit=new FitAddon.FitAddon();s={term,fit,pane,panel,status,close,finished:false,closing:false};term.parser.registerOscHandler(52,()=>true);term.loadAddon(fit);term.open(panel);fit.fit();
 s.id=await invoke('terminal_start',{tool,action,cols:Math.max(20,Math.min(500,term.cols)),rows:Math.max(5,Math.min(200,term.rows))});sessions.set(s.id,s);status.textContent='';close.onclick=()=>closeSession(s);pane.onpointerdown=()=>select(s.id,false);header.onclick=e=>{if(e.target!==close)select(s.id)};
 let writes=Promise.resolve();term.onData(data=>{if(!s.finished&&!s.closing)writes=writes.then(()=>invoke('terminal_write',{id:s.id,data})).catch(error)});term.attachCustomKeyEventHandler(e=>{if(e.type==='keydown'&&e.ctrlKey&&e.shiftKey&&e.code==='KeyC'&&term.hasSelection()){document.execCommand('copy');return false}return true});
 select(s.id);layout();pane.scrollIntoView({block:'nearest',inline:'nearest'});poll(s);
 }catch(e){if(s){if(s.id)await invoke('terminal_close',{id:s.id}).catch(()=>{});sessions.delete(s.id);s.term.dispose();s.pane.remove()}error(e)}finally{busy=false;update();layout()}
}
for(const button of $('launcher').querySelectorAll('[data-tool]'))button.onclick=()=>launch(button.dataset.tool);
new ResizeObserver(layout).observe($('terminals'));
document.addEventListener('copy',e=>{const s=sessions.get(active);if(s?.term.hasSelection()){e.clipboardData.setData('text/plain',s.term.getSelection());e.preventDefault()}});
update();
// Capabilities are reconciled without exposing a manual execution-mode selector.
invoke('execution_status').catch(error);

setInterval(()=>invoke('execution_status').catch(error),60000);
