const APP_COMMANDS: &[&str] = &[
    "get_catalogue",
    "get_runtime_snapshot",
    "simulate_cost",
    "preview_candidates",
    "refresh_provider_finance",
    "get_bootstrap_status",
    "run_bootstrap_preflight",
    "run_bootstrap_corrective_preflight",
    "run_bootstrap_recovery_preflight",
    "begin_bootstrap",
    "begin_corrective_bootstrap",
    "begin_recovery_bootstrap",
    "get_bootstrap_result",
    "cancel_bootstrap",
    "save_provider_credential",
    "open_zulip",
];

fn main() {
    // Declaring the app commands here removes Tauri's implicit all-window
    // access. The capability in capabilities/main.json then grants each one
    // explicitly to the sole local window.
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(APP_COMMANDS)),
    )
    .expect("failed to build the Tauri application manifest");
}
