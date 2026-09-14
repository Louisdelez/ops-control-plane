//! Native PTYs. Output stays in bounded memory and is accessible only to the local terminal view.
use portable_pty::{native_pty_system, CommandBuilder, MasterPty, PtySize, Child};
use serde::Serialize;
use std::{collections::HashMap, io::{Read,Write}, sync::{Mutex,mpsc::{sync_channel,Receiver}}, process::Command};
use tauri::{State,Manager};
const PROJECT: &str = "/home/ops-user/ops-control-plane";
#[derive(Default)]
pub struct Terminals { sessions: Mutex<HashMap<String,Session>>, serial: Mutex<u64> }
struct Session { master: Box<dyn MasterPty+Send>, writer: Box<dyn Write+Send>, child: Box<dyn Child+Send+Sync>, output: Receiver<Vec<u8>>, unit: String }
impl Drop for Session { fn drop(&mut self) { let _=self.child.kill(); let _=self.child.wait(); let _=Command::new("/usr/bin/systemctl").args(["--user","--no-block","stop",&self.unit]).stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null()).status(); } }
fn size(cols:u16,rows:u16)->Result<PtySize,String> { if !(20..=500).contains(&cols)||!(5..=200).contains(&rows) {return Err("Dimensions invalides".into())} Ok(PtySize{rows,cols,pixel_width:0,pixel_height:0}) }
fn arguments(tool:&str,action:&str)->Result<Vec<&'static str>,String> {
    if action=="pilot" { return match tool {"codex"=>Ok(vec!["--tool","codex"]),"claude"=>Ok(vec!["--tool","claude"]),_=>Err("Outil inconnu".into())}; }
    if action=="missions" {
        let mut args=arguments(tool,"start")?;
        args.push("Lis native_ops/docs/PILOTAGE.md et tes instructions de supervision. Avec les outils MCP Ops, liste les missions et incidents ouverts de infra-shared. Lis les demandes Zulip et les actions liées. Reprends les observations autorisées et rends compte par le protocole handoff. Ne fabrique aucune approbation humaine et ne modifie pas les services hors des runbooks autorisés.");
        return Ok(args);
    }
    match (tool,action) {
        ("codex","start")=>Ok(vec!["-c","forced_login_method=\"chatgpt\"","-c","model_provider=\"openai\"","-c","approval_policy=\"on-request\"","-c","sandbox_mode=\"workspace-write\""]),
        ("codex","resume")=>Ok(vec!["-c","forced_login_method=\"chatgpt\"","-c","model_provider=\"openai\"","-c","approval_policy=\"on-request\"","-c","sandbox_mode=\"workspace-write\"","resume"]),
        ("codex","login")=>Ok(vec!["login"]),
        ("codex","status")=>Ok(vec!["login","status"]),
        ("claude","start")=>Ok(vec!["--settings","{\"forceLoginMethod\":\"claudeai\"}"]),
        ("claude","resume")=>Ok(vec!["--settings","{\"forceLoginMethod\":\"claudeai\"}","--resume"]),
        ("claude","login")=>Ok(vec!["auth","login","--claudeai"]),
        ("claude","status")=>Ok(vec!["auth","status"]),
        ("codex"|"claude","version")=>Ok(vec!["--version"]),
        _=>Err("Outil ou action inconnu".into())
    }
}
fn mode_helper(action:&str)->Result<serde_json::Value,String> {
    let out=Command::new("/usr/bin/sudo").args(["-n","/usr/local/libexec/ops-execution-mode",action]).output().map_err(|_|"Gestionnaire de mode indisponible")?;
    if !out.status.success(){return Err("Changement de mode impossible ; consulter le diagnostic local".into())}
    serde_json::from_slice(&out.stdout).map_err(|_|"État du mode invalide".into())
}
#[tauri::command]
pub async fn execution_status()->Result<serde_json::Value,String> {mode_helper("status")}
#[tauri::command]
pub async fn get_native_budget()->Result<serde_json::Value,String> {Ok(mode_helper("status")?["budgets"].clone())}
#[tauri::command]
pub async fn execution_mode(state:State<'_,Terminals>,mode:String)->Result<serde_json::Value,String> {
    if !["cli","api","hybrid","activate"].contains(&mode.as_str()){return Err("Mode inconnu".into())}
    let sessions=state.sessions.lock().map_err(|_|"Sessions indisponibles")?;
    if mode=="api"&&!sessions.is_empty(){return Err("Ferme les sessions avant de passer en API uniquement.".into())}
    mode_helper(&mode)
}
#[tauri::command]
pub async fn terminal_start(state:State<'_,Terminals>,tool:String,action:String,cols:u16,rows:u16)->Result<String,String> {
    let argv=arguments(&tool,&action)?;let dimensions=size(cols,rows)?;
    let mut sessions=state.sessions.lock().map_err(|_|"Sessions indisponibles")?;
    if mode_helper("status")?["mode"]=="api" {mode_helper("hybrid")?;}
    if sessions.len()>=4 {return Err("Quatre sessions maximum ; ferme une session.".into())}
    let mut serial=state.serial.lock().map_err(|_|"Sessions indisponibles")?;*serial+=1;
    let id=format!("{}-{}",std::process::id(),serial);
    let unit=format!("ops-terminal-{}.scope",id);
    let pair=native_pty_system().openpty(dimensions).map_err(|_|"Création du terminal impossible")?;
    let mut cmd=CommandBuilder::new("/usr/bin/systemd-run");
    cmd.args(["--user","--scope","--slice=ops-agents.slice","--quiet","--collect","--unit",&unit,"--property=MemoryMax=1500M","--property=TasksMax=128","--"]);
    cmd.arg(if action=="pilot" {"/opt/ops-native/venv/bin/ops-native-cli-pilot"} else if tool=="codex" {"/home/ops-user/.local/bin/codex"} else {"/usr/bin/claude"});cmd.args(argv);
    cmd.cwd(PROJECT);cmd.env_clear();
    for key in ["HOME","USER","LOGNAME","LANG","LC_ALL","DISPLAY","WAYLAND_DISPLAY","XDG_RUNTIME_DIR","DBUS_SESSION_BUS_ADDRESS","XAUTHORITY"] {
        if let Ok(value)=std::env::var(key){cmd.env(key,value);}
    }
    cmd.env("PATH","/home/ops-user/.local/bin:/usr/local/bin:/usr/bin:/bin");cmd.env("TERM","xterm-256color");cmd.env("COLORTERM","truecolor");
    let child=pair.slave.spawn_command(cmd).map_err(|_|"Démarrage du CLI impossible")?;drop(pair.slave);
    let mut reader=pair.master.try_clone_reader().map_err(|_|"Lecture du terminal impossible")?;
    let writer=pair.master.take_writer().map_err(|_|"Écriture du terminal impossible")?;
    let (tx,rx)=sync_channel(64);
    std::thread::spawn(move|| {let mut buf=[0u8;8192];loop{match reader.read(&mut buf){Ok(0)|Err(_)=>break,Ok(n)=>if tx.send(buf[..n].to_vec()).is_err(){break}}}});
    sessions.insert(id.clone(),Session{master:pair.master,writer,child,output:rx,unit});Ok(id)
}
#[derive(Serialize)]
pub struct Poll { data:Vec<u8>, exited:bool }
#[tauri::command]
pub async fn terminal_poll(state:State<'_,Terminals>,id:String)->Result<Poll,String> {
    let mut sessions=state.sessions.lock().map_err(|_|"Sessions indisponibles")?;let s=sessions.get_mut(&id).ok_or("Session fermée")?;
    let mut data=Vec::new();while data.len()<65536 {match s.output.try_recv(){Ok(chunk)=>data.extend(chunk),Err(_)=>break}}
    let exited=s.child.try_wait().map_err(|_|"État indisponible")?.is_some();Ok(Poll{data,exited})
}
#[tauri::command]
pub async fn terminal_write(state:State<'_,Terminals>,id:String,data:String)->Result<(),String> {
    if data.len()>65536{return Err("Entrée trop longue".into())}
    state.sessions.lock().map_err(|_|"Sessions indisponibles")?.get_mut(&id).ok_or("Session fermée")?.writer.write_all(data.as_bytes()).map_err(|_|"Session terminée".into())
}
#[tauri::command]
pub async fn terminal_resize(state:State<'_,Terminals>,id:String,cols:u16,rows:u16)->Result<(),String> {
    let dimensions=size(cols,rows)?;state.sessions.lock().map_err(|_|"Sessions indisponibles")?.get(&id).ok_or("Session fermée")?.master.resize(dimensions).map_err(|_|"Redimensionnement impossible".into())
}
#[tauri::command]
pub async fn terminal_close(state:State<'_,Terminals>,id:String)->Result<(),String> {state.sessions.lock().map_err(|_|"Sessions indisponibles")?.remove(&id);Ok(())}
pub fn close_all(app:&tauri::AppHandle){if let Ok(mut sessions)=app.state::<Terminals>().sessions.lock(){sessions.clear();}}
#[cfg(test)] mod tests {use super::*;
 #[test] fn real_pty_input_resize_and_reap(){
    let pair=native_pty_system().openpty(size(80,24).unwrap()).unwrap();
    let mut cmd=CommandBuilder::new("/usr/bin/python3");
    cmd.args(["-c","import os,tty; tty.setraw(0); os.write(1,b'READY'); data=os.read(0,4); z=os.get_terminal_size(0); os.write(1,('%s %s '%(z.columns,z.lines)).encode()+data+b'DONE')"]);
    let mut child=pair.slave.spawn_command(cmd).unwrap();drop(pair.slave);
    let mut reader=pair.master.try_clone_reader().unwrap();let mut writer=pair.master.take_writer().unwrap();
    let (tx,rx)=sync_channel(10);std::thread::spawn(move||{let mut b=[0;256];while let Ok(n)=reader.read(&mut b){if n==0{break}if tx.send(b[..n].to_vec()).is_err(){break}}});
    let mut output=Vec::new();while !output.ends_with(b"READY"){output.extend(rx.recv_timeout(std::time::Duration::from_secs(5)).unwrap());}
    pair.master.resize(size(103,31).unwrap()).unwrap();writer.write_all(b"PING").unwrap();
    while !output.ends_with(b"DONE"){output.extend(rx.recv_timeout(std::time::Duration::from_secs(5)).unwrap());}
    assert!(String::from_utf8_lossy(&output).contains("103 31 PINGDONE"));assert!(child.wait().unwrap().success());
 }
 #[test] fn no_arbitrary_commands(){assert!(arguments("bash","start").is_err());assert!(arguments("codex",";id").is_err());assert!(size(0,24).is_err());assert!(size(80,24).is_ok());}
 #[test] fn native_account_and_permissions(){let a=arguments("codex","start").unwrap();assert!(a.contains(&"forced_login_method=\"chatgpt\""));assert!(a.contains(&"sandbox_mode=\"workspace-write\""));assert!(!a.iter().any(|x|x.contains("bypass")));assert!(arguments("claude","start").unwrap().contains(&"{\"forceLoginMethod\":\"claudeai\"}"));}
}
