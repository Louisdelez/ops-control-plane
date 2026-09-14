//! Credentials stay in the desktop Secret Service; no password IPC read command.
use gtk::prelude::*;
use webkit2gtk::{WebViewExt,UserContentManagerExt};
use std::{collections::HashMap,path::PathBuf,sync::{Mutex,OnceLock,atomic::{AtomicBool,Ordering}},time::Instant};
use serde::{Deserialize, Serialize};
use std::{io::Write, process::{Command,Stdio}};
use zeroize::{Zeroize,Zeroizing};
use tauri::Manager;

static LAST_SAVE_FAILED:AtomicBool=AtomicBool::new(false);
static ENABLED:AtomicBool=AtomicBool::new(true);
static SETTINGS:OnceLock<PathBuf>=OnceLock::new();
static PENDING:OnceLock<Mutex<HashMap<&'static str,(Instant,Credential)>>>=OnceLock::new();
fn pending()-> &'static Mutex<HashMap<&'static str,(Instant,Credential)>>{PENDING.get_or_init(||Mutex::new(HashMap::new()))}
pub fn initialize(path:PathBuf){
 let enabled=match std::fs::read(&path){Ok(raw)=>serde_json::from_slice::<serde_json::Value>(&raw).ok().and_then(|v|v["enabled"].as_bool()).unwrap_or(false),Err(e) if e.kind()==std::io::ErrorKind::NotFound=>true,Err(_)=>false};
 ENABLED.store(enabled,Ordering::SeqCst);let _=SETTINGS.set(path);
}
fn set_enabled(value:bool)->bool{
 use std::os::unix::fs::OpenOptionsExt;
 let Some(path)=SETTINGS.get() else{return false};
 let Some(parent)=path.parent() else{return false};if std::fs::create_dir_all(parent).is_err(){return false;}
 let next=path.with_extension("next");
 let write=(||->std::io::Result<()>{let mut f=std::fs::OpenOptions::new().create(true).truncate(true).write(true).mode(0o600).open(&next)?;f.write_all(if value{b"{\"enabled\":true}"}else{b"{\"enabled\":false}"})?;f.sync_all()?;std::fs::rename(&next,path)})();
 if write.is_err(){return false;}
 ENABLED.store(value,Ordering::SeqCst);if !value{pending().lock().unwrap().clear();}true
}
#[derive(Clone,Copy)]
pub struct Service { pub id: &'static str, pub kind: &'static str, pub origin: &'static str, pub title: &'static str }
pub fn service(id:&str)->Option<Service>{match id {
 "zulip"=>Some(Service{id:"zulip",kind:"zulip",origin:"https://zulip.example.org",title:"Zulip"}),
 "deepseek"=>Some(Service{id:"deepseek",kind:"deepseek",origin:"https://platform.deepseek.com",title:"DeepSeek"}),
 "openbao"=>Some(Service{id:"openbao",kind:"openbao",origin:"https://127.0.0.1:8200",title:"OpenBao"}),_=>None}}
fn login_url(s:Service,url:&str)->bool {
 let Ok(u)=tauri::Url::parse(url) else{return false};
 u.origin().ascii_serialization()==s.origin && u.username().is_empty() && u.password().is_none()
 && match s.kind {"deepseek"=>u.path()=="/sign_in", "zulip"=>u.path()=="/login/", "openbao"=>u.path()=="/ui/vault/auth" || u.path()=="/ui/vault/auth/",_=>false}
}
#[derive(Clone,Serialize,Deserialize)]
struct Credential { username:String,password:String,automatic:bool }
impl Drop for Credential {fn drop(&mut self){self.username.zeroize();self.password.zeroize();}}
fn command(s:Service,operation:&str)->Command{
 let mut c=Command::new("/usr/bin/secret-tool");c.arg(operation);
 if operation=="store"{c.arg("--label").arg(format!("Ops — {}",s.title));}
 c.args(["application","ch.ops-user.ops-desktop","service",s.id,"origin",s.origin]);
 c.stderr(Stdio::null());c
}
fn load(s:Service)->Option<Credential>{
 let mut out=command(s,"lookup").stdin(Stdio::null()).output().ok()?;
 let value=if out.status.success(){serde_json::from_slice::<Credential>(&out.stdout).ok()}else{None};out.stdout.zeroize();
 value.filter(|c|!c.username.is_empty() && c.username.len()<=512 && !c.password.is_empty() && c.password.len()<=4096)
}
fn save(s:Service,c:&Credential)->bool{
 let Ok(raw)=serde_json::to_vec(c) else{return false};let raw=Zeroizing::new(raw);
 let Ok(mut child)=command(s,"store").stdin(Stdio::piped()).stdout(Stdio::null()).spawn() else{return false};
 let written=child.stdin.take().is_some_and(|mut input|input.write_all(&raw).is_ok());
 child.wait().is_ok_and(|status|status.success()) && written
}
fn forget(s:Service)->bool{pending().lock().unwrap().remove(s.id);command(s,"clear").stdin(Stdio::null()).stdout(Stdio::null()).status().is_ok_and(|s|s.success())}
fn script(s:Service,c:&Credential)->Zeroizing<String>{
 let config=serde_json::json!({"origin":s.origin,"service":s.kind,"username":c.username,"password":c.password});
 Zeroizing::new(include_str!("../keyring-autofill.js").replace("__OPS_CREDENTIAL__",&config.to_string()))
}
fn autofill(view:&webkit2gtk::WebView,s:Service){
 if !ENABLED.load(Ordering::SeqCst) || !view.uri().is_some_and(|u|login_url(s,&u)){return;}
 let weak=view.downgrade();
 let (tx,rx)=gtk::glib::MainContext::channel::<Option<Credential>>(gtk::glib::Priority::DEFAULT);
 rx.attach(None,move |credential|{
  if let (Some(view),Some(c))=(weak.upgrade(),credential){
   if ENABLED.load(Ordering::SeqCst) && c.automatic && view.uri().is_some_and(|u|login_url(s,&u)){
    let js=script(s,&c);view.run_javascript(&js,None::<&gio::Cancellable>, |_|{});
   }
  }
  gtk::glib::ControlFlow::Break
 });
 std::thread::spawn(move ||{let _=tx.send(load(s));});
}
#[derive(Deserialize)]
struct Capture {url:String,username:String,password:String}
impl Drop for Capture{fn drop(&mut self){self.password.zeroize();self.username.zeroize();}}
fn captured(s:Service,native_url:&str,raw:&str)->Option<Credential>{
 if raw.len()>16384 || !login_url(s,native_url){return None;}
 let c:Capture=serde_json::from_str(raw).ok()?;
 if c.url!=native_url || c.username.is_empty() || c.username.len()>512 || c.password.is_empty() || c.password.len()>4096{return None;}
 Some(Credential{username:c.username.clone(),password:c.password.clone(),automatic:true})
}
fn signed_in_url(s:Service,url:&str)->bool{
 let Ok(u)=tauri::Url::parse(url) else{return false};
 if u.origin().ascii_serialization()!=s.origin || !u.username().is_empty() || u.password().is_some(){return false;}
 if s.kind=="deepseek"{matches!(u.path(),"/balance"|"/usage"|"/api_keys")}else if s.kind=="zulip"{u.path()=="/"}else{["/ui/vault/secrets","/ui/vault/access","/ui/vault/policies","/ui/vault/tools"].iter().any(|p|u.path()==*p || u.path().starts_with(&format!("{p}/")))}
}
fn page_changed(v:&webkit2gtk::WebView,s:Service,finished:bool){
 let Some(url)=v.uri() else{return};
 let enabled=ENABLED.load(Ordering::SeqCst);
 if login_url(s,&url){
  let script=include_str!("../keyring-capture.js").replace("__OPS_ENABLED__",if enabled{"true"}else{"false"});
  v.run_javascript(&script,None::<&gio::Cancellable>,|_|{});autofill(v,s);
 }else if enabled && signed_in_url(s,&url) && (matches!(s.kind,"openbao"|"deepseek") || finished){
  if let Some((time,c))=pending().lock().unwrap().remove(s.id){
   if time.elapsed().as_secs()<180{std::thread::spawn(move||{if ENABLED.load(Ordering::SeqCst){let mut c=c;if let Some(old)=load(s){c.automatic=old.automatic;}LAST_SAVE_FAILED.store(!save(s,&c),Ordering::SeqCst);}});}
  }
 }
}
pub fn attach(view:&webkit2gtk::WebView,id:&str){
 let Some(s)=service(id) else{return};attach_service(view,s);
}
fn attach_service(view:&webkit2gtk::WebView,s:Service){
 if let Some(manager)=view.user_content_manager(){
  let weak=view.downgrade();
  manager.connect_script_message_received(Some("opsCredentialCapture"),move|_,message|{
   if !ENABLED.load(Ordering::SeqCst){return;}
   let (Some(v),Some(value))=(weak.upgrade(),message.js_value()) else{return};
   let Some(url)=v.uri() else{return};let raw=Zeroizing::new(value.to_string());
   if let Some(c)=captured(s,&url,&raw){
    let captured_at=Instant::now();pending().lock().unwrap().insert(s.id,(captured_at,c));
    gtk::glib::timeout_add_seconds_local_once(180,move||{let mut p=pending().lock().unwrap();if p.get(s.id).is_some_and(|(when,_)|*when==captured_at){p.remove(s.id);}});
   }
  });
  manager.register_script_message_handler("opsCredentialCapture");
 }
 view.connect_load_changed(move |v,event|{if event==webkit2gtk::LoadEvent::Finished{page_changed(v,s,true);}});
 view.connect_notify_local(Some("uri"),move |v,_|page_changed(v,s,false));
}
pub fn settings_state()->serde_json::Value {
 serde_json::json!({"enabled":ENABLED.load(Ordering::SeqCst),"save_failed":LAST_SAVE_FAILED.load(Ordering::SeqCst),"version":env!("CARGO_PKG_VERSION")})
}
pub fn update_enabled(app:&tauri::AppHandle,enabled:bool)->Result<(),String>{
 if !set_enabled(enabled){return Err("Impossible d’enregistrer ce réglage.".into());}
 for id in ["zulip","openbao","deepseek"]{if let Some(v)=app.get_webview(id){let _=v.eval(if enabled{"window.__opsKeyringEnabled=true"}else{"window.__opsKeyringEnabled=false"});}}
 Ok(())
}
pub async fn manage(app:tauri::AppHandle,id:String)->Result<(),String>{
 let s=service(&id).ok_or("Trousseau disponible pour Zulip, OpenBao et DeepSeek")?;
 let existing=tauri::async_runtime::spawn_blocking(move||load(s)).await.map_err(|_|"Trousseau indisponible")?;
 let app2=app.clone();
 app.run_on_main_thread(move||{
  let dialog=gtk::Dialog::with_buttons(Some(&format!("Trousseau — {}",s.title)),None::<&gtk::Window>,gtk::DialogFlags::MODAL,
   &[("Annuler",gtk::ResponseType::Cancel),("Oublier cet accès",gtk::ResponseType::Reject),("Enregistrer",gtk::ResponseType::Accept)]);
  if let Some(window)=app2.get_window("main"){if let Ok(parent)=window.gtk_window(){dialog.set_transient_for(Some(&parent));}}
  dialog.set_default_size(460,280);
  let area=dialog.content_area();area.set_spacing(12);area.set_margin_start(20);area.set_margin_end(20);area.set_margin_top(16);area.set_margin_bottom(16);
  let info=gtk::Label::new(Some("Identifiants conservés dans le trousseau sécurisé de Fedora."));info.set_line_wrap(true);area.add(&info);
  let user=gtk::Entry::new();user.set_placeholder_text(Some(if s.kind=="openbao"{"Nom d’utilisateur (userpass)"}else{"Adresse e-mail ou identifiant"}));user.set_max_length(512);area.add(&user);
  let password=gtk::Entry::new();password.set_visibility(false);password.set_input_purpose(gtk::InputPurpose::Password);password.set_placeholder_text(Some("Mot de passe"));password.set_max_length(4096);area.add(&password);
  let automatic=gtk::CheckButton::with_label("Se reconnecter automatiquement après une déconnexion");automatic.set_active(true);area.add(&automatic);
  let status=gtk::Label::new(None);status.set_line_wrap(true);area.add(&status);
  if let Some(c)=existing {user.set_text(&c.username);password.set_text(&c.password);automatic.set_active(c.automatic);status.set_text("Accès enregistré. Tu peux le modifier ou l’oublier.");}
  dialog.connect_response(move|d,response|{
   if response!=gtk::ResponseType::Accept && response!=gtk::ResponseType::Reject{password.set_text("");d.close();return;}
   let credential=Credential{username:user.text().trim().to_owned(),password:password.text().to_string(),automatic:automatic.is_active()};
   if response==gtk::ResponseType::Accept && (credential.username.is_empty() || credential.password.is_empty()){status.set_text("Renseigne l’identifiant et le mot de passe.");return;}
   d.set_response_sensitive(gtk::ResponseType::Accept,false);d.set_response_sensitive(gtk::ResponseType::Reject,false);d.set_response_sensitive(gtk::ResponseType::Cancel,false);
   status.set_text("Accès au trousseau…");let (tx,rx)=gtk::glib::MainContext::channel::<bool>(gtk::glib::Priority::DEFAULT);
   let d=d.clone();let status=status.clone();let password=password.clone();let app=app2.clone();let auto=credential.automatic;
   rx.attach(None,move|ok|{
    if ok{password.set_text("");d.close();if response==gtk::ResponseType::Accept && auto {if let Some(v)=app.get_webview(s.id){let _=v.navigate(format!("{}/{}",s.origin,match s.kind{"zulip"=>"login/","deepseek"=>"sign_in",_=>"ui/vault/auth?with=userpass"}).parse().unwrap());}}}
    else{status.set_text("Le trousseau n’a pas enregistré la modification. Déverrouille-le et réessaie.");for r in [gtk::ResponseType::Accept,gtk::ResponseType::Reject,gtk::ResponseType::Cancel]{d.set_response_sensitive(r,true);}}
    gtk::glib::ControlFlow::Break
   });
   std::thread::spawn(move||{let _=tx.send(if response==gtk::ResponseType::Reject{forget(s)}else{save(s,&credential)});});
  });dialog.show_all();
 }).map_err(|_|"Impossible d’ouvrir le trousseau".into())
}
#[cfg(test)]mod tests{use super::*;
 #[test]fn only_exact_login_origins(){let s=service("zulip").unwrap();assert!(login_url(s,"https://zulip.example.org/login/?next=/"));for u in ["http://zulip.example.org/login/","https://zulip.example.org.evil/login/","https://zulip.example.org:8443/login/","https://user@zulip.example.org/login/","https://zulip.example.org/"]{assert!(!login_url(s,u));}let b=service("openbao").unwrap();assert!(login_url(b,"https://127.0.0.1:8200/ui/vault/auth?with=userpass"));assert!(!login_url(b,"https://127.0.0.1:8200/ui/vault/secrets"));assert!(service("terminals").is_none());let d=service("deepseek").unwrap();assert!(login_url(d,"https://platform.deepseek.com/sign_in"));assert!(!login_url(d,"https://accounts.google.com/sign_in"));assert!(!signed_in_url(d,"https://platform.deepseek.com/sign_in"));assert!(signed_in_url(d,"https://platform.deepseek.com/balance"));}
}

#[cfg(test)]mod integration_tests {
 use super::*;
 #[test] #[ignore = "Creates and removes an isolated Secret Service item"]
 fn secret_service_round_trip(){
  let s=Service{id:"selftest-20260913",kind:"zulip",origin:"https://ops-selftest.invalid",title:"Test temporaire"};
  assert!(load(s).is_none(),"Test slot must be empty");
  let c=Credential{username:"test-user".into(),password:format!("temporary-{:?}",std::time::SystemTime::now()),automatic:false};
  assert!(save(s,&c),"Secret Service store failed");
  let result=load(s);
  let deleted=forget(s);
  assert!(deleted,"Temporary item cleanup failed");
  assert!(result.is_some_and(|r|r.username==c.username && r.password==c.password && !r.automatic));
  assert!(load(s).is_none());
 }
}

#[cfg(test)]mod capture_tests{use super::*;
 #[test]fn capture_requires_matching_native_login(){
  let s=service("zulip").unwrap();let url="https://zulip.example.org/login/";
  let raw=serde_json::json!({"url":url,"username":"test@example.invalid","password":"synthetic"}).to_string();
  assert!(captured(s,url,&raw).is_some());assert!(captured(s,"https://zulip.example.org/",&raw).is_none());
  assert!(captured(s,"https://evil.invalid/login/",&raw).is_none());
  assert!(captured(s,url,&raw.replace(url,"https://evil.invalid/login/")).is_none());
  assert!(captured(s,url,&raw.replace("synthetic","")).is_none());
 }
 #[test]fn failed_login_is_not_a_success_route(){
  let z=service("zulip").unwrap();assert!(signed_in_url(z,"https://zulip.example.org/#narrow/channel/4"));
  assert!(!signed_in_url(z,"https://zulip.example.org/login/?error=1"));assert!(!signed_in_url(z,"https://evil.invalid/"));
  let b=service("openbao").unwrap();assert!(signed_in_url(b,"https://127.0.0.1:8200/ui/vault/secrets"));
  assert!(!signed_in_url(b,"https://127.0.0.1:8200/ui/vault/auth?with=userpass"));assert!(!signed_in_url(b,"https://127.0.0.1:8200/ui/vault/secrets-fake"));
 }
}

#[cfg(test)]mod webview_capture_test{
 use super::*;
 fn wait_for(mut f:impl FnMut()->bool){let until=Instant::now()+std::time::Duration::from_secs(12);while Instant::now()<until{while gtk::glib::MainContext::default().pending(){gtk::glib::MainContext::default().iteration(false);}if f(){return;}std::thread::sleep(std::time::Duration::from_millis(20));}assert!(f(),"WebView condition timed out");}
 #[test] #[ignore="Needs Xvfb and creates an isolated temporary Secret Service item"]
 fn automatic_capture_after_success(){
  gtk::init().unwrap();ENABLED.store(true,Ordering::SeqCst);
  for (id,kind,origin,login,success,html,submit) in [
   ("selftest-webview-20260913","zulip","https://zulip.example.org","https://zulip.example.org/login/","https://zulip.example.org/","<html><body><form><input name='username' value='synthetic@example.invalid'><input name='password' type='password' value='synthetic-only'></form><script>document.addEventListener('submit',e=>e.preventDefault());</script></body></html>","document.querySelector('form').requestSubmit()"),
   ("selftest-deepseek-20260913","deepseek","https://platform.deepseek.com","https://platform.deepseek.com/sign_in","https://platform.deepseek.com/balance","<html><body><input placeholder='Phone number / email address' value='synthetic@example.invalid'><input type='password' placeholder='Password' value='synthetic-only'><div role='button'>Log in</div></body></html>","document.querySelector('[role=button]').click()")
  ] {
  let s=Service{id,kind,origin,title:"Test capture temporaire"};
  assert!(load(s).is_none());let v=webkit2gtk::WebView::new();let window=gtk::Window::new(gtk::WindowType::Toplevel);window.add(&v);window.show_all();attach_service(&v,s);
  v.load_html(html,Some(login));wait_for(||!v.is_loading() && v.uri().is_some_and(|u|u==login));
  v.run_javascript(submit,None::<&gio::Cancellable>,|_|{});
  wait_for(||pending().lock().unwrap().contains_key(s.id));assert!(load(s).is_none(),"Must not save before success");
  v.load_html("<html><body>Authenticated route fixture</body></html>",Some(success));
  wait_for(||!v.is_loading() && !pending().lock().unwrap().contains_key(s.id));
  let until=Instant::now()+std::time::Duration::from_secs(5);let mut result=None;
  while Instant::now()<until{result=load(s);if result.is_some(){break;}std::thread::sleep(std::time::Duration::from_millis(100));}
  assert!(result.is_some_and(|c|c.username=="synthetic@example.invalid" && c.password=="synthetic-only"));
  let empty=html.replace("value='synthetic@example.invalid'", "").replace("value='synthetic-only'", "");
  v.load_html(&empty,Some(login));
  wait_for(||!v.is_loading() && pending().lock().unwrap().contains_key(s.id));
  assert!(pending().lock().unwrap().get(s.id).is_some_and(|(_,c)|c.username=="synthetic@example.invalid" && c.password=="synthetic-only"),"Saved access must fill the synthetic login");
  let deleted=forget(s);window.close();assert!(deleted);
  }
 }
}
