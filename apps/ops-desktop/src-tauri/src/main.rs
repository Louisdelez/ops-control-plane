mod keyring;
mod infrastructure;
mod approval_notifications;
mod providers;
mod terminals;
use terminals::*;
use tauri::{Manager, WebviewUrl, Webview, LogicalPosition, LogicalSize};
use tauri::webview::WebviewBuilder;
use tauri::window::WindowBuilder;
use webkit2gtk::{WebViewExt, WebContextExt};
use gtk::prelude::*;
const ATLAS_COMMANDS: &[&str] = &["get_catalogue","get_runtime_snapshot","simulate_cost","preview_candidates","refresh_provider_finance","save_provider_credential","get_bootstrap_status","get_bootstrap_result"];
const BAR: f64 = 56.;
const SERVICES: [&str;10] = ["atlas", "infrastructure", "zulip", "approvals", "openbao", "hermes", "terminals", "settings", "deepseek", "jina"];
fn destination(service: &str) -> Option<&'static str> {
    match service {
        "zulip" => Some("https://zulip.example.org/"),
        "approvals" => Some("https://autorisations.example.org/"),
        "openbao" => Some("https://127.0.0.1:8200/ui/"),
        "hermes" => Some("http://127.0.0.1:9119/"),
        _ => providers::portal(service),
    }
}
fn local_app(url: &tauri::Url) -> bool {
    url.scheme() == "tauri" && url.host_str()==Some("localhost") && url.username().is_empty() && url.password().is_none()
}
fn allowed(url: &tauri::Url) -> bool {
    let public_zulip = url.scheme()=="https" && url.host_str()==Some("zulip.example.org")
        && url.port_or_known_default()==Some(443) && url.username().is_empty() && url.password().is_none();
    local_app(url) || public_zulip || (url.origin()==tauri::Url::parse("https://zulip.ops.local:8443/").unwrap().origin() && url.username().is_empty() && url.password().is_none()) || ["zulip", "openbao", "hermes"].iter().any(|id| {
        let target=tauri::Url::parse(destination(id).unwrap()).unwrap();
        url.origin()==target.origin() && url.username().is_empty() && url.password().is_none()
    })
}
fn open_external(url: &tauri::Url) {
    if url.scheme()=="https" && url.username().is_empty() && url.password().is_none() {
        let _=std::process::Command::new("/usr/bin/xdg-open").arg(url.as_str())
            .stdin(std::process::Stdio::null()).stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null()).spawn();
    }
}

#[tauri::command]
async fn open_service(webview: Webview, service: String) -> Result<(), String> {
    if webview.label() != "tabs" || !local_app(&webview.url().map_err(|e|e.to_string())?) {
        return Err("Commande réservée aux onglets".into());
    }
    if !SERVICES.contains(&service.as_str()) {return Err("Service inconnu".into());}
    let app=webview.app_handle();
    for id in SERVICES {
        let view=app.get_webview(id).ok_or("Onglet indisponible")?;
        if id == service {view.show()} else {view.hide()}.map_err(|e|e.to_string())?;
    }
    Ok(())
}
#[tauri::command]
async fn reload_service(webview: Webview, service: String) -> Result<(), String> {
    if webview.label() != "tabs" || !local_app(&webview.url().map_err(|e|e.to_string())?) {
        return Err("Commande réservée aux onglets".into());
    }
    if !matches!(service.as_str(), "zulip" | "approvals" | "openbao" | "hermes" | "deepseek" | "jina") {
        return Err("Cet onglet ne se recharge pas".into());
    }
    webview.app_handle().get_webview(&service).ok_or("Onglet indisponible")?
        .reload().map_err(|e|e.to_string())
}
#[tauri::command]
async fn open_settings(webview: Webview) -> Result<(), String> {
 if webview.label()!="tabs" || !local_app(&webview.url().map_err(|e|e.to_string())?){return Err("Commande réservée aux onglets".into());}
 open_service(webview,"settings".into()).await
}
#[tauri::command]
fn settings_state() -> serde_json::Value { keyring::settings_state() }
#[tauri::command]
fn settings_enabled(webview: Webview, enabled: bool) -> Result<(),String> {
 keyring::update_enabled(webview.app_handle(),enabled)
}
#[tauri::command]
async fn settings_manage(webview: Webview, service: String) -> Result<(),String> {
 keyring::manage(webview.app_handle().clone(),service).await
}
#[tauri::command]
fn provider_tabs(webview:Webview)->Result<serde_json::Value,String>{
 if !matches!(webview.label(),"tabs"|"settings") || !webview.url().is_ok_and(|u|local_app(&u)){return Err("Commande locale uniquement".into());}
 Ok(providers::state())
}
#[tauri::command]
fn provider_browser(webview:Webview,service:String)->Result<(),String>{
 if !matches!(webview.label(),"tabs"|"settings") || !webview.url().is_ok_and(|u|local_app(&u)){return Err("Commande locale uniquement".into());}
 let url=providers::portal(&service).ok_or("Fournisseur inconnu")?;
 std::process::Command::new("/usr/bin/xdg-open").arg(url).stdin(std::process::Stdio::null()).stdout(std::process::Stdio::null()).stderr(std::process::Stdio::null()).spawn().map_err(|_|"Impossible d’ouvrir le navigateur")?;
 Ok(())
}
#[tauri::command]
fn settings_providers(visible:Vec<String>)->Result<(),String>{providers::update(visible)}
fn provider_popup(app:&tauri::AppHandle,id:&'static str,url:tauri::Url,features:tauri::webview::NewWindowFeatures)->tauri::webview::NewWindowResponse<tauri::Wry> {
 if url.as_str()!="about:blank" && !providers::same_origin(id,&url) && !providers::authentication(&url){open_external(&url);return tauri::webview::NewWindowResponse::Deny;}
 // Related WebKit view: preserves window.opener, postMessage and the cookie store.
 static NEXT:std::sync::atomic::AtomicU64=std::sync::atomic::AtomicU64::new(1);
 let label=format!("provider-auth-{}",NEXT.fetch_add(1,std::sync::atomic::Ordering::Relaxed));
 let child_app=app.clone();
 let builder=tauri::WebviewWindowBuilder::new(app,label,WebviewUrl::External("about:blank".parse().unwrap()))
  .window_features(features).inner_size(520.,720.).title("Connexion au compte")
  .on_navigation(move|u|providers::navigation_allowed(id,u))
  .on_new_window(move|u,f|provider_popup(&child_app,id,u,f))
  .on_document_title_changed(|w,_|{if let Ok(u)=w.url(){if let Some(host)=u.host_str(){let _=w.set_title(&format!("Connexion — {host}"));}}});
 match builder.build(){Ok(window)=>tauri::webview::NewWindowResponse::Create{window},Err(_)=>tauri::webview::NewWindowResponse::Deny}
}
fn main() {
    tauri::Builder::default()
         .manage(Terminals::default())
         .on_window_event(|window,event| {if window.label()=="main" && matches!(event,tauri::WindowEvent::Destroyed) {terminals::close_all(window.app_handle());}})
         .invoke_handler(|invoke| {
            if matches!(invoke.message.command(), "open_service" | "reload_service" | "open_settings" | "provider_tabs" | "provider_browser") {
                let handler: Box<dyn Fn(tauri::ipc::Invoke) -> bool> = Box::new(tauri::generate_handler![open_service,reload_service,open_settings,provider_tabs,provider_browser]);
                return handler(invoke);
            }
            let view=invoke.message.webview();
            if matches!(invoke.message.command(),"infrastructure_snapshot"|"infrastructure_series"|"infrastructure_export_series"|"infrastructure_history"|"infrastructure_logs"|"infrastructure_export_logs") {
                if view.label()!="infrastructure" || !view.url().is_ok_and(|url|local_app(&url)) {invoke.resolver.reject("Commande réservée à Infrastructure");return true;}
                let handler:Box<dyn Fn(tauri::ipc::Invoke)->bool>=Box::new(tauri::generate_handler![infrastructure::infrastructure_snapshot,infrastructure::infrastructure_series,infrastructure::infrastructure_export_series,infrastructure::infrastructure_history,infrastructure::infrastructure_logs,infrastructure::infrastructure_export_logs]);
                return handler(invoke);
            }

            if invoke.message.command().starts_with("settings_") {
                if view.label()!="settings" || !view.url().is_ok_and(|url|local_app(&url)) {invoke.resolver.reject("Commande réservée aux paramètres locaux");return true;}
                let handler: Box<dyn Fn(tauri::ipc::Invoke)->bool> = Box::new(tauri::generate_handler![settings_state,settings_enabled,settings_manage,settings_providers]);
                return handler(invoke);
            }
            if invoke.message.command().starts_with("terminal_") || invoke.message.command().starts_with("execution_") {
                if view.label()!="terminals" || !view.url().is_ok_and(|url|local_app(&url)) {invoke.resolver.reject("Commande réservée aux terminaux locaux");return true;}
                let handler: Box<dyn Fn(tauri::ipc::Invoke)->bool> = Box::new(tauri::generate_handler![terminal_start,terminal_poll,terminal_write,terminal_resize,terminal_close,execution_status,execution_mode]);
                return handler(invoke);
            }
            if view.label() != "atlas" || !view.url().is_ok_and(|url|local_app(&url)) {
                invoke.resolver.reject("Commande réservée à Atlas");return true;
            }
            if invoke.message.command()=="get_native_budget" {
                let handler: Box<dyn Fn(tauri::ipc::Invoke)->bool> = Box::new(tauri::generate_handler![get_native_budget]);
                return handler(invoke);
            }
            if !ATLAS_COMMANDS.contains(&invoke.message.command()) {
                invoke.resolver.reject("L’ancien bootstrap est archivé ; les services sont gérés par leur installation native.");return true;
            }
            ops_model_manager_lib::handle_invoke(invoke)
        })
        .setup(|app| {
            ops_model_manager_lib::initialize_embedded(app.handle());
            providers::initialize(app.path().app_config_dir()?.join("provider-tabs.json"));
            keyring::initialize(app.path().app_config_dir()?.join("keyring-settings.json"));
            let data_dir=app.path().app_data_dir()?;
            std::fs::create_dir_all(&data_dir)?;
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&data_dir,std::fs::Permissions::from_mode(0o700))?;
            let window=WindowBuilder::new(app,"main").title("Ops")
                .inner_size(1320.,860.).min_inner_size(860.,600.).build()?;
            let tabs=WebviewBuilder::new("tabs",WebviewUrl::App("index.html".into()))
                .on_navigation(local_app);
            let tabs=window.add_child(tabs,LogicalPosition::new(0.,0.),LogicalSize::new(1320.,BAR))?;
            tabs.with_webview(|view| {
                let widget=view.inner();
                widget.set_size_request(-1,BAR as i32);
                if let Some(parent)=widget.parent().and_then(|p|p.downcast::<gtk::Box>().ok()) {
                    parent.set_child_packing(&widget,false,true,0,gtk::PackType::Start);
                }
            })?;
            let atlas=WebviewBuilder::new("atlas",WebviewUrl::App("atlas/index.html".into()))
                .data_directory(data_dir.clone()).on_navigation(local_app);
            window.add_child(atlas,LogicalPosition::new(0.,BAR),LogicalSize::new(1320.,860.-BAR))?;
            let infrastructure=WebviewBuilder::new("infrastructure",WebviewUrl::App("infrastructure/index.html".into())).on_navigation(local_app);
            window.add_child(infrastructure,LogicalPosition::new(0.,BAR),LogicalSize::new(1320.,860.-BAR))?;
            let terminal_view=WebviewBuilder::new("terminals",WebviewUrl::App("terminals/index.html".into())).on_navigation(local_app);
            window.add_child(terminal_view,LogicalPosition::new(0.,BAR),LogicalSize::new(1320.,860.-BAR))?;
            let settings=WebviewBuilder::new("settings",WebviewUrl::App("settings/index.html".into())).on_navigation(local_app);
            window.add_child(settings,LogicalPosition::new(0.,BAR),LogicalSize::new(1320.,860.-BAR))?;
            for id in ["zulip","openbao","hermes"] {
                let mut builder=WebviewBuilder::new(id,WebviewUrl::External("about:blank".parse()?))
                    .data_directory(data_dir.clone())
                    .on_navigation(|url| {if url.as_str()=="about:blank" || allowed(url) {true} else {open_external(url);false}})
                    .on_new_window(|url,_| {open_external(&url);tauri::webview::NewWindowResponse::Deny});
                if id == "zulip" {builder=builder.initialization_script(include_str!("../zulip-scroll.js"));}
                let view=window.add_child(builder,LogicalPosition::new(0.,BAR),LogicalSize::new(1320.,860.-BAR))?;
                view.with_webview(move |view| {
                    keyring::attach(&view.inner(),id);
                    if let Some(context)=view.inner().context() {
                        for (host,path) in [
                            ("zulip.ops.local","/etc/pki/ca-trust/source/anchors/zulip-standard-local.crt"),
                            ("127.0.0.1","/etc/pki/ca-trust/source/anchors/openbao-local.crt")
                        ] {
                            if let Ok(pem)=std::fs::read_to_string(path) {
                                if let Ok(cert)=gio::TlsCertificate::from_pem(&pem) {
                                    context.allow_tls_certificate_for_host(&cert,host);
                                }
                            }
                        }
                    }
                })?;
                if id != "atlas" {view.hide()?;}
                view.navigate(destination(id).unwrap().parse()?)?;
            }
            let approvals=WebviewBuilder::new("approvals",WebviewUrl::External(destination("approvals").unwrap().parse()?))
                .data_directory(data_dir.clone())
                .on_navigation(|url| {
                    let target=tauri::Url::parse(destination("approvals").unwrap()).unwrap();
                    if url.origin()==target.origin() && url.username().is_empty() && url.password().is_none(){true}
                    else{open_external(url);false}
                })
                .on_new_window(|url,_|{open_external(&url);tauri::webview::NewWindowResponse::Deny});
            let approvals_view=window.add_child(approvals,LogicalPosition::new(0.,BAR),LogicalSize::new(1320.,860.-BAR))?;
            approvals_view.hide()?;
            let notification_app=app.handle().clone();
            approvals_view.with_webview(move |native| {
                approval_notifications::attach(&native.inner(),notification_app);
            })?;
            for id in ["deepseek","jina"] {
                // Provider pages have no IPC permissions and cannot navigate into local services.
                let popup_app=app.handle().clone();
                let builder=WebviewBuilder::new(id,WebviewUrl::External("about:blank".parse()?))
                    .data_directory(data_dir.clone())
                    .on_navigation(move |url| {
                        providers::navigation_allowed(id,url)
                    })
                    .on_new_window(move |url,features|provider_popup(&popup_app,id,url,features));
                let view=window.add_child(builder,LogicalPosition::new(0.,BAR),LogicalSize::new(1320.,860.-BAR))?;
                view.with_webview(move|v|{
                    let native=v.inner();
                    // The managed provider profile supports cross-site SSO state cookies.
                    // Google and provider callbacks keep their own OAuth/CSRF checks.
                    if let Some(context)=native.context(){
                        use webkit2gtk::CookieManagerExt;
                        if let Some(cookies)=context.cookie_manager(){cookies.set_accept_policy(webkit2gtk::CookieAcceptPolicy::Always);}
                    }
                    native.connect_load_changed(move|v,event|{if event==webkit2gtk::LoadEvent::Finished{if let Some(u)=v.uri().and_then(|u|u.parse().ok()){providers::observe(id,&u,"finished");}}});
                    native.connect_load_failed(move|_,_,uri,_|{if let Ok(u)=uri.parse(){providers::observe(id,&u,"load_failed");}false});
                    if id=="deepseek"{keyring::attach(&native,id);}
                })?;
                view.hide()?;
                view.navigate(providers::portal(id).unwrap().parse()?)?;
            }
            // GTK allocates the fixed-height tab bar and the expanding active view.
            // Creating a child can show siblings, so apply visibility after all exist.
            for id in SERVICES {if id != "atlas" {app.get_webview(id).unwrap().hide()?;}}
            Ok(())
        })
        .run(tauri::generate_context!()).expect("Impossible de démarrer Ops");
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test] fn only_known_origins_are_embedded() {
        for value in ["https://zulip.example.org/#narrow/channel/4", "https://zulip.ops.local:8443/#narrow/channel/4", "https://127.0.0.1:8200/ui/vault/auth", "http://127.0.0.1:9119/chat"] {assert!(allowed(&value.parse().unwrap()));}
        for value in ["https://zulip.example.org.evil.test/", "https://zulip.example.org:8443/", "http://zulip.example.org/", "https://zulip.ops.local.evil.test:8443/", "http://127.0.0.1:8200/", "file:///etc/passwd", "https://user:password@zulip.ops.local:8443/", "https://example.org/"] {assert!(!allowed(&value.parse().unwrap()));}
        assert!(destination("shell").is_none());
        assert!(!local_app(&"http://127.0.0.1:9119/".parse().unwrap()));
    }
}
