use crate::{runtime, CommandError};
use serde::{Deserialize, Serialize};
use std::cmp::Ordering;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::OnceLock;

const EMBEDDED_CATALOGUE: &str = include_str!("../../../../catalog/model-catalog.v2.json");
const MAX_TOKENS_PER_SIMULATION: u64 = 1_000_000_000_000;
const MAX_PREVIEW_CANDIDATES: usize = 50;
const MICRO_USD_PER_USD: u64 = 1_000_000;
static PARSED_CATALOGUE: OnceLock<Result<Catalogue, CommandError>> = OnceLock::new();

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Catalogue {
    schema_version: u8,
    revision: String,
    generated_at: String,
    source: CatalogueSource,
    normalization: Normalization,
    comparison_profile: ComparisonProfile,
    capability_dimensions: Vec<String>,
    capability_profiles: BTreeMap<String, BTreeMap<String, u8>>,
    capability_provenance: CapabilityProvenance,
    model_id_provenance: BTreeMap<String, ModelIdProvenance>,
    provider_accounts: Vec<ProviderAccount>,
    cards: Vec<ModelCard>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct CatalogueSource {
    label: String,
    sha256: String,
    updated_on: String,
    entry_count: u32,
    priced_entry_count: u32,
    variable_entry_count: u32,
    provider_label_count: u32,
    scope: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Normalization {
    card_count: u32,
    priced_card_count: u32,
    quarantined_family_count: u32,
    operations: Vec<NormalizationOperation>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct NormalizationOperation {
    kind: String,
    source_lines: Vec<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    card_id: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    card_ids: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    note: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ComparisonProfile {
    id: String,
    calls: u64,
    input_tokens_per_call: u64,
    output_tokens_per_call: u64,
    input_tokens_total: u64,
    output_tokens_total: u64,
    cache_hit_percent: u8,
    batch: bool,
    currency: String,
    taxes_included: bool,
    price_policy: String,
    note: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct CapabilityProvenance {
    kind: String,
    confidence: String,
    sample_count: u64,
    warning: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ModelIdProvenance {
    exact_model_id: String,
    confidence: String,
    source: String,
    verified_on: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ProviderAccount {
    id: String,
    name: String,
    country: String,
    credential_ref: String,
    balance_mode: String,
    financial_capabilities: FinancialCapabilities,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct FinancialCapabilities {
    cash_balance: String,
    plan_quota: String,
    usage: String,
    cost: String,
    rate_limits: String,
    spending_limit: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct ModelCard {
    card_id: String,
    display_name: String,
    entity_kind: String,
    provider_account_id: String,
    developer: String,
    country: String,
    description: String,
    profile: String,
    specialties: Vec<String>,
    integration_stage: String,
    deployment_ids: Vec<String>,
    exact_model_id: Option<String>,
    context_window_tokens: Option<u64>,
    languages: Vec<String>,
    limitations: Vec<String>,
    latency_p50_ms: Option<u64>,
    latency_p95_ms: Option<u64>,
    technical_metadata_confidence: String,
    technical_metadata_source: String,
    source_lines: Vec<u32>,
    pricing: Vec<PriceSchedule>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Eq, Serialize)]
#[serde(deny_unknown_fields)]
struct PriceSchedule {
    mode: String,
    region_scope: String,
    input_usd_per_million: Option<String>,
    output_usd_per_million: Option<String>,
    cached_input_usd_per_million: Option<String>,
    declared_simulation_usd: String,
    confidence: String,
    source: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SimulateCostRequest {
    card_id: String,
    #[serde(default)]
    pricing_mode: Option<String>,
    input_tokens: u64,
    output_tokens: u64,
}

#[derive(Debug, Serialize)]
pub struct SimulateCostResponse {
    card_id: String,
    pricing_mode: Option<String>,
    input_tokens: u64,
    output_tokens: u64,
    available: bool,
    total_microusd: Option<u64>,
    declared_reference_microusd: Option<u64>,
    basis: Option<&'static str>,
    unavailable_reason: Option<&'static str>,
    currency: &'static str,
    rounding: &'static str,
}

#[derive(Clone, Copy, Debug, Default, Deserialize)]
#[serde(rename_all = "snake_case")]
enum PreviewSource {
    #[default]
    Local,
    Runtime,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PreviewCandidatesRequest {
    #[serde(default)]
    source: PreviewSource,
    #[serde(default)]
    project_id: Option<String>,
    #[serde(default)]
    requirements: BTreeMap<String, u8>,
    #[serde(default)]
    required_specialties: Vec<String>,
    #[serde(default)]
    maximum_monthly_cost_microusd: Option<u64>,
    #[serde(default)]
    runtime_only: bool,
    #[serde(default = "default_preview_limit")]
    limit: u8,
}

const fn default_preview_limit() -> u8 {
    10
}

#[derive(Clone, Debug, Serialize)]
pub struct PreviewCandidate {
    card_id: String,
    display_name: String,
    provider_account_id: String,
    integration_stage: String,
    capabilities: BTreeMap<String, u8>,
    conservative_monthly_cost_microusd: u64,
    capability_margin: u16,
    cost_basis: String,
    selected_deployment_id: Option<String>,
    runtime_available: Option<bool>,
}

#[derive(Debug, Serialize)]
pub struct PreviewCandidatesResponse {
    catalogue_revision: String,
    source: &'static str,
    policy: &'static str,
    runtime_only: bool,
    requirements: BTreeMap<String, u8>,
    required_specialties: Vec<String>,
    candidate_count: usize,
    excluded: BTreeMap<String, u64>,
    candidates: Vec<PreviewCandidate>,
    advisory_only: bool,
}

#[derive(Debug, Serialize)]
struct RemotePreviewRequest {
    project_id: String,
    requirements: BTreeMap<String, u8>,
    required_specialties: Vec<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    maximum_monthly_cost_usd: Option<String>,
    runtime_only: bool,
    limit: u8,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RemotePreviewResponse {
    catalogue_revision: String,
    project_id: String,
    policy: String,
    runtime_only: bool,
    requirements: BTreeMap<String, u8>,
    required_specialties: Vec<String>,
    candidate_count: usize,
    excluded: BTreeMap<String, u64>,
    candidates: Vec<RemoteCandidate>,
    advisory_only: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RemoteCandidate {
    card_id: String,
    display_name: String,
    entity_kind: String,
    provider_account_id: String,
    developer: String,
    country: String,
    description: String,
    profile: String,
    specialties: Vec<String>,
    integration_stage: String,
    deployment_ids: Vec<String>,
    exact_model_id: Option<String>,
    context_window_tokens: Option<u64>,
    languages: Vec<String>,
    limitations: Vec<String>,
    latency_p50_ms: Option<u64>,
    latency_p95_ms: Option<u64>,
    technical_metadata_confidence: String,
    technical_metadata_source: String,
    source_lines: Vec<u32>,
    pricing: Vec<PriceSchedule>,
    capabilities: BTreeMap<String, u8>,
    simulations: Vec<RemoteSimulation>,
    runtime: RemoteCardRuntime,
    routing_preview: RemoteRoutingPreview,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RemoteSimulation {
    mode: String,
    cost_microusd: u64,
    basis: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RemoteCardRuntime {
    deployments: Vec<RemoteDeployment>,
    availability_scope: String,
    available: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RemoteDeployment {
    deployment_id: String,
    provider_account_id: Option<String>,
    role: String,
    model: Option<String>,
    available: bool,
    reason: String,
    comparison_cost_microusd: Option<u64>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RemoteRoutingPreview {
    conservative_monthly_cost_microusd: u64,
    capability_margin: u16,
    cost_basis: String,
    selected_deployment_id: Option<String>,
    selected_provider_account_id: String,
    not_an_activation_authorization: bool,
}

pub fn load_catalogue() -> Result<Catalogue, CommandError> {
    PARSED_CATALOGUE.get_or_init(parse_catalogue).clone()
}

fn parse_catalogue() -> Result<Catalogue, CommandError> {
    if EMBEDDED_CATALOGUE.len() > 1_048_576 {
        return Err(CommandError::new(
            "catalogue_too_large",
            "Le catalogue embarque depasse la limite autorisee.",
        ));
    }
    let catalogue: Catalogue = serde_json::from_str(EMBEDDED_CATALOGUE).map_err(|_| {
        CommandError::new(
            "catalogue_invalid",
            "Le catalogue embarque ne respecte pas son schema.",
        )
    })?;
    validate_catalogue(&catalogue)?;
    Ok(catalogue)
}

pub(crate) fn provider_exists(provider_account_id: &str) -> bool {
    load_catalogue()
        .map(|catalogue| {
            catalogue
                .provider_accounts
                .iter()
                .any(|provider| provider.id == provider_account_id)
        })
        .unwrap_or(false)
}

pub(crate) fn runtime_identity(catalogue: &Catalogue) -> (String, u64, u64) {
    (
        catalogue.revision.clone(),
        catalogue.cards.len() as u64,
        catalogue.provider_accounts.len() as u64,
    )
}

pub fn simulate_cost(request: SimulateCostRequest) -> Result<SimulateCostResponse, CommandError> {
    validate_token_counts(request.input_tokens, request.output_tokens)?;
    if !is_identifier(&request.card_id) {
        return Err(CommandError::new(
            "invalid_card_id",
            "L'identifiant du modele est invalide.",
        ));
    }
    if request
        .pricing_mode
        .as_deref()
        .is_some_and(|value| !is_identifier(value))
    {
        return Err(CommandError::new(
            "invalid_pricing_mode",
            "Le mode tarifaire est invalide.",
        ));
    }

    let catalogue = load_catalogue()?;
    let card = catalogue
        .cards
        .iter()
        .find(|card| card.card_id == request.card_id)
        .ok_or_else(|| {
            CommandError::new("unknown_card", "Ce modele n'existe pas dans le catalogue.")
        })?;
    let schedule = select_schedule(card, request.pricing_mode.as_deref());

    let Some(schedule) = schedule else {
        let reason = if card.pricing.is_empty() {
            "price_unavailable"
        } else {
            "pricing_mode_unavailable"
        };
        return Ok(SimulateCostResponse {
            card_id: card.card_id.clone(),
            pricing_mode: request.pricing_mode,
            input_tokens: request.input_tokens,
            output_tokens: request.output_tokens,
            available: false,
            total_microusd: None,
            declared_reference_microusd: None,
            basis: None,
            unavailable_reason: Some(reason),
            currency: "USD",
            rounding: "ceiling_to_microusd",
        });
    };

    let declared_reference_microusd = parse_usd_as_microusd(&schedule.declared_simulation_usd);
    let computed = schedule_cost_microusd(
        request.input_tokens,
        request.output_tokens,
        schedule,
        &catalogue.comparison_profile,
    )?;
    let (total_microusd, basis, unavailable_reason) = match computed {
        Some((cost, basis)) => (Some(cost), Some(basis), None),
        None => (
            None,
            None,
            Some("token_rates_unavailable_for_custom_volume"),
        ),
    };

    Ok(SimulateCostResponse {
        card_id: card.card_id.clone(),
        pricing_mode: Some(schedule.mode.clone()),
        input_tokens: request.input_tokens,
        output_tokens: request.output_tokens,
        available: total_microusd.is_some(),
        total_microusd,
        declared_reference_microusd,
        basis,
        unavailable_reason,
        currency: "USD",
        rounding: "ceiling_to_microusd",
    })
}

pub fn preview_candidates(
    request: PreviewCandidatesRequest,
) -> Result<PreviewCandidatesResponse, CommandError> {
    let catalogue = load_catalogue()?;
    validate_preview_request(&request, &catalogue)?;
    match request.source {
        PreviewSource::Local => local_preview(request, catalogue),
        PreviewSource::Runtime => remote_preview(request, catalogue),
    }
}

fn local_preview(
    request: PreviewCandidatesRequest,
    catalogue: Catalogue,
) -> Result<PreviewCandidatesResponse, CommandError> {
    if request.runtime_only {
        return Err(CommandError::new(
            "runtime_source_required",
            "La disponibilite reelle exige la source runtime.",
        ));
    }

    let required_specialties: BTreeSet<&str> = request
        .required_specialties
        .iter()
        .map(String::as_str)
        .collect();
    let mut exclusions = BTreeMap::<String, u64>::new();
    let mut candidates = Vec::<PreviewCandidate>::new();

    for card in &catalogue.cards {
        let profile = &catalogue.capability_profiles[&card.profile];
        let exclusion = if card.integration_stage == "quarantined" {
            Some("quarantined")
        } else if request
            .requirements
            .iter()
            .any(|(dimension, minimum)| profile[dimension] < *minimum)
        {
            Some("capability_below_minimum")
        } else if !required_specialties.iter().all(|specialty| {
            card.specialties
                .iter()
                .any(|item| item.as_str() == *specialty)
        }) {
            Some("specialty_missing")
        } else {
            None
        };

        if let Some(reason) = exclusion {
            *exclusions.entry(reason.to_owned()).or_default() += 1;
            continue;
        }

        let cost = conservative_reference_cost(card, &catalogue.comparison_profile)?;
        let Some(cost) = cost else {
            *exclusions
                .entry("price_unavailable".to_owned())
                .or_default() += 1;
            continue;
        };
        if request
            .maximum_monthly_cost_microusd
            .is_some_and(|maximum| cost > maximum)
        {
            *exclusions
                .entry("cost_above_maximum".to_owned())
                .or_default() += 1;
            continue;
        }
        let capability_margin = request
            .requirements
            .iter()
            .map(|(dimension, minimum)| u16::from(profile[dimension] - *minimum))
            .sum();
        candidates.push(PreviewCandidate {
            card_id: card.card_id.clone(),
            display_name: card.display_name.clone(),
            provider_account_id: card.provider_account_id.clone(),
            integration_stage: card.integration_stage.clone(),
            capabilities: profile.clone(),
            conservative_monthly_cost_microusd: cost,
            capability_margin,
            cost_basis: "catalogue_card".to_owned(),
            selected_deployment_id: None,
            runtime_available: None,
        });
    }
    candidates.sort_by(compare_candidates);
    let candidate_count = candidates.len();
    candidates.truncate(usize::from(request.limit));

    Ok(PreviewCandidatesResponse {
        catalogue_revision: catalogue.revision,
        source: "local_catalogue",
        policy: "hard-capabilities-then-lowest-conservative-cost",
        runtime_only: false,
        requirements: request.requirements,
        required_specialties: request.required_specialties,
        candidate_count,
        excluded: exclusions,
        candidates,
        advisory_only: true,
    })
}

fn remote_preview(
    request: PreviewCandidatesRequest,
    catalogue: Catalogue,
) -> Result<PreviewCandidatesResponse, CommandError> {
    let project_id = request.project_id.clone().ok_or_else(|| {
        CommandError::new(
            "project_id_required",
            "Le projet est obligatoire pour consulter le runtime.",
        )
    })?;
    let payload = RemotePreviewRequest {
        project_id: project_id.clone(),
        requirements: request.requirements.clone(),
        required_specialties: request.required_specialties.clone(),
        maximum_monthly_cost_usd: request
            .maximum_monthly_cost_microusd
            .map(format_microusd_as_usd),
        runtime_only: request.runtime_only,
        limit: request.limit,
    };
    let response: RemotePreviewResponse = runtime::post_preview(&payload)?;
    if response.project_id != project_id
        || response.catalogue_revision != catalogue.revision
        || response.policy != "hard-capabilities-then-lowest-conservative-cost"
        || response.requirements != request.requirements
        || response.required_specialties != request.required_specialties
        || response.runtime_only != request.runtime_only
        || !response.advisory_only
        || response.candidates.len() > usize::from(request.limit)
        || response.candidates.len() > MAX_PREVIEW_CANDIDATES
        || response.candidate_count > 10_000
        || response.excluded.len() > 32
        || response
            .excluded
            .keys()
            .any(|reason| !is_identifier(reason))
    {
        return Err(CommandError::new(
            "runtime_response_invalid",
            "La reponse du routeur local est incoherente.",
        ));
    }

    let mut candidates = Vec::with_capacity(response.candidates.len());
    for candidate in response.candidates {
        let Some(embedded_card) = catalogue
            .cards
            .iter()
            .find(|card| card.card_id == candidate.card_id)
        else {
            return Err(CommandError::new(
                "runtime_response_invalid",
                "La reponse du routeur local est incoherente.",
            ));
        };
        validate_remote_candidate(&candidate, embedded_card, &catalogue, &request)?;
        candidates.push(PreviewCandidate {
            card_id: candidate.card_id,
            display_name: embedded_card.display_name.clone(),
            provider_account_id: candidate.routing_preview.selected_provider_account_id,
            integration_stage: embedded_card.integration_stage.clone(),
            capabilities: candidate.capabilities,
            conservative_monthly_cost_microusd: candidate
                .routing_preview
                .conservative_monthly_cost_microusd,
            capability_margin: candidate.routing_preview.capability_margin,
            cost_basis: candidate.routing_preview.cost_basis,
            selected_deployment_id: candidate.routing_preview.selected_deployment_id,
            runtime_available: Some(candidate.runtime.available),
        });
    }

    let unique_candidates: BTreeSet<&str> = candidates
        .iter()
        .map(|candidate| candidate.card_id.as_str())
        .collect();
    if unique_candidates.len() != candidates.len()
        || response.candidate_count < candidates.len()
        || response.candidate_count > catalogue.cards.len()
        || response
            .excluded
            .values()
            .try_fold(response.candidate_count, |total, value| {
                usize::try_from(*value)
                    .ok()
                    .and_then(|value| total.checked_add(value))
            })
            != Some(catalogue.cards.len())
        || candidates
            .windows(2)
            .any(|items| compare_candidates(&items[0], &items[1]) == Ordering::Greater)
    {
        return Err(remote_response_invalid());
    }

    Ok(PreviewCandidatesResponse {
        catalogue_revision: response.catalogue_revision,
        source: "orchestrator_runtime",
        policy: "hard-capabilities-then-lowest-conservative-cost",
        runtime_only: response.runtime_only,
        requirements: response.requirements,
        required_specialties: response.required_specialties,
        candidate_count: response.candidate_count,
        excluded: response.excluded,
        candidates,
        advisory_only: true,
    })
}

fn validate_remote_candidate(
    candidate: &RemoteCandidate,
    embedded_card: &ModelCard,
    catalogue: &Catalogue,
    request: &PreviewCandidatesRequest,
) -> Result<(), CommandError> {
    let expected_capabilities = &catalogue.capability_profiles[&embedded_card.profile];
    let identity_matches = candidate.display_name == embedded_card.display_name
        && candidate.entity_kind == embedded_card.entity_kind
        && candidate.provider_account_id == embedded_card.provider_account_id
        && candidate.developer == embedded_card.developer
        && candidate.country == embedded_card.country
        && candidate.description == embedded_card.description
        && candidate.profile == embedded_card.profile
        && candidate.specialties == embedded_card.specialties
        && candidate.integration_stage == embedded_card.integration_stage
        && candidate.deployment_ids == embedded_card.deployment_ids
        && candidate.exact_model_id == embedded_card.exact_model_id
        && candidate.context_window_tokens == embedded_card.context_window_tokens
        && candidate.languages == embedded_card.languages
        && candidate.limitations == embedded_card.limitations
        && candidate.latency_p50_ms == embedded_card.latency_p50_ms
        && candidate.latency_p95_ms == embedded_card.latency_p95_ms
        && candidate.technical_metadata_confidence == embedded_card.technical_metadata_confidence
        && candidate.technical_metadata_source == embedded_card.technical_metadata_source
        && candidate.source_lines == embedded_card.source_lines
        && candidate.pricing == embedded_card.pricing
        && &candidate.capabilities == expected_capabilities;
    let constraints_match = candidate.integration_stage != "quarantined"
        && request
            .requirements
            .iter()
            .all(|(dimension, minimum)| candidate.capabilities[dimension] >= *minimum)
        && request.required_specialties.iter().all(|required| {
            candidate
                .specialties
                .iter()
                .any(|specialty| specialty == required)
        });
    let expected_margin: u16 = request
        .requirements
        .iter()
        .map(|(dimension, minimum)| u16::from(candidate.capabilities[dimension] - *minimum))
        .sum();
    if !candidate.routing_preview.not_an_activation_authorization
        || !is_identifier(&candidate.card_id)
        || !is_identifier(&candidate.provider_account_id)
        || !identity_matches
        || !constraints_match
        || candidate.routing_preview.capability_margin != expected_margin
        || !catalogue
            .provider_accounts
            .iter()
            .any(|account| account.id == candidate.routing_preview.selected_provider_account_id)
        || request
            .maximum_monthly_cost_microusd
            .is_some_and(|maximum| {
                candidate.routing_preview.conservative_monthly_cost_microusd > maximum
            })
    {
        return Err(remote_response_invalid());
    }

    if candidate.simulations.len() != embedded_card.pricing.len() {
        return Err(remote_response_invalid());
    }
    for (simulation, schedule) in candidate.simulations.iter().zip(&embedded_card.pricing) {
        let Some((expected_cost, _)) = schedule_cost_microusd(
            catalogue.comparison_profile.input_tokens_total,
            catalogue.comparison_profile.output_tokens_total,
            schedule,
            &catalogue.comparison_profile,
        )?
        else {
            return Err(remote_response_invalid());
        };
        let expected_basis = if schedule.input_usd_per_million.is_some() {
            "computed_from_rates"
        } else {
            "source_simulation_only"
        };
        if simulation.mode != schedule.mode
            || simulation.cost_microusd != expected_cost
            || simulation.basis != expected_basis
        {
            return Err(remote_response_invalid());
        }
    }

    let mut deployment_ids = BTreeSet::new();
    for deployment in &candidate.runtime.deployments {
        let Some(account_id) = deployment.provider_account_id.as_deref() else {
            return Err(remote_response_invalid());
        };
        if !is_identifier(&deployment.deployment_id)
            || !deployment_ids.insert(deployment.deployment_id.as_str())
            || !embedded_card
                .deployment_ids
                .contains(&deployment.deployment_id)
            || !catalogue
                .provider_accounts
                .iter()
                .any(|account| account.id == account_id)
            || !is_bounded_runtime_text(&deployment.role, 64)
            || deployment
                .model
                .as_deref()
                .is_some_and(|model| !is_bounded_runtime_text(model, 256))
            || !is_bounded_runtime_text(&deployment.reason, 256)
        {
            return Err(remote_response_invalid());
        }
    }
    let expected_deployments: BTreeSet<&str> = embedded_card
        .deployment_ids
        .iter()
        .map(String::as_str)
        .collect();
    if candidate.runtime.availability_scope != "configuration-and-secret-only"
        || deployment_ids != expected_deployments
        || candidate.runtime.available
            != candidate
                .runtime
                .deployments
                .iter()
                .any(|deployment| deployment.available)
    {
        return Err(remote_response_invalid());
    }

    if request.runtime_only {
        let Some(selected_id) = candidate.routing_preview.selected_deployment_id.as_deref() else {
            return Err(remote_response_invalid());
        };
        let Some(selected) = candidate
            .runtime
            .deployments
            .iter()
            .find(|deployment| deployment.deployment_id == selected_id)
        else {
            return Err(remote_response_invalid());
        };
        let cheapest_available = candidate
            .runtime
            .deployments
            .iter()
            .filter(|deployment| deployment.available)
            .filter_map(|deployment| {
                deployment
                    .comparison_cost_microusd
                    .map(|cost| (cost, deployment.deployment_id.as_str()))
            })
            .min();
        if candidate.routing_preview.cost_basis != "runtime_deployment"
            || !candidate.runtime.available
            || !selected.available
            || cheapest_available
                != Some((
                    candidate.routing_preview.conservative_monthly_cost_microusd,
                    selected_id,
                ))
            || selected.provider_account_id.as_deref()
                != Some(
                    candidate
                        .routing_preview
                        .selected_provider_account_id
                        .as_str(),
                )
            || selected.comparison_cost_microusd
                != Some(candidate.routing_preview.conservative_monthly_cost_microusd)
        {
            return Err(remote_response_invalid());
        }
    } else {
        let expected_cost =
            conservative_reference_cost(embedded_card, &catalogue.comparison_profile)?;
        if candidate.routing_preview.cost_basis != "catalogue_card"
            || candidate.routing_preview.selected_deployment_id.is_some()
            || candidate.routing_preview.selected_provider_account_id
                != embedded_card.provider_account_id
            || expected_cost != Some(candidate.routing_preview.conservative_monthly_cost_microusd)
        {
            return Err(remote_response_invalid());
        }
    }
    Ok(())
}

fn remote_response_invalid() -> CommandError {
    CommandError::new(
        "runtime_response_invalid",
        "La reponse du routeur local est incoherente.",
    )
}

fn is_bounded_runtime_text(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && !value
            .bytes()
            .any(|byte| matches!(byte, b'\0' | b'\r' | b'\n'))
}

fn validate_catalogue(catalogue: &Catalogue) -> Result<(), CommandError> {
    let invalid = || {
        CommandError::new(
            "catalogue_invalid",
            "Le catalogue embarque ne respecte pas son schema.",
        )
    };
    if catalogue.schema_version != 2
        || !is_revision(&catalogue.revision)
        || catalogue.cards.is_empty()
        || catalogue.cards.len() > 10_000
        || catalogue.provider_accounts.is_empty()
        || catalogue.provider_accounts.len() > 256
        || catalogue.capability_dimensions.is_empty()
        || catalogue.capability_dimensions.len() > 64
        || catalogue.comparison_profile.currency != "USD"
        || catalogue.comparison_profile.cache_hit_percent > 100
        || catalogue.comparison_profile.input_tokens_total > MAX_TOKENS_PER_SIMULATION
        || catalogue.comparison_profile.output_tokens_total > MAX_TOKENS_PER_SIMULATION
        || catalogue.comparison_profile.calls == 0
        || catalogue
            .comparison_profile
            .calls
            .checked_mul(catalogue.comparison_profile.input_tokens_per_call)
            != Some(catalogue.comparison_profile.input_tokens_total)
        || catalogue
            .comparison_profile
            .calls
            .checked_mul(catalogue.comparison_profile.output_tokens_per_call)
            != Some(catalogue.comparison_profile.output_tokens_total)
    {
        return Err(invalid());
    }

    let dimensions: BTreeSet<&str> = catalogue
        .capability_dimensions
        .iter()
        .map(String::as_str)
        .collect();
    if dimensions.len() != catalogue.capability_dimensions.len()
        || dimensions.iter().any(|value| !is_dimension(value))
    {
        return Err(invalid());
    }
    if catalogue.capability_profiles.is_empty()
        || catalogue.capability_profiles.len() > 256
        || catalogue
            .capability_profiles
            .iter()
            .any(|(profile_id, scores)| {
                !is_identifier(profile_id)
                    || scores.len() != dimensions.len()
                    || scores.iter().any(|(dimension, score)| {
                        !dimensions.contains(dimension.as_str()) || *score > 10
                    })
            })
    {
        return Err(invalid());
    }

    let mut provider_ids = BTreeSet::new();
    for provider in &catalogue.provider_accounts {
        if !is_identifier(&provider.id)
            || !provider_ids.insert(provider.id.as_str())
            || provider.name.is_empty()
            || provider.name.len() > 128
            || !provider
                .credential_ref
                .starts_with("kv-infra-shared/data/llm/")
            || provider.credential_ref.contains("..")
            || !matches!(
                provider.balance_mode.as_str(),
                "unsupported"
                    | "unknown"
                    | "quota_only"
                    | "provider_dependent"
                    | "verified_balance"
            )
            || !valid_financial_capabilities(&provider.financial_capabilities)
        {
            return Err(invalid());
        }
    }

    let mut card_ids = BTreeSet::new();
    for card in &catalogue.cards {
        if !is_identifier(&card.card_id)
            || !card_ids.insert(card.card_id.as_str())
            || !provider_ids.contains(card.provider_account_id.as_str())
            || !catalogue.capability_profiles.contains_key(&card.profile)
            || card.display_name.is_empty()
            || card.display_name.len() > 160
            || card.description.is_empty()
            || card.description.len() > 512
            || !matches!(
                card.integration_stage.as_str(),
                "catalogued" | "configured" | "quarantined"
            )
            || card.specialties.is_empty()
            || card.specialties.len() > 16
            || card.specialties.iter().any(|value| !is_specialty(value))
            || card
                .context_window_tokens
                .is_some_and(|value| value == 0 || value > 1_000_000_000)
            || card.languages.len() > 64
            || card
                .languages
                .iter()
                .any(|value| !is_bounded_runtime_text(value, 128))
            || card.languages.iter().collect::<BTreeSet<_>>().len() != card.languages.len()
            || card.limitations.len() > 32
            || card
                .limitations
                .iter()
                .any(|value| !is_bounded_runtime_text(value, 512))
            || card.limitations.iter().collect::<BTreeSet<_>>().len() != card.limitations.len()
            || card.latency_p50_ms.is_some_and(|value| value > 3_600_000)
            || card.latency_p95_ms.is_some_and(|value| value > 3_600_000)
            || matches!((card.latency_p50_ms, card.latency_p95_ms), (Some(p50), Some(p95)) if p95 < p50)
            || !matches!(
                card.technical_metadata_confidence.as_str(),
                "unknown" | "source_declared_unverified" | "official_verified" | "observed"
            )
            || !is_bounded_runtime_text(&card.technical_metadata_source, 256)
            || card.pricing.len() > 32
        {
            return Err(invalid());
        }
        for schedule in &card.pricing {
            let input_present = schedule.input_usd_per_million.is_some();
            let output_present = schedule.output_usd_per_million.is_some();
            if !is_identifier(&schedule.mode)
                || input_present != output_present
                || parse_usd_as_microusd(&schedule.declared_simulation_usd).is_none()
                || schedule
                    .input_usd_per_million
                    .as_deref()
                    .is_some_and(|value| parse_usd_as_microusd(value).is_none())
                || schedule
                    .output_usd_per_million
                    .as_deref()
                    .is_some_and(|value| parse_usd_as_microusd(value).is_none())
                || schedule
                    .cached_input_usd_per_million
                    .as_deref()
                    .is_some_and(|value| parse_usd_as_microusd(value).is_none())
                || !matches!(
                    schedule.confidence.as_str(),
                    "declared_unverified"
                        | "approximate"
                        | "revalidation_required"
                        | "discussed_reference"
                        | "shared_source_price"
                        | "public_unverified"
                        | "promotion_unverified"
                        | "official_verified"
                )
                || !is_bounded_runtime_text(&schedule.source, 256)
            {
                return Err(invalid());
            }
        }
    }
    if catalogue.model_id_provenance.len() > catalogue.cards.len() {
        return Err(invalid());
    }
    for (card_id, provenance) in &catalogue.model_id_provenance {
        let Some(card) = catalogue.cards.iter().find(|card| card.card_id == *card_id) else {
            return Err(invalid());
        };
        if card.exact_model_id.as_deref() != Some(provenance.exact_model_id.as_str())
            || provenance.confidence != "official_verified"
            || !provenance.source.starts_with("https://")
            || !is_bounded_runtime_text(&provenance.source, 512)
            || provenance.verified_on.len() != 10
            || !provenance
                .verified_on
                .bytes()
                .enumerate()
                .all(|(index, byte)| {
                    matches!(index, 4 | 7) && byte == b'-'
                        || !matches!(index, 4 | 7) && byte.is_ascii_digit()
                })
        {
            return Err(invalid());
        }
    }
    if usize::try_from(catalogue.normalization.card_count).ok() != Some(catalogue.cards.len()) {
        return Err(invalid());
    }
    Ok(())
}

fn validate_preview_request(
    request: &PreviewCandidatesRequest,
    catalogue: &Catalogue,
) -> Result<(), CommandError> {
    if request.limit == 0 || usize::from(request.limit) > MAX_PREVIEW_CANDIDATES {
        return Err(CommandError::new(
            "invalid_preview_limit",
            "La limite doit etre comprise entre 1 et 50.",
        ));
    }
    if request.requirements.len() > catalogue.capability_dimensions.len()
        || request.requirements.iter().any(|(dimension, score)| {
            !catalogue.capability_dimensions.contains(dimension) || *score > 10
        })
    {
        return Err(CommandError::new(
            "invalid_requirements",
            "Les exigences de capacites sont invalides.",
        ));
    }
    let unique_specialties: BTreeSet<&str> = request
        .required_specialties
        .iter()
        .map(String::as_str)
        .collect();
    if request.required_specialties.len() > 16
        || unique_specialties.len() != request.required_specialties.len()
        || unique_specialties.iter().any(|value| !is_specialty(value))
    {
        return Err(CommandError::new(
            "invalid_specialties",
            "Les specialites demandees sont invalides.",
        ));
    }
    if request
        .project_id
        .as_deref()
        .is_some_and(|project_id| !is_project_id(project_id))
    {
        return Err(CommandError::new(
            "invalid_project_id",
            "L'identifiant du projet est invalide.",
        ));
    }
    Ok(())
}

fn validate_token_counts(input_tokens: u64, output_tokens: u64) -> Result<(), CommandError> {
    if input_tokens > MAX_TOKENS_PER_SIMULATION || output_tokens > MAX_TOKENS_PER_SIMULATION {
        return Err(CommandError::new(
            "token_count_too_large",
            "La simulation est limitee a mille milliards de jetons par direction.",
        ));
    }
    Ok(())
}

fn select_schedule<'a>(card: &'a ModelCard, requested: Option<&str>) -> Option<&'a PriceSchedule> {
    if let Some(mode) = requested {
        return card.pricing.iter().find(|schedule| schedule.mode == mode);
    }
    card.pricing
        .iter()
        .filter_map(|schedule| {
            parse_usd_as_microusd(&schedule.declared_simulation_usd)
                .map(|reference_cost| (schedule, reference_cost))
        })
        .max_by_key(|(_, reference_cost)| *reference_cost)
        .map(|(schedule, _)| schedule)
}

fn conservative_reference_cost(
    card: &ModelCard,
    comparison: &ComparisonProfile,
) -> Result<Option<u64>, CommandError> {
    let mut costs = Vec::with_capacity(card.pricing.len());
    for schedule in &card.pricing {
        if let Some((cost, _)) = schedule_cost_microusd(
            comparison.input_tokens_total,
            comparison.output_tokens_total,
            schedule,
            comparison,
        )? {
            costs.push(cost);
        }
    }
    Ok(costs.into_iter().max())
}

fn schedule_cost_microusd(
    input_tokens: u64,
    output_tokens: u64,
    schedule: &PriceSchedule,
    comparison: &ComparisonProfile,
) -> Result<Option<(u64, &'static str)>, CommandError> {
    match (
        schedule.input_usd_per_million.as_deref(),
        schedule.output_usd_per_million.as_deref(),
    ) {
        (Some(input_rate), Some(output_rate)) => {
            let input_rate = parse_usd_as_microusd(input_rate).ok_or_else(|| {
                CommandError::new("catalogue_invalid", "Un tarif du catalogue est invalide.")
            })?;
            let output_rate = parse_usd_as_microusd(output_rate).ok_or_else(|| {
                CommandError::new("catalogue_invalid", "Un tarif du catalogue est invalide.")
            })?;
            let numerator = u128::from(input_tokens)
                .checked_mul(u128::from(input_rate))
                .and_then(|value| {
                    u128::from(output_tokens)
                        .checked_mul(u128::from(output_rate))
                        .and_then(|output| value.checked_add(output))
                })
                .ok_or_else(|| {
                    CommandError::new("cost_overflow", "Le cout simule depasse la limite.")
                })?;
            let rounded = numerator
                .checked_add(u128::from(MICRO_USD_PER_USD - 1))
                .ok_or_else(|| {
                    CommandError::new("cost_overflow", "Le cout simule depasse la limite.")
                })?
                / u128::from(MICRO_USD_PER_USD);
            let total = u64::try_from(rounded).map_err(|_| {
                CommandError::new("cost_overflow", "Le cout simule depasse la limite.")
            })?;
            Ok(Some((total, "computed_from_token_rates")))
        }
        (None, None)
            if input_tokens == comparison.input_tokens_total
                && output_tokens == comparison.output_tokens_total =>
        {
            let declared =
                parse_usd_as_microusd(&schedule.declared_simulation_usd).ok_or_else(|| {
                    CommandError::new(
                        "catalogue_invalid",
                        "Une simulation declaree du catalogue est invalide.",
                    )
                })?;
            Ok(Some((declared, "source_reference_simulation")))
        }
        (None, None) => Ok(None),
        _ => Err(CommandError::new(
            "catalogue_invalid",
            "Un tarif du catalogue est incomplet.",
        )),
    }
}

fn parse_usd_as_microusd(value: &str) -> Option<u64> {
    if value.is_empty() || value.len() > 32 || value.starts_with('+') || value.starts_with('-') {
        return None;
    }
    let (whole, fraction) = value.split_once('.').unwrap_or((value, ""));
    if whole.is_empty()
        || (whole.len() > 1 && whole.starts_with('0'))
        || !whole.bytes().all(|byte| byte.is_ascii_digit())
        || fraction.len() > 6
        || !fraction.bytes().all(|byte| byte.is_ascii_digit())
        || value.ends_with('.')
    {
        return None;
    }
    let whole: u64 = whole.parse().ok()?;
    let mut fractional = if fraction.is_empty() {
        0
    } else {
        fraction.parse::<u64>().ok()?
    };
    for _ in fraction.len()..6 {
        fractional = fractional.checked_mul(10)?;
    }
    whole
        .checked_mul(MICRO_USD_PER_USD)
        .and_then(|value| value.checked_add(fractional))
}

fn format_microusd_as_usd(value: u64) -> String {
    let whole = value / MICRO_USD_PER_USD;
    let remainder = value % MICRO_USD_PER_USD;
    if remainder == 0 {
        return whole.to_string();
    }
    let mut fraction = format!("{remainder:06}");
    while fraction.ends_with('0') {
        fraction.pop();
    }
    format!("{whole}.{fraction}")
}

fn compare_candidates(left: &PreviewCandidate, right: &PreviewCandidate) -> Ordering {
    left.conservative_monthly_cost_microusd
        .cmp(&right.conservative_monthly_cost_microusd)
        .then_with(|| right.capability_margin.cmp(&left.capability_margin))
        .then_with(|| left.card_id.cmp(&right.card_id))
}

fn is_identifier(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=128).contains(&bytes.len())
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes.iter().skip(1).all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'_' || *byte == b'-'
        })
}

fn is_revision(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=128).contains(&bytes.len())
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes.iter().skip(1).all(|byte| {
            byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || matches!(*byte, b'_' | b'-' | b'.')
        })
}

fn is_dimension(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=32).contains(&bytes.len())
        && bytes[0].is_ascii_lowercase()
        && bytes
            .iter()
            .skip(1)
            .all(|byte| byte.is_ascii_lowercase() || *byte == b'_')
}

fn is_specialty(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=64).contains(&bytes.len())
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes
            .iter()
            .skip(1)
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || *byte == b'-')
}

fn valid_financial_capabilities(value: &FinancialCapabilities) -> bool {
    [
        value.cash_balance.as_str(),
        value.plan_quota.as_str(),
        value.usage.as_str(),
        value.cost.as_str(),
        value.rate_limits.as_str(),
        value.spending_limit.as_str(),
    ]
    .iter()
    .all(|mode| {
        matches!(
            *mode,
            "direct_api"
                | "admin_api"
                | "cloud_billing_api"
                | "console_only"
                | "not_applicable"
                | "unknown"
        )
    })
}

fn is_project_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=128).contains(&bytes.len())
        && (bytes[0].is_ascii_alphanumeric())
        && bytes
            .iter()
            .skip(1)
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(*byte, b'_' | b'-' | b'.' | b':'))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedded_catalogue_is_strict_and_complete() {
        let catalogue = load_catalogue().expect("embedded catalogue must validate");
        assert_eq!(catalogue.schema_version, 2);
        assert_eq!(catalogue.cards.len(), 58);
        assert_eq!(catalogue.provider_accounts.len(), 21);
        assert_eq!(
            catalogue
                .cards
                .iter()
                .filter(|card| card.context_window_tokens.is_some())
                .count(),
            8
        );
        assert!(catalogue.cards.iter().all(|card| {
            card.latency_p50_ms.is_none()
                && card.latency_p95_ms.is_none()
                && (card.technical_metadata_source == "catalogue_modeles_IA_API_2026.txt"
                    || (card.technical_metadata_source.starts_with("https://")
                        && card.technical_metadata_confidence == "official_verified"))
                && card.pricing.iter().all(|price| {
                    (price.source == "catalogue_modeles_IA_API_2026.txt"
                        && price.cached_input_usd_per_million.is_none())
                        || (price.source.starts_with("https://")
                            && price.confidence == "official_verified")
                })
        }));
    }

    #[test]
    fn decimal_conversion_is_exact() {
        assert_eq!(parse_usd_as_microusd("0.028"), Some(28_000));
        assert_eq!(parse_usd_as_microusd("180.00"), Some(180_000_000));
        assert_eq!(parse_usd_as_microusd("01"), None);
        assert_eq!(parse_usd_as_microusd("1.0000001"), None);
        assert_eq!(parse_usd_as_microusd("NaN"), None);
    }

    #[test]
    fn reference_simulation_uses_integer_microdollars() {
        let result = simulate_cost(SimulateCostRequest {
            card_id: "qwen-3-7-flash".to_owned(),
            pricing_mode: Some("standard".to_owned()),
            input_tokens: 10_000_000,
            output_tokens: 2_000_000,
        })
        .expect("simulation must succeed");
        assert_eq!(result.total_microusd, Some(500_000));
        assert_eq!(result.basis, Some("computed_from_token_rates"));
    }

    #[test]
    fn default_simulation_uses_the_conservative_price_schedule() {
        let result = simulate_cost(SimulateCostRequest {
            card_id: "deepseek-v4-flash".to_owned(),
            pricing_mode: None,
            input_tokens: 10_000_000,
            output_tokens: 2_000_000,
        })
        .expect("simulation must succeed");
        assert_eq!(result.pricing_mode.as_deref(), Some("peak"));
        assert_eq!(result.total_microusd, Some(7_040_000));
    }

    #[test]
    fn deepseek_price_schedules_are_all_preserved() {
        let catalogue = load_catalogue().expect("embedded catalogue must validate");
        let flash = catalogue
            .cards
            .iter()
            .find(|card| card.card_id == "deepseek-v4-flash")
            .expect("DeepSeek V4 Flash card");
        assert_eq!(
            flash
                .pricing
                .iter()
                .map(|schedule| schedule.mode.as_str())
                .collect::<Vec<_>>(),
            vec!["off_peak", "peak"]
        );
        assert_eq!(flash.pricing[0].declared_simulation_usd, "3.52");
        assert_eq!(flash.pricing[1].declared_simulation_usd, "7.04");
    }

    #[test]
    fn quarantined_aya_keeps_its_historical_txt_price() {
        let catalogue = load_catalogue().expect("embedded catalogue must validate");
        let aya = catalogue
            .cards
            .iter()
            .find(|card| card.card_id == "cohere-aya-expanse-8b")
            .expect("Aya 8B card");
        assert_eq!(aya.integration_stage, "quarantined");
        assert!(aya.deployment_ids.is_empty());
        assert_eq!(aya.pricing.len(), 1);
        assert_eq!(
            aya.pricing[0].input_usd_per_million.as_deref(),
            Some("0.50")
        );
        assert_eq!(
            aya.pricing[0].output_usd_per_million.as_deref(),
            Some("1.50")
        );
        assert_eq!(aya.pricing[0].source, "catalogue_modeles_IA_API_2026.txt");
    }

    #[test]
    fn low_volume_is_rounded_up_without_floats() {
        let result = simulate_cost(SimulateCostRequest {
            card_id: "qwen-3-7-flash".to_owned(),
            pricing_mode: Some("standard".to_owned()),
            input_tokens: 1,
            output_tokens: 0,
        })
        .expect("simulation must succeed");
        assert_eq!(result.total_microusd, Some(1));
    }

    #[test]
    fn simulation_only_price_cannot_be_scaled_to_an_arbitrary_volume() {
        let result = simulate_cost(SimulateCostRequest {
            card_id: "hunyuan-hy4-family".to_owned(),
            pricing_mode: Some("simulation_only".to_owned()),
            input_tokens: 1,
            output_tokens: 1,
        })
        .expect("unavailable is a valid result");
        assert!(!result.available);
        assert_eq!(
            result.unavailable_reason,
            Some("token_rates_unavailable_for_custom_volume")
        );
    }

    #[test]
    fn identifiers_reject_shell_metacharacters() {
        assert!(is_identifier("mistral-ai"));
        assert!(!is_identifier("Mistral"));
        assert!(!is_identifier("deepseek;sh"));
        assert!(!is_identifier("../../helper"));
        assert!(!is_identifier(""));
    }

    #[test]
    fn formatting_microdollars_never_uses_floating_point() {
        assert_eq!(format_microusd_as_usd(15_000_000), "15");
        assert_eq!(format_microusd_as_usd(1_250_001), "1.250001");
    }

    #[test]
    fn runtime_candidate_must_select_the_cheapest_available_host_account() {
        let catalogue = load_catalogue().expect("embedded catalogue must validate");
        let card = catalogue
            .cards
            .iter()
            .find(|card| card.card_id == "deepseek-v4-flash")
            .expect("DeepSeek V4 Flash card");
        let capabilities = &catalogue.capability_profiles[&card.profile];
        let minimum_reasoning = 7;
        let margin = u16::from(capabilities["reasoning"] - minimum_reasoning);
        let mut raw = serde_json::to_value(card).expect("serializable card");
        raw["capabilities"] =
            serde_json::to_value(capabilities).expect("serializable capabilities");
        raw["simulations"] = serde_json::json!([
            {"mode": "off_peak", "cost_microusd": 3_520_000, "basis": "computed_from_rates"},
            {"mode": "peak", "cost_microusd": 7_040_000, "basis": "computed_from_rates"}
        ]);
        raw["runtime"] = serde_json::json!({
            "deployments": [
                {
                    "deployment_id": "alibaba-deepseek-ops-api",
                    "provider_account_id": "alibaba",
                    "role": "ROLE_LOCAL_OPS",
                    "model": "deepseek-v4-flash",
                    "available": true,
                    "reason": "configured secret is readable",
                    "comparison_cost_microusd": 1_930_000
                },
                {
                    "deployment_id": "deepseek-ops-api",
                    "provider_account_id": "deepseek",
                    "role": "ROLE_LOCAL_OPS",
                    "model": "deepseek-v4-flash",
                    "available": true,
                    "reason": "configured secret is readable",
                    "comparison_cost_microusd": 7_040_000
                }
            ],
            "availability_scope": "configuration-and-secret-only",
            "available": true
        });
        raw["routing_preview"] = serde_json::json!({
            "conservative_monthly_cost_microusd": 1_930_000,
            "capability_margin": margin,
            "cost_basis": "runtime_deployment",
            "selected_deployment_id": "alibaba-deepseek-ops-api",
            "selected_provider_account_id": "alibaba",
            "not_an_activation_authorization": true
        });
        let mut candidate: RemoteCandidate =
            serde_json::from_value(raw).expect("valid remote candidate fixture");
        let request = PreviewCandidatesRequest {
            source: PreviewSource::Runtime,
            project_id: Some("infra-shared".to_owned()),
            requirements: BTreeMap::from([("reasoning".to_owned(), minimum_reasoning)]),
            required_specialties: Vec::new(),
            maximum_monthly_cost_microusd: None,
            runtime_only: true,
            limit: 10,
        };
        validate_remote_candidate(&candidate, card, &catalogue, &request)
            .expect("cheapest Alibaba deployment must validate");

        candidate.routing_preview.selected_deployment_id = Some("deepseek-ops-api".to_owned());
        candidate.routing_preview.selected_provider_account_id = "deepseek".to_owned();
        candidate.routing_preview.conservative_monthly_cost_microusd = 7_040_000;
        assert!(validate_remote_candidate(&candidate, card, &catalogue, &request).is_err());
    }
}
