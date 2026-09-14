#![forbid(unsafe_code)]

mod catalogue;
mod onboarding;
mod runtime;
mod security;

use catalogue::{Catalogue, PreviewCandidatesRequest, PreviewCandidatesResponse};
use catalogue::{SimulateCostRequest, SimulateCostResponse};
use runtime::RuntimeSnapshot;
use security::{OpenZulipStatus, SaveProviderCredentialStatus};
use serde::Serialize;
use tauri::{AppHandle, State};

#[derive(Clone, Copy, Debug, Serialize)]
pub struct CommandError {
    code: &'static str,
    message: &'static str,
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct FinanceRefreshStatus {
    status: &'static str,
}

impl CommandError {
    const fn new(code: &'static str, message: &'static str) -> Self {
        Self { code, message }
    }
}

#[tauri::command]
fn get_catalogue() -> Result<Catalogue, CommandError> {
    catalogue::load_catalogue()
}

#[tauri::command]
async fn get_runtime_snapshot() -> Result<RuntimeSnapshot, CommandError> {
    let catalogue = catalogue::load_catalogue()?;
    let (revision, cards, provider_accounts) = catalogue::runtime_identity(&catalogue);
    tauri::async_runtime::spawn_blocking(move || {
        runtime::get_runtime_snapshot(&revision, cards, provider_accounts)
    })
    .await
    .map_err(|_| CommandError::new("runtime_worker_failed", "La lecture locale a echoue."))
}

#[tauri::command]
fn simulate_cost(request: SimulateCostRequest) -> Result<SimulateCostResponse, CommandError> {
    catalogue::simulate_cost(request)
}

#[tauri::command]
async fn preview_candidates(
    request: PreviewCandidatesRequest,
) -> Result<PreviewCandidatesResponse, CommandError> {
    tauri::async_runtime::spawn_blocking(move || catalogue::preview_candidates(request))
        .await
        .map_err(|_| CommandError::new("preview_worker_failed", "La previsualisation a echoue."))?
}

#[tauri::command]
fn get_bootstrap_status(
    state: State<'_, onboarding::BootstrapManager>,
) -> Result<onboarding::BootstrapStatus, CommandError> {
    onboarding::get_bootstrap_status(state.inner())
}

#[tauri::command]
async fn run_bootstrap_preflight(
    app: AppHandle,
    state: State<'_, onboarding::BootstrapManager>,
) -> Result<onboarding::BootstrapPreflightResponse, CommandError> {
    onboarding::run_bootstrap_preflight(state.inner().clone(), app).await
}

#[tauri::command]
async fn run_bootstrap_corrective_preflight(
    app: AppHandle,
    state: State<'_, onboarding::BootstrapManager>,
) -> Result<onboarding::BootstrapPreflightResponse, CommandError> {
    onboarding::run_bootstrap_corrective_preflight(state.inner().clone(), app).await
}

#[tauri::command]
async fn run_bootstrap_recovery_preflight(
    app: AppHandle,
    state: State<'_, onboarding::BootstrapManager>,
) -> Result<onboarding::BootstrapPreflightResponse, CommandError> {
    onboarding::run_bootstrap_recovery_preflight(state.inner().clone(), app).await
}

#[tauri::command]
fn begin_bootstrap(
    app: AppHandle,
    state: State<'_, onboarding::BootstrapManager>,
    open_bao_password: Option<String>,
    zulip_password: Option<String>,
    confirmed_release_id: String,
    confirmed_digest_suffix: String,
) -> Result<onboarding::BootstrapStartResponse, CommandError> {
    onboarding::begin_bootstrap(
        state.inner().clone(),
        app,
        open_bao_password,
        zulip_password,
        confirmed_release_id,
        confirmed_digest_suffix,
    )
}

#[tauri::command]
fn begin_corrective_bootstrap(
    app: AppHandle,
    state: State<'_, onboarding::BootstrapManager>,
    open_bao_password: Option<String>,
    zulip_password: Option<String>,
    confirmed_release_id: String,
    confirmed_digest_suffix: String,
) -> Result<onboarding::BootstrapStartResponse, CommandError> {
    onboarding::begin_corrective_bootstrap(
        state.inner().clone(),
        app,
        open_bao_password,
        zulip_password,
        confirmed_release_id,
        confirmed_digest_suffix,
    )
}

#[tauri::command]
fn begin_recovery_bootstrap(
    app: AppHandle,
    state: State<'_, onboarding::BootstrapManager>,
    open_bao_password: Option<String>,
    zulip_password: Option<String>,
    confirmed_release_id: String,
    confirmed_digest_suffix: String,
) -> Result<onboarding::BootstrapStartResponse, CommandError> {
    onboarding::begin_recovery_bootstrap(
        state.inner().clone(),
        app,
        open_bao_password,
        zulip_password,
        confirmed_release_id,
        confirmed_digest_suffix,
    )
}

#[tauri::command]
fn get_bootstrap_result(
    state: State<'_, onboarding::BootstrapManager>,
) -> Result<onboarding::BootstrapResult, CommandError> {
    onboarding::get_bootstrap_result(state.inner())
}

#[tauri::command]
fn cancel_bootstrap(
    state: State<'_, onboarding::BootstrapManager>,
) -> Result<onboarding::BootstrapCancelResponse, CommandError> {
    onboarding::cancel_bootstrap(state.inner())
}

#[tauri::command]
async fn save_provider_credential(
    provider_account_id: String,
    open_bao_password: String,
    api_key: String,
) -> Result<SaveProviderCredentialStatus, CommandError> {
    security::save_provider_credential(provider_account_id, open_bao_password, api_key).await
}

#[tauri::command]
fn open_zulip() -> Result<OpenZulipStatus, CommandError> {
    security::open_zulip()
}

#[tauri::command]
async fn refresh_provider_finance(
    provider_account_id: String,
) -> Result<FinanceRefreshStatus, CommandError> {
    if !security::is_provider_identifier(&provider_account_id)
        || !catalogue::provider_exists(&provider_account_id)
    {
        return Err(CommandError::new(
            "invalid_provider_account_id",
            "Ce compte fournisseur n'est pas valide.",
        ));
    }
    tauri::async_runtime::spawn_blocking(move || {
        runtime::refresh_provider_finance(&provider_account_id)?;
        Ok(FinanceRefreshStatus {
            status: "refreshed",
        })
    })
    .await
    .map_err(|_| CommandError::new("finance_worker_failed", "La lecture du solde a echoue."))?
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(onboarding::BootstrapManager::default())
        .invoke_handler(tauri::generate_handler![
            get_catalogue,
            get_runtime_snapshot,
            simulate_cost,
            preview_candidates,
            refresh_provider_finance,
            get_bootstrap_status,
            run_bootstrap_preflight,
            run_bootstrap_corrective_preflight,
            run_bootstrap_recovery_preflight,
            begin_bootstrap,
            begin_corrective_bootstrap,
            begin_recovery_bootstrap,
            get_bootstrap_result,
            cancel_bootstrap,
            save_provider_credential,
            open_zulip
        ])
        .run(tauri::generate_context!())
        .expect("failed to run the model manager");
}
