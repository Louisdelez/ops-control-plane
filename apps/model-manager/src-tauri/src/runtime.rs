use crate::CommandError;
use serde::{de::DeserializeOwned, Deserialize, Serialize};
#[cfg(unix)]
use socket2::{Domain, SockAddr, Socket, Type};
use std::collections::BTreeMap;
use std::fs;
use std::io::{Read, Write};
#[cfg(unix)]
use std::os::fd::OwnedFd;
#[cfg(unix)]
use std::os::unix::fs::FileTypeExt;
#[cfg(unix)]
use std::os::unix::net::UnixStream;
use std::time::{Duration, Instant};

const ORCHESTRATOR_SOCKET: &str = "/run/ops-orchestrator/api.sock";
const MAX_HEADER_BYTES: usize = 16 * 1024;
const MAX_BODY_BYTES: usize = 512 * 1024;
const MAX_REQUEST_BYTES: usize = 16 * 1024;
const IO_TIMEOUT: Duration = Duration::from_secs(3);

#[derive(Debug, Serialize)]
pub struct RuntimeSnapshot {
    source: &'static str,
    overall_available: bool,
    #[cfg(feature="native-desktop")]
    native_service: Option<NativeService>,
    health: RuntimeSection<HealthSnapshot>,
    budgets: RuntimeSection<BudgetSnapshot>,
    performance: RuntimeSection<PerformanceSnapshot>,
    provider_integrations: RuntimeSection<ProviderIntegrationsSnapshot>,
}

#[derive(Debug, Serialize)]
struct RuntimeSection<T: Serialize> {
    available: bool,
    data: Option<T>,
    error_code: Option<&'static str>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct HealthSnapshot {
    status: String,
    database: String,
    mode: String,
    provider_availability_scope: String,
    provider_network_probe: bool,
    providers: Vec<HealthProvider>,
    limits: RuntimeLimits,
    catalogue: RuntimeCatalogue,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct HealthProvider {
    id: String,
    provider_account_id: Option<String>,
    role: String,
    location: String,
    available: bool,
    reason: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RuntimeLimits {
    max_iterations: u64,
    max_provider_calls: u64,
    max_context_tokens: u64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct RuntimeCatalogue {
    schema_version: u8,
    revision: String,
    cards: u64,
    provider_accounts: u64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct BudgetSnapshot {
    providers: Vec<ProviderBudget>,
    accounts: Vec<AccountBudget>,
    reservations: Vec<ReservationSummary>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ProviderBudget {
    provider: String,
    provider_account_id: Option<String>,
    role: String,
    location: String,
    usage: PeriodUsage,
    limits: ProviderBudgetLimits,
    remaining: PeriodLimits,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PeriodUsage {
    daily: UsageCounter,
    monthly: UsageCounter,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct UsageCounter {
    calls: u64,
    tokens: u64,
    cost: u64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ProviderBudgetLimits {
    daily: BudgetLimit,
    monthly: BudgetLimit,
    mission_cost_microusd: u64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct BudgetLimit {
    calls: u64,
    tokens: u64,
    cost_microusd: u64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PeriodLimits {
    daily: BudgetLimit,
    monthly: BudgetLimit,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct AccountBudget {
    provider_account_id: String,
    deployments: Vec<String>,
    usage: AccountPeriodUsage,
    summed_deployment_limits: PeriodLimits,
    hard_shared_cap: bool,
    enforcement_scope: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct AccountPeriodUsage {
    daily: AccountUsageCounter,
    monthly: AccountUsageCounter,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct AccountUsageCounter {
    calls: u64,
    tokens: u64,
    cost_microusd: u64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ReservationSummary {
    provider_id: String,
    status: String,
    calls: u64,
    tokens: u64,
    cost_microusd: u64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PerformanceSnapshot {
    schema_version: u8,
    adaptive_policy: AdaptivePolicy,
    provider_account_caps: Vec<PerformanceAccountCap>,
    validated_outcomes: Vec<ValidatedOutcome>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct AdaptivePolicy {
    algorithm_version: String,
    baseline_quality_milli: u64,
    prior_weight_units: u64,
    validation_weight_units: u64,
    attempt_weight_units: u64,
    minimum_validations: u64,
    minimum_attempts: u64,
    maximum_history_rows: u64,
    maximum_adjustment_ppm: u64,
    minimum_latency_samples: u64,
    maximum_latency_sample_ms: u64,
    latency_policy: BTreeMap<String, LatencyPolicyEntry>,
    catalogue_mutation: bool,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct LatencyPolicyEntry {
    target_ms: u64,
    maximum_adjustment_ppm: u64,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PerformanceAccountCap {
    provider_account_id: String,
    daily: BudgetLimit,
    monthly: BudgetLimit,
    task: BudgetLimit,
    hard_shared_cap: bool,
    atomicity: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ValidatedOutcome {
    task_type: String,
    provider_id: String,
    provider_account_id: Option<String>,
    model: String,
    validation_count: u64,
    successful_validations: u64,
    average_quality_milli: u64,
    average_corrections_required: u64,
    average_attempts: u64,
    average_uncertain_attempts: u64,
    average_cost_microusd: u64,
    average_duration_ms: u64,
    first_recorded_at: String,
    last_recorded_at: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ProviderIntegrationsSnapshot {
    schema_version: u8,
    revision: String,
    verified_on: String,
    provider_accounts: Vec<ProviderIntegrationAccount>,
    deployment_mappings: Vec<ProviderDeploymentMapping>,
    finance_cache: FinanceCache,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ProviderIntegrationAccount {
    id: String,
    display_name: String,
    inference: PublicInference,
    credentials: PublicCredentials,
    financial_capabilities: BTreeMap<String, String>,
    finance_endpoints: Vec<PublicFinanceEndpoint>,
    official_sources: Vec<OfficialSource>,
    limitations: Vec<String>,
    finance_state: FinanceState,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PublicInference {
    protocols: Vec<String>,
    sites: Vec<PublicInferenceSite>,
    model_discovery: PublicModelDiscovery,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PublicInferenceSite {
    id: String,
    region: String,
    base_url: Option<String>,
    status: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PublicModelDiscovery {
    mode: String,
    method: Option<String>,
    endpoint: Option<String>,
    operation: Option<String>,
    credential_scope: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PublicCredentials {
    inference: PublicInferenceCredential,
    admin_finance: PublicAdminCredential,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PublicInferenceCredential {
    auth_scheme: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PublicAdminCredential {
    auth_scheme: String,
    separate_credential_required: bool,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct PublicFinanceEndpoint {
    capabilities: Vec<String>,
    access: String,
    method: String,
    endpoint: String,
    operation: Option<String>,
    credential_scope: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct OfficialSource {
    title: String,
    url: String,
    verified_on: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct FinanceState {
    snapshot_id: Option<String>,
    provider_account_id: String,
    registry_revision: String,
    captured_at: Option<String>,
    cash_balance_mode: String,
    status: String,
    is_available: Option<bool>,
    balances: Vec<FinanceBalance>,
    error_code: Option<String>,
    duration_ms: Option<u64>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct FinanceBalance {
    currency: Option<String>,
    available: String,
    granted: Option<String>,
    topped_up: Option<String>,
    cash: Option<String>,
    voucher: Option<String>,
    total_cash: Option<String>,
    total_voucher: Option<String>,
    billing_type: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ProviderDeploymentMapping {
    deployment_id: String,
    card_id: String,
    developer_id: String,
    inference_provider_account_id: String,
    exact_model_id: String,
    activation_state: String,
    source: String,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct FinanceCache {
    persistence: String,
    maximum_history_rows_per_query: u64,
    maximum_snapshots_per_account: u64,
    currency_conversion: bool,
}

#[derive(Debug, Serialize)]
struct FinanceRefreshRequest<'a> {
    project_id: &'static str,
    provider_account_id: &'a str,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FinanceRefreshResponse {
    status: String,
    registry_revision: String,
    requested_account: String,
    results: Vec<FinanceState>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum RuntimeError {
    Unavailable,
    Timeout,
    AccessDenied,
    CatalogueMismatch,
    InvalidResponse,
    ResponseTooLarge,
}

impl RuntimeError {
    const fn code(self) -> &'static str {
        match self {
            Self::Unavailable => "socket_unavailable",
            Self::Timeout => "socket_timeout",
            Self::AccessDenied => "access_denied",
            Self::CatalogueMismatch => "catalogue_revision_mismatch",
            Self::InvalidResponse => "invalid_response",
            Self::ResponseTooLarge => "response_too_large",
        }
    }

    const fn command_error(self) -> CommandError {
        match self {
            Self::Unavailable => CommandError::new(
                "orchestrator_unavailable",
                "Le routeur local n'est pas disponible.",
            ),
            Self::Timeout => CommandError::new(
                "orchestrator_timeout",
                "Le routeur local n'a pas repondu a temps.",
            ),
            Self::AccessDenied => CommandError::new(
                "orchestrator_access_denied",
                "Le compte local n'est pas autorise pour ce projet.",
            ),
            Self::CatalogueMismatch => CommandError::new(
                "orchestrator_catalogue_mismatch",
                "Le routeur local et l'application n'utilisent pas le meme catalogue.",
            ),
            Self::InvalidResponse | Self::ResponseTooLarge => CommandError::new(
                "orchestrator_response_invalid",
                "Le routeur local a retourne une reponse invalide.",
            ),
        }
    }
}

pub fn get_runtime_snapshot(
    expected_revision: &str,
    expected_cards: u64,
    expected_provider_accounts: u64,
) -> RuntimeSnapshot {
    let health_result = get_json::<HealthSnapshot>("/v1/health").and_then(|value| {
        validate_health(
            &value,
            expected_revision,
            expected_cards,
            expected_provider_accounts,
        )?;
        Ok(value)
    });
    let budgets_result = match health_result.as_ref() {
        Ok(_) => get_json::<BudgetSnapshot>("/v1/budgets").and_then(|value| {
            validate_budgets(&value)?;
            Ok(value)
        }),
        Err(error) => Err(*error),
    };
    let performance_result = match health_result.as_ref() {
        Ok(_) => get_json::<PerformanceSnapshot>("/v1/model-performance").and_then(|value| {
            validate_performance(&value)?;
            Ok(value)
        }),
        Err(error) => Err(*error),
    };
    let provider_integrations_result = match health_result.as_ref() {
        Ok(_) => get_json::<ProviderIntegrationsSnapshot>("/v1/provider-integrations").and_then(
            |value| {
                validate_provider_integrations(&value, expected_provider_accounts)?;
                Ok(value)
            },
        ),
        Err(error) => Err(*error),
    };
    let health = section(health_result);
    let budgets = section(budgets_result);
    let performance = section(performance_result);
    let provider_integrations = section(provider_integrations_result);
    RuntimeSnapshot {
        #[cfg(feature="native-desktop")]
        native_service: if health.available {None} else {read_native_service()},
        source: "orchestrator_unix_socket",
        overall_available: health.available
            && budgets.available
            && performance.available
            && provider_integrations.available,
        health,
        budgets,
        performance,
        provider_integrations,
    }
}

// Compatibility projection for the native service API already installed on the
// workstation. It does not assert a match with Atlas catalogue deployments.
#[cfg(feature="native-desktop")]
#[derive(Debug, Deserialize, Serialize)]
struct NativeHealth {
    status: String,
    database: String,
    mode: String,
    providers: Vec<HealthProvider>,
    limits: RuntimeLimits,
}
#[cfg(feature="native-desktop")]
#[derive(Debug, Deserialize, Serialize)]
struct NativeBudget {
    provider: String,
    role: String,
    location: String,
    usage: PeriodUsage,
    limits: ProviderBudgetLimits,
}
#[cfg(feature="native-desktop")]
#[derive(Debug, Deserialize, Serialize)]
struct NativeBudgets {providers: Vec<NativeBudget>}
#[cfg(feature="native-desktop")]
#[derive(Debug, Serialize)]
struct NativeService {health: NativeHealth, budgets: Option<NativeBudgets>}
#[cfg(feature="native-desktop")]
fn read_native_service() -> Option<NativeService> {
    let mut health=get_json::<NativeHealth>("/v1/health").ok()?;
    if !bounded_text(&health.status,32) || !bounded_text(&health.database,32)
        || !bounded_text(&health.mode,64) || health.providers.len()>512
        || health.providers.iter().any(|p|!bounded_identifier(&p.id,128)||!bounded_text(&p.reason,256)||!bounded_text(&p.role,64)||!bounded_text(&p.location,64)) {return None;}
    // Inactive local-provider placeholders are not API options in this desktop.
    health.providers.retain(|p|p.location=="remote");
    let budgets=get_json::<NativeBudgets>("/v1/budgets").ok().and_then(|mut b| {
        if b.providers.len()>512 || b.providers.iter().any(|p|!bounded_identifier(&p.provider,128)||!bounded_text(&p.role,64)||!bounded_text(&p.location,64)) {return None;}
        b.providers.retain(|p|p.location=="remote");Some(b)
    });
    Some(NativeService {health,budgets})
}

pub(crate) fn post_preview<P, R>(payload: &P) -> Result<R, CommandError>
where
    P: Serialize,
    R: DeserializeOwned,
{
    let body = serde_json::to_vec(payload).map_err(|_| {
        CommandError::new(
            "preview_request_invalid",
            "La requete de previsualisation est invalide.",
        )
    })?;
    if body.len() > MAX_REQUEST_BYTES {
        return Err(CommandError::new(
            "preview_request_too_large",
            "La requete de previsualisation est trop grande.",
        ));
    }
    request_json("POST", "/v1/catalogue/route-preview", Some(&body))
        .map_err(RuntimeError::command_error)
}

pub(crate) fn refresh_provider_finance(provider_account_id: &str) -> Result<(), CommandError> {
    let payload = FinanceRefreshRequest {
        project_id: "infra-shared",
        provider_account_id,
    };
    let body = serde_json::to_vec(&payload).map_err(|_| {
        CommandError::new(
            "finance_request_invalid",
            "La demande de solde est invalide.",
        )
    })?;
    let response: FinanceRefreshResponse = request_json_with_timeout(
        "POST",
        "/v1/provider-finance/refresh",
        Some(&body),
        Duration::from_secs(12),
    )
    .map_err(RuntimeError::command_error)?;
    if response.status != "completed"
        || !bounded_identifier(&response.registry_revision, 128)
        || response.requested_account != provider_account_id
        || response.results.len() != 1
        || response.results.iter().any(|result| {
            result.provider_account_id != provider_account_id
                || result.registry_revision != response.registry_revision
                || !bounded_identifier(&result.status, 64)
                || result.balances.len() > 8
        })
    {
        return Err(RuntimeError::InvalidResponse.command_error());
    }
    Ok(())
}

fn section<T: Serialize>(result: Result<T, RuntimeError>) -> RuntimeSection<T> {
    match result {
        Ok(data) => RuntimeSection {
            available: true,
            data: Some(data),
            error_code: None,
        },
        Err(error) => RuntimeSection {
            available: false,
            data: None,
            error_code: Some(error.code()),
        },
    }
}

fn get_json<T: DeserializeOwned>(path: &'static str) -> Result<T, RuntimeError> {
    request_json("GET", path, None)
}

fn request_json<T: DeserializeOwned>(
    method: &'static str,
    path: &'static str,
    body: Option<&[u8]>,
) -> Result<T, RuntimeError> {
    request_json_with_timeout(method, path, body, IO_TIMEOUT)
}

fn request_json_with_timeout<T: DeserializeOwned>(
    method: &'static str,
    path: &'static str,
    body: Option<&[u8]>,
    timeout: Duration,
) -> Result<T, RuntimeError> {
    #[cfg(not(unix))]
    {
        let _ = (method, path, body, timeout);
        return Err(RuntimeError::Unavailable);
    }

    #[cfg(unix)]
    {
        verify_socket()?;
        let deadline = Instant::now() + timeout;
        let socket = Socket::new(Domain::UNIX, Type::STREAM, None).map_err(map_io_error)?;
        let address = SockAddr::unix(ORCHESTRATOR_SOCKET).map_err(map_io_error)?;
        socket
            .connect_timeout(&address, remaining_timeout(deadline)?)
            .map_err(map_io_error)?;
        let owned_fd: OwnedFd = socket.into();
        let mut stream = UnixStream::from(owned_fd);
        stream
            .set_read_timeout(Some(remaining_timeout(deadline)?))
            .map_err(map_io_error)?;
        stream
            .set_write_timeout(Some(remaining_timeout(deadline)?))
            .map_err(map_io_error)?;

        let request_head = if let Some(body) = body {
            format!(
                "{method} {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                body.len()
            )
        } else {
            format!(
                "{method} {path} HTTP/1.1\r\nHost: localhost\r\nAccept: application/json\r\nConnection: close\r\n\r\n"
            )
        };
        write_all_before(&mut stream, request_head.as_bytes(), deadline)?;
        if let Some(body) = body {
            write_all_before(&mut stream, body, deadline)?;
        }
        stream
            .set_write_timeout(Some(remaining_timeout(deadline)?))
            .map_err(map_io_error)?;
        stream.flush().map_err(map_io_error)?;
        let response = read_response_before(&mut stream, deadline)?;
        parse_http_json(&response)
    }
}

#[cfg(unix)]
fn remaining_timeout(deadline: Instant) -> Result<Duration, RuntimeError> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|remaining| !remaining.is_zero())
        .ok_or(RuntimeError::Timeout)
}

#[cfg(unix)]
fn write_all_before(
    stream: &mut UnixStream,
    mut bytes: &[u8],
    deadline: Instant,
) -> Result<(), RuntimeError> {
    while !bytes.is_empty() {
        stream
            .set_write_timeout(Some(remaining_timeout(deadline)?))
            .map_err(map_io_error)?;
        let written = stream.write(bytes).map_err(map_io_error)?;
        if written == 0 {
            return Err(RuntimeError::Unavailable);
        }
        bytes = &bytes[written..];
    }
    Ok(())
}

#[cfg(unix)]
fn read_response_before(
    stream: &mut UnixStream,
    deadline: Instant,
) -> Result<Vec<u8>, RuntimeError> {
    let total_limit = MAX_HEADER_BYTES + 4 + MAX_BODY_BYTES;
    let mut response = Vec::with_capacity(8 * 1024);
    let mut expected_length = None;
    loop {
        if let Some(expected) = expected_length {
            if response.len() == expected {
                return Ok(response);
            }
            if response.len() > expected {
                return Err(RuntimeError::InvalidResponse);
            }
        }
        if response.len() >= total_limit {
            return Err(RuntimeError::ResponseTooLarge);
        }
        let remaining_capacity = expected_length
            .map(|expected| expected - response.len())
            .unwrap_or(total_limit - response.len())
            .min(8 * 1024);
        let mut buffer = [0_u8; 8 * 1024];
        stream
            .set_read_timeout(Some(remaining_timeout(deadline)?))
            .map_err(map_io_error)?;
        let read = stream
            .read(&mut buffer[..remaining_capacity])
            .map_err(map_io_error)?;
        if read == 0 {
            return Err(RuntimeError::InvalidResponse);
        }
        response.extend_from_slice(&buffer[..read]);
        if expected_length.is_none() {
            expected_length = framed_response_length(&response)?;
        }
    }
}

fn framed_response_length(response: &[u8]) -> Result<Option<usize>, RuntimeError> {
    let Some(separator) = response.windows(4).position(|window| window == b"\r\n\r\n") else {
        if response.len() > MAX_HEADER_BYTES {
            return Err(RuntimeError::ResponseTooLarge);
        }
        return Ok(None);
    };
    if separator > MAX_HEADER_BYTES {
        return Err(RuntimeError::ResponseTooLarge);
    }
    let header =
        std::str::from_utf8(&response[..separator]).map_err(|_| RuntimeError::InvalidResponse)?;
    let mut content_length = None;
    for line in header.split("\r\n").skip(1) {
        let (name, value) = line.split_once(':').ok_or(RuntimeError::InvalidResponse)?;
        if name.trim().eq_ignore_ascii_case("content-length") {
            if content_length.is_some()
                || value.trim().is_empty()
                || !value.trim().bytes().all(|byte| byte.is_ascii_digit())
            {
                return Err(RuntimeError::InvalidResponse);
            }
            content_length = value.trim().parse::<usize>().ok();
        }
    }
    let body_length = content_length.ok_or(RuntimeError::InvalidResponse)?;
    if body_length > MAX_BODY_BYTES {
        return Err(RuntimeError::ResponseTooLarge);
    }
    separator
        .checked_add(4)
        .and_then(|header_length| header_length.checked_add(body_length))
        .map(Some)
        .ok_or(RuntimeError::ResponseTooLarge)
}

#[cfg(unix)]
fn verify_socket() -> Result<(), RuntimeError> {
    let metadata = fs::symlink_metadata(ORCHESTRATOR_SOCKET).map_err(map_io_error)?;
    if !metadata.file_type().is_socket() || metadata.file_type().is_symlink() {
        return Err(RuntimeError::Unavailable);
    }
    Ok(())
}

fn parse_http_json<T: DeserializeOwned>(response: &[u8]) -> Result<T, RuntimeError> {
    let separator = response
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .ok_or(RuntimeError::InvalidResponse)?;
    if separator > MAX_HEADER_BYTES {
        return Err(RuntimeError::ResponseTooLarge);
    }
    let header =
        std::str::from_utf8(&response[..separator]).map_err(|_| RuntimeError::InvalidResponse)?;
    let body = &response[separator + 4..];
    if body.len() > MAX_BODY_BYTES {
        return Err(RuntimeError::ResponseTooLarge);
    }

    let mut lines = header.split("\r\n");
    let status_line = lines.next().ok_or(RuntimeError::InvalidResponse)?;
    let mut status_parts = status_line.split_ascii_whitespace();
    if status_parts.next() != Some("HTTP/1.1") {
        return Err(RuntimeError::InvalidResponse);
    }
    let status = status_parts
        .next()
        .and_then(|value| value.parse::<u16>().ok())
        .ok_or(RuntimeError::InvalidResponse)?;
    if status == 403 {
        return Err(RuntimeError::AccessDenied);
    }
    if status != 200 {
        return Err(RuntimeError::InvalidResponse);
    }

    let mut content_length = None;
    let mut json_content_type = None;
    for line in lines {
        let (name, value) = line.split_once(':').ok_or(RuntimeError::InvalidResponse)?;
        let name = name.trim().to_ascii_lowercase();
        let value = value.trim();
        if name == "transfer-encoding" {
            return Err(RuntimeError::InvalidResponse);
        }
        if name == "content-length" {
            if content_length.is_some()
                || value.is_empty()
                || !value.bytes().all(|byte| byte.is_ascii_digit())
            {
                return Err(RuntimeError::InvalidResponse);
            }
            content_length = value.parse::<usize>().ok();
        } else if name == "content-type" {
            if json_content_type.is_some() {
                return Err(RuntimeError::InvalidResponse);
            }
            json_content_type = Some(
                value
                    .split(';')
                    .next()
                    .is_some_and(|kind| kind.trim().eq_ignore_ascii_case("application/json")),
            );
        }
    }
    if content_length != Some(body.len()) || json_content_type != Some(true) {
        return Err(RuntimeError::InvalidResponse);
    }
    serde_json::from_slice(body).map_err(|_| RuntimeError::InvalidResponse)
}

#[cfg(unix)]
fn map_io_error(error: std::io::Error) -> RuntimeError {
    match error.kind() {
        std::io::ErrorKind::PermissionDenied => RuntimeError::AccessDenied,
        std::io::ErrorKind::TimedOut | std::io::ErrorKind::WouldBlock => RuntimeError::Timeout,
        _ => RuntimeError::Unavailable,
    }
}

fn validate_health(
    snapshot: &HealthSnapshot,
    expected_revision: &str,
    expected_cards: u64,
    expected_provider_accounts: u64,
) -> Result<(), RuntimeError> {
    if snapshot.providers.len() > 512
        || snapshot.status.len() > 32
        || snapshot.database.len() > 32
        || snapshot.mode.len() > 64
        || !bounded_identifier(&snapshot.catalogue.revision, 128)
        || snapshot.catalogue.cards > 10_000
        || snapshot.catalogue.provider_accounts > 256
        || snapshot.providers.iter().any(|provider| {
            !bounded_identifier(&provider.id, 128)
                || provider
                    .provider_account_id
                    .as_deref()
                    .is_some_and(|value| !bounded_identifier(value, 128))
                || !bounded_text(&provider.role, 64)
                || !bounded_text(&provider.location, 64)
                || !bounded_text(&provider.reason, 256)
        })
    {
        return Err(RuntimeError::InvalidResponse);
    }
    if snapshot.catalogue.schema_version != 2
        || snapshot.catalogue.revision != expected_revision
        || snapshot.catalogue.cards != expected_cards
        || snapshot.catalogue.provider_accounts != expected_provider_accounts
    {
        return Err(RuntimeError::CatalogueMismatch);
    }
    Ok(())
}

fn validate_budgets(snapshot: &BudgetSnapshot) -> Result<(), RuntimeError> {
    if snapshot.providers.len() > 512
        || snapshot.accounts.len() > 256
        || snapshot.reservations.len() > 2_048
        || snapshot.providers.iter().any(|provider| {
            !bounded_identifier(&provider.provider, 128)
                || provider
                    .provider_account_id
                    .as_deref()
                    .is_some_and(|value| !bounded_identifier(value, 128))
                || !bounded_text(&provider.role, 64)
                || !bounded_text(&provider.location, 64)
        })
        || snapshot.accounts.iter().any(|account| {
            !bounded_identifier(&account.provider_account_id, 128)
                || account.deployments.len() > 64
                || account
                    .deployments
                    .iter()
                    .any(|value| !bounded_identifier(value, 128))
                || !bounded_text(&account.enforcement_scope, 64)
        })
        || snapshot.reservations.iter().any(|reservation| {
            !bounded_identifier(&reservation.provider_id, 128)
                || !bounded_text(&reservation.status, 64)
        })
    {
        return Err(RuntimeError::InvalidResponse);
    }
    Ok(())
}

fn validate_performance(snapshot: &PerformanceSnapshot) -> Result<(), RuntimeError> {
    let policy = &snapshot.adaptive_policy;
    const URGENCY_LEVELS: [&str; 4] = ["routine", "normal", "urgent", "emergency"];
    if snapshot.schema_version != 1
        || snapshot.provider_account_caps.len() > 256
        || snapshot.validated_outcomes.len() > 200
        || !bounded_identifier(&policy.algorithm_version, 128)
        || policy.baseline_quality_milli > 1_000
        || policy.maximum_adjustment_ppm > 1_000_000
        || policy.maximum_history_rows > 100_000
        || !(1..=1_000).contains(&policy.minimum_latency_samples)
        || !(1..=3_600_000).contains(&policy.maximum_latency_sample_ms)
        || policy.latency_policy.len() != URGENCY_LEVELS.len()
        || URGENCY_LEVELS
            .iter()
            .any(|urgency| !policy.latency_policy.contains_key(*urgency))
        || policy.latency_policy.iter().any(|(urgency, entry)| {
            !URGENCY_LEVELS.contains(&urgency.as_str())
                || entry.target_ms == 0
                || entry.target_ms > policy.maximum_latency_sample_ms
                || entry.maximum_adjustment_ppm > policy.maximum_adjustment_ppm
        })
        || policy.catalogue_mutation
        || snapshot.provider_account_caps.iter().any(|account| {
            !bounded_identifier(&account.provider_account_id, 128)
                || !account.hard_shared_cap
                || !bounded_identifier(&account.atomicity, 64)
        })
        || snapshot.validated_outcomes.iter().any(|outcome| {
            !bounded_identifier(&outcome.task_type, 64)
                || !bounded_identifier(&outcome.provider_id, 128)
                || outcome
                    .provider_account_id
                    .as_deref()
                    .is_some_and(|value| !bounded_identifier(value, 128))
                || !bounded_text(&outcome.model, 200)
                || outcome.successful_validations > outcome.validation_count
                || outcome.average_quality_milli > 1_000
                || !bounded_text(&outcome.first_recorded_at, 64)
                || !bounded_text(&outcome.last_recorded_at, 64)
        })
    {
        return Err(RuntimeError::InvalidResponse);
    }
    Ok(())
}

fn validate_provider_integrations(
    snapshot: &ProviderIntegrationsSnapshot,
    expected_provider_accounts: u64,
) -> Result<(), RuntimeError> {
    const CAPABILITIES: [&str; 6] = [
        "cash_balance",
        "plan_quota",
        "usage",
        "cost",
        "rate_limits",
        "spending_limit",
    ];
    const MODES: [&str; 6] = [
        "direct_api",
        "admin_api",
        "cloud_billing_api",
        "console_only",
        "not_applicable",
        "unknown",
    ];
    let expected_count =
        usize::try_from(expected_provider_accounts).map_err(|_| RuntimeError::InvalidResponse)?;
    if snapshot.schema_version != 1
        || snapshot.provider_accounts.len() != expected_count
        || snapshot.provider_accounts.len() > 256
        || snapshot.deployment_mappings.len() > 1_024
        || !bounded_identifier(&snapshot.revision, 128)
        || !bounded_text(&snapshot.verified_on, 32)
        || snapshot.finance_cache.persistence != "sqlite_append_only"
        || snapshot.finance_cache.maximum_history_rows_per_query == 0
        || snapshot.finance_cache.maximum_history_rows_per_query > 200
        || snapshot.finance_cache.maximum_snapshots_per_account == 0
        || snapshot.finance_cache.maximum_snapshots_per_account > 50_000
        || snapshot.finance_cache.currency_conversion
    {
        return Err(RuntimeError::InvalidResponse);
    }

    let mut account_ids = std::collections::BTreeSet::new();
    for account in &snapshot.provider_accounts {
        if !bounded_identifier(&account.id, 128)
            || !bounded_text(&account.display_name, 160)
            || !account_ids.insert(account.id.as_str())
            || account.inference.protocols.is_empty()
            || account.inference.protocols.len() > 8
            || account.inference.sites.is_empty()
            || account.inference.sites.len() > 16
            || account.finance_endpoints.len() > 16
            || account.official_sources.is_empty()
            || account.official_sources.len() > 16
            || account.limitations.is_empty()
            || account.limitations.len() > 16
            || account.financial_capabilities.len() != CAPABILITIES.len()
            || CAPABILITIES.iter().any(|name| {
                account
                    .financial_capabilities
                    .get(*name)
                    .is_none_or(|mode| !MODES.contains(&mode.as_str()))
            })
            || !bounded_identifier(&account.credentials.inference.auth_scheme, 64)
            || !bounded_identifier(&account.credentials.admin_finance.auth_scheme, 64)
            || !bounded_identifier(&account.inference.model_discovery.mode, 64)
            || !bounded_identifier(&account.inference.model_discovery.credential_scope, 64)
            || account
                .inference
                .model_discovery
                .method
                .as_deref()
                .is_some_and(|value| !matches!(value, "GET" | "POST"))
            || account
                .inference
                .model_discovery
                .endpoint
                .as_deref()
                .is_some_and(|value| !bounded_https_url(value))
            || account
                .inference
                .model_discovery
                .operation
                .as_deref()
                .is_some_and(|value| !bounded_text(value, 160))
        {
            return Err(RuntimeError::InvalidResponse);
        }
        for site in &account.inference.sites {
            if !bounded_identifier(&site.id, 128)
                || !bounded_text(&site.region, 128)
                || site
                    .base_url
                    .as_deref()
                    .is_some_and(|value| !bounded_https_url(value))
                || !bounded_identifier(&site.status, 64)
            {
                return Err(RuntimeError::InvalidResponse);
            }
        }
        for endpoint in &account.finance_endpoints {
            if endpoint.capabilities.is_empty()
                || endpoint.capabilities.len() > CAPABILITIES.len()
                || endpoint
                    .capabilities
                    .iter()
                    .any(|value| !CAPABILITIES.contains(&value.as_str()))
                || !matches!(
                    endpoint.access.as_str(),
                    "direct_api" | "admin_api" | "cloud_billing_api"
                )
                || !matches!(endpoint.method.as_str(), "GET" | "POST")
                || !bounded_https_url(&endpoint.endpoint)
                || endpoint
                    .operation
                    .as_deref()
                    .is_some_and(|value| !bounded_text(value, 160))
                || !matches!(
                    endpoint.credential_scope.as_str(),
                    "inference" | "admin_finance"
                )
            {
                return Err(RuntimeError::InvalidResponse);
            }
        }
        if account.official_sources.iter().any(|source| {
            !bounded_text(&source.title, 160)
                || !bounded_https_url(&source.url)
                || !bounded_text(&source.verified_on, 32)
        }) || account
            .limitations
            .iter()
            .any(|value| !bounded_text(value, 512))
        {
            return Err(RuntimeError::InvalidResponse);
        }
        let finance = &account.finance_state;
        if finance.provider_account_id != account.id
            || finance.registry_revision != snapshot.revision
            || finance.cash_balance_mode
                != *account
                    .financial_capabilities
                    .get("cash_balance")
                    .ok_or(RuntimeError::InvalidResponse)?
            || !matches!(
                finance.status.as_str(),
                "ok" | "credential_unavailable"
                    | "provider_unavailable"
                    | "unsupported_schema"
                    | "requires_admin_credential"
                    | "console_only"
                    | "unsupported"
                    | "never_refreshed"
            )
            || finance.balances.len() > 8
            || finance
                .snapshot_id
                .as_deref()
                .is_some_and(|value| !bounded_identifier(value, 128))
            || finance
                .captured_at
                .as_deref()
                .is_some_and(|value| !bounded_text(value, 64))
            || finance
                .error_code
                .as_deref()
                .is_some_and(|value| !bounded_identifier(value, 64))
            || finance.duration_ms.is_some_and(|value| value > 60_000)
            || finance.balances.iter().any(|balance| {
                balance.currency.as_deref().is_some_and(|value| {
                    value.len() != 3 || !value.bytes().all(|byte| byte.is_ascii_uppercase())
                }) || !bounded_amount(&balance.available)
                    || [
                        balance.granted.as_deref(),
                        balance.topped_up.as_deref(),
                        balance.cash.as_deref(),
                        balance.voucher.as_deref(),
                        balance.total_cash.as_deref(),
                        balance.total_voucher.as_deref(),
                    ]
                    .into_iter()
                    .flatten()
                    .any(|value| !bounded_amount(value))
                    || balance
                        .billing_type
                        .as_deref()
                        .is_some_and(|value| !matches!(value, "prepaid" | "postpaid"))
            })
        {
            return Err(RuntimeError::InvalidResponse);
        }
        if finance.status == "ok" {
            if finance.snapshot_id.is_none()
                || finance.captured_at.is_none()
                || finance.duration_ms.is_none()
                || finance.error_code.is_some()
                || finance.balances.is_empty()
            {
                return Err(RuntimeError::InvalidResponse);
            }
        } else if (finance.status.ends_with("unavailable")
            || matches!(
                finance.status.as_str(),
                "provider_unavailable" | "unsupported_schema"
            ))
            && (finance.snapshot_id.is_none()
                || finance.captured_at.is_none()
                || finance.duration_ms.is_none()
                || finance.is_available.is_some()
                || finance.error_code.is_none()
                || !finance.balances.is_empty())
        {
            return Err(RuntimeError::InvalidResponse);
        }
    }

    let mut deployment_ids = std::collections::BTreeSet::new();
    if snapshot.deployment_mappings.iter().any(|mapping| {
        !bounded_identifier(&mapping.deployment_id, 128)
            || !deployment_ids.insert(mapping.deployment_id.as_str())
            || !bounded_identifier(&mapping.card_id, 128)
            || !bounded_identifier(&mapping.developer_id, 128)
            || !account_ids.contains(mapping.inference_provider_account_id.as_str())
            || !bounded_text(&mapping.exact_model_id, 200)
            || !matches!(
                mapping.activation_state.as_str(),
                "configured_not_network_verified" | "canary_verified" | "disabled"
            )
            || mapping.source != "local_orchestrator_configuration"
    }) {
        return Err(RuntimeError::InvalidResponse);
    }
    Ok(())
}

fn bounded_https_url(value: &str) -> bool {
    value.starts_with("https://")
        && value.len() <= 1_024
        && !value
            .bytes()
            .any(|byte| matches!(byte, b'\0' | b'\r' | b'\n' | b'\\'))
}

fn bounded_amount(value: &str) -> bool {
    if value.is_empty() || value.len() > 32 || value.starts_with('-') {
        return false;
    }
    let mut parts = value.split('.');
    let integer = parts.next().unwrap_or_default();
    let fraction = parts.next();
    !integer.is_empty()
        && integer.len() <= 15
        && integer.bytes().all(|byte| byte.is_ascii_digit())
        && (integer == "0" || !integer.starts_with('0'))
        && fraction.is_none_or(|digits| {
            !digits.is_empty()
                && digits.len() <= 12
                && digits.bytes().all(|byte| byte.is_ascii_digit())
        })
        && parts.next().is_none()
}

fn bounded_identifier(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.' | b':'))
}

fn bounded_text(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && !value
            .bytes()
            .any(|byte| matches!(byte, b'\0' | b'\r' | b'\n'))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Value};

    #[test]
    fn parses_a_bounded_json_response() {
        let response = b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{\"ok\":true}";
        let parsed: BTreeMap<String, bool> = parse_http_json(response).expect("valid response");
        assert_eq!(parsed.get("ok"), Some(&true));
    }

    #[test]
    fn rejects_chunked_or_ambiguous_responses() {
        let chunked = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n";
        assert!(parse_http_json::<serde_json::Value>(chunked).is_err());
        let duplicate = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}";
        assert!(parse_http_json::<serde_json::Value>(duplicate).is_err());
    }

    #[test]
    fn rejects_a_body_length_mismatch() {
        let response =
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 999\r\n\r\n{}";
        assert!(parse_http_json::<serde_json::Value>(response).is_err());
    }

    #[test]
    fn frames_a_response_without_waiting_for_connection_close() {
        let response =
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}";
        assert_eq!(framed_response_length(response), Ok(Some(response.len())));
    }

    #[test]
    fn rejects_a_runtime_with_a_different_embedded_catalogue() {
        let snapshot = HealthSnapshot {
            status: "ok".to_owned(),
            database: "ok".to_owned(),
            mode: "api-first".to_owned(),
            provider_availability_scope: "configuration-and-secret-only".to_owned(),
            provider_network_probe: false,
            providers: Vec::new(),
            limits: RuntimeLimits {
                max_iterations: 4,
                max_provider_calls: 5,
                max_context_tokens: 4096,
            },
            catalogue: RuntimeCatalogue {
                schema_version: 2,
                revision: "old-revision".to_owned(),
                cards: 58,
                provider_accounts: 21,
            },
        };
        assert!(matches!(
            validate_health(&snapshot, "current-revision", 58, 21),
            Err(RuntimeError::CatalogueMismatch)
        ));
    }

    #[test]
    fn accepts_only_the_bounded_latency_policy_contract() {
        let latency_policy = BTreeMap::from([
            (
                "routine".to_owned(),
                LatencyPolicyEntry {
                    target_ms: 12_000,
                    maximum_adjustment_ppm: 25_000,
                },
            ),
            (
                "normal".to_owned(),
                LatencyPolicyEntry {
                    target_ms: 8_000,
                    maximum_adjustment_ppm: 50_000,
                },
            ),
            (
                "urgent".to_owned(),
                LatencyPolicyEntry {
                    target_ms: 4_000,
                    maximum_adjustment_ppm: 100_000,
                },
            ),
            (
                "emergency".to_owned(),
                LatencyPolicyEntry {
                    target_ms: 2_000,
                    maximum_adjustment_ppm: 150_000,
                },
            ),
        ]);
        let mut snapshot = PerformanceSnapshot {
            schema_version: 1,
            adaptive_policy: AdaptivePolicy {
                algorithm_version: "adaptive-quality-latency-v2".to_owned(),
                baseline_quality_milli: 750,
                prior_weight_units: 48,
                validation_weight_units: 4,
                attempt_weight_units: 1,
                minimum_validations: 5,
                minimum_attempts: 8,
                maximum_history_rows: 500,
                maximum_adjustment_ppm: 150_000,
                minimum_latency_samples: 5,
                maximum_latency_sample_ms: 300_000,
                latency_policy,
                catalogue_mutation: false,
            },
            provider_account_caps: Vec::new(),
            validated_outcomes: Vec::new(),
        };
        assert!(validate_performance(&snapshot).is_ok());
        snapshot
            .adaptive_policy
            .latency_policy
            .get_mut("emergency")
            .expect("emergency policy")
            .maximum_adjustment_ppm = 150_001;
        assert!(matches!(
            validate_performance(&snapshot),
            Err(RuntimeError::InvalidResponse)
        ));
    }

    #[test]
    fn accepts_the_reviewed_public_provider_registry_contract() {
        let mut source: Value = serde_json::from_str(include_str!(
            "../../../../catalog/provider-integrations.v1.json"
        ))
        .expect("reviewed provider registry");
        let root = source.as_object_mut().expect("registry object");
        root.remove("finance_capability_names");
        root.remove("finance_access_modes");
        let revision = root
            .get("revision")
            .and_then(Value::as_str)
            .expect("registry revision")
            .to_owned();
        let accounts = root
            .get_mut("provider_accounts")
            .and_then(Value::as_array_mut)
            .expect("provider accounts");
        for account in accounts {
            let account = account.as_object_mut().expect("provider account");
            let account_id = account
                .get("id")
                .and_then(Value::as_str)
                .expect("provider account id")
                .to_owned();
            let credentials = account
                .get("credentials")
                .and_then(Value::as_object)
                .expect("credentials");
            let inference_scheme = credentials["inference"]["auth_scheme"]
                .as_str()
                .expect("inference auth scheme")
                .to_owned();
            let admin_scheme = credentials["admin_finance"]["auth_scheme"]
                .as_str()
                .expect("admin auth scheme")
                .to_owned();
            let separate_admin = !credentials["admin_finance"]["credential_ref"].is_null();
            account.insert(
                "credentials".to_owned(),
                json!({
                    "inference": {"auth_scheme": inference_scheme},
                    "admin_finance": {
                        "auth_scheme": admin_scheme,
                        "separate_credential_required": separate_admin
                    }
                }),
            );
            let cash_mode = account["financial_capabilities"]["cash_balance"]
                .as_str()
                .expect("cash balance mode")
                .to_owned();
            let status = match cash_mode.as_str() {
                "admin_api" | "cloud_billing_api" => "requires_admin_credential",
                "console_only" => "console_only",
                "not_applicable" | "unknown" => "unsupported",
                "direct_api"
                    if matches!(account_id.as_str(), "deepseek" | "moonshot" | "stepfun") =>
                {
                    "never_refreshed"
                }
                "direct_api" => "unsupported_schema",
                _ => panic!("unreviewed cash balance mode"),
            };
            account.insert(
                "finance_state".to_owned(),
                json!({
                    "snapshot_id": null,
                    "provider_account_id": account_id,
                    "registry_revision": revision,
                    "captured_at": null,
                    "cash_balance_mode": cash_mode,
                    "status": status,
                    "is_available": null,
                    "balances": [],
                    "error_code": null,
                    "duration_ms": null
                }),
            );
        }
        root.insert(
            "finance_cache".to_owned(),
            json!({
                "persistence": "sqlite_append_only",
                "maximum_history_rows_per_query": 200,
                "maximum_snapshots_per_account": 50_000,
                "currency_conversion": false
            }),
        );

        let snapshot: ProviderIntegrationsSnapshot =
            serde_json::from_value(source).expect("public provider registry contract");
        validate_provider_integrations(&snapshot, 21).expect("valid public registry");
    }
}
