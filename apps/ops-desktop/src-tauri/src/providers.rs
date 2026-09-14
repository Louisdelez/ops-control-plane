//! Official provider portals. Never imports browser cookies or Google credentials.
use std::{path::PathBuf,sync::{Mutex,OnceLock}};
use serde_json::{json,Value};
const IDS:[&str;2]=["deepseek","jina"];
static PATH:OnceLock<PathBuf>=OnceLock::new();
static VISIBLE:Mutex<Vec<String>>=Mutex::new(Vec::new());
pub fn initialize(path:PathBuf){
 let selected=std::fs::read(&path).ok().and_then(|b|serde_json::from_slice::<Vec<String>>(&b).ok()).filter(|v|valid(v)).unwrap_or_else(||IDS.iter().map(|s|s.to_string()).collect());
 *VISIBLE.lock().unwrap()=selected;let _=PATH.set(path);
}
fn valid(ids:&[String])->bool{ids.len()<=IDS.len() && ids.iter().all(|id|IDS.contains(&id.as_str())) && (ids.len()<2 || ids[0]!=ids[1])}
pub fn portal(id:&str)->Option<&'static str>{match id{"deepseek"=>Some("https://platform.deepseek.com/balance"),"jina"=>Some("https://jina.ai/api-dashboard/"),_=>None}}
pub fn same_origin(id:&str,url:&tauri::Url)->bool{portal(id).is_some_and(|p|url.origin()==tauri::Url::parse(p).unwrap().origin()) && url.username().is_empty() && url.password().is_none()}
pub fn authentication(url:&tauri::Url)->bool{
 url.scheme()=="https" && url.port_or_known_default()==Some(443) && url.username().is_empty() && url.password().is_none() && (matches!(url.host_str(),Some("accounts.google.com"|"accounts.googleusercontent.com"|"accounts.youtube.com"|"sefo-api-key.firebaseapp.com")) || (url.host_str()==Some("jina.ai") && url.path().starts_with("/__/auth/")))
}
// Diagnostic metadata only: never retain query strings, fragments or cookie values.
static EVENTS:Mutex<Vec<Value>>=Mutex::new(Vec::new());
pub fn observe(id:&str,url:&tauri::Url,event:&str){
 let Some(path)=PATH.get() else{return};
 let route=match url.path(){"/CheckCookie"=>"CheckCookie","/SetSID"=>"SetSID","/balance"=>"balance","/sign_in"=>"sign_in",_=>"other"};
 let item=json!({"provider":id,"event":event,"scheme":url.scheme(),"host":url.host_str(),"route":route});
 if let Ok(mut events)=EVENTS.lock(){
  if events.last()==Some(&item){return;}
  events.push(item);if events.len()>32{events.remove(0);}
  if let Ok(raw)=serde_json::to_vec(&*events){
   let file=path.with_file_name("provider-navigation.json");let next=file.with_extension("next");
   use std::os::unix::fs::OpenOptionsExt;use std::io::Write;
   if let Some(parent)=file.parent(){let _=std::fs::create_dir_all(parent);}
   if let Ok(mut f)=std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(&next){if f.write_all(&raw).is_ok(){let _=std::fs::rename(next,file);}}
  }
 }
}
pub fn navigation_allowed(id:&str,url:&tauri::Url)->bool{
 let allow=url.as_str()=="about:blank" || authentication(url) || same_origin(id,url);
 if !allow{observe(id,url,"blocked");}
 allow
}
pub fn state()->Value{json!({"visible":VISIBLE.lock().unwrap().clone(),"providers":[{"id":"deepseek","name":"DeepSeek","url":portal("deepseek")},{"id":"jina","name":"Jina","url":portal("jina")}]})}
pub fn update(ids:Vec<String>)->Result<(),String>{
 if !valid(&ids){return Err("Fournisseur inconnu ou répété".into());}
 let mut current=VISIBLE.lock().map_err(|_|"Réglage indisponible")?;
 let path=PATH.get().ok_or("Réglage indisponible")?;
 use std::os::unix::fs::OpenOptionsExt;use std::io::Write;
 let save=(||->std::io::Result<()>{std::fs::create_dir_all(path.parent().unwrap())?;let next=path.with_extension("next");let mut f=std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(0o600).open(&next)?;f.write_all(&serde_json::to_vec(&ids)?)?;f.sync_all()?;std::fs::rename(next,path)})();
 save.map_err(|_|"Impossible d’enregistrer les onglets")?;*current=ids;Ok(())
}
#[cfg(test)]mod tests{use super::*;
 #[test]fn portal_origins_are_exact(){for u in ["https://platform.deepseek.com/balance","https://platform.deepseek.com/sign_in"]{assert!(same_origin("deepseek",&u.parse().unwrap()));}for u in ["https://platform.deepseek.com.evil.test/","https://user@platform.deepseek.com/","https://platform.deepseek.com:8443/","https://127.0.0.1:8200/","tauri://localhost/index.html"]{assert!(!same_origin("deepseek",&u.parse().unwrap()));}assert!(!same_origin("jina",&"https://accounts.google.com/".parse().unwrap()));}
 #[test]fn only_supported_tabs(){assert!(valid(&[]));assert!(valid(&vec!["jina".into(),"deepseek".into()]));assert!(!valid(&vec!["google".into()]));assert!(!valid(&vec!["jina".into(),"jina".into()]));}
 #[test]fn google_and_firebase_origins_are_exact(){assert!(authentication(&"https://accounts.google.com/o/oauth2/auth".parse().unwrap()));assert!(authentication(&"https://jina.ai/__/auth/handler".parse().unwrap()));assert!(!authentication(&"https://accounts.google.com.evil.test/".parse().unwrap()));assert!(!authentication(&"https://accounts.google.com:8443/".parse().unwrap()));assert!(!authentication(&"https://user@accounts.google.com/".parse().unwrap()));}
}
