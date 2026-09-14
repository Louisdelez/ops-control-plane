"use strict";

const DEFAULT_SCENARIO = Object.freeze({
  inputTokens: 10_000_000,
  outputTokens: 2_000_000,
  cachedInputTokens: 0,
});

const CAPABILITY_LABELS = Object.freeze({
  general: "Général",
  reasoning: "Raisonnement",
  code: "Programmation",
  mathematics: "Mathématiques",
  research: "Recherche",
  tools: "Outils",
  agentic: "Agentique",
  instruction_following: "Instructions",
  reliability: "Fiabilité",
  factuality: "Exactitude",
  speed: "Vitesse",
  multilingual: "Langues",
});

const CAPABILITY_ALIASES = Object.freeze({
  general: ["general", "general_score", "polyvalence"],
  reasoning: ["reasoning", "reasoning_score", "raisonnement"],
  code: ["code", "coding", "programming", "programmation"],
  mathematics: ["mathematics", "math", "maths", "mathematiques"],
  research: ["research", "search", "recherche"],
  tools: ["tools", "tool_calling", "tool_use", "outils"],
  agentic: ["agentic", "agency", "agentique"],
  instruction_following: ["instruction_following", "instructions", "instruction"],
  reliability: ["reliability", "fiability", "fiabilite"],
  factuality: ["factuality", "accuracy", "exactitude"],
  speed: ["speed", "latency", "vitesse"],
  multilingual: ["multilingual", "languages", "langues"],
});

const state = {
  models: [],
  providers: [],
  accounts: [],
  capabilityProfiles: {},
  healthDeployments: [],
  runtimeBudgetAccounts: [],
  runtimeProviderBudgets: [],
  runtimePerformance: null,
  providerIntegrations: [],
  runtime: null,
  catalogueMeta: {},
  runtimeError: null,
  catalogueError: null,
  activeTab: "catalogue",
  quickFilter: "all",
  search: "",
  provider: "",
  specialty: "",
  status: "",
  sort: "cost",
  visibleLimit: 12,
  scenario: { ...DEFAULT_SCENARIO },
  backendEstimates: new Map(),
  pricingModes: new Map(),
  activeModelId: null,
  credentialDialog: {
    providerAccountId: null,
    providerName: null,
    visibilityTimers: new Set(),
    unlisten: null,
    operationComplete: false,
  },
};

const elements = {};

document.addEventListener("DOMContentLoaded", async () => {
  cacheElements();
  bindNavigation();
  bindCatalogueControls();
  bindRoutingControls();
  bindCostControls();
  bindRuntimePopover();
  bindGlobalShortcuts();
  if (window.AtlasOnboarding?.init) {
    await window.AtlasOnboarding.init({ callNative, onReady: boot });
  } else {
    await boot();
  }
});

function cacheElements() {
  const ids = [
    "runtime-pill",
    "runtime-pill-label",
    "runtime-detail",
    "runtime-popover",
    "close-runtime-popover",
    "model-count",
    "comparison-summary",
    "catalogue-search",
    "provider-filter",
    "specialty-filter",
    "status-filter",
    "sort-filter",
    "active-filter-count",
    "reset-filters",
    "catalogue-state",
    "model-grid",
    "result-count",
    "pagination-sentinel",
    "show-more",
    "routing-form",
    "task-description",
    "task-character-count",
    "reset-profile",
    "preview-routing",
    "routing-results",
    "candidate-list",
    "input-tokens",
    "output-tokens",
    "apply-scenario",
    "cheapest-cost",
    "cheapest-model",
    "median-cost",
    "priced-count",
    "unknown-price-count",
    "cost-list",
    "account-count",
    "account-list",
    "router-mode",
    "router-strategy",
    "router-escalation",
    "catalogue-version",
    "last-sync",
    "decision-count",
    "validated-outcome-count",
    "adaptive-policy",
    "performance-list",
    "model-dialog",
    "credential-dialog",
    "credential-dialog-content",
  ];

  for (const id of ids) {
    elements[toCamelCase(id)] = document.getElementById(id);
  }
}

async function boot() {
  setRuntimeStatus("loading", "Connexion…", "Lecture du contrôle local et des comptes fournisseurs…");

  if (!getInvoke()) {
    state.catalogueError = new Error("Le pont natif Tauri n’est pas disponible.");
    state.runtimeError = state.catalogueError;
    renderCatalogueError(
      "Interface native indisponible",
      "Ouvre Atlas depuis l’application Tauri. Aucune donnée distante ni catalogue de démonstration n’est chargé dans un navigateur classique.",
    );
    setRuntimeStatus("offline", "Hors application", "Le pont IPC local Tauri est absent.");
    renderSettings();
    return;
  }

  const [catalogueResult, runtimeResult] = await Promise.allSettled([
    callNative("get_catalogue"),
    callNative("get_runtime_snapshot"),
  ]);

  if (catalogueResult.status === "fulfilled") {
    hydrateCatalogue(catalogueResult.value);
  } else {
    state.catalogueError = toError(catalogueResult.reason);
  }

  if (runtimeResult.status === "fulfilled") {
    hydrateRuntime(runtimeResult.value);
  } else {
    state.runtimeError = toError(runtimeResult.reason);
  }

  if (state.catalogueError) {
    renderCatalogueError(
      "Catalogue impossible à lire",
      readableError(state.catalogueError, "Le service local n’a pas renvoyé le catalogue."),
    );
  } else {
    populateFilters();
    renderCatalogue();
    void refreshBackendEstimates(false);
  }

  renderRuntimeStatus();
  renderSettings();
  renderCosts();
}

function getInvoke() {
  return window.__TAURI__?.core?.invoke ?? null;
}

async function callNative(command, args) {
  const invoke = getInvoke();
  if (!invoke) {
    throw new Error("Pont natif Tauri indisponible");
  }
  const payload = await invoke(command, args);
  return parsePayload(payload);
}

function parsePayload(payload) {
  if (typeof payload !== "string") return payload;
  try {
    return JSON.parse(payload);
  } catch {
    return payload;
  }
}

function hydrateCatalogue(payload) {
  const source = payload && typeof payload === "object" ? payload : {};
  const rawModels = firstArray(source.models, source.cards, source.catalogue, payload);
  const rawProviders = firstArray(source.providers, source.provider_accounts, source.accounts);

  state.capabilityProfiles =
    source.capability_profiles && typeof source.capability_profiles === "object"
      ? source.capability_profiles
      : {};
  state.providers = rawProviders.map(normalizeProvider).filter((provider) => provider.id);
  state.models = rawModels
    .map((model, index) => normalizeModel(model, index, state.capabilityProfiles))
    .filter((model) => model.id && model.name);
  for (const model of state.models) {
    const provider = state.providers.find((candidate) => candidate.id === model.providerId);
    if (provider) model.providerName = provider.name;
  }
  state.catalogueMeta = {
    version: firstValue(source.version, source.catalogue_version, source.schema_version),
    revision: firstValue(source.revision, source.catalogue_revision),
    generatedAt: firstValue(source.generated_at, source.updated_at, source.generatedAt),
    source: firstValue(source.source, source.source_name),
    capabilityConfidence: nullableString(source.capability_provenance?.confidence),
    capabilityKind: nullableString(source.capability_provenance?.kind),
    capabilityWarning: nullableString(source.capability_provenance?.warning),
  };

  elements.modelCount.textContent = formatInteger(state.models.length);
  elements.catalogueVersion.textContent = state.catalogueMeta.version
    ? `v${String(state.catalogueMeta.version).replace(/^v/i, "")} · ${state.catalogueMeta.revision ?? "révision locale"}`
    : `${formatInteger(state.models.length)} cartes chargées`;
  rebuildAccounts();
}

function hydrateRuntime(payload) {
  const source = payload && typeof payload === "object" ? payload : {};
  const healthData = source.health?.data && typeof source.health.data === "object" ? source.health.data : {};
  const budgetData = source.budgets?.data && typeof source.budgets.data === "object" ? source.budgets.data : {};
  const performanceData =
    source.performance?.data && typeof source.performance.data === "object"
      ? source.performance.data
      : null;
  const providerIntegrationData =
    source.provider_integrations?.data &&
    typeof source.provider_integrations.data === "object"
      ? source.provider_integrations.data
      : {};
  state.healthDeployments = firstArray(healthData.providers).map((provider) => ({
    id: String(firstValue(provider?.id, "")),
    accountId: nullableString(provider?.provider_account_id),
    available: Boolean(provider?.available),
    reason: nullableString(provider?.reason),
    role: nullableString(provider?.role),
  }));
  const budgetAccounts = firstArray(budgetData.accounts);
  const providerBudgets = firstArray(budgetData.providers);
  state.runtimeBudgetAccounts = budgetAccounts;
  state.runtimeProviderBudgets = providerBudgets;
  state.runtimePerformance = performanceData;
  state.providerIntegrations = firstArray(providerIntegrationData.provider_accounts);
  state.runtime = {
    status: source.overall_available
      ? normalizeRuntimeHealth(firstValue(healthData.status, "online"))
      : source.health?.available || source.budgets?.available
        ? "degraded"
        : "offline",
    overallAvailable: Boolean(source.overall_available),
    routerMode: firstValue(healthData.mode, source.router?.mode, source.router_mode, "supervisé"),
    routerStrategy: firstValue(source.router?.strategy, source.router_strategy),
    escalation: firstValue(source.router?.escalation, source.escalation_policy),
    decisionCount: numberOrNull(
      firstValue(source.metrics?.decision_count, source.decision_count, source.routing_decisions),
    ),
    updatedAt: firstValue(source.generated_at, source.updated_at, source.timestamp),
    healthErrorCode: nullableString(source.health?.error_code),
    budgetsErrorCode: nullableString(source.budgets?.error_code),
    performanceErrorCode: nullableString(source.performance?.error_code),
    providerIntegrationsErrorCode: nullableString(source.provider_integrations?.error_code),
    providerNetworkProbe: Boolean(healthData.provider_network_probe),
    availabilityScope: nullableString(healthData.provider_availability_scope),
  };
  rebuildAccounts();
}

function normalizeModel(raw, index, capabilityProfiles = {}) {
  const source = raw && typeof raw === "object" ? raw : {};
  const providerObject = source.provider && typeof source.provider === "object" ? source.provider : {};
  const pricing = normalizePricing(source.pricing ?? source.price ?? source.costs ?? {});
  const profileScores = source.profile ? capabilityProfiles[source.profile] : null;
  const capabilities = normalizeCapabilities(
    source.capabilities ?? source.scores ?? source.skills ?? profileScores ?? {},
  );
  const providerId = slugify(
    firstValue(
      source.provider_id,
      source.provider_account_id,
      source.vendor_id,
      providerObject.id,
      typeof source.provider === "string" ? source.provider : null,
      source.vendor,
      "inconnu",
    ),
  );
  const providerName = String(
    firstValue(
      providerObject.name,
      source.provider_name,
      source.developer,
      typeof source.provider === "string" ? source.provider : null,
      source.vendor,
      titleCase(providerId),
    ),
  );
  const id = String(firstValue(source.card_id, source.id, source.model_id, source.slug, `model-${index + 1}`));
  const exactId = String(
    firstValue(
      source.exact_model_id,
      source.api_model_id,
      source.model_id,
      source.slug,
      "Identifiant API à valider",
    ),
  );
  const rawStatus = source.status && typeof source.status === "object" ? source.status : {};
  const integrationStage = String(firstValue(source.integration_stage, "catalogued")).toLowerCase();
  const retired = Boolean(firstValue(source.retired, rawStatus.retired, false));
  const needsReview = Boolean(
    firstValue(source.needs_review, source.requires_verification, rawStatus.needs_review, false),
  );
  const statusString = String(
    firstValue(
      typeof source.status === "string" ? source.status : null,
      source.integration_status,
      source.availability,
      "",
    ),
  ).toLowerCase();

  let catalogueStatus = "catalogue";
  if (retired || /retir|deprecated|obsolete/.test(statusString)) catalogueStatus = "retired";
  else if (integrationStage === "quarantined") catalogueStatus = "review";
  else if (needsReview || /review|verif|incertain|unverified/.test(statusString)) catalogueStatus = "review";
  else if (integrationStage === "configured") catalogueStatus = "deployed";

  return {
    id,
    index: index + 1,
    name: String(firstValue(source.name, source.display_name, source.model_name, exactId)),
    exactId,
    providerId,
    providerName,
    providerAccountId: nullableString(
      firstValue(source.provider_account_id, source.account_id, providerObject.account_id),
    ),
    integrationStage,
    deploymentIds: normalizeStringArray(firstValue(source.deployment_ids, [])),
    runtimeDeployments: firstArray(source.runtime?.deployments).map((deployment) => ({
      deploymentId: String(firstValue(deployment?.deployment_id, deployment?.id, "")),
      accountId: nullableString(deployment?.provider_account_id),
      available: booleanOrNull(deployment?.available),
      reason: nullableString(deployment?.reason),
    })).filter((deployment) => deployment.deploymentId),
    entityKind: nullableString(source.entity_kind),
    developer: nullableString(source.developer),
    country: nullableString(source.country),
    family: nullableString(firstValue(source.family, source.model_family)),
    description: String(
      firstValue(
        source.short_description,
        source.description,
        source.use_case,
        "Carte catalogue en attente d’une description validée.",
      ),
    ),
    specialties: normalizeStringArray(
      firstValue(source.specialties, source.specialisations, source.strengths, source.tags, []),
    ),
    capabilities,
    pricing,
    contextWindow: numberOrNull(
      firstValue(
        source.context_window_tokens,
        source.context_window,
        source.context_tokens,
        source.max_context_tokens,
      ),
    ),
    languages: normalizeStringArray(firstValue(source.languages, source.langues, [])),
    modalities: normalizeStringArray(firstValue(source.modalities, source.input_modalities, [])),
    limitations: normalizeStringArray(firstValue(source.limitations, source.constraints, [])),
    latencyP50Ms: numberOrNull(firstValue(source.latency_p50_ms, source.p50_latency_ms)),
    latencyP95Ms: numberOrNull(firstValue(source.latency_p95_ms, source.p95_latency_ms)),
    technicalMetadataConfidence: nullableString(source.technical_metadata_confidence),
    technicalMetadataSource: nullableString(source.technical_metadata_source),
    sourceLines: firstArray(source.source_lines).filter(
      (line) => Number.isInteger(line) && line > 0,
    ),
    availability: nullableString(firstValue(source.availability, rawStatus.availability)),
    catalogueStatus,
    deploymentDeclared: integrationStage === "configured",
    priceVerified:
      booleanOrNull(firstValue(pricing.verified, source.price_verified, rawStatus.price_verified)) ??
      (pricing.confidence ? pricing.confidence === "official_verified" : null),
    sourceLabel: nullableString(firstValue(source.source_label, source.source, source.pricing_source)),
    updatedAt: nullableString(firstValue(source.updated_at, source.last_verified_at)),
  };
}

function normalizePricing(raw) {
  let rawSchedules = Array.isArray(raw) ? raw : [];
  if (!Array.isArray(raw) && raw && typeof raw === "object" && Object.keys(raw).length) {
    const nested = raw.standard ?? raw.default;
    rawSchedules = [nested && typeof nested === "object" ? nested : raw];
  }
  const schedules = rawSchedules
    .filter((item) => item && typeof item === "object")
    .map(normalizePriceSchedule);
  const source = conservativePricingSchedule(schedules) ?? normalizePriceSchedule({});

  return {
    ...source,
    schedules,
    defaultMode: source.mode,
    selectionPolicy: "conservative_highest_reference_cost",
  };
}

function normalizePriceSchedule(source) {
  const value = source && typeof source === "object" ? source : {};

  return {
    currency: String(firstValue(value.currency, "USD")).toUpperCase(),
    inputPerMillion: numberOrNull(
      firstValue(
        value.input_per_million_usd,
        value.input_usd_per_million,
        value.input_per_million,
        value.input_price_per_million,
        value.input,
      ),
    ),
    outputPerMillion: numberOrNull(
      firstValue(
        value.output_per_million_usd,
        value.output_usd_per_million,
        value.output_per_million,
        value.output_price_per_million,
        value.output,
      ),
    ),
    cachedInputPerMillion: numberOrNull(
      firstValue(
        value.cached_input_per_million_usd,
        value.cached_input_per_million,
        value.cache_input,
        value.cached_input,
      ),
    ),
    basis: nullableString(firstValue(value.basis, value.unit, "par million de tokens")),
    type: String(firstValue(value.type, value.pricing_type, "token")),
    note: nullableString(firstValue(value.note, value.notes, value.conditions)),
    verified: booleanOrNull(firstValue(value.verified, value.price_verified)),
    mode: nullableString(value.mode),
    regionScope: nullableString(value.region_scope),
    confidence: nullableString(value.confidence),
    source: nullableString(value.source),
    declaredReference: numberOrNull(value.declared_simulation_usd),
  };
}

function conservativePricingSchedule(schedules) {
  if (!schedules.length) return null;
  return schedules.reduce((selected, candidate) => {
    const selectedCost = referenceCostForSchedule(selected);
    const candidateCost = referenceCostForSchedule(candidate);
    if (candidateCost === null) return selected;
    if (selectedCost === null || candidateCost > selectedCost) return candidate;
    return selected;
  });
}

function referenceCostForSchedule(schedule) {
  if (!schedule) return null;
  if (schedule.declaredReference !== null) return schedule.declaredReference;
  if (schedule.inputPerMillion === null || schedule.outputPerMillion === null) return null;
  return (
    (DEFAULT_SCENARIO.inputTokens / 1_000_000) * schedule.inputPerMillion +
    (DEFAULT_SCENARIO.outputTokens / 1_000_000) * schedule.outputPerMillion
  );
}

function normalizeCapabilities(raw) {
  const source = raw && typeof raw === "object" ? raw : {};
  const normalized = {};

  for (const [capability, aliases] of Object.entries(CAPABILITY_ALIASES)) {
    const value = aliases.map((alias) => source[alias]).find((candidate) => candidate !== undefined);
    const score = normalizeScore(value);
    if (score !== null) normalized[capability] = score;
  }

  return normalized;
}

function normalizeProvider(raw, index) {
  const source = raw && typeof raw === "object" ? raw : {};
  const fallback = typeof raw === "string" ? raw : `provider-${index + 1}`;
  const id = slugify(firstValue(source.id, source.provider_id, source.slug, fallback));
  return {
    id,
    name: String(firstValue(source.name, source.display_name, titleCase(id))),
    accountId: nullableString(firstValue(source.provider_account_id, source.account_id, source.id)),
    connectorStatus: nullableString(firstValue(source.connector_status, source.status)),
    balanceMode: nullableString(source.balance_mode),
    financialCapabilities:
      source.financial_capabilities && typeof source.financial_capabilities === "object"
        ? { ...source.financial_capabilities }
        : {},
  };
}

function normalizeAccount(raw, index) {
  const source = raw && typeof raw === "object" ? raw : {};
  const providerObject = source.provider && typeof source.provider === "object" ? source.provider : {};
  const providerId = slugify(
    firstValue(
      source.provider_id,
      providerObject.id,
      typeof source.provider === "string" ? source.provider : null,
      source.vendor,
      `provider-${index + 1}`,
    ),
  );
  const budgetSource = source.internal_budget ?? source.budget ?? {};
  const balanceSource = source.supplier_balance ?? source.provider_balance ?? {};
  const credentialState = String(firstValue(source.credential_state, "not_observable")).toLowerCase();

  return {
    id: String(firstValue(source.provider_account_id, source.account_id, source.id, `${providerId}-default`)),
    providerId,
    providerName: String(
      firstValue(
        source.provider_name,
        providerObject.name,
        typeof source.provider === "string" ? source.provider : null,
        titleCase(providerId),
      ),
    ),
    label: nullableString(firstValue(source.label, source.account_name, source.profile)),
    credentialState,
    credentialResolved: credentialState === "resolved_by_runtime",
    deploymentDeclared: Boolean(source.deployment_declared),
    lastRotatedAt: nullableString(
      firstValue(source.last_rotated_at, source.credential_updated_at, source.updated_at),
    ),
    runtimeStatus: String(firstValue(source.runtime_status, source.health, source.availability, "unknown")),
    budget: {
      limit: numberOrNull(
        firstValue(budgetSource.limit_usd, budgetSource.monthly_limit_usd, source.monthly_budget_usd),
      ),
      remaining: numberOrNull(
        firstValue(budgetSource.remaining_usd, budgetSource.available_usd, source.remaining_budget_usd),
      ),
      spent: numberOrNull(firstValue(budgetSource.spent_usd, source.monthly_spend_usd)),
      period: String(firstValue(budgetSource.period, "mensuel")),
      hardSharedCap: Boolean(budgetSource.hard_shared_cap),
      enforcementScope: nullableString(budgetSource.enforcement_scope),
    },
    supplierBalance: {
      supported: Boolean(
        firstValue(
          balanceSource.supported,
          balanceSource.available,
          source.provider_balance_supported,
          false,
        ),
      ),
      remaining: numberOrNull(
        firstValue(balanceSource.remaining_usd, balanceSource.amount_usd, balanceSource.remaining),
      ),
      limit: numberOrNull(firstValue(balanceSource.limit_usd, balanceSource.total_usd, balanceSource.limit)),
      updatedAt: nullableString(firstValue(balanceSource.updated_at, balanceSource.as_of)),
      currency: nullableString(firstValue(balanceSource.currency, source.provider_balance_currency)),
      accessMode: nullableString(
        firstValue(balanceSource.access_mode, source.balance_mode, source.cash_balance_mode),
      ),
      status: nullableString(firstValue(balanceSource.status, source.provider_balance_status)),
      errorCode: nullableString(
        firstValue(balanceSource.error_code, source.provider_balance_error_code),
      ),
      balances: firstArray(balanceSource.balances).map((balance) => ({
        currency: nullableString(balance?.currency),
        available: numberOrNull(balance?.available),
        billingType: nullableString(balance?.billingType ?? balance?.billing_type),
      })),
    },
    financialCapabilities:
      source.financial_capabilities && typeof source.financial_capabilities === "object"
        ? { ...source.financial_capabilities }
        : {},
  };
}

function rebuildAccounts() {
  if (!state.providers.length) {
    state.accounts = [];
    return;
  }

  state.accounts = state.providers.map((provider, index) => {
    const integration = state.providerIntegrations.find((item) => item?.id === provider.id);
    const financeState =
      integration?.finance_state && typeof integration.finance_state === "object"
        ? integration.finance_state
        : {};
    const financeBalances = firstArray(financeState.balances)
      .map((balance) => ({
        currency: nullableString(balance?.currency),
        available: numberOrNull(balance?.available),
        billingType: nullableString(balance?.billing_type),
      }))
      .filter((balance) => balance.available !== null);
    const providerModels = state.models.filter((model) => model.providerAccountId === provider.id);
    const budgetAccount = state.runtimeBudgetAccounts.find(
      (account) => account?.provider_account_id === provider.id,
    );
    const mappedDeploymentIds = state.runtimeProviderBudgets
      .filter((budget) => budget?.provider_account_id === provider.id)
      .map((budget) => String(firstValue(budget?.provider, budget?.id, "")))
      .filter(Boolean)
      .concat(normalizeStringArray(budgetAccount?.deployments))
      .concat(
        state.healthDeployments
          .filter((deployment) => deployment.accountId === provider.id)
          .map((deployment) => deployment.id),
      )
      .concat(
        state.models.flatMap((model) =>
          model.runtimeDeployments
            .filter((deployment) => deployment.accountId === provider.id)
            .map((deployment) => deployment.deploymentId),
        ),
      );
    const deploymentDeclared =
      providerModels.some((model) => model.integrationStage === "configured") || mappedDeploymentIds.length > 0;
    const deploymentIds = new Set(mappedDeploymentIds);
    const healthMatches = state.healthDeployments.filter((deployment) => deploymentIds.has(deployment.id));
    const embeddedMatches = state.models.flatMap((model) =>
      model.runtimeDeployments.filter(
        (deployment) => deployment.accountId === provider.id && deployment.available !== null,
      ),
    );
    const accountLimitMicro = numberOrNull(
      budgetAccount?.summed_deployment_limits?.monthly?.cost_microusd,
    );
    const accountSpentMicro = numberOrNull(budgetAccount?.usage?.monthly?.cost_microusd);
    const limitMicro = accountLimitMicro;
    const spentMicro = accountSpentMicro;
    const remainingMicro =
      limitMicro !== null && spentMicro !== null ? Math.max(0, limitMicro - spentMicro) : null;
    const credentialState =
      healthMatches.some((deployment) => deployment.available) ||
      embeddedMatches.some((deployment) => deployment.available)
      ? "resolved_by_runtime"
      : healthMatches.length || embeddedMatches.length
        ? "not_resolved_by_runtime"
        : "not_observable";

    return normalizeAccount(
      {
        provider_account_id: provider.id,
        provider_id: provider.id,
        provider_name: firstValue(integration?.display_name, provider.name),
        deployment_declared: deploymentDeclared,
        credential_state: credentialState,
        runtime_status: healthMatches.length || embeddedMatches.length
          ? healthMatches.some((deployment) => deployment.available) ||
            embeddedMatches.some((deployment) => deployment.available)
            ? "available"
            : "unavailable"
          : "unknown",
        internal_budget: {
          limit_usd: microusdToUsd(limitMicro),
          remaining_usd: microusdToUsd(remainingMicro),
          spent_usd: microusdToUsd(spentMicro),
          period: "mensuel",
          hard_shared_cap: Boolean(budgetAccount?.hard_shared_cap),
          enforcement_scope: nullableString(budgetAccount?.enforcement_scope),
        },
        supplier_balance: {
          supported: financeState.status === "ok",
          remaining: financeBalances[0]?.available,
          currency: financeBalances[0]?.currency,
          balances: financeBalances,
          updated_at: financeState.captured_at,
          access_mode: firstValue(
            financeState.cash_balance_mode,
            integration?.financial_capabilities?.cash_balance,
            provider.financialCapabilities.cash_balance,
            provider.balanceMode,
          ),
          status: firstValue(financeState.status, "never_refreshed"),
          error_code: financeState.error_code,
        },
        financial_capabilities: firstValue(
          integration?.financial_capabilities,
          provider.financialCapabilities,
        ),
      },
      index,
    );
  });
}

function bindNavigation() {
  const tabs = Array.from(document.querySelectorAll("[role='tab'][data-tab]"));

  for (const tab of tabs) {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
    tab.addEventListener("keydown", (event) => {
      const currentIndex = tabs.indexOf(event.currentTarget);
      let targetIndex = null;
      if (event.key === "ArrowRight") targetIndex = (currentIndex + 1) % tabs.length;
      if (event.key === "ArrowLeft") targetIndex = (currentIndex - 1 + tabs.length) % tabs.length;
      if (event.key === "Home") targetIndex = 0;
      if (event.key === "End") targetIndex = tabs.length - 1;
      if (targetIndex === null) return;
      event.preventDefault();
      switchTab(tabs[targetIndex].dataset.tab);
      tabs[targetIndex].focus();
    });
  }

  document.querySelectorAll("[data-open-costs]").forEach((button) => {
    button.addEventListener("click", () => switchTab("costs"));
  });
}

function switchTab(tabName) {
  const targetTab = document.querySelector(`[role="tab"][data-tab="${cssEscape(tabName)}"]`);
  if (!targetTab) return;

  state.activeTab = tabName;
  for (const tab of document.querySelectorAll("[role='tab'][data-tab]")) {
    const selected = tab.dataset.tab === tabName;
    tab.classList.toggle("tab-button--active", selected);
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
  }

  for (const panel of document.querySelectorAll("[role='tabpanel'][data-panel]")) {
    const selected = panel.dataset.panel === tabName;
    panel.hidden = !selected;
    panel.classList.toggle("tab-panel--active", selected);
  }

  if (tabName === "costs") renderCosts();
  if (tabName === "settings") renderSettings();
  window.scrollTo({ top: 0, behavior: prefersReducedMotion() ? "auto" : "smooth" });
}

function bindCatalogueControls() {
  elements.catalogueSearch.addEventListener("input", (event) => {
    state.search = event.target.value.trim().toLocaleLowerCase("fr");
    state.visibleLimit = 12;
    renderCatalogue();
  });

  const filterBindings = [
    [elements.providerFilter, "provider"],
    [elements.specialtyFilter, "specialty"],
    [elements.statusFilter, "status"],
    [elements.sortFilter, "sort"],
  ];
  for (const [element, property] of filterBindings) {
    element.addEventListener("change", (event) => {
      state[property] = event.target.value;
      state.visibleLimit = 12;
      updateAdvancedFilterBadge();
      renderCatalogue();
    });
  }

  document.querySelectorAll("[data-quick-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      state.quickFilter = button.dataset.quickFilter;
      state.visibleLimit = 12;
      document.querySelectorAll("[data-quick-filter]").forEach((candidate) => {
        const active = candidate === button;
        candidate.classList.toggle("filter-chip--active", active);
        candidate.setAttribute("aria-pressed", String(active));
      });
      renderCatalogue();
    });
  });

  elements.resetFilters.addEventListener("click", resetFilters);
  elements.showMore.addEventListener("click", () => {
    state.visibleLimit += 12;
    renderCatalogue();
  });

  document.querySelectorAll(".system-defaults [data-configure-account]").forEach((button) => {
    button.addEventListener("click", () => { void beginCredentialChange(button.dataset.configureAccount, button); });
  });

  elements.modelGrid.addEventListener("click", (event) => {
    const detailsButton = event.target.closest("[data-model-details]");
    if (detailsButton) {
      openModelDialog(detailsButton.dataset.modelDetails);
      return;
    }
    const configureButton = event.target.closest("[data-configure-account]");
    if (configureButton) void beginCredentialChange(configureButton.dataset.configureAccount, configureButton);
  });

  elements.catalogueState.addEventListener("click", (event) => {
    if (event.target.closest("[data-retry-load]")) void retryLoad();
    if (event.target.closest("[data-reset-empty]")) resetFilters();
  });

  elements.modelDialog.addEventListener("click", (event) => {
    if (event.target.closest("[data-close-dialog]")) elements.modelDialog.close();
    const configureButton = event.target.closest("[data-configure-account]");
    if (configureButton) void beginCredentialChange(configureButton.dataset.configureAccount, configureButton);
  });

  elements.modelDialog.addEventListener("click", (event) => {
    if (event.target !== elements.modelDialog) return;
    const rect = elements.modelDialog.getBoundingClientRect();
    const inside =
      event.clientX >= rect.left &&
      event.clientX <= rect.right &&
      event.clientY >= rect.top &&
      event.clientY <= rect.bottom;
    if (!inside) elements.modelDialog.close();
  });

  elements.modelDialog.addEventListener("change", (event) => {
    const selector = event.target.closest("[data-pricing-mode]");
    if (!selector || !state.activeModelId) return;
    state.pricingModes.set(state.activeModelId, selector.value);
    openModelDialog(state.activeModelId);
  });

  elements.credentialDialog.querySelector(".credential-dialog-close")?.addEventListener("click", () => {
    closeCredentialDialog();
  });
  elements.credentialDialog.addEventListener("cancel", (event) => {
    if (elements.credentialDialog.dataset.busy === "true") {
      event.preventDefault();
      return;
    }
    window.setTimeout(clearCredentialDialog, 0);
  });
  elements.credentialDialog.addEventListener("click", (event) => {
    if (event.target !== elements.credentialDialog || elements.credentialDialog.dataset.busy === "true") return;
    closeCredentialDialog();
  });
}

function bindRoutingControls() {
  const runtimeOnly = elements.routingForm.elements.namedItem("configured_only");
  const runtimeProject = elements.routingForm.elements.namedItem("project");
  const syncRuntimeProject = () => {
    runtimeProject.disabled = !runtimeOnly.checked;
  };
  runtimeOnly.addEventListener("change", syncRuntimeProject);
  syncRuntimeProject();

  document.querySelectorAll(".range-field input[type='range']").forEach((input) => {
    input.addEventListener("input", () => {
      const output = input.closest(".range-field")?.querySelector("output");
      if (output) output.textContent = `${input.value}/10`;
    });
  });

  elements.taskDescription.addEventListener("input", () => {
    elements.taskCharacterCount.textContent = String(elements.taskDescription.value.length);
  });

  elements.resetProfile.addEventListener("click", () => {
    const defaults = { reasoning: 5, code: 5, research: 3, speed: 6 };
    for (const [name, value] of Object.entries(defaults)) {
      const input = elements.routingForm.elements.namedItem(name);
      if (!input) continue;
      input.value = String(value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
  });

  elements.routingForm.addEventListener("submit", (event) => {
    event.preventDefault();
    void previewCandidates();
  });
}

function bindCostControls() {
  document.querySelectorAll("[data-scenario]").forEach((button) => {
    button.addEventListener("click", () => {
      elements.inputTokens.value = button.dataset.input;
      elements.outputTokens.value = button.dataset.output;
      document.querySelectorAll("[data-scenario]").forEach((candidate) => {
        candidate.classList.toggle("active", candidate === button);
      });
    });
  });

  for (const input of [elements.inputTokens, elements.outputTokens]) {
    input.addEventListener("input", () => {
      document.querySelectorAll("[data-scenario]").forEach((button) => button.classList.remove("active"));
    });
  }

  elements.applyScenario.addEventListener("click", () => {
    const inputTokens = clampInteger(elements.inputTokens.value, 0, 10_000_000_000);
    const outputTokens = clampInteger(elements.outputTokens.value, 0, 10_000_000_000);
    if (inputTokens === null || outputTokens === null) {
      showToast("Saisis des volumes de tokens valides.", "error");
      return;
    }
    state.scenario = { inputTokens, outputTokens, cachedInputTokens: 0 };
    state.backendEstimates.clear();
    updateScenarioLabels();
    renderCosts();
    renderCatalogue();
    void refreshBackendEstimates(true);
  });
}

function bindRuntimePopover() {
  elements.runtimePill.addEventListener("click", () => {
    const willOpen = elements.runtimePopover.hidden;
    elements.runtimePopover.hidden = !willOpen;
    elements.runtimePill.setAttribute("aria-expanded", String(willOpen));
  });
  elements.closeRuntimePopover.addEventListener("click", () => closeRuntimePopover());
  document.addEventListener("click", (event) => {
    if (elements.runtimePopover.hidden) return;
    if (elements.runtimePopover.contains(event.target) || elements.runtimePill.contains(event.target)) return;
    closeRuntimePopover();
  });
}

function closeRuntimePopover() {
  elements.runtimePopover.hidden = true;
  elements.runtimePill.setAttribute("aria-expanded", "false");
}

function bindGlobalShortcuts() {
  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      switchTab("catalogue");
      elements.catalogueSearch.focus();
    }
    if (event.key === "Escape" && !elements.runtimePopover.hidden) closeRuntimePopover();
  });
}

function populateFilters() {
  while (elements.providerFilter.options.length > 1) elements.providerFilter.remove(1);
  while (elements.specialtyFilter.options.length > 1) elements.specialtyFilter.remove(1);
  const providers = new Map();
  for (const provider of state.providers) providers.set(provider.id, provider.name);
  for (const model of state.models) providers.set(model.providerId, model.providerName);

  appendOptions(
    elements.providerFilter,
    Array.from(providers.entries())
      .sort((a, b) => a[1].localeCompare(b[1], "fr"))
      .map(([value, label]) => ({ value, label })),
  );

  const specialties = Array.from(
    new Set(state.models.flatMap((model) => model.specialties).filter(Boolean)),
  ).sort((a, b) => a.localeCompare(b, "fr"));
  appendOptions(
    elements.specialtyFilter,
    specialties.map((specialty) => ({ value: specialty.toLocaleLowerCase("fr"), label: specialty })),
  );
}

function appendOptions(select, options) {
  const fragment = document.createDocumentFragment();
  for (const { value, label } of options) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    fragment.append(option);
  }
  select.append(fragment);
}

function resetFilters() {
  state.search = "";
  state.provider = "";
  state.specialty = "";
  state.status = "";
  state.sort = "cost";
  state.quickFilter = "all";
  state.visibleLimit = 12;
  elements.catalogueSearch.value = "";
  elements.providerFilter.value = "";
  elements.specialtyFilter.value = "";
  elements.statusFilter.value = "";
  elements.sortFilter.value = "cost";
  document.querySelectorAll("[data-quick-filter]").forEach((button) => {
    const active = button.dataset.quickFilter === "all";
    button.classList.toggle("filter-chip--active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  updateAdvancedFilterBadge();
  renderCatalogue();
}

function updateAdvancedFilterBadge() {
  const count = [state.provider, state.specialty, state.status, state.sort !== "cost" ? state.sort : ""].filter(
    Boolean,
  ).length;
  elements.activeFilterCount.textContent = String(count);
  elements.activeFilterCount.hidden = count === 0;
}

function filteredModels() {
  const searchTokens = state.search.split(/\s+/).filter(Boolean);
  const result = state.models.filter((model) => {
    const account = accountForModel(model);
    const haystack = [
      model.name,
      model.exactId,
      model.providerName,
      model.description,
      model.family,
      ...model.specialties,
    ]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase("fr");

    if (searchTokens.some((token) => !haystack.includes(token))) return false;
    if (state.provider && model.providerId !== state.provider) return false;
    if (
      state.specialty &&
      !model.specialties.some((specialty) => specialty.toLocaleLowerCase("fr") === state.specialty)
    ) {
      return false;
    }
    if (state.status === "runtime_available" && runtimeAvailabilityForModel(model) !== true) return false;
    if (state.status && state.status !== "runtime_available" && model.catalogueStatus !== state.status) return false;
    if (state.quickFilter === "deployed" && !model.deploymentDeclared) return false;
    if (state.quickFilter === "credential_resolved" && !account?.credentialResolved) return false;
    if (state.quickFilter === "economical") {
      const estimate = estimateForModel(model);
      if (estimate === null || estimate > economicalThreshold()) return false;
    }
    return true;
  });

  return result.sort((left, right) => compareModels(left, right, state.sort));
}

function compareModels(left, right, sort) {
  if (sort === "name") return left.name.localeCompare(right.name, "fr");
  if (sort === "provider") {
    return left.providerName.localeCompare(right.providerName, "fr") || left.name.localeCompare(right.name, "fr");
  }
  if (sort === "fit") return averageCapability(right) - averageCapability(left) || compareCost(left, right);
  return compareCost(left, right);
}

function compareCost(left, right) {
  const leftCost = estimateForModel(left);
  const rightCost = estimateForModel(right);
  if (leftCost === null && rightCost === null) return left.name.localeCompare(right.name, "fr");
  if (leftCost === null) return 1;
  if (rightCost === null) return -1;
  return leftCost - rightCost || left.name.localeCompare(right.name, "fr");
}

function economicalThreshold() {
  const values = state.models.map(estimateForModel).filter((value) => value !== null).sort((a, b) => a - b);
  if (!values.length) return Number.POSITIVE_INFINITY;
  return values[Math.max(0, Math.floor(values.length * 0.35) - 1)];
}

function renderCatalogue() {
  if (state.catalogueError) return;
  const models = filteredModels();
  const visible = models.slice(0, state.visibleLimit);

  elements.catalogueState.setAttribute("aria-busy", "false");
  elements.resultCount.textContent = `${formatInteger(models.length)} résultat${models.length > 1 ? "s" : ""}`;

  if (!state.models.length) {
    elements.modelGrid.hidden = true;
    elements.paginationSentinel.hidden = true;
    elements.catalogueState.hidden = false;
    elements.catalogueState.innerHTML = stateCardMarkup(
      "◇",
      "Catalogue vide",
      "Aucune carte modèle n’a été fournie par le service local.",
      "",
    );
    return;
  }

  if (!models.length) {
    elements.modelGrid.hidden = true;
    elements.paginationSentinel.hidden = true;
    elements.catalogueState.hidden = false;
    elements.catalogueState.innerHTML = stateCardMarkup(
      "⌕",
      "Aucun modèle ne correspond",
      "Élargis la recherche ou retire un filtre pour retrouver des cartes.",
      '<button class="secondary-button" type="button" data-reset-empty>Effacer les filtres</button>',
    );
    return;
  }

  elements.catalogueState.hidden = true;
  elements.modelGrid.hidden = false;
  elements.modelGrid.innerHTML = visible.map(modelCardMarkup).join("");
  elements.paginationSentinel.hidden = visible.length >= models.length;
  elements.showMore.textContent = `Afficher ${formatInteger(Math.min(12, models.length - visible.length))} modèle${
    models.length - visible.length > 1 ? "s" : ""
  } de plus`;
  renderCosts();
}

function renderCatalogueError(title, message) {
  elements.catalogueState.setAttribute("aria-busy", "false");
  elements.catalogueState.hidden = false;
  elements.modelGrid.hidden = true;
  elements.paginationSentinel.hidden = true;
  elements.resultCount.textContent = "Indisponible";
  elements.catalogueState.innerHTML = stateCardMarkup(
    "!",
    title,
    message,
    '<button class="secondary-button" type="button" data-retry-load>Réessayer</button>',
  );
}

function modelCardMarkup(model) {
  const account = accountForModel(model);
  const accessEntries = runtimeAccessesForModel(model);
  const catalogueStatus = statusPresentation(model);
  const credentialState = accessSummaryPresentation(accessEntries);
  const capabilities = selectCardCapabilities(model.capabilities);
  const estimate = estimateForModel(model);
  const providerClass = providerThemeClass(model.providerId, model.providerName);
  const firstInput = model.pricing.inputPerMillion;
  const pricingMode = pricingModeLabel(model.pricing.defaultMode);
  const pricePrefix = model.priceVerified === false ? "≈ " : "";
  const specialtyMarkup = model.specialties
    .slice(0, 3)
    .map((specialty) => `<li>${escapeHTML(specialty)}</li>`)
    .join("");
  const configureButton = credentialButtonMarkup(model, account, "card-button");

  return `
    <article class="model-card ${providerClass}" data-model-id="${escapeAttribute(model.id)}">
      <div class="model-card__body">
        <div class="model-card__topline">
          <span class="model-number">CARTE ${String(model.index).padStart(3, "0")}</span>
          <span class="status-chip ${catalogueStatus.className}">${escapeHTML(catalogueStatus.label)}</span>
        </div>
        <div class="model-card__identity">
          <span class="provider-symbol" aria-hidden="true">${escapeHTML(providerInitials(model.providerName))}</span>
          <div class="model-card__title">
            <h3 title="${escapeAttribute(model.name)}">${escapeHTML(model.name)}</h3>
            <span class="model-exact-id" title="${escapeAttribute(model.exactId)}">${escapeHTML(model.exactId)}</span>
          </div>
          <span class="status-chip ${credentialState.className}">
            ${escapeHTML(credentialState.shortLabel)}
          </span>
        </div>
        <p class="model-card__description">${escapeHTML(model.description)}</p>
        ${modelAccessMarkup(model)}
        ${specialtyMarkup ? `<ul class="badge-list" aria-label="Spécialités">${specialtyMarkup}</ul>` : ""}
        <div class="capability-grid" role="group" aria-label="Compétences principales">
          ${capabilities.map(([key, score]) => capabilityMarkup(key, score)).join("")}
        </div>
        <p class="capability-provenance">Prior éditorial · confiance faible · pas un benchmark</p>
        <div class="price-panel">
          <div>
            <span>Entrée / 1 M · ${escapeHTML(pricingMode)}</span>
            <strong>${firstInput === null ? "À vérifier" : `${pricePrefix}${formatCurrency(firstInput)}`}</strong>
          </div>
          <span class="price-panel__divider" aria-hidden="true"></span>
          <div class="price-panel__simulation">
            <span>Simulation conservatrice / mois</span>
            <strong>${estimate === null ? "Non chiffrée" : `${pricePrefix}${formatCurrency(estimate)}`}</strong>
          </div>
        </div>
        ${pricingVariantsMarkup(model, "card")}
        ${modelBudgetMarkup(model)}
      </div>
      <div class="model-card__actions">
        <button class="card-button" type="button" data-model-details="${escapeAttribute(model.id)}" aria-label="Voir la fiche complète de ${escapeAttribute(model.name)}">
          Voir la fiche
        </button>
        ${configureButton}
      </div>
    </article>`;
}

function capabilityMarkup(key, score) {
  const label = CAPABILITY_LABELS[key] ?? titleCase(key);
  return `
    <div class="capability-item">
      <span class="capability-item__label"><span>${escapeHTML(label)}</span><strong>${formatScore(score)}/10</strong></span>
      <progress max="10" value="${escapeAttribute(score)}" aria-label="${escapeAttribute(label)} : ${escapeAttribute(formatScore(score))} sur 10"></progress>
    </div>`;
}

function budgetMarkup(account) {
  if (!account) {
    return '<div class="model-budget"><span class="balance-note">Budget interne non défini · solde fournisseur inconnu</span></div>';
  }

  const budget = account.budget;
  const balance = account.supplierBalance;
  const hasBudget = budget.limit !== null && budget.limit > 0;
  const remaining = budget.remaining ?? (budget.spent !== null && hasBudget ? Math.max(0, budget.limit - budget.spent) : null);
  const percent = hasBudget && remaining !== null ? Math.max(0, Math.min(100, (remaining / budget.limit) * 100)) : null;
  const observedBalanceText = firstArray(balance.balances)
    .filter((item) => item?.available !== null && item?.available !== undefined)
    .map((item) =>
      item.currency
        ? formatMoney(item.available, item.currency)
        : `${formatNumber(item.available, 4)} (devise non exposée)`,
    )
    .join(" · ");
  const providerBalance =
    balance.supported && observedBalanceText
      ? `Solde fournisseur : ${observedBalanceText}`
      : balance.accessMode === "direct_api"
        ? "Solde API non encore actualisé"
        : balance.accessMode === "cloud_billing_api" || balance.accessMode === "admin_api"
          ? "Solde : accès financier séparé requis"
          : balance.accessMode === "console_only"
            ? "Solde visible dans la console fournisseur"
            : "Solde fournisseur non exposé par API";

  if (!hasBudget || remaining === null) {
    return `<div class="model-budget"><span class="balance-note">Budget interne non défini · ${escapeHTML(providerBalance)}</span></div>`;
  }

  return `
    <div class="model-budget">
      <div class="budget-label">
        <span>${budget.hardSharedCap ? "Plafond partagé du compte restant" : "Somme indicative des budgets restants"}</span>
        <strong>${formatCurrency(remaining)} / ${formatCurrency(budget.limit)}</strong>
      </div>
      <progress max="100" value="${escapeAttribute(percent)}" aria-label="Budget interne restant : ${escapeAttribute(Math.round(percent))} pour cent"></progress>
      <span class="balance-note">${budget.hardSharedCap ? "Plafond partagé appliqué" : "Limites appliquées par déploiement"} · ${escapeHTML(providerBalance)}</span>
    </div>`;
}

function modelBudgetMarkup(model) {
  const accountIds = Array.from(
    new Set(runtimeAccessesForModel(model).map((entry) => entry.accountId).filter(Boolean)),
  );
  if (!accountIds.length) return budgetMarkup(accountForModel(model));
  if (accountIds.length === 1) return budgetMarkup(state.accounts.find((account) => account.id === accountIds[0]));

  return `<div class="multi-budget" role="group" aria-label="Budgets internes par compte">
    ${accountIds.map((accountId) => {
      const account = state.accounts.find((candidate) => candidate.id === accountId);
      return `<div><strong>${escapeHTML(account?.providerName ?? accountId)}</strong>${budgetMarkup(account)}</div>`;
    }).join("")}
  </div>`;
}

function credentialButtonMarkup(model, account, baseClass) {
  const runtimeAccountIds = Array.from(
    new Set(runtimeAccessesForModel(model).map((entry) => entry.accountId).filter(Boolean)),
  );
  if (baseClass === "card-button" && runtimeAccountIds.length > 1) {
    return `<button class="${baseClass} card-button--configure" type="button" data-model-details="${escapeAttribute(model.id)}" aria-label="Gérer les ${runtimeAccountIds.length} accès de ${escapeAttribute(model.name)}">Gérer ${runtimeAccountIds.length} accès</button>`;
  }
  const accountId =
    runtimeAccountIds.length === 1
      ? runtimeAccountIds[0]
      : model.providerAccountId ?? account?.id ?? providerAccountId(model.providerId);
  if (!accountId) {
    return `<button class="${baseClass}" type="button" disabled title="Ajoute d’abord un compte fournisseur dans le registre natif">Connecteur requis</button>`;
  }
  const targetAccount = state.accounts.find((candidate) => candidate.id === accountId) ?? account;
  const resolved = targetAccount?.credentialResolved === true;
  const verb = resolved ? "Changer l’accès" : "Configurer l’accès";
  return `<button class="${baseClass} ${baseClass === "card-button" ? "card-button--configure" : ""}" type="button" data-configure-account="${escapeAttribute(accountId)}" aria-label="${verb} pour ${escapeAttribute(targetAccount?.providerName ?? model.providerName)}">${verb}</button>`;
}

function runtimeAccessesForModel(model) {
  const entries = model.deploymentIds.map((deploymentId) => {
    const embeddedDeployment = model.runtimeDeployments.find(
      (deployment) => deployment.deploymentId === deploymentId,
    );
    const budgetDeployment = state.runtimeProviderBudgets.find(
      (budget) => String(firstValue(budget?.provider, budget?.id, "")) === deploymentId,
    );
    const health = state.healthDeployments.find((deployment) => deployment.id === deploymentId);
    const accountId = nullableString(
      firstValue(
        embeddedDeployment?.accountId,
        budgetDeployment?.provider_account_id,
        health?.accountId,
        model.providerAccountId,
      ),
    );
    const account = accountId ? state.accounts.find((candidate) => candidate.id === accountId) : null;
    return {
      deploymentId,
      accountId,
      accountName: account?.providerName ?? accountId ?? model.providerName,
      credentialState: health || (embeddedDeployment && embeddedDeployment.available !== null)
        ? (health?.available ?? embeddedDeployment?.available)
          ? "resolved_by_runtime"
          : "not_resolved_by_runtime"
        : "not_observable",
      runtimeAvailable: health ? health.available : embeddedDeployment?.available ?? null,
      catalogueFallback: false,
    };
  });

  if (entries.length) return entries;
  const account = accountForModel(model);
  return [
    {
      deploymentId: null,
      accountId: model.providerAccountId ?? account?.id ?? null,
      accountName: account?.providerName ?? model.providerName,
      credentialState: "not_observable",
      runtimeAvailable: null,
      catalogueFallback: true,
    },
  ];
}

function accessSummaryPresentation(entries) {
  if (entries.length > 1) {
    const resolved = entries.filter((entry) => entry.credentialState === "resolved_by_runtime").length;
    return {
      shortLabel: `${resolved}/${entries.length} accès résolus`,
      className: resolved === entries.length ? "status-chip--ready" : resolved ? "status-chip--warning" : "status-chip--muted",
    };
  }
  return credentialPresentation(entries[0]?.credentialState);
}

function modelAccessMarkup(model, withButtons = false) {
  const entries = runtimeAccessesForModel(model);
  return `<div class="model-access-list" role="group" aria-label="Accès et comptes de cette carte">
    ${entries.map((entry) => {
      const presentation = credentialPresentation(entry.credentialState);
      const accountLabel = entry.accountName ?? "Compte runtime non attribuable";
      const scopeLabel = entry.catalogueFallback ? "compte catalogue · runtime non observable" : presentation.label.toLocaleLowerCase("fr");
      const action =
        withButtons && entry.accountId
          ? `<button class="mini-button" type="button" data-configure-account="${escapeAttribute(entry.accountId)}">Gérer</button>`
          : "";
      return `<div>
        <span class="access-dot ${presentation.className}" aria-hidden="true"></span>
        <span><strong>${escapeHTML(accountLabel)}</strong><small>${escapeHTML(scopeLabel)}</small></span>
        ${action}
      </div>`;
    }).join("")}
  </div>`;
}

function statusPresentation(model) {
  const runtimeAvailable = runtimeAvailabilityForModel(model);
  if (runtimeAvailable === true) return { label: "Runtime local disponible", className: "status-chip--ready" };
  if (runtimeAvailable === false) return { label: "Runtime local indisponible", className: "status-chip--danger" };
  if (model.catalogueStatus === "deployed") return { label: "Déploiement déclaré", className: "status-chip--warning" };
  if (model.catalogueStatus === "review") return { label: "En quarantaine", className: "status-chip--warning" };
  if (model.catalogueStatus === "retired") return { label: "Retiré", className: "status-chip--danger" };
  return { label: "Catalogue", className: "status-chip--catalogue" };
}

function credentialPresentation(credentialState) {
  if (credentialState === "resolved_by_runtime") {
    return {
      shortLabel: "Clé résolue",
      label: "Clé résolue par le runtime",
      className: "status-chip--ready",
    };
  }
  if (credentialState === "not_resolved_by_runtime") {
    return {
      shortLabel: "Clé non résolue",
      label: "Clé non résolue par le runtime",
      className: "status-chip--danger",
    };
  }
  return {
    shortLabel: "Clé non observable",
    label: "État de clé non observable",
    className: "status-chip--muted",
  };
}

function selectCardCapabilities(capabilities) {
  const preferred = ["reasoning", "code", "tools", "speed", "reliability", "research"];
  const selected = preferred.filter((key) => capabilities[key] !== undefined).slice(0, 4);
  if (selected.length < 4) {
    for (const key of Object.keys(capabilities)) {
      if (selected.includes(key)) continue;
      selected.push(key);
      if (selected.length === 4) break;
    }
  }
  return selected.map((key) => [key, capabilities[key]]);
}

function openModelDialog(modelId) {
  const model = state.models.find((candidate) => candidate.id === modelId);
  if (!model) return;
  state.activeModelId = modelId;
  const status = statusPresentation(model);
  const estimate = estimateForModel(model);
  const selectedSchedule = selectedPricingSchedule(model);
  const selectedEstimate = estimateForSchedule(selectedSchedule);
  const capabilityEntries = Object.entries(model.capabilities).sort((a, b) => b[1] - a[1]);
  const limitations = model.limitations.length
    ? `<ul class="dialog-list">${model.limitations.map((item) => `<li>${escapeHTML(item)}</li>`).join("")}</ul>`
    : '<p class="dialog-description">Aucune limitation documentée dans la carte. Cela ne signifie pas qu’il n’en existe aucune.</p>';
  const specialties = model.specialties.length ? model.specialties.join(", ") : "Non documentées";
  const languages = model.languages.length ? model.languages.join(", ") : "Non documentées";
  const latency = formatLatency(model.latencyP50Ms, model.latencyP95Ms);
  const technicalSource = model.technicalMetadataSource
    ? `${model.technicalMetadataSource}${model.sourceLines.length ? ` · lignes ${model.sourceLines.join(", ")}` : ""}`
    : "Non documentée";
  const technicalConfidence = technicalConfidenceLabel(model.technicalMetadataConfidence);
  const priceSource = model.pricing.source ?? "Non documentée";
  const priceConfidence = priceConfidenceLabel(model.pricing.confidence);

  elements.modelDialog.innerHTML = `
    <div class="dialog-sheet">
      <header class="dialog-header" aria-label="Fiche modèle ${escapeAttribute(model.name)}">
        <div>
          <p class="section-kicker">${escapeHTML(model.providerName)} · ${escapeHTML(status.label)}</p>
          <h2 id="model-dialog-title">${escapeHTML(model.name)}</h2>
          <span class="model-exact-id">${escapeHTML(model.exactId)}</span>
        </div>
        <button class="icon-button" type="button" data-close-dialog aria-label="Fermer la fiche">×</button>
      </header>
      <div class="dialog-content">
        <section class="dialog-section">
          <p class="dialog-description">${escapeHTML(model.description)}</p>
        </section>
        <section class="dialog-section">
          <h3>Accès et comptes pertinents</h3>
          ${modelAccessMarkup(model, true)}
          <p class="balance-note">« Résolu par le runtime » vérifie uniquement configuration + présence locale du secret. Aucun réseau fournisseur n’est testé.</p>
        </section>
        <section class="dialog-section">
          <h3>Compétences de la carte</h3>
          <div class="dialog-capabilities">
            ${
              capabilityEntries.length
                ? capabilityEntries.map(([key, score]) => capabilityMarkup(key, score)).join("")
                : '<p class="dialog-description">Scores en attente de validation.</p>'
            }
          </div>
          <p class="capability-provenance">Scores initiaux issus d’un prior éditorial à faible confiance, pas de benchmarks validés. Aucun apprentissage automatique n’est appliqué à cette carte.</p>
        </section>
        <section class="dialog-section">
          <h3>Tarification et simulation commune</h3>
          ${pricingModeSelectorMarkup(model, selectedSchedule)}
          <div class="dialog-price-grid">
            <div>
              <span>Entrée / 1 M</span>
              <strong>${formatNullableCurrency(selectedSchedule?.inputPerMillion ?? null)}</strong>
              <small>${model.priceVerified === false ? "Prix indicatif" : "USD"}</small>
            </div>
            <div>
              <span>Sortie / 1 M</span>
              <strong>${formatNullableCurrency(selectedSchedule?.outputPerMillion ?? null)}</strong>
              <small>${model.priceVerified === false ? "Prix indicatif" : "USD"}</small>
            </div>
            <div>
              <span>Entrée cache / 1 M</span>
              <strong>${formatNullableCurrency(selectedSchedule?.cachedInputPerMillion ?? null)}</strong>
              <small>${selectedSchedule?.cachedInputPerMillion == null ? "non communiqué" : "USD"}</small>
            </div>
            <div>
              <span>${escapeHTML(shortScenarioLabel())}</span>
              <strong>${formatNullableCurrency(selectedEstimate)}</strong>
              <small>hors cache et remises</small>
            </div>
          </div>
          ${pricingVariantsMarkup(model, "dialog")}
          <p class="balance-note">Par défaut, Atlas compare le mode le plus coûteux du scénario de référence : ${escapeHTML(pricingModeLabel(model.pricing.defaultMode))} à ${formatNullableCurrency(estimate)}. Source prix : ${escapeHTML(priceSource)} · ${escapeHTML(priceConfidence)}</p>
          ${selectedSchedule?.note ? `<p class="balance-note">${escapeHTML(selectedSchedule.note)}</p>` : ""}
        </section>
        <section class="dialog-section">
          <h3>Caractéristiques</h3>
          <dl class="dialog-facts">
            <div><dt>Contexte</dt><dd>${formatContext(model.contextWindow)}</dd></div>
            <div><dt>Spécialités</dt><dd>${escapeHTML(specialties)}</dd></div>
            <div><dt>Langues</dt><dd>${escapeHTML(languages)}</dd></div>
            <div><dt>Latence</dt><dd>${escapeHTML(latency)}</dd></div>
            <div><dt>Confiance technique</dt><dd>${escapeHTML(technicalConfidence)}</dd></div>
            <div><dt>Source technique</dt><dd>${escapeHTML(technicalSource)}</dd></div>
            <div><dt>Déploiement</dt><dd>${model.deploymentDeclared ? "Déclaré" : "Non déclaré"}</dd></div>
            <div><dt>Runtime local</dt><dd>${escapeHTML(status.label)}</dd></div>
            <div><dt>Réseau fournisseur</dt><dd>Non testé</dd></div>
          </dl>
        </section>
        <section class="dialog-section">
          <h3>Contraintes connues</h3>
          ${limitations}
        </section>
        <section class="dialog-section">
          ${modelBudgetMarkup(model)}
          <div class="dialog-actions">
            <button class="secondary-button secondary-button--wide" type="button" data-close-dialog>Fermer</button>
          </div>
        </section>
      </div>
    </div>`;
  if (!elements.modelDialog.open) elements.modelDialog.showModal();
}

async function previewCandidates() {
  if (!elements.routingForm.reportValidity()) return;
  const budgetTier = elements.routingForm.elements.namedItem("budget").value;
  const budgetCaps = {
    low: 10_000_000,
    medium: 50_000_000,
    high: 200_000_000,
    exceptional: null,
  };
  const requirements = {
    reasoning: Number(elements.routingForm.elements.namedItem("reasoning").value),
    code: Number(elements.routingForm.elements.namedItem("code").value),
    research: Number(elements.routingForm.elements.namedItem("research").value),
    speed: Number(elements.routingForm.elements.namedItem("speed").value),
  };
  if (elements.routingForm.elements.namedItem("tools_required").checked) requirements.tools = 6;
  const runtimeOnly = elements.routingForm.elements.namedItem("configured_only").checked;
  const request = {
    source: runtimeOnly ? "runtime" : "local",
    requirements,
    required_specialties: elements.routingForm.elements.namedItem("context").value
      ? [elements.routingForm.elements.namedItem("context").value]
      : [],
    maximum_monthly_cost_microusd: budgetCaps[budgetTier],
    runtime_only: runtimeOnly,
    limit: 30,
  };
  if (runtimeOnly) request.project_id = elements.routingForm.elements.namedItem("project").value;

  setButtonBusy(elements.previewRouting, true, "Analyse…");
  elements.routingResults.hidden = false;
  elements.candidateList.innerHTML = '<div class="inline-loading"><span></span> Classement des ressources…</div>';

  try {
    const response = await callNative("preview_candidates", { request });
    let candidates = firstArray(response?.candidates, response?.ranked_candidates, response?.results, response);
    renderCandidates(candidates, response);
  } catch (error) {
    elements.candidateList.innerHTML = stateCardMarkup(
      "!",
      "Prévisualisation indisponible",
      readableError(error, "Le routeur local n’a pas pu classer les candidats."),
      "",
    );
  } finally {
    setButtonBusy(elements.previewRouting, false);
  }
}

function renderCandidates(rawCandidates, response = {}) {
  if (!rawCandidates.length) {
    elements.candidateList.innerHTML = stateCardMarkup(
      "◇",
      "Aucun candidat admissible",
      "Les contraintes actuelles excluent tout le catalogue. Retire une exigence ou désactive le filtre des déploiements déclarés.",
      "",
    );
    return;
  }

  elements.candidateList.innerHTML = rawCandidates.slice(0, 8).map((raw, index) => {
    const source = raw && typeof raw === "object" ? raw : {};
    const modelId = String(firstValue(source.model_id, source.id, source.card_id, ""));
    const model = state.models.find((candidate) => candidate.id === modelId || candidate.exactId === modelId);
    const name = String(firstValue(source.model_name, source.name, model?.name, modelId || "Modèle inconnu"));
    const selectedAccountId = nullableString(source.provider_account_id);
    const selectedAccount = selectedAccountId
      ? state.providers.find((candidate) => candidate.id === selectedAccountId)
      : null;
    const provider = String(
      firstValue(source.provider_name, source.provider, selectedAccount?.name, model?.providerName, "Fournisseur inconnu"),
    );
    const selectedDeploymentId = nullableString(source.selected_deployment_id);
    const accessDescription = selectedDeploymentId
      ? `${provider} · compte ${selectedAccountId ?? "non communiqué"} · ${selectedDeploymentId}`
      : `${provider} · compte catalogue principal`;
    const score = normalizeScore(firstValue(source.score, source.fit_score, source.total_score));
    const margin = numberOrNull(source.capability_margin);
    const reasons = normalizeStringArray(firstValue(source.reasons, source.strengths, source.explanation, []));
    if (!reasons.length) {
      if (source.integration_stage === "configured") reasons.push("déploiement déclaré");
      reasons.push("seuils de capacités respectés", "trié par coût conservateur");
      if (source.runtime_available === true) reasons.push("disponible dans le runtime");
    }
    const estimatedCost = microusdToUsd(
      firstValue(source.conservative_monthly_cost_microusd, source.estimated_cost_microusd),
    ) ?? numberOrNull(firstValue(source.estimated_cost_usd, source.cost_usd));

    return `
      <article class="candidate-card ${index === 0 ? "candidate-card--winner" : ""}">
        <div class="candidate-heading">
          <span class="candidate-rank">${index + 1}</span>
          <div class="candidate-identity">
            <strong>${escapeHTML(name)}</strong>
            <span>${escapeHTML(accessDescription)}${estimatedCost === null ? "" : ` · ${formatCurrency(estimatedCost)} estimés`}</span>
          </div>
          <span class="candidate-score">${
            margin !== null ? `+${formatInteger(margin)}` : score === null ? "—" : `${formatScore(score)}/10`
          }</span>
        </div>
        ${
          reasons.length
            ? `<ul class="candidate-reasons">${reasons.slice(0, 4).map((reason) => `<li>${escapeHTML(reason)}</li>`).join("")}</ul>`
            : ""
        }
      </article>`;
  }).join("");

  if (response?.advisory_only) {
    elements.candidateList.insertAdjacentHTML(
      "beforeend",
      '<aside class="notice notice--neutral"><span aria-hidden="true">i</span><p>Prévisualisation consultative : ce classement n’autorise ni appel de modèle ni dépense.</p></aside>',
    );
  }
}

async function refreshBackendEstimates(notifyUser) {
  if (!state.models.length || !getInvoke()) return;
  try {
    const next = new Map();
    for (let offset = 0; offset < state.models.length; offset += 8) {
      const batch = state.models.slice(offset, offset + 8);
      const estimates = await Promise.all(
        batch.map((model) =>
          callNative("simulate_cost", {
            request: {
              card_id: model.id,
              pricing_mode: null,
              input_tokens: state.scenario.inputTokens,
              output_tokens: state.scenario.outputTokens,
            },
          }),
        ),
      );
      for (const estimate of estimates) {
        if (!estimate || typeof estimate !== "object" || !estimate.available) continue;
        const modelId = String(firstValue(estimate.card_id, ""));
        const amount = microusdToUsd(estimate.total_microusd);
        if (modelId && amount !== null) next.set(modelId, amount);
      }
    }
    state.backendEstimates = next;
    renderCosts();
    renderCatalogue();
    if (state.activeModelId && elements.modelDialog.open) openModelDialog(state.activeModelId);
    if (notifyUser) showToast("Simulation recalculée par le service local.");
  } catch (error) {
    if (notifyUser) {
      showToast(
        `Estimation locale affichée. Le simulateur natif répond : ${readableError(error, "indisponible")}`,
        "warning",
      );
    }
  }
}

function renderCosts() {
  updateScenarioLabels();
  if (!state.models.length) {
    elements.costList.innerHTML = state.catalogueError
      ? '<div class="inline-loading">Catalogue indisponible</div>'
      : '<div class="inline-loading">Aucun modèle chiffrable</div>';
    elements.cheapestCost.textContent = "—";
    elements.cheapestModel.textContent = "Catalogue indisponible";
    elements.medianCost.textContent = "—";
    elements.pricedCount.textContent = "0";
    elements.unknownPriceCount.textContent = "— prix à vérifier";
    return;
  }

  const priced = state.models
    .map((model) => ({ model, cost: estimateForModel(model) }))
    .filter((item) => item.cost !== null)
    .sort((a, b) => a.cost - b.cost);
  const unknownCount = state.models.length - priced.length;
  const median = priced.length
    ? priced.length % 2
      ? priced[Math.floor(priced.length / 2)].cost
      : (priced[priced.length / 2 - 1].cost + priced[priced.length / 2].cost) / 2
    : null;

  elements.cheapestCost.textContent = priced.length ? formatCurrency(priced[0].cost) : "—";
  elements.cheapestModel.textContent = priced.length ? priced[0].model.name : "Aucun prix connu";
  elements.medianCost.textContent = median === null ? "—" : formatCurrency(median);
  elements.pricedCount.textContent = formatInteger(priced.length);
  elements.unknownPriceCount.textContent = `${formatInteger(unknownCount)} prix à vérifier`;

  if (!priced.length) {
    elements.costList.innerHTML = stateCardMarkup(
      "$",
      "Prix non comparables",
      "Les cartes sont présentes mais aucun tarif entrée/sortie exploitable n’est validé.",
      "",
    );
    return;
  }

  elements.costList.innerHTML = priced.map(({ model, cost }, index) => `
    <div class="cost-list__row">
      <span class="cost-list__rank">${String(index + 1).padStart(2, "0")}</span>
      <div class="cost-list__model">
        <strong>${escapeHTML(model.name)}</strong>
        <span>${escapeHTML(model.providerName)} · ${model.priceVerified === false ? "indicatif" : "tarif catalogue"}${costVariantSummary(model)}</span>
      </div>
      <div class="cost-list__amount">
        <strong>${formatCurrency(cost)}</strong>
        <span>/ mois · défaut conservateur</span>
      </div>
    </div>`).join("");
}

function updateScenarioLabels() {
  elements.inputTokens.value = String(state.scenario.inputTokens);
  elements.outputTokens.value = String(state.scenario.outputTokens);
  elements.comparisonSummary.textContent = `${formatCompactTokens(state.scenario.inputTokens)} entrée + ${formatCompactTokens(
    state.scenario.outputTokens,
  )} sortie / mois · hors cache`;
}

function renderSettings() {
  const accounts = effectiveAccounts();
  elements.accountCount.textContent = `${formatInteger(accounts.length)} compte${accounts.length > 1 ? "s" : ""}`;

  if (!accounts.length) {
    elements.accountList.innerHTML = state.runtimeError
      ? stateCardMarkup(
          "!",
          "État des accès indisponible",
          readableError(state.runtimeError, "Le service local ne répond pas."),
          "",
        )
      : stateCardMarkup("◇", "Aucun compte déclaré", "Ajoute un compte dans le registre natif avant de configurer un accès.", "");
  } else {
    elements.accountList.innerHTML = accounts.map(accountCardMarkup).join("");
    elements.accountList.querySelectorAll("[data-configure-account]").forEach((button) => {
      button.addEventListener("click", () => void beginCredentialChange(button.dataset.configureAccount, button));
    });
    elements.accountList.querySelectorAll("[data-refresh-balance]").forEach((button) => {
      button.addEventListener("click", () =>
        void refreshProviderFinance(button.dataset.refreshBalance, button),
      );
    });
  }

  if (state.runtime) {
    elements.routerMode.textContent = String(state.runtime.routerMode);
    elements.routerMode.className = `status-chip ${state.runtime.overallAvailable ? "status-chip--ready" : "status-chip--warning"}`;
    if (state.runtime.routerStrategy) elements.routerStrategy.textContent = String(state.runtime.routerStrategy);
    if (state.runtime.escalation) elements.routerEscalation.textContent = String(state.runtime.escalation);
    elements.lastSync.textContent = formatDateTime(state.runtime.updatedAt ?? state.catalogueMeta.generatedAt);
    elements.decisionCount.textContent =
      state.runtime.decisionCount === null ? "Non communiqué" : formatInteger(state.runtime.decisionCount);
    renderPerformance();
  } else {
    elements.routerMode.textContent = "Inconnu";
    elements.routerMode.className = "status-chip status-chip--muted";
    elements.lastSync.textContent = formatDateTime(state.catalogueMeta.generatedAt);
    elements.decisionCount.textContent = "Non communiqué";
    renderPerformance();
  }
}

function renderPerformance() {
  const performance = state.runtimePerformance;
  const outcomes = firstArray(performance?.validated_outcomes);
  const policy = performance?.adaptive_policy;
  elements.validatedOutcomeCount.textContent = formatInteger(
    outcomes.reduce((total, item) => total + (numberOrNull(item?.validation_count) ?? 0), 0),
  );
  elements.adaptivePolicy.textContent = policy?.algorithm_version
    ? `${policy.algorithm_version} · ajustement borné à ±${formatNumber(
        (numberOrNull(policy.maximum_adjustment_ppm) ?? 0) / 10_000,
        1,
      )} %`
    : "Politique non disponible";

  if (!outcomes.length) {
    elements.performanceList.innerHTML = state.runtime?.performanceErrorCode
      ? stateCardMarkup(
          "!",
          "Historique indisponible",
          runtimeIssueDescription(state.runtime.performanceErrorCode),
          "",
        )
      : stateCardMarkup(
          "◇",
          "Pas encore de résultat validé",
          "Les premières validations humaines ou par test alimenteront ici la qualité observée.",
          "",
        );
    return;
  }

  elements.performanceList.innerHTML = outcomes
    .slice()
    .sort((left, right) =>
      (numberOrNull(right.validation_count) ?? 0) - (numberOrNull(left.validation_count) ?? 0),
    )
    .slice(0, 12)
    .map((item) => {
      const validations = numberOrNull(item.validation_count) ?? 0;
      const successes = numberOrNull(item.successful_validations) ?? 0;
      const successRate = validations ? (successes / validations) * 100 : 0;
      const quality = (numberOrNull(item.average_quality_milli) ?? 0) / 10;
      const cost = (numberOrNull(item.average_cost_microusd) ?? 0) / 1_000_000;
      return `
        <article class="performance-row">
          <div>
            <strong>${escapeHTML(String(firstValue(item.model, item.provider_id, "Modèle")))}</strong>
            <span>${escapeHTML(String(firstValue(item.task_type, "TÂCHE")))} · ${formatInteger(validations)} validation${validations > 1 ? "s" : ""}</span>
          </div>
          <div class="performance-row__metrics">
            <span>${formatNumber(successRate, 0)} % réussite</span>
            <span>${formatNumber(quality, 1)} % qualité</span>
            <span>${formatCurrency(cost)} moyen</span>
          </div>
        </article>`;
    })
    .join("");
}

function effectiveAccounts() {
  const fromProviders = state.providers
    .filter((provider) => provider.accountId)
    .map((provider) => ({
      id: provider.accountId,
      providerId: provider.id,
      providerName: provider.name,
      label: null,
      credentialState: "not_observable",
      credentialResolved: false,
      deploymentDeclared: false,
      lastRotatedAt: null,
      runtimeStatus: provider.connectorStatus ?? "unknown",
      budget: { limit: null, remaining: null, spent: null, period: "mensuel" },
      supplierBalance: { supported: false, remaining: null, limit: null, updatedAt: null },
    }));
  const accounts = state.accounts.length ? state.accounts.slice() : fromProviders;
  for (const preset of [
    {id: "deepseek", name: "DeepSeek", label: "Génération par défaut · DeepSeek Flash · prépayé"},
    {id: "jina", name: "Jina AI — Mémoire", label: "Embeddings v5 Text Small + Reranker v3.5 · une clé commune · prépayé"},
  ]) {
    const existing = accounts.find((account) => account.id === preset.id);
    if (existing) {
      existing.providerName = preset.name;
      existing.label = preset.label;
    } else {
      accounts.push({
        id: preset.id, providerId: preset.id, providerName: preset.name, label: preset.label,
        credentialState: "not_observable", credentialResolved: false,
        deploymentDeclared: true, lastRotatedAt: null, runtimeStatus: "unknown",
        budget: { limit: null, remaining: null, spent: null, period: "mensuel" },
        supplierBalance: { supported: false, remaining: null, limit: null, updatedAt: null },
      });
    }
  }
  return accounts.sort((a, b) => Number(["deepseek", "jina"].includes(b.id)) - Number(["deepseek", "jina"].includes(a.id)));
}

function accountCardMarkup(account) {
  const credential = credentialPresentation(account.credentialState);
  const ready = account.credentialResolved;
  const deploymentText = account.deploymentDeclared ? "Déploiement déclaré" : "Aucun déploiement déclaré";
  const statusText = `${credential.label} · ${deploymentText} · réseau fournisseur non testé`;
  const label = account.label ? ` · ${account.label}` : "";
  const canRefreshBalance = account.supplierBalance.accessMode === "direct_api";

  return `
    <article class="account-card ${["deepseek", "jina"].includes(account.id) ? "system-recommended" : ""}">
      ${["deepseek", "jina"].includes(account.id) ? '<span class="recommendation-badge">Recommandé par le système · Par défaut</span>' : ""}
      <div class="account-card__heading">
        <div class="account-card__identity">
          <strong>${escapeHTML(account.providerName)}${escapeHTML(label)}</strong>
          <span>${escapeHTML(account.id)}</span>
        </div>
        <div class="account-card__actions">
          ${canRefreshBalance ? `<button class="card-button" type="button" data-refresh-balance="${escapeAttribute(account.id)}">Actualiser le solde</button>` : ""}
          <button class="card-button ${ready ? "" : "card-button--configure"}" type="button" data-configure-account="${escapeAttribute(account.id)}">
            ${ready ? "Changer" : "Configurer"}
          </button>
        </div>
      </div>
      <div class="account-card__status ${ready ? "account-card__status--ready" : "account-card__status--warning"}">
        <span aria-hidden="true"></span><span>${escapeHTML(statusText)}</span>
      </div>
      ${financialCapabilityMarkup(account.financialCapabilities)}
      ${budgetMarkup(account)}
    </article>`;
}

function financialCapabilityMarkup(capabilities) {
  const source = capabilities && typeof capabilities === "object" ? capabilities : {};
  const labels = {
    cash_balance: "Solde",
    usage: "Usage",
    cost: "Coûts",
    spending_limit: "Plafond",
  };
  const entries = Object.entries(labels)
    .map(([key, label]) => [label, nullableString(source[key])])
    .filter((entry) => entry[1]);
  if (!entries.length) return "";
  return `<div class="finance-capabilities" role="group" aria-label="Capacités financières">
    ${entries
      .map(
        ([label, mode]) =>
          `<span title="${escapeAttribute(mode)}">${escapeHTML(label)} · ${escapeHTML(financeModeLabel(mode))}</span>`,
      )
      .join("")}
  </div>`;
}

function financeModeLabel(mode) {
  const labels = {
    direct_api: "API",
    admin_api: "API admin",
    cloud_billing_api: "facturation cloud",
    console_only: "console",
    not_available: "indisponible",
    not_applicable: "sans objet",
    provider_dependent: "variable",
    unknown: "à vérifier",
  };
  return labels[String(mode)] ?? String(mode).replaceAll("_", " ");
}

function beginCredentialChange(providerAccountId) {
  if (!providerAccountId || !getInvoke()) {
    showToast("Ce compte fournisseur ne peut pas ouvrir la saisie sécurisée.", "error");
    return;
  }

  const accountId = String(providerAccountId);
  const account = effectiveAccounts().find((candidate) => candidate.id === accountId);
  if (!account) {
    showToast("Ce compte fournisseur n’est pas déclaré dans le catalogue local.", "error");
    return;
  }
  clearCredentialDialog();
  state.credentialDialog.providerAccountId = account.id;
  state.credentialDialog.providerName = account.providerName;
  if (!elements.credentialDialog.open) elements.credentialDialog.showModal();
  renderCredentialForm();
}

function renderCredentialForm() {
  state.credentialDialog.operationComplete = false;
  elements.credentialDialog.dataset.busy = "false";
  elements.credentialDialog.querySelector(".credential-dialog-close").disabled = false;
  elements.credentialDialogContent.innerHTML = `
    <div class="credential-dialog-heading">
      <span class="credential-provider-mark" aria-hidden="true">${escapeHTML(providerInitials(state.credentialDialog.providerName))}</span>
      <div><small>COMPTE FOURNISSEUR</small><strong id="credential-provider-name"></strong><code id="credential-account-id"></code></div>
    </div>
    <form class="credential-entry-form" id="credential-entry-form" autocomplete="off" novalidate>
      <label class="password-field">
        <span>Mot de passe OpenBao</span>
        <span class="password-control"><input id="credential-openbao-password" type="password" minlength="14" maxlength="100" autocomplete="current-password" spellcheck="false" autocapitalize="none" required><button class="password-visibility" type="button" data-credential-reveal="openbao" aria-pressed="false">Afficher 15 s</button></span>
        <small>Autorise cette écriture unique dans le coffre local.</small>
      </label>
      <label class="password-field">
        <span>Clé API fournisseur</span>
        <span class="password-control"><input id="credential-api-key" type="password" minlength="8" maxlength="4096" autocomplete="off" spellcheck="false" autocapitalize="none" required><button class="password-visibility" type="button" data-credential-reveal="api" aria-pressed="false">Afficher 15 s</button></span>
        <small>La clé sera publiée aux services autorisés sans être renvoyée à Atlas.</small>
      </label>
      <ul class="password-checks credential-checks" aria-live="polite">
        <li id="credential-check-password"><i aria-hidden="true"></i>OpenBao : 14 à 100 caractères</li>
        <li id="credential-check-api"><i aria-hidden="true"></i>Clé API : 8 à 4 096 caractères</li>
        <li id="credential-check-format"><i aria-hidden="true"></i>ASCII imprimable, sans espace</li>
      </ul>
      <div class="security-note"><span aria-hidden="true">⌁</span><p>Après validation, les deux champs sont vidés puis supprimés du DOM. Aucun secret n’est journalisé ou placé dans le stockage du navigateur.</p></div>
      <div class="dialog-actions"><button class="secondary-button" type="button" id="credential-cancel">Annuler</button><button class="primary-button" type="submit" id="credential-save" disabled>Enregistrer dans OpenBao</button></div>
    </form>`;
  document.getElementById("credential-provider-name").textContent = state.credentialDialog.providerName;
  document.getElementById("credential-account-id").textContent = state.credentialDialog.providerAccountId;
  const form = document.getElementById("credential-entry-form");
  const openBaoInput = document.getElementById("credential-openbao-password");
  const apiKeyInput = document.getElementById("credential-api-key");
  const saveButton = document.getElementById("credential-save");
  const validate = () => {
    const password = openBaoInput.value;
    const apiKey = apiKeyInput.value;
    const validation = window.AtlasOnboarding.validateProviderCredential({
      openBaoPassword: password,
      apiKey,
    });
    toggleCredentialCheck("credential-check-password", validation.checks.openBaoLength, password.length > 0);
    toggleCredentialCheck("credential-check-api", validation.checks.apiKeyLength, apiKey.length > 0);
    toggleCredentialCheck(
      "credential-check-format",
      validation.checks.openBaoFormat && validation.checks.apiKeyFormat,
      password.length > 0 || apiKey.length > 0,
    );
    saveButton.disabled = !validation.valid;
    return !saveButton.disabled;
  };
  openBaoInput.addEventListener("input", validate);
  apiKeyInput.addEventListener("input", validate);
  bindCredentialVisibility(document.querySelector("[data-credential-reveal='openbao']"), openBaoInput);
  bindCredentialVisibility(document.querySelector("[data-credential-reveal='api']"), apiKeyInput);
  document.getElementById("credential-cancel").addEventListener("click", closeCredentialDialog);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!validate()) return;
    void saveProviderCredential(openBaoInput, apiKeyInput);
  });
  openBaoInput.focus();
}

function toggleCredentialCheck(id, met, touched) {
  const element = document.getElementById(id);
  if (!element) return;
  element.classList.toggle("is-met", met);
  element.classList.toggle("is-unmet", touched && !met);
}

function bindCredentialVisibility(button, input) {
  let timer = null;
  const hide = () => {
    input.type = "password";
    button.setAttribute("aria-pressed", "false");
    button.textContent = "Afficher 15 s";
    if (timer) {
      window.clearTimeout(timer);
      state.credentialDialog.visibilityTimers.delete(timer);
      timer = null;
    }
  };
  button.addEventListener("click", () => {
    if (button.getAttribute("aria-pressed") === "true") {
      hide();
      return;
    }
    input.type = "text";
    button.setAttribute("aria-pressed", "true");
    button.textContent = "Masquer";
    timer = window.setTimeout(hide, 15_000);
    state.credentialDialog.visibilityTimers.add(timer);
  });
  input.addEventListener("blur", () => {
    if (document.hidden) hide();
  });
}

async function saveProviderCredential(openBaoInput, apiKeyInput) {
  const request = {
    providerAccountId: state.credentialDialog.providerAccountId,
    openBaoPassword: openBaoInput.value,
    apiKey: apiKeyInput.value,
  };
  openBaoInput.type = "password";
  apiKeyInput.type = "password";
  openBaoInput.value = "";
  apiKeyInput.value = "";
  renderCredentialProgress("authenticate", 8);
  await attachCredentialProgressListener();
  let invocation;
  try {
    invocation = callNative("save_provider_credential", request);
  } finally {
    request.openBaoPassword = "";
    request.apiKey = "";
  }
  try {
    const response = await invocation;
    const status = String(firstValue(response?.status, response?.state, "failed")).toLowerCase();
    if (["saved", "rotated", "success", "succeeded", "complete", "completed"].includes(status)) {
      await renderCredentialSuccess();
      return;
    }
    renderCredentialFailure(credentialErrorCode(response, "save_failed"));
  } catch (error) {
    renderCredentialFailure(credentialErrorCode(error, "save_failed"));
  }
}

function renderCredentialProgress(phase, percent) {
  elements.credentialDialog.dataset.busy = "true";
  elements.credentialDialog.querySelector(".credential-dialog-close").disabled = true;
  scrubCredentialInputs();
  elements.credentialDialogContent.innerHTML = `
    <div class="credential-operation" role="status">
      <span class="onboarding-spinner" aria-hidden="true"></span>
      <p class="section-kicker">ÉCRITURE SÉCURISÉE</p>
      <h3 id="credential-progress-title">Authentification OpenBao</h3>
      <p id="credential-progress-detail">Atlas remet les valeurs au composant natif puis les retire immédiatement de l’interface.</p>
      <div class="install-progress" role="progressbar" aria-label="Progression de l’enregistrement" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><span id="credential-progress-bar"></span></div>
      <span class="credential-progress-value" id="credential-progress-value">0 %</span>
    </div>`;
  updateCredentialProgress(phase, percent);
}

async function attachCredentialProgressListener() {
  if (state.credentialDialog.unlisten) return;
  const listen = window.__TAURI__?.event?.listen;
  if (typeof listen !== "function") return;
  try {
    state.credentialDialog.unlisten = await listen("credential-progress", (event) => {
      const payload = parsePayload(event?.payload);
      if (!payload || typeof payload !== "object") return;
      const status = String(firstValue(payload.status, "running")).toLowerCase();
      if (["failed", "error"].includes(status)) {
        renderCredentialFailure(credentialErrorCode(payload, "save_failed"));
        return;
      }
      if (["complete", "completed", "saved", "success", "succeeded"].includes(status)) {
        void renderCredentialSuccess();
        return;
      }
      updateCredentialProgress(payload.phase, payload.percent);
    });
  } catch {
    state.credentialDialog.unlisten = null;
  }
}

function updateCredentialProgress(rawPhase, rawPercent) {
  const phases = {
    authenticate: ["Authentification OpenBao", "Validation locale du mot de passe du coffre"],
    validate: ["Validation de la clé", "Vérification du format sans appel au fournisseur"],
    store: ["Écriture dans le coffre", "Enregistrement chiffré et atomique dans OpenBao"],
    publish: ["Publication locale", "Mise à jour du bundle destiné aux services autorisés"],
    reload: ["Activation des services", "Rechargement des consommateurs concernés"],
    complete: ["Clé enregistrée", "Le secret est disponible pour le routeur local"],
  };
  const phase = String(rawPhase ?? "authenticate").toLowerCase().replace(/[ -]+/g, "_");
  const presentation = phases[phase] ?? phases.authenticate;
  const percent = Math.max(0, Math.min(100, Math.round(Number(rawPercent) || 0)));
  const progress = elements.credentialDialogContent.querySelector("[role='progressbar']");
  if (!progress) return;
  progress.setAttribute("aria-valuenow", String(percent));
  document.getElementById("credential-progress-bar").style.width = `${percent}%`;
  document.getElementById("credential-progress-title").textContent = presentation[0];
  document.getElementById("credential-progress-detail").textContent = presentation[1];
  document.getElementById("credential-progress-value").textContent = `${percent} %`;
}

async function renderCredentialSuccess() {
  if (state.credentialDialog.operationComplete) return;
  state.credentialDialog.operationComplete = true;
  stopCredentialProgressListener();
  elements.credentialDialog.dataset.busy = "false";
  elements.credentialDialog.querySelector(".credential-dialog-close").disabled = false;
  elements.credentialDialogContent.innerHTML = `
    <div class="credential-result" role="status">
      <span class="success-mark" aria-hidden="true"><i>✓</i></span>
      <p class="section-kicker">ACCÈS ENREGISTRÉ</p>
      <h3>La clé API est dans OpenBao.</h3>
      <p>Elle a été publiée uniquement aux services locaux autorisés. Atlas n’a reçu aucune copie en retour.</p>
      <div class="privacy-callout privacy-callout--success"><span aria-hidden="true">✓</span><p>Les champs sensibles ont été vidés et supprimés de l’interface.</p></div>
      <button class="primary-button primary-button--wide" id="credential-finish" type="button">Terminer</button>
    </div>`;
  document.getElementById("credential-finish").addEventListener("click", closeCredentialDialog);
  document.getElementById("credential-finish").focus();
  await refreshRuntime();
  showToast("La clé API a été enregistrée dans OpenBao.", "success");
}

function renderCredentialFailure(code) {
  if (state.credentialDialog.operationComplete) return;
  state.credentialDialog.operationComplete = true;
  stopCredentialProgressListener();
  elements.credentialDialog.dataset.busy = "false";
  elements.credentialDialog.querySelector(".credential-dialog-close").disabled = false;
  const errors = {
    authentication_failed: ["Mot de passe OpenBao refusé", "Vérifie le mot de passe du coffre, puis saisis de nouveau les deux valeurs."],
    credential_invalid: ["Valeurs refusées", "Le mot de passe ou la clé API ne respecte pas les limites du coffre local."],
    vault_locked: ["Coffre indisponible", "OpenBao est verrouillé ou ne répond pas. Vérifie son état local avant de réessayer."],
    invalid_api_key: ["Clé API refusée", "La valeur ne respecte pas le format accepté pour ce compte fournisseur."],
    provider_not_found: ["Compte inconnu", "Le compte fournisseur n’existe plus dans le registre local. Recharge Atlas."],
    invalid_provider_account_id: ["Compte inconnu", "Le compte fournisseur n’existe plus dans le registre local. Recharge Atlas."],
    publish_failed: ["Publication interrompue", "La clé n’a pas pu être publiée aux services. L’opération native a conservé son état transactionnel."],
    already_running: ["Écriture déjà en cours", "Attends la fin de l’autre modification de clé, puis réessaie."],
    helper_unavailable: ["Gestionnaire indisponible", "Le composant natif sécurisé est absent ou inaccessible sur cette machine."],
    credential_helper_failed: ["Gestionnaire interrompu", "Le canal natif vers le coffre local n’a pas pu terminer l’écriture."],
    credential_worker_failed: ["Gestionnaire interrompu", "Le processus natif chargé du coffre s’est arrêté avant la confirmation."],
    credential_write_failed: ["Écriture refusée", "OpenBao n’a pas confirmé l’enregistrement de la clé API."],
    invalid_helper_result: ["Confirmation invalide", "Le gestionnaire local n’a pas fourni de preuve d’écriture exploitable."],
    cancelled: ["Enregistrement annulé", "Aucune confirmation d’écriture n’a été reçue; saisis de nouveau les deux valeurs pour réessayer."],
    unsupported_platform: ["Fonction indisponible", "La gestion sécurisée des clés n’est pas disponible sur cette plateforme."],
    save_failed: ["Enregistrement interrompu", "La clé n’a pas pu être enregistrée. Les champs ont été effacés de l’interface."],
  };
  const [title, message] = errors[code] ?? errors.save_failed;
  elements.credentialDialogContent.innerHTML = `
    <div class="credential-result credential-result--failure" role="alert">
      <span class="failure-mark" aria-hidden="true">!</span>
      <p class="section-kicker">ACTION NÉCESSAIRE</p>
      <h3 id="credential-failure-title"></h3>
      <p id="credential-failure-message"></p>
      <div class="failure-detail"><div><small>CODE LOCAL</small><code id="credential-failure-code"></code></div></div>
      <div class="security-note"><span aria-hidden="true">⌁</span><p>Par sécurité, le mot de passe et la clé doivent être saisis à nouveau.</p></div>
      <div class="dialog-actions"><button class="secondary-button" id="credential-failure-close" type="button">Fermer</button><button class="primary-button" id="credential-retry" type="button">Réessayer</button></div>
    </div>`;
  document.getElementById("credential-failure-title").textContent = title;
  document.getElementById("credential-failure-message").textContent = message;
  document.getElementById("credential-failure-code").textContent = code;
  document.getElementById("credential-failure-close").addEventListener("click", closeCredentialDialog);
  document.getElementById("credential-retry").addEventListener("click", renderCredentialForm);
  document.getElementById("credential-retry").focus();
}

function credentialErrorCode(error, fallback) {
  const raw = error && typeof error === "object" ? firstValue(error.error_code, error.code) : error;
  const code = String(raw ?? "").trim().toLowerCase();
  return /^[a-z0-9_-]{1,80}$/.test(code) ? code : fallback;
}

function stopCredentialProgressListener() {
  if (typeof state.credentialDialog.unlisten === "function") state.credentialDialog.unlisten();
  state.credentialDialog.unlisten = null;
}

function closeCredentialDialog() {
  if (elements.credentialDialog.dataset.busy === "true") return;
  if (elements.credentialDialog.open) elements.credentialDialog.close();
  clearCredentialDialog();
}

function clearCredentialDialog() {
  stopCredentialProgressListener();
  scrubCredentialInputs();
  for (const timer of state.credentialDialog.visibilityTimers) window.clearTimeout(timer);
  state.credentialDialog.visibilityTimers.clear();
  if (elements.credentialDialogContent) elements.credentialDialogContent.replaceChildren();
  state.credentialDialog.providerAccountId = null;
  state.credentialDialog.providerName = null;
  state.credentialDialog.operationComplete = false;
}

function scrubCredentialInputs() {
  if (!elements.credentialDialogContent?.querySelectorAll) return;
  elements.credentialDialogContent
    .querySelectorAll("input[type='password'], #credential-openbao-password, #credential-api-key")
    .forEach((input) => {
      input.value = "";
      input.type = "password";
    });
}

async function refreshProviderFinance(providerAccountId, button) {
  if (!providerAccountId || !getInvoke()) {
    showToast("Le service local de solde n’est pas disponible.", "error");
    return;
  }
  const originalText = button.textContent;
  setButtonBusy(button, true, "Lecture…");
  try {
    const response = await callNative("refresh_provider_finance", { providerAccountId });
    if (String(response?.status) !== "refreshed") {
      throw new Error("Résultat de lecture du solde invalide");
    }
    await refreshRuntime();
    showToast("Le solde fournisseur a été actualisé sans conversion de devise.", "success");
  } catch (error) {
    showToast(
      readableError(error, "Le solde fournisseur n’a pas pu être actualisé."),
      "error",
    );
  } finally {
    setButtonBusy(button, false, originalText);
  }
}

async function refreshRuntime() {
  try {
    const payload = await callNative("get_runtime_snapshot");
    hydrateRuntime(payload);
    state.runtimeError = null;
    renderRuntimeStatus();
    renderSettings();
    renderCatalogue();
    if (state.activeModelId && elements.modelDialog.open) openModelDialog(state.activeModelId);
  } catch (error) {
    state.runtimeError = toError(error);
    renderRuntimeStatus();
  }
}

async function retryLoad() {
  state.catalogueError = null;
  state.runtimeError = null;
  elements.catalogueState.hidden = false;
  elements.catalogueState.setAttribute("aria-busy", "true");
  elements.catalogueState.innerHTML = `
    <div class="skeleton-grid" role="status" aria-label="Chargement du catalogue">
      <div class="skeleton-card"><span></span><span></span><span></span><span></span></div>
      <div class="skeleton-card"><span></span><span></span><span></span><span></span></div>
    </div>`;
  await boot();
}

function renderRuntimeStatus() {
  if (state.runtimeError && !state.runtime) {
    setRuntimeStatus(
      "warning",
      "État inconnu",
      `Catalogue local disponible, mais l’état runtime ne répond pas : ${readableError(state.runtimeError, "erreur inconnue")}`,
    );
    return;
  }
  const health = state.runtime?.status ?? "unknown";
  const resolved = state.accounts.filter((account) => account.credentialResolved).length;
  const total = state.accounts.length;
  const runtimeIssue = runtimeIssueDescription(
    state.runtime?.healthErrorCode ??
      state.runtime?.budgetsErrorCode ??
      state.runtime?.performanceErrorCode ??
      state.runtime?.providerIntegrationsErrorCode,
  );
  if (health === "offline" || health === "error") {
    setRuntimeStatus(
      "offline",
      "Runtime arrêté",
      `${runtimeIssue ? `${runtimeIssue} · ` : ""}${resolved}/${total} clés résolues localement · réseau fournisseur non testé`,
    );
  } else if (health === "degraded" || health === "warning") {
    setRuntimeStatus(
      "warning",
      "Mode dégradé",
      `${runtimeIssue ? `${runtimeIssue} · ` : ""}${resolved}/${total} clés résolues localement · réseau fournisseur non testé`,
    );
  } else {
    setRuntimeStatus("online", "Contrôle local prêt", `${resolved}/${total} clés résolues par le runtime · réseau fournisseur non testé`);
  }
}

function runtimeIssueDescription(code) {
  const messages = {
    catalogue_revision_mismatch: "catalogue runtime incompatible, migration requise",
    access_denied: "accès au socket refusé",
    socket_timeout: "délai du runtime dépassé",
    socket_unavailable: "socket local indisponible",
    response_too_large: "réponse runtime refusée car trop grande",
    invalid_response: "réponse runtime invalide",
  };
  return code ? messages[String(code)] ?? "état runtime non vérifiable" : "";
}

function setRuntimeStatus(kind, label, detail) {
  elements.runtimePill.className = `runtime-pill runtime-pill--${kind}`;
  elements.runtimePillLabel.textContent = label;
  elements.runtimeDetail.textContent = detail;
}

function estimateForModel(model) {
  if (state.backendEstimates.has(model.id)) return state.backendEstimates.get(model.id);
  if (state.backendEstimates.has(model.exactId)) return state.backendEstimates.get(model.exactId);
  return estimateForSchedule(model.pricing);
}

function estimateForSchedule(schedule) {
  if (!schedule) return null;
  const inputPrice = schedule.inputPerMillion;
  const outputPrice = schedule.outputPerMillion;
  if (inputPrice === null || outputPrice === null) {
    const isReferenceScenario =
      state.scenario.inputTokens === DEFAULT_SCENARIO.inputTokens &&
      state.scenario.outputTokens === DEFAULT_SCENARIO.outputTokens &&
      state.scenario.cachedInputTokens === 0;
    return isReferenceScenario ? schedule.declaredReference : null;
  }

  const uncachedInput = Math.max(0, state.scenario.inputTokens - state.scenario.cachedInputTokens);
  let amount = (uncachedInput / 1_000_000) * inputPrice;
  if (state.scenario.cachedInputTokens > 0) {
    const cachePrice = schedule.cachedInputPerMillion;
    if (cachePrice === null) return null;
    amount += (state.scenario.cachedInputTokens / 1_000_000) * cachePrice;
  }
  amount += (state.scenario.outputTokens / 1_000_000) * outputPrice;
  return Number.isFinite(amount) ? amount : null;
}

function selectedPricingSchedule(model) {
  const requestedMode = state.pricingModes.get(model.id);
  return (
    model.pricing.schedules.find((schedule) => schedule.mode === requestedMode) ??
    conservativePricingSchedule(model.pricing.schedules)
  );
}

function pricingModeSelectorMarkup(model, selectedSchedule) {
  if (model.pricing.schedules.length < 2) return "";
  return `<label class="pricing-mode-selector">
    <span>Mode tarifaire simulé</span>
    <select data-pricing-mode aria-label="Mode tarifaire simulé pour ${escapeAttribute(model.name)}">
      ${model.pricing.schedules.map((schedule) => `<option value="${escapeAttribute(schedule.mode)}"${schedule.mode === selectedSchedule?.mode ? " selected" : ""}>${escapeHTML(pricingModeLabel(schedule.mode))}</option>`).join("")}
    </select>
  </label>`;
}

function pricingVariantsMarkup(model, surface) {
  if (model.pricing.schedules.length < 2) return "";
  return `<div class="pricing-variants pricing-variants--${escapeAttribute(surface)}" role="group" aria-label="Toutes les grilles tarifaires">
    ${model.pricing.schedules.map((schedule) => {
      const cost = estimateForSchedule(schedule);
      const isDefault = schedule.mode === model.pricing.defaultMode;
      return `<div>
        <strong>${escapeHTML(pricingModeLabel(schedule.mode))}${isDefault ? " · défaut conservateur" : ""}</strong>
        <span>E ${formatNullableCurrency(schedule.inputPerMillion)} · S ${formatNullableCurrency(schedule.outputPerMillion)} · ${formatNullableCurrency(cost)} / mois</span>
      </div>`;
    }).join("")}
  </div>`;
}

function costVariantSummary(model) {
  if (model.pricing.schedules.length < 2) return "";
  const variants = model.pricing.schedules
    .map((schedule) => `${pricingModeLabel(schedule.mode)} ${formatNullableCurrency(estimateForSchedule(schedule))}`)
    .join(" · ");
  return ` · ${escapeHTML(variants)} · défaut conservateur`;
}

function pricingModeLabel(mode) {
  const labels = {
    standard: "Standard",
    off_peak: "Heures creuses",
    peak: "Heures pleines",
    simulation_only: "Simulation source",
  };
  return labels[String(mode)] ?? String(mode ?? "Tarif à vérifier").replaceAll("_", " ");
}

function accountForModel(model) {
  if (!state.accounts.length) return null;
  if (model.providerAccountId) {
    const exact = state.accounts.find((account) => account.id === model.providerAccountId);
    if (exact) return exact;
  }
  return state.accounts.find((account) => account.providerId === model.providerId) ?? null;
}

function runtimeAvailabilityForModel(model) {
  if (!model.deploymentIds.length) return null;
  const deployments = state.healthDeployments.filter((deployment) => model.deploymentIds.includes(deployment.id));
  if (deployments.length) return deployments.some((deployment) => deployment.available);
  const embedded = model.runtimeDeployments.filter((deployment) => deployment.available !== null);
  if (!embedded.length) return null;
  return embedded.some((deployment) => deployment.available);
}

function providerAccountId(providerId) {
  return state.providers.find((provider) => provider.id === providerId)?.accountId ?? null;
}

function stateCardMarkup(icon, title, message, actionMarkup) {
  return `
    <div class="state-card">
      <span class="state-card__icon" aria-hidden="true">${escapeHTML(icon)}</span>
      <h3>${escapeHTML(title)}</h3>
      <p>${escapeHTML(message)}</p>
      ${actionMarkup}
    </div>`;
}

function showToast(message, kind = "success") {
  let region = document.querySelector(".toast-region");
  if (!region) {
    region = document.createElement("div");
    region.className = "toast-region";
    region.setAttribute("role", "status");
    region.setAttribute("aria-live", "polite");
    document.body.append(region);
  }
  const toast = document.createElement("div");
  toast.className = `toast toast--${kind}`;
  toast.textContent = message;
  region.append(toast);
  window.setTimeout(() => toast.remove(), 5200);
}

function setButtonBusy(button, busy, temporaryLabel) {
  if (!button) return;
  if (busy) {
    button.dataset.originalLabel = button.textContent;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    if (temporaryLabel) button.textContent = temporaryLabel;
    return;
  }
  button.disabled = false;
  button.removeAttribute("aria-busy");
  button.textContent = temporaryLabel ?? button.dataset.originalLabel ?? button.textContent;
  delete button.dataset.originalLabel;
}

function firstArray(...candidates) {
  for (const candidate of candidates) {
    if (Array.isArray(candidate)) return candidate;
  }
  return [];
}

function firstValue(...candidates) {
  return candidates.find((value) => value !== undefined && value !== null && value !== "");
}

function normalizeStringArray(value) {
  if (Array.isArray(value)) {
    return value
      .map((item) => {
        if (typeof item === "string" || typeof item === "number") return String(item).trim();
        if (item && typeof item === "object") return String(firstValue(item.name, item.label, item.value, "")).trim();
        return "";
      })
      .filter(Boolean);
  }
  if (typeof value === "string") {
    return value
      .split(/[,;|]/)
      .map((item) => item.trim())
      .filter(Boolean);
  }
  return [];
}

function normalizeScore(value) {
  const numeric = numberOrNull(value);
  if (numeric === null) return null;
  if (numeric > 10 && numeric <= 100) return Math.round(numeric) / 10;
  return Math.max(0, Math.min(10, Math.round(numeric * 10) / 10));
}

function averageCapability(model) {
  const values = Object.values(model.capabilities);
  return values.length ? values.reduce((sum, score) => sum + score, 0) / values.length : 0;
}

function numberOrNull(value) {
  if (value === undefined || value === null || value === "") return null;
  if (typeof value === "string") {
    const normalized = value.trim().replace(/[$€£\s]/g, "").replace(",", ".");
    if (!normalized || /^(n\/?a|unknown|variable|inconnu)$/i.test(normalized)) return null;
    value = normalized;
  }
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : null;
}

function microusdToUsd(value) {
  const micro = numberOrNull(value);
  return micro === null ? null : micro / 1_000_000;
}

function booleanOrNull(value) {
  if (typeof value === "boolean") return value;
  if (typeof value === "string") {
    if (/^(true|yes|oui|1)$/i.test(value)) return true;
    if (/^(false|no|non|0)$/i.test(value)) return false;
  }
  return null;
}

function nullableString(value) {
  if (value === undefined || value === null || value === "") return null;
  return String(value);
}

function clampInteger(value, min, max) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < min || number > max) return null;
  return Math.round(number);
}

function normalizeRuntimeHealth(value) {
  const normalized = String(value ?? "unknown").toLowerCase();
  if (/healthy|ready|online|ok|active/.test(normalized)) return "online";
  if (/degrad|warn|partial/.test(normalized)) return "degraded";
  if (/offline|stopped|down|error|failed/.test(normalized)) return "offline";
  return "unknown";
}

function formatCurrency(value) {
  if (value === null || !Number.isFinite(Number(value))) return "—";
  const amount = Number(value);
  const maximumFractionDigits = amount < 0.01 ? 4 : amount < 1 ? 3 : amount < 1000 ? 2 : 0;
  return new Intl.NumberFormat("fr-FR", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 0,
    maximumFractionDigits,
  }).format(amount);
}

function formatMoney(value, currency) {
  if (value === null || !Number.isFinite(Number(value))) return "—";
  const normalizedCurrency = /^[A-Z]{3}$/.test(String(currency ?? "").toUpperCase())
    ? String(currency).toUpperCase()
    : "USD";
  try {
    return new Intl.NumberFormat("fr-FR", {
      style: "currency",
      currency: normalizedCurrency,
      maximumFractionDigits: Number(value) < 1 ? 4 : 2,
    }).format(Number(value));
  } catch {
    return `${formatNumber(Number(value), 4)} ${normalizedCurrency}`;
  }
}

function formatNullableCurrency(value) {
  return value === null ? "À vérifier" : formatCurrency(value);
}

function formatInteger(value) {
  return new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 0 }).format(value ?? 0);
}

function formatNumber(value, maximumFractionDigits = 1) {
  return new Intl.NumberFormat("fr-FR", { maximumFractionDigits }).format(value ?? 0);
}

function formatScore(value) {
  return Number.isInteger(value) ? String(value) : Number(value).toFixed(1).replace(".", ",");
}

function formatCompactTokens(value) {
  if (value >= 1_000_000_000) return `${trimDecimal(value / 1_000_000_000)} Md`;
  if (value >= 1_000_000) return `${trimDecimal(value / 1_000_000)} M`;
  if (value >= 1_000) return `${trimDecimal(value / 1_000)} k`;
  return formatInteger(value);
}

function trimDecimal(value) {
  return new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 1 }).format(value);
}

function shortScenarioLabel() {
  return `${formatCompactTokens(state.scenario.inputTokens)} in + ${formatCompactTokens(state.scenario.outputTokens)} out`;
}

function formatContext(value) {
  if (value === null) return "Non documenté";
  return `${formatCompactTokens(value)} tokens`;
}

function formatLatency(p50Ms, p95Ms) {
  if (p50Ms === null && p95Ms === null) return "Non mesurée";
  const values = [];
  if (p50Ms !== null) values.push(`p50 ${formatInteger(p50Ms)} ms`);
  if (p95Ms !== null) values.push(`p95 ${formatInteger(p95Ms)} ms`);
  return values.join(" · ");
}

function technicalConfidenceLabel(value) {
  const labels = {
    unknown: "Inconnue",
    source_declared_unverified: "Déclarée dans la source · non vérifiée",
    official_verified: "Vérifiée officiellement",
    observed: "Observée localement",
  };
  return labels[value] ?? "Inconnue";
}

function priceConfidenceLabel(value) {
  const labels = {
    declared_unverified: "déclaré · non vérifié",
    approximate: "approximatif",
    revalidation_required: "revalidation requise",
    discussed_reference: "référence discutée",
    shared_source_price: "prix partagé par la source",
    public_unverified: "public · non vérifié",
    promotion_unverified: "promotion · non vérifiée",
    official_verified: "vérifié officiellement",
  };
  return labels[value] ?? "confiance inconnue";
}

function formatDateTime(value) {
  if (!value) return "Non communiqué";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("fr-FR", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function providerInitials(name) {
  const words = String(name).trim().split(/\s+/).filter(Boolean);
  if (!words.length) return "IA";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return `${words[0][0]}${words[1][0]}`.toUpperCase();
}

function providerThemeClass(...values) {
  const haystack = values.join(" ").toLowerCase();
  if (/qwen|alibaba/.test(haystack)) return "model-card--qwen";
  if (/deepseek/.test(haystack)) return "model-card--deepseek";
  if (/openai|gpt/.test(haystack)) return "model-card--openai";
  if (/google|gemini/.test(haystack)) return "model-card--google";
  if (/anthropic|claude/.test(haystack)) return "model-card--anthropic";
  if (/mistral/.test(haystack)) return "model-card--mistral";
  if (/xai|grok/.test(haystack)) return "model-card--xai";
  return "";
}

function titleCase(value) {
  return String(value)
    .replace(/[-_]+/g, " ")
    .replace(/\b\p{L}/gu, (letter) => letter.toLocaleUpperCase("fr"));
}

function slugify(value) {
  return String(value ?? "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

function toCamelCase(value) {
  return value.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
}

function cssEscape(value) {
  if (window.CSS?.escape) return window.CSS.escape(String(value));
  return String(value).replace(/[^a-zA-Z0-9_-]/g, "");
}

function escapeHTML(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeAttribute(value) {
  return escapeHTML(value);
}

function toError(error) {
  if (error instanceof Error) return error;
  if (typeof error === "string") return new Error(error);
  if (error && typeof error === "object") {
    return new Error(String(firstValue(error.message, error.error, error.code, "Erreur inconnue")));
  }
  return new Error("Erreur inconnue");
}

function readableError(error, fallback) {
  const normalized = toError(error);
  const message = normalized.message.trim();
  return message && message !== "[object Object]" ? message : fallback;
}

function prefersReducedMotion() {
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}
