//! Notifications from the authenticated approval view; no execution or secret IPC.
use gtk::prelude::*;
use webkit2gtk::{UserContentManagerExt, WebViewExt};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::Manager;

fn valid_event(origin: &str, raw: &str) -> bool {
    let Ok(url) = tauri::Url::parse(origin) else { return false; };
    if url.origin().ascii_serialization() != "https://autorisations.example.org"
        || !url.username().is_empty() || url.password().is_some() || raw.len() > 128 { return false; }
    let Ok(value) = serde_json::from_str::<serde_json::Value>(raw) else { return false; };
    value.as_object().is_some_and(|v| v.len() == 1)
        && value["pending"].as_u64().is_some_and(|n| (1..=100).contains(&n))
}

pub fn attach(view: &webkit2gtk::WebView, app: tauri::AppHandle) {
    if let Some(manager) = view.user_content_manager() {
        let weak = view.downgrade();
        let last: Arc<Mutex<Option<Instant>>> = Arc::new(Mutex::new(None));
        manager.connect_script_message_received(Some("opsApprovalsPending"), move |_, message| {
            let (Some(view), Some(value)) = (weak.upgrade(), message.js_value()) else { return; };
            let Some(uri) = view.uri() else { return; };
            if !valid_event(&uri, &value.to_string()) { return; }
            // The independent desktop service also works with this window closed.
            // Keep the webview path as fallback if that service is unavailable.
            let runtime=std::env::var("XDG_RUNTIME_DIR").unwrap_or_default();
            let same_session=std::env::var("DBUS_SESSION_BUS_ADDRESS").is_ok_and(|address| address.split(',').next()==Some(format!("unix:path={runtime}/bus").as_str()));
            if let Ok(raw) = if same_session {std::fs::read("/var/lib/ops-desktop-notifications/status.json")} else {Err(std::io::Error::from(std::io::ErrorKind::NotFound))} {
                if let Ok(status) = serde_json::from_slice::<serde_json::Value>(&raw) {
                    let now=std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d|d.as_secs_f64()).unwrap_or(0.);
                    let age=now-status["checked_at"].as_f64().unwrap_or(0.);
                    if status["status"]=="healthy" && (0.0..30.0).contains(&age) { return; }
                }
            }
            let mut previous = last.lock().unwrap();
            if previous.is_some_and(|when| when.elapsed() < Duration::from_secs(15)) { return; }
            *previous = Some(Instant::now());
            let app = app.clone();
            std::thread::spawn(move || {
                use std::process::{Command, Stdio};
                let result = Command::new("/usr/bin/notify-send")
                    .args(["--app-name=Autorisations", "--icon=ch.ops-user.ops-desktop", "--expire-time=15000",
                           "--action=open=Ouvrir Autorisations", "--wait", "Autorisations",
                           "Une nouvelle demande attend ta décision."])
                    .stdin(Stdio::null()).stderr(Stdio::null()).output();
                if result.is_ok_and(|r| r.status.success() && r.stdout == b"open\n") {
                    if let Some(window) = app.get_window("main") { let _ = window.show(); let _ = window.set_focus(); }
                    for id in crate::SERVICES {
                        if let Some(view) = app.get_webview(id) {
                            let _ = if id == "approvals" { view.show() } else { view.hide() };
                        }
                    }
                }
            });
        });
        manager.register_script_message_handler("opsApprovalsPending");
    }
    view.connect_load_changed(|view, event| {
        if event == webkit2gtk::LoadEvent::Finished {
            view.run_javascript(include_str!("../approvals-notify.js"), None::<&gio::Cancellable>, |_| {});
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_exact_origin_and_bounded_count() {
        assert!(valid_event("https://autorisations.example.org/", r#"{"pending":1}"#));
        assert!(!valid_event("https://autorisations.example.org.evil/", r#"{"pending":1}"#));
        assert!(!valid_event("https://autorisations.example.org/", r#"{"pending":0}"#));
        assert!(!valid_event("https://autorisations.example.org/", r#"{"pending":1,"command":"x"}"#));
    }
}
