"use strict";

(function exposeAtlasOnboarding(globalObject, factory) {
  const api = factory();
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (globalObject) globalObject.AtlasOnboarding = api;
})(typeof window !== "undefined" ? window : null, () => {
  const MIN_PASSWORD_LENGTH = 14;
  const CORRECTIVE_PREDECESSOR_RELEASE_ID = "atlas-api-zulip-2026.09.08.10";
  const STEP_ORDER = ["welcome", "account", "credentials", "review", "install"];
  const TERMINAL_SUCCESS_STATES = new Set(["complete", "completed", "installed", "succeeded", "success"]);
  const RECOVERY_REQUIRED_STATES = new Set(["recovery-required", "recovery_required"]);
  const TERMINAL_FAILURE_STATES = new Set([
    "failed",
    "error",
    "cancelled",
    "canceled",
    "rolled_back",
    "rolled-back",
    "rollback_complete",
    "rollback-complete",
  ]);
  const RUNNING_STATES = new Set(["running", "installing", "in_progress", "started", "preflight-running"]);

  const INSTALL_PHASES = Object.freeze([
    { id: "preflight", label: "Vérifications préalables", detail: "Intégrité de la release et état de la machine" },
    { id: "authentication", label: "Authentification locale", detail: "Autorisation physique limitée à cette installation" },
    { id: "secrets", label: "Création des accès", detail: "OpenBao et propriétaire Zulip" },
    { id: "packages", label: "Installation des composants", detail: "Atlas, orchestrateur, Hermes et dépendances déclarées" },
    { id: "services", label: "Démarrage des services", detail: "Réseaux et services auto-hébergés" },
    { id: "verification", label: "Contrôles de fonctionnement", detail: "Santé, accès local et persistance" },
  ]);

  const PHASE_ALIASES = Object.freeze({
    checking: "preflight",
    validate: "preflight",
    validation: "preflight",
    preflight_passed: "preflight",
    preflight_complete: "preflight",
    authorize: "authentication",
    authorisation: "authentication",
    authorization: "authentication",
    enrollment: "authentication",
    polkit: "authentication",
    credentials: "secrets",
    secret_channel_ready: "secrets",
    confirmation: "secrets",
    openbao: "secrets",
    zulip: "secrets",
    install: "packages",
    installing: "packages",
    deployment: "packages",
    deploy: "packages",
    mutation_commit_ready: "packages",
    mutation_started: "packages",
    rollback_finalize_ready: "packages",
    rollback_finalizing: "verification",
    rollback_finalized: "verification",
    release_staged: "packages",
    recovery_armed: "packages",
    broker_quiesced: "packages",
    snapshotted: "packages",
    rollback_armed: "packages",
    services_quiesced: "packages",
    backup_complete: "packages",
    openbao_access: "packages",
    start: "services",
    services_installed: "services",
    health: "verification",
    verify: "verification",
    health_verified: "verification",
    cleanup_complete: "verification",
    finished: "complete",
    done: "complete",
    completed: "complete",
    success: "complete",
    succeeded: "complete",
  });

  let session = null;

  function firstDefined(...values) {
    return values.find((value) => value !== undefined && value !== null && value !== "");
  }

  function normalizeDigest(value) {
    const candidate = String(value ?? "").trim().toLowerCase().replace(/^sha256:/, "");
    return /^[a-f0-9]{64}$/.test(candidate) ? candidate : "";
  }

  function normalizeBootstrapStatus(rawValue) {
    const raw = rawValue && typeof rawValue === "object" ? rawValue : {};
    const release = raw.release && typeof raw.release === "object" ? raw.release : {};
    const state = String(firstDefined(raw.state, raw.status, raw.bootstrap_state, "required")).toLowerCase();
    const sourceTreeSha256 = normalizeDigest(
      firstDefined(raw.source_tree_sha256, raw.tree_sha256, release.source_tree_sha256, release.tree_sha256),
    );
    const manifestSha256 = normalizeDigest(
      firstDefined(raw.manifest_sha256, release.manifest_sha256, raw.release_manifest_sha256),
    );
    const rpmSha256 = normalizeDigest(firstDefined(raw.rpm_sha256, raw.atlas_rpm_sha256, release.rpm_sha256));
    const hermesSourceArchiveSha256 = normalizeDigest(
      firstDefined(
        raw.hermes_source_archive_sha256,
        raw.hermes_archive_sha256,
        release.hermes_source_archive_sha256,
      ),
    );
    const bootstrapHelperSha256 = normalizeDigest(
      firstDefined(raw.bootstrap_helper_sha256, raw.helper_sha256, release.bootstrap_helper_sha256),
    );
    const suppliedSuffix = String(firstDefined(raw.confirmation_digest_suffix, release.confirmation_digest_suffix, ""))
      .trim()
      .toLowerCase();
    const requiredValue = firstDefined(raw.required, raw.bootstrap_required, raw.installation_required);
    const required =
      typeof requiredValue === "boolean" ? requiredValue : !TERMINAL_SUCCESS_STATES.has(state);

    return {
      required,
      state,
      releaseId: String(firstDefined(raw.release_id, release.id, release.release_id, "")).trim(),
      sourceTreeSha256,
      manifestSha256,
      rpmSha256,
      hermesSourceArchiveSha256,
      bootstrapHelperSha256,
      confirmationDigestSuffix: /^[a-f0-9]{12}$/.test(suppliedSuffix) ? suppliedSuffix : "",
      preflightPassed: Boolean(
        firstDefined(raw.preflight_passed, raw.preflight?.passed, state === "preflight-passed"),
      ),
      localUsername: String(firstDefined(raw.local_username, raw.operator, "ops-user")),
      zulipOwnerEmail: String(firstDefined(raw.zulip_owner_email, raw.owner_email, "admin@ops.local")),
      zulipUrl: safeZulipUrl(firstDefined(raw.zulip_url, raw.urls?.zulip)),
      atlasAvailable: Boolean(firstDefined(raw.atlas_available, raw.application_available, false)),
      resumeAvailable: Boolean(firstDefined(raw.resume_available, false)),
      rollbackFinalizeResume: Boolean(firstDefined(raw.rollback_finalize_resume, false)),
      correctiveAvailable: Boolean(firstDefined(raw.corrective_available, false)),
      operationMode: String(firstDefined(raw.operation_mode, "")).trim().toLowerCase(),
      recoveryFromReleaseId: String(firstDefined(raw.recovery_from_release_id, "")).trim(),
      cancellationAllowed: Boolean(firstDefined(raw.cancellation_allowed, false)),
      phase: normalizePhase(firstDefined(raw.phase, raw.current_phase, state)),
      percent: normalizePercent(firstDefined(raw.percent, raw.progress_percent, 0)),
      errorCode: normalizeErrorCode(firstDefined(raw.error_code, raw.code)),
    };
  }

  function bootstrapRoute(status) {
    const state = String(status?.state ?? "").toLowerCase();
    // Recovery always wins over a contradictory `required: false` value. Atlas
    // must only become reachable after the native side publishes installed.
    if (RECOVERY_REQUIRED_STATES.has(state)) return "recovery";
    if (!status?.required || TERMINAL_SUCCESS_STATES.has(state)) return "atlas";
    if (RUNNING_STATES.has(state)) return "progress";
    if (TERMINAL_FAILURE_STATES.has(state)) return "failure";
    return "welcome";
  }

  function confirmationDigestSuffix(status) {
    const supplied = String(firstDefined(status?.confirmationDigestSuffix, status?.confirmation_digest_suffix, ""))
      .trim()
      .toLowerCase();
    return /^[a-f0-9]{12}$/.test(supplied) ? supplied : "";
  }

  function normalizePhase(value) {
    const phase = String(value ?? "preflight").trim().toLowerCase().replace(/[ -]+/g, "_");
    if (INSTALL_PHASES.some((candidate) => candidate.id === phase) || phase === "complete") return phase;
    return PHASE_ALIASES[phase] ?? "preflight";
  }

  function normalizePercent(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? Math.max(0, Math.min(100, Math.round(numeric))) : 0;
  }

  function normalizeErrorCode(value) {
    const code = String(value ?? "").trim().toLowerCase();
    return /^[a-z0-9_-]{1,80}$/.test(code) ? code : "unknown_error";
  }

  function safeZulipUrl(value) {
    const fallback = "https://zulip.ops.local:8443";
    if (!value) return fallback;
    try {
      const url = new URL(String(value));
      if (url.protocol !== "https:" || url.hostname !== "zulip.ops.local") return fallback;
      if (url.username || url.password) return fallback;
      return url.href.replace(/\/$/, "");
    } catch {
      return fallback;
    }
  }

  function validatePasswordSet(values) {
    const openBaoPassword = String(values?.openBaoPassword ?? "");
    const openBaoConfirmation = String(values?.openBaoConfirmation ?? "");
    const zulipPassword = String(values?.zulipPassword ?? "");
    const zulipConfirmation = String(values?.zulipConfirmation ?? "");
    const checks = {
      openBaoLength: openBaoPassword.length >= MIN_PASSWORD_LENGTH,
      openBaoBounds: openBaoPassword.length <= 100 && openBaoPassword === openBaoPassword.trim() && !/[\u0000-\u001f\u007f]/.test(openBaoPassword),
      openBaoMatch: openBaoPassword.length > 0 && openBaoPassword === openBaoConfirmation,
      zulipLength: zulipPassword.length >= MIN_PASSWORD_LENGTH,
      zulipBounds: zulipPassword.length <= 100 && zulipPassword === zulipPassword.trim() && !/[\u0000-\u001f\u007f]/.test(zulipPassword),
      zulipMatch: zulipPassword.length > 0 && zulipPassword === zulipConfirmation,
      passwordsDiffer: openBaoPassword.length > 0 && zulipPassword.length > 0 && openBaoPassword !== zulipPassword,
    };
    return { valid: Object.values(checks).every(Boolean), checks };
  }

  function validateProviderCredential(values) {
    const openBaoPassword = String(values?.openBaoPassword ?? "");
    const apiKey = String(values?.apiKey ?? "");
    const checks = {
      openBaoLength: openBaoPassword.length >= MIN_PASSWORD_LENGTH && openBaoPassword.length <= 100,
      openBaoFormat:
        openBaoPassword === openBaoPassword.trim() && !/[\u0000-\u001f\u007f]/.test(openBaoPassword),
      apiKeyLength: apiKey.length >= 8 && apiKey.length <= 4096,
      apiKeyFormat: /^[\x21-\x7e]+$/.test(apiKey),
    };
    return { valid: Object.values(checks).every(Boolean), checks };
  }

  function operationalError(errorCode, phase = "") {
    const code = normalizeErrorCode(errorCode);
    const category = (() => {
      if (["physical_auth_required", "physical_session_required", "enrollment_failed", "gui_invoker_invalid"].includes(code)) return "authentication_failed";
      if (["bootstrap_secret_invalid", "secret_invalid", "secret_mismatch", "credential_invalid", "gui_input_invalid"].includes(code)) return "password_policy_failed";
      if (["bootstrap_release_changed", "preflight_changed", "artifact_changed", "checksum_mismatch", "source_changed", "confirmation_rejected", "bootstrap_confirmation_rejected"].includes(code)) return "release_mismatch";
      if (["health_failed", "step_timeout", "step_failed"].includes(code)) return "service_unhealthy";
      if (["openbao_unavailable", "openbao_rejected", "openbao_invalid"].includes(code)) return "openbao_unavailable";
      if (code === "rollback_failed") return "rollback_failed";
      if (code === "bootstrap_busy") return "bootstrap_busy";
      return code;
    })();
    const catalogue = {
      authentication_cancelled: {
        title: "Authentification annulée",
        message: "L’autorisation physique n’a pas été accordée. Relance l’installation et valide la fenêtre système lorsqu’elle apparaît.",
        action: "Recommencer",
      },
      authentication_failed: {
        title: "Authentification refusée",
        message: "Le système n’a pas pu confirmer la session locale. Vérifie que tu utilises bien la session graphique de ops-user, puis réessaie.",
        action: "Recommencer",
      },
      password_policy_failed: {
        title: "Mots de passe refusés",
        message: "Un mot de passe ne respecte pas la politique du service. Saisis deux mots de passe différents d’au moins 14 caractères.",
        action: "Saisir à nouveau",
      },
      preflight_failed: {
        title: "Vérification préalable interrompue",
        message: "Atlas n’a rien installé. Corrige le point indiqué par le contrôle local, puis relance la vérification.",
        action: "Relancer la vérification",
      },
      preflight_cancelled: {
        title: "Vérification annulée",
        message: "Le contrôle a été annulé avant toute modification. Tu peux le relancer lorsque tu es prêt.",
        action: "Relancer la vérification",
      },
      cancelled: {
        title: "Installation annulée",
        message: "L’opération a été arrêtée avant la première modification. Les mots de passe ont été retirés de l’interface.",
        action: "Recommencer",
      },
      release_mismatch: {
        title: "Release non conforme",
        message: "Une empreinte a changé depuis l’écran de validation. L’installation est bloquée pour protéger la machine.",
        action: "Recharger la release",
      },
      helper_unavailable: {
        title: "Installateur indisponible",
        message: "Le composant d’installation sécurisé est absent ou inaccessible. Réinstalle le paquet Atlas validé, puis réessaie.",
        action: "Revérifier",
      },
      service_unhealthy: {
        title: "Un service ne répond pas",
        message: "L’installation a été contrôlée mais un service local n’est pas sain. Atlas conserve le diagnostic et permet de relancer les vérifications.",
        action: "Revérifier",
      },
      zulip_unreachable: {
        title: "Zulip n’est pas accessible",
        message: "Le portail local ne répond pas encore sur zulip.ops.local. Vérifie le service et le certificat local, puis relance le contrôle.",
        action: "Revérifier",
      },
      rollback_complete: {
        title: "Installation annulée proprement",
        message: "Une étape a échoué et les modifications de cette tentative ont été annulées. Aucun mot de passe n’a été conservé dans l’interface.",
        action: "Recommencer",
      },
      status_unavailable: {
        title: "État d’installation indisponible",
        message: "Atlas ne peut pas joindre son composant natif d’installation. Ferme puis rouvre l’application; si le problème persiste, réinstalle le paquet Atlas.",
        action: "Réessayer",
      },
      install_failed: {
        title: "Installation interrompue",
        message: "Le déploiement local n’a pas abouti. Les accès saisis ont été effacés de l’interface; tu peux relancer après le diagnostic local.",
        action: "Recommencer",
      },
      openbao_unavailable: {
        title: "OpenBao ne répond pas",
        message: "Le coffre local n’a pas accepté l’opération. Vérifie son état, puis relance avec une nouvelle saisie des mots de passe.",
        action: "Recommencer",
      },
      rollback_failed: {
        title: "Récupération incomplète",
        message: "Le retour arrière automatique demande une vérification locale avant toute nouvelle tentative. Ne relance pas l’installation tant que cet état persiste.",
        action: "Revérifier l’état",
      },
      bootstrap_busy: {
        title: "Installation déjà en cours",
        message: "Une opération Atlas est active sur cette machine. Attends sa fin puis recharge son état.",
        action: "Actualiser",
      },
    };
    return (
      catalogue[category] ?? {
        title: "L’étape n’a pas abouti",
        message: phase
          ? `L’installateur s’est arrêté pendant « ${phase} ». Les mots de passe ont été retirés de l’interface.`
          : "L’installateur local s’est arrêté. Les mots de passe ont été retirés de l’interface.",
        action: "Recommencer",
      }
    );
  }

  async function init({ callNative, onReady }) {
    const root = document.getElementById("bootstrap-onboarding");
    const content = document.getElementById("onboarding-content");
    const appShell = document.getElementById("atlas-app-shell");
    if (!root || !content || typeof callNative !== "function") {
      if (typeof onReady === "function") await onReady();
      return;
    }

    disposeSession();
    session = {
      root,
      content,
      appShell,
      callNative,
      onReady,
      status: null,
      transientSecrets: null,
      visibilityTimers: new Set(),
      visibilityCleanups: new Set(),
      progressTimer: null,
      unlisten: null,
      currentStep: "welcome",
      progress: 0,
      activePhase: "preflight",
      cancellationAllowed: false,
      resultPollFailures: 0,
      workflowMode: "fresh",
      recoveryWorkflow: false,
      verifyingInstalledProof: false,
    };
    activateOnboarding();
    renderLoading("Vérification locale", "Atlas vérifie l’état de l’installation sans modifier la machine.");

    try {
      const rawStatus = await session.callNative("get_bootstrap_status");
      if (!session) return;
      session.status = normalizeBootstrapStatus(rawStatus);
      session.workflowMode = session.status.operationMode === "corrective" ? "corrective" : "fresh";
      const route = bootstrapRoute(session.status);
      if (route === "recovery") {
        session.recoveryWorkflow = true;
        renderRecoveryRequired();
        return;
      }
      if (route === "atlas") {
        await openAtlas();
        return;
      }
      if (route === "progress") {
        renderProgress(session.status.phase, session.status.percent);
        await attachProgressListener();
        scheduleResultPoll(400);
        return;
      }
      if (route === "failure") {
        renderFailure(session.status.errorCode || session.status.state, session.status.phase);
        return;
      }
      renderWelcome();
    } catch {
      if (session) renderFailure("status_unavailable");
    }
  }

  function activateOnboarding() {
    session.root.hidden = false;
    document.body.classList.add("onboarding-active");
    if (session.appShell) {
      session.appShell.inert = true;
      session.appShell.setAttribute("aria-hidden", "true");
    }
    updateStepMarkers("welcome");
  }

  function deactivateOnboarding() {
    if (!session) return;
    session.root.hidden = true;
    document.body.classList.remove("onboarding-active");
    if (session.appShell) {
      session.appShell.inert = false;
      session.appShell.removeAttribute("aria-hidden");
    }
  }

  function disposeSession() {
    if (!session) return;
    clearTransientSecrets();
    scrubSensitiveInputs(session.content);
    for (const timer of session.visibilityTimers) window.clearTimeout(timer);
    session.visibilityTimers.clear();
    for (const cleanup of session.visibilityCleanups) cleanup();
    session.visibilityCleanups.clear();
    if (session.progressTimer) window.clearTimeout(session.progressTimer);
    if (typeof session.unlisten === "function") session.unlisten();
    session = null;
  }

  function clearTransientSecrets() {
    if (!session?.transientSecrets) return;
    session.transientSecrets.openBaoPassword = "";
    session.transientSecrets.zulipPassword = "";
    session.transientSecrets = null;
  }

  function setView(markup, step) {
    if (!session) return;
    scrubSensitiveInputs(session.content);
    for (const cleanup of session.visibilityCleanups) cleanup();
    session.visibilityCleanups.clear();
    for (const timer of session.visibilityTimers) window.clearTimeout(timer);
    session.visibilityTimers.clear();
    session.currentStep = step;
    session.content.innerHTML = markup;
    updateStepMarkers(step);
    window.requestAnimationFrame(() => session?.content.focus({ preventScroll: true }));
  }

  function updateStepMarkers(step) {
    if (!session) return;
    const normalizedStep = step === "success" || step === "error" || step === "progress" ? "install" : step;
    const currentIndex = Math.max(0, STEP_ORDER.indexOf(normalizedStep));
    session.root.querySelectorAll("[data-onboarding-marker]").forEach((marker, index) => {
      marker.classList.toggle("is-active", index === currentIndex);
      marker.classList.toggle("is-complete", index < currentIndex || step === "success");
      if (index === currentIndex) marker.setAttribute("aria-current", "step");
      else marker.removeAttribute("aria-current");
    });
  }

  function announce(message) {
    const live = document.getElementById("onboarding-live");
    if (!live) return;
    live.textContent = "";
    window.requestAnimationFrame(() => {
      live.textContent = message;
    });
  }

  function renderLoading(title, message) {
    setView(
      `<div class="onboarding-loading" role="status">
        <span class="onboarding-spinner" aria-hidden="true"></span>
        <div><h1 id="onboarding-title">${title}</h1><p>${message}</p></div>
      </div>`,
      "welcome",
    );
  }

  function renderRecoveryRequired() {
    clearTransientSecrets();
    if (session?.progressTimer) window.clearTimeout(session.progressTimer);
    if (typeof session?.unlisten === "function") session.unlisten();
    if (session) session.unlisten = null;
    const correctiveAvailable = Boolean(
      session?.status?.correctiveAvailable &&
        session.status.operationMode === "corrective" &&
        session.status.recoveryFromReleaseId === CORRECTIVE_PREDECESSOR_RELEASE_ID,
    );
    const resume = correctiveAvailable && Boolean(session?.status?.resumeAvailable);
    setView(
      `<div class="failure-hero" role="status">
        <span class="failure-mark" aria-hidden="true">↻</span>
        <p class="section-kicker">REPRISE LOCALE NÉCESSAIRE</p>
        <h1 id="onboarding-title">${correctiveAvailable ? (resume ? "La réparation peut reprendre." : "Atlas peut réparer l’installation.") : "L’installation doit être récupérée."}</h1>
        <p>${correctiveAvailable ? "La première transaction a été annulée proprement. Une release corrective distincte peut maintenant terminer l’installation sans rouvrir le parcours initial." : "Une transaction précédente a commencé, mais Atlas ne dispose pas encore de la preuve finale permettant d’ouvrir l’application."}</p>
      </div>
      <div class="failure-detail"><div><small>ÉTAT</small><strong>${correctiveAvailable ? (resume ? "Réparation à reprendre" : "Réparation vérifiée disponible") : "Récupération sécurisée"}</strong></div><div><small>RELEASE</small><code id="recovery-release"></code></div></div>
      ${correctiveAvailable ? `<div class="release-transition" aria-label="Transition corrective"><span><small>TRANSACTION ANNULÉE</small><code id="recovery-from-release"></code></span><b aria-hidden="true">→</b><span><small>RELEASE CORRECTIVE</small><code id="recovery-target-release"></code></span></div>` : ""}
      <div class="security-note"><span aria-hidden="true">⌁</span><p>${correctiveAvailable ? "Aucun champ de mot de passe n’est affiché avant que le helper racine scellé ait attesté le mode correctif, l’ancienne transaction et la nouvelle release." : "La reprise ou le retour arrière est géré localement par les services système déjà enregistrés. Atlas ne relance jamais une première installation dans cet état."}</p></div>
      <div class="info-callout"><span aria-hidden="true">i</span><p>${correctiveAvailable ? "Une authentification système va vérifier l’état racine. Les quatre champs de mot de passe apparaîtront uniquement après cette attestation." : "Laisse la récupération locale se terminer, puis revérifie son état. Le catalogue ne s’ouvrira qu’après publication d’une preuve d’installation complète."}</p></div>
      <div class="onboarding-actions"><button class="primary-button" id="recovery-action" type="button">${correctiveAvailable ? (resume ? "Reprendre la réparation" : "Vérifier et réparer") : "Revérifier l’état"}</button></div>`,
      "error",
    );
    setText("recovery-release", session?.status?.releaseId || "État local protégé");
    setText("recovery-from-release", session?.status?.recoveryFromReleaseId || "Release précédente protégée");
    setText("recovery-target-release", session?.status?.releaseId || "Release corrective indisponible");
    document.getElementById("recovery-action")?.addEventListener("click", (event) => {
      if (!session) return;
      if (correctiveAvailable) {
        session.recoveryWorkflow = true;
        session.workflowMode = "corrective";
        void runPreflight(event.currentTarget, true);
      } else {
        const options = { callNative: session.callNative, onReady: session.onReady };
        setBusy(event.currentTarget, true, "Revérification…");
        void init(options);
      }
    });
    announce(correctiveAvailable ? "Réparation corrective disponible. Une vérification racine est obligatoire avant toute saisie." : "Récupération locale nécessaire. Atlas reste verrouillé jusqu’à la preuve de fin d’installation.");
  }

  function renderWelcome() {
    setView(
      `<div class="onboarding-hero">
        <span class="onboarding-hero-icon" aria-hidden="true"><i></i><i></i><i></i></span>
        <p class="section-kicker">BIENVENUE DANS ATLAS</p>
        <h1 id="onboarding-title">Ton système IA,<br><span>entièrement chez toi.</span></h1>
        <p>Ce guide installe et configure le contrôle Hermes sur cette machine. Les modèles restent accessibles par API, mais les clés, le routage, la mémoire et Zulip sont administrés localement.</p>
      </div>
      <div class="onboarding-feature-grid" aria-label="Composants installés">
        <article><span aria-hidden="true">◇</span><div><strong>Atlas</strong><small>Catalogue, coûts et clés API</small></div></article>
        <article><span aria-hidden="true">↗</span><div><strong>Hermes</strong><small>Routage économique multi-modèles</small></div></article>
        <article><span aria-hidden="true">⌁</span><div><strong>Zulip local</strong><small>Validations et suivi des missions</small></div></article>
        <article><span aria-hidden="true">▣</span><div><strong>OpenBao</strong><small>Coffre local chiffré</small></div></article>
      </div>
      <div class="privacy-callout"><span aria-hidden="true">✓</span><p><strong>Aucun compte cloud supplémentaire.</strong> Les deux mots de passe demandés à l’étape suivante ne quittent pas le processus d’installation et ne sont pas enregistrés par l’interface.</p></div>
      <div class="onboarding-actions"><button class="primary-button" id="onboarding-start" type="button">Commencer <span aria-hidden="true">→</span></button></div>`,
      "welcome",
    );
    document.getElementById("onboarding-start")?.addEventListener("click", renderAccount);
  }

  function renderAccount() {
    const status = session.status;
    setView(
      `<div class="onboarding-heading">
        <p class="section-kicker">COMPTE LOCAL</p>
        <h1 id="onboarding-title">Tes accès administrateur</h1>
        <p>Ces identifiants sont fixés par l’installation pour que les services sachent exactement quel propriétaire autoriser.</p>
      </div>
      <div class="identity-card">
        <div class="identity-avatar" aria-hidden="true">L</div>
        <div><small>SESSION DE CETTE MACHINE</small><strong id="local-owner-name"></strong><span>Propriétaire du contrôle local</span></div>
        <span class="verified-badge">Vérifié</span>
      </div>
      <div class="identity-card">
        <div class="identity-avatar identity-avatar--violet" aria-hidden="true">Z</div>
        <div><small>PROPRIÉTAIRE ZULIP</small><strong id="zulip-owner-email"></strong><span>Compte administrateur de la messagerie locale</span></div>
        <span class="locked-badge">Fixe</span>
      </div>
      <div class="info-callout"><span aria-hidden="true">i</span><p>L’étape suivante ouvre la validation système locale. Rien n’est installé pendant ce contrôle préalable.</p></div>
      <div class="onboarding-actions onboarding-actions--split">
        <button class="secondary-button" id="account-back" type="button">Retour</button>
        <button class="primary-button" id="account-continue" type="button">Vérifier la machine <span aria-hidden="true">→</span></button>
      </div>`,
      "account",
    );
    document.getElementById("local-owner-name").textContent = status.localUsername;
    document.getElementById("zulip-owner-email").textContent = status.zulipOwnerEmail;
    document.getElementById("account-back")?.addEventListener("click", renderWelcome);
    document.getElementById("account-continue")?.addEventListener("click", (event) => {
      void runPreflight(event.currentTarget, false);
    });
  }

  async function runPreflight(button, corrective = false) {
    if (corrective) session.recoveryWorkflow = true;
    let expectedMode = corrective && session.status.operationMode === "fresh" ? "fresh" : (corrective ? "corrective" : "fresh");
    if (session.status.preflightPassed && session.status.operationMode === expectedMode) {
      session.workflowMode = expectedMode;
      if (session.status.rollbackFinalizeResume) renderRollbackFinalizeReview();
      else renderCredentials();
      return;
    }
    setBusy(button, true, "Vérification en cours…");
    announce("Vérification préalable en cours");
    try {
      const response = await session.callNative(corrective ? "run_bootstrap_recovery_preflight" : "run_bootstrap_preflight");
      const rawState = String(firstDefined(response?.status, response?.state, "")).toLowerCase();
      const passed = Boolean(firstDefined(response?.preflight_passed, response?.passed, ["passed", "success", "succeeded", "ok", "preflight-passed", "corrective-preflight-passed"].includes(rawState)));
      const rollbackFinalizeResume = response?.rollback_finalize_resume === true;
      const attestedMode = String(firstDefined(response?.operation_mode, expectedMode)).toLowerCase();
      if (!passed) {
        renderFailure(
          firstDefined(response?.error_code, rawState === "cancelled" ? "preflight_cancelled" : "preflight_failed"),
          "Vérifications préalables",
        );
        return;
      }
      if (!["fresh", "corrective"].includes(attestedMode) || (!corrective && attestedMode !== "fresh")) {
        renderFailure("bootstrap_preflight_attestation_invalid", "Vérifications préalables");
        return;
      }
      expectedMode = attestedMode;
      const refreshed = normalizeBootstrapStatus(await session.callNative("get_bootstrap_status"));
      const identityComplete = Boolean(
        refreshed.releaseId &&
          refreshed.confirmationDigestSuffix &&
          refreshed.bootstrapHelperSha256 &&
          refreshed.manifestSha256 &&
          refreshed.sourceTreeSha256 &&
          refreshed.rpmSha256 &&
          refreshed.hermesSourceArchiveSha256,
      );
      if (
        !refreshed.preflightPassed ||
        refreshed.operationMode !== expectedMode ||
        refreshed.rollbackFinalizeResume !== rollbackFinalizeResume ||
        !identityComplete ||
        (corrective && expectedMode === "corrective" &&
          (!refreshed.correctiveAvailable ||
            refreshed.recoveryFromReleaseId !== CORRECTIVE_PREDECESSOR_RELEASE_ID)) ||
        (!corrective && RECOVERY_REQUIRED_STATES.has(refreshed.state))
      ) {
        renderFailure("bootstrap_preflight_attestation_invalid", "Vérifications préalables");
        return;
      }
      session.status = refreshed;
      session.workflowMode = expectedMode;
      announce("Vérification préalable réussie");
      if (rollbackFinalizeResume) renderRollbackFinalizeReview();
      else renderCredentials();
    } catch (error) {
      renderFailure(errorCodeFrom(error, "preflight_failed"), "Vérifications préalables");
    }
  }

  function renderRollbackFinalizeReview() {
    clearTransientSecrets();
    const status = session?.status;
    const modeValid = status?.preflightPassed && status.rollbackFinalizeResume &&
      ["fresh", "corrective"].includes(session?.workflowMode);
    const releaseComplete = Boolean(
      status?.releaseId &&
        status.confirmationDigestSuffix &&
        status.bootstrapHelperSha256 &&
        status.manifestSha256 &&
        status.sourceTreeSha256 &&
        status.rpmSha256 &&
        status.hermesSourceArchiveSha256,
    );
    if (!modeValid || !releaseComplete) {
      renderFailure("bootstrap_preflight_attestation_invalid", "Finalisation du rollback");
      return;
    }
    setView(
      `<div class="onboarding-heading">
        <p class="section-kicker">REPRISE · JOURNAL LOCAL</p>
        <h1 id="onboarding-title">Finaliser le retour arrière</h1>
        <p>La coupure est survenue après le retour à l’état initial. Atlas va uniquement terminer le journal d’audit et retirer le mécanisme de reprise devenu inutile.</p>
      </div>
      <dl class="release-card">
        <div><dt>Release</dt><dd id="rollback-review-release"></dd></div>
        <div><dt>Installateur</dt><dd class="digest-value" id="rollback-review-helper"></dd></div>
        <div><dt>Sources</dt><dd class="digest-value" id="rollback-review-source"></dd></div>
        <div><dt>Archive Hermes</dt><dd class="digest-value" id="rollback-review-hermes"></dd></div>
      </dl>
      <div class="security-note"><span aria-hidden="true">⌁</span><p><strong>Aucun mot de passe n’est nécessaire ni accepté.</strong> Cette voie ne peut installer aucun paquet, démarrer aucun service et ne transmet aucun secret.</p></div>
      <label class="confirmation-check"><input id="rollback-finalize-confirmation" type="checkbox"><span><i aria-hidden="true"></i><span>Je confirme la finalisation locale de <strong id="rollback-confirm-release"></strong><small>Empreinte : <code id="rollback-confirm-suffix"></code></small></span></span></label>
      <div class="onboarding-actions onboarding-actions--split"><button class="secondary-button" id="rollback-finalize-back" type="button">Retour</button><button class="primary-button" id="rollback-finalize-start" type="button" disabled>Finaliser le rollback <span aria-hidden="true">→</span></button></div>`,
      "review",
    );
    setText("rollback-review-release", status.releaseId);
    setText("rollback-review-helper", status.bootstrapHelperSha256);
    setText("rollback-review-source", status.sourceTreeSha256);
    setText("rollback-review-hermes", status.hermesSourceArchiveSha256);
    setText("rollback-confirm-release", status.releaseId);
    setText("rollback-confirm-suffix", status.confirmationDigestSuffix);
    const checkbox = document.getElementById("rollback-finalize-confirmation");
    const button = document.getElementById("rollback-finalize-start");
    checkbox?.addEventListener("change", () => {
      button.disabled = !checkbox.checked;
    });
    document.getElementById("rollback-finalize-back")?.addEventListener("click", () => {
      if (session?.workflowMode === "corrective") renderRecoveryRequired();
      else renderWelcome();
    });
    button?.addEventListener("click", () => {
      if (checkbox.checked) void beginRollbackFinalization();
    });
    announce("Le rollback est prêt à être finalisé sans saisie de mot de passe.");
  }

  async function beginRollbackFinalization() {
    if (!session?.status?.rollbackFinalizeResume || !session.status.preflightPassed) {
      renderFailure("bootstrap_preflight_attestation_invalid", "Finalisation du rollback");
      return;
    }
    const request = {
      confirmedReleaseId: session.status.releaseId,
      confirmedDigestSuffix: session.status.confirmationDigestSuffix,
    };
    renderProgress("rollback-finalizing", 5);
    await attachProgressListener();
    try {
      const result = await session.callNative(
        session.recoveryWorkflow ? "begin_recovery_bootstrap" : "begin_bootstrap",
        request,
      );
      handleBootstrapResult(result);
    } catch (error) {
      renderFailure(errorCodeFrom(error, "rollback_finalize_failed"), "Finalisation du rollback");
    }
  }

  function renderCredentials() {
    clearTransientSecrets();
    const corrective = session?.workflowMode === "corrective";
    setView(
      `<div class="onboarding-heading">
        <p class="section-kicker">${corrective ? "RÉPARATION · SÉCURITÉ" : "SÉCURITÉ"}</p>
        <h1 id="onboarding-title">${corrective ? "Saisis à nouveau tes mots de passe" : "Crée tes mots de passe"}</h1>
        <p>Utilise deux mots de passe différents d’au moins 14 caractères. Atlas les transmet une seule fois au helper ${corrective ? "correctif " : ""}local attesté.</p>
      </div>
      <form id="credentials-form" class="credentials-form" novalidate autocomplete="off">
        <fieldset class="credential-group">
          <legend><span class="service-glyph" aria-hidden="true">▣</span><span><strong>OpenBao</strong><small>Coffre des clés API et secrets système</small></span></legend>
          <label class="password-field"><span>Mot de passe OpenBao</span><span class="password-control"><input id="openbao-password" name="openbao-password" type="password" minlength="14" maxlength="100" autocomplete="new-password" spellcheck="false" autocapitalize="none" required><button class="password-visibility" type="button" data-show-password="openbao" aria-pressed="false">Afficher 15 s</button></span></label>
          <label class="password-field"><span>Confirmer le mot de passe</span><span class="password-control"><input id="openbao-confirmation" name="openbao-confirmation" type="password" minlength="14" maxlength="100" autocomplete="new-password" spellcheck="false" autocapitalize="none" required></span></label>
          <ul class="password-checks" aria-live="polite"><li id="check-openbao-length"><i aria-hidden="true"></i>14 caractères minimum</li><li id="check-openbao-bounds"><i aria-hidden="true"></i>100 caractères maximum, sans espace extérieur</li><li id="check-openbao-match"><i aria-hidden="true"></i>Les deux saisies correspondent</li></ul>
        </fieldset>
        <fieldset class="credential-group">
          <legend><span class="service-glyph service-glyph--violet" aria-hidden="true">Z</span><span><strong>Zulip</strong><small>Compte propriétaire admin@ops.local</small></span></legend>
          <label class="password-field"><span>Mot de passe Zulip</span><span class="password-control"><input id="zulip-password" name="zulip-password" type="password" minlength="14" maxlength="100" autocomplete="new-password" spellcheck="false" autocapitalize="none" required><button class="password-visibility" type="button" data-show-password="zulip" aria-pressed="false">Afficher 15 s</button></span></label>
          <label class="password-field"><span>Confirmer le mot de passe</span><span class="password-control"><input id="zulip-confirmation" name="zulip-confirmation" type="password" minlength="14" maxlength="100" autocomplete="new-password" spellcheck="false" autocapitalize="none" required></span></label>
          <ul class="password-checks" aria-live="polite"><li id="check-zulip-length"><i aria-hidden="true"></i>14 caractères minimum</li><li id="check-zulip-bounds"><i aria-hidden="true"></i>100 caractères maximum, sans espace extérieur</li><li id="check-zulip-match"><i aria-hidden="true"></i>Les deux saisies correspondent</li><li id="check-passwords-differ"><i aria-hidden="true"></i>Différent du mot de passe OpenBao</li></ul>
        </fieldset>
        <div class="security-note"><span aria-hidden="true">⌁</span><p>Les champs sont supprimés de l’écran dès validation. Ils ne sont placés ni dans les journaux, ni dans le stockage du navigateur.</p></div>
        <div class="onboarding-actions onboarding-actions--split"><button class="secondary-button" id="credentials-back" type="button">Retour</button><button class="primary-button" id="credentials-continue" type="submit" disabled>Continuer <span aria-hidden="true">→</span></button></div>
      </form>`,
      "credentials",
    );

    const form = document.getElementById("credentials-form");
    const fields = {
      openBaoPassword: document.getElementById("openbao-password"),
      openBaoConfirmation: document.getElementById("openbao-confirmation"),
      zulipPassword: document.getElementById("zulip-password"),
      zulipConfirmation: document.getElementById("zulip-confirmation"),
    };
    const submit = document.getElementById("credentials-continue");
    const update = () => {
      const result = validatePasswordSet(Object.fromEntries(Object.entries(fields).map(([key, input]) => [key, input.value])));
      toggleCheck("check-openbao-length", result.checks.openBaoLength, fields.openBaoPassword.value.length > 0);
      toggleCheck("check-openbao-bounds", result.checks.openBaoBounds, fields.openBaoPassword.value.length > 0);
      toggleCheck("check-openbao-match", result.checks.openBaoMatch, fields.openBaoConfirmation.value.length > 0);
      toggleCheck("check-zulip-length", result.checks.zulipLength, fields.zulipPassword.value.length > 0);
      toggleCheck("check-zulip-bounds", result.checks.zulipBounds, fields.zulipPassword.value.length > 0);
      toggleCheck("check-zulip-match", result.checks.zulipMatch, fields.zulipConfirmation.value.length > 0);
      toggleCheck("check-passwords-differ", result.checks.passwordsDiffer, fields.zulipPassword.value.length > 0 && fields.openBaoPassword.value.length > 0);
      submit.disabled = !result.valid;
      return result;
    };
    Object.values(fields).forEach((input) => input.addEventListener("input", update));
    bindTemporaryVisibility("openbao", [fields.openBaoPassword, fields.openBaoConfirmation]);
    bindTemporaryVisibility("zulip", [fields.zulipPassword, fields.zulipConfirmation]);
    document.getElementById("credentials-back")?.addEventListener("click", corrective ? renderRecoveryRequired : renderAccount);
    form?.addEventListener("submit", (event) => {
      event.preventDefault();
      if (!update().valid) {
        announce("Vérifie les cinq critères de mot de passe avant de continuer.");
        return;
      }
      session.transientSecrets = {
        openBaoPassword: fields.openBaoPassword.value,
        zulipPassword: fields.zulipPassword.value,
      };
      Object.values(fields).forEach((input) => {
        input.type = "password";
        input.value = "";
      });
      form.reset();
      renderReview();
    });
    fields.openBaoPassword.focus();
  }

  function toggleCheck(id, met, touched) {
    const element = document.getElementById(id);
    if (!element) return;
    element.classList.toggle("is-met", met);
    element.classList.toggle("is-unmet", touched && !met);
  }

  function bindTemporaryVisibility(group, inputs) {
    const button = document.querySelector(`[data-show-password="${group}"]`);
    if (!button) return;
    let timer = null;
    const hide = () => {
      inputs.forEach((input) => {
        input.type = "password";
      });
      button.setAttribute("aria-pressed", "false");
      button.textContent = "Afficher 15 s";
      if (timer) {
        window.clearTimeout(timer);
        session?.visibilityTimers.delete(timer);
        timer = null;
      }
    };
    button.addEventListener("click", () => {
      if (button.getAttribute("aria-pressed") === "true") {
        hide();
        return;
      }
      inputs.forEach((input) => {
        input.type = "text";
      });
      button.setAttribute("aria-pressed", "true");
      button.textContent = "Masquer";
      timer = window.setTimeout(hide, 15_000);
      session.visibilityTimers.add(timer);
    });
    const onVisibilityChange = () => {
      if (document.hidden) hide();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    session.visibilityCleanups.add(() => document.removeEventListener("visibilitychange", onVisibilityChange));
  }

  function renderReview() {
    const status = session.status;
    const corrective = session?.workflowMode === "corrective";
    const releaseComplete = Boolean(
      status.releaseId &&
        status.confirmationDigestSuffix &&
        status.bootstrapHelperSha256 &&
        status.manifestSha256 &&
        status.sourceTreeSha256 &&
        status.rpmSha256 &&
        status.hermesSourceArchiveSha256,
    );
    setView(
      `<div class="onboarding-heading">
        <p class="section-kicker">${corrective ? "RÉPARATION · VALIDATION FINALE" : "VALIDATION FINALE"}</p>
        <h1 id="onboarding-title">${corrective ? "Vérifie la transition corrective" : "Vérifie la release"}</h1>
        <p>Ces empreintes identifient exactement le code et le paquet qui seront autorisés sur cette machine.</p>
      </div>
      ${corrective ? `<div class="release-transition release-transition--review" aria-label="Transition corrective confirmée"><span><small>DEPUIS</small><code id="review-recovery-from"></code></span><b aria-hidden="true">→</b><span><small>VERS</small><code id="review-recovery-target"></code></span></div>` : ""}
      <dl class="release-card">
        <div><dt>Release</dt><dd id="review-release"></dd></div>
        <div><dt>Installateur</dt><dd class="digest-value" id="review-helper"></dd></div>
        <div><dt>Manifeste</dt><dd class="digest-value" id="review-manifest"></dd></div>
        <div><dt>Sources</dt><dd class="digest-value" id="review-source"></dd></div>
        <div><dt>Paquet Atlas</dt><dd class="digest-value" id="review-rpm"></dd></div>
        <div><dt>Archive Hermes</dt><dd class="digest-value" id="review-hermes"></dd></div>
      </dl>
      <div class="install-scope">
        <h2>${corrective ? "Cette réparation va" : "Cette installation va"}</h2>
        <ul><li><i aria-hidden="true">✓</i>Créer le coffre OpenBao et le compte propriétaire Zulip</li><li><i aria-hidden="true">✓</i>Installer Atlas, Hermes, le routeur et leurs services locaux</li><li><i aria-hidden="true">✓</i>Configurer le démarrage automatique et vérifier la santé</li><li><i aria-hidden="true">–</i>Ne contacter aucun fournisseur IA sans clé ajoutée ensuite dans Atlas</li></ul>
      </div>
      <label class="confirmation-check${releaseComplete ? "" : " confirmation-check--disabled"}"><input id="release-confirmation" type="checkbox" ${releaseComplete ? "" : "disabled"}><span><i aria-hidden="true"></i><span>${corrective ? "J’autorise la réparation corrective vers" : "J’autorise l’installation de"} <strong id="confirm-release"></strong><small>Empreinte de confirmation ${corrective ? "corrective " : ""}: <code id="confirm-suffix"></code></small></span></span></label>
      <p class="release-warning" id="release-warning" ${releaseComplete ? "hidden" : ""}>Les empreintes complètes ne sont pas disponibles. Par sécurité, l’installation reste bloquée.</p>
      <div class="onboarding-actions onboarding-actions--split"><button class="secondary-button" id="review-back" type="button">Modifier les mots de passe</button><button class="primary-button" id="install-start" type="button" disabled>${corrective ? "Réparer maintenant" : "Installer maintenant"} <span aria-hidden="true">→</span></button></div>`,
      "review",
    );
    setText("review-release", status.releaseId || "Indisponible");
    setText("review-helper", status.bootstrapHelperSha256 || "Empreinte indisponible");
    setText("review-manifest", status.manifestSha256 || "Empreinte indisponible");
    setText("review-source", status.sourceTreeSha256 || "Empreinte indisponible");
    setText("review-rpm", status.rpmSha256 || "Empreinte indisponible");
    setText("review-hermes", status.hermesSourceArchiveSha256 || "Empreinte indisponible");
    setText("confirm-release", status.releaseId || "la release sélectionnée");
    setText("confirm-suffix", status.confirmationDigestSuffix || "indisponible");
    setText("review-recovery-from", status.recoveryFromReleaseId || "Release précédente protégée");
    setText("review-recovery-target", status.releaseId || "Release corrective indisponible");
    const checkbox = document.getElementById("release-confirmation");
    const installButton = document.getElementById("install-start");
    checkbox?.addEventListener("change", () => {
      installButton.disabled = !checkbox.checked || !releaseComplete;
    });
    document.getElementById("review-back")?.addEventListener("click", () => {
      clearTransientSecrets();
      renderCredentials();
    });
    installButton?.addEventListener("click", () => {
      if (checkbox.checked && releaseComplete) void beginInstallation();
    });
  }

  async function beginInstallation() {
    if (!session || !["fresh", "corrective"].includes(session.workflowMode)) {
      clearTransientSecrets();
      renderFailure("bootstrap_operation_mode_invalid", "Validation finale");
      return;
    }
    if (!session.transientSecrets) {
      renderCredentials();
      announce("Les mots de passe doivent être saisis à nouveau.");
      return;
    }
    const request = {
      openBaoPassword: session.transientSecrets.openBaoPassword,
      zulipPassword: session.transientSecrets.zulipPassword,
      confirmedReleaseId: session.status.releaseId,
      confirmedDigestSuffix: session.status.confirmationDigestSuffix,
    };
    renderProgress("authentication", 5);
    await attachProgressListener();
    let invocation;
    try {
      invocation = session.recoveryWorkflow
        ? session.callNative("begin_recovery_bootstrap", request)
        : session.callNative("begin_bootstrap", request);
    } finally {
      clearTransientSecrets();
      request.openBaoPassword = "";
      request.zulipPassword = "";
    }
    try {
      const result = await invocation;
      handleBootstrapResult(result);
    } catch (error) {
      renderFailure(errorCodeFrom(error, "install_failed"), phaseLabel(session.activePhase));
    }
  }

  async function attachProgressListener() {
    if (!session || session.unlisten) return;
    const listen = window.__TAURI__?.event?.listen;
    if (typeof listen !== "function") return;
    try {
      session.unlisten = await listen("bootstrap-progress", (event) => {
        handleProgressPayload(event?.payload);
      });
    } catch {
      session.unlisten = null;
    }
  }

  function handleProgressPayload(rawPayload) {
    if (!session) return;
    let payload = rawPayload;
    if (typeof payload === "string") {
      try {
        payload = JSON.parse(payload);
      } catch {
        payload = {};
      }
    }
    payload = payload && typeof payload === "object" ? payload : {};
    const phase = normalizePhase(firstDefined(payload.phase, payload.step, session.activePhase));
    const status = String(firstDefined(payload.status, "running")).toLowerCase();
    const percent = Math.max(session.progress, normalizePercent(firstDefined(payload.percent, session.progress)));
    session.activePhase = phase;
    session.progress = percent;
    if (typeof payload.cancellation_allowed === "boolean") {
      session.cancellationAllowed = payload.cancellation_allowed;
      updateCancellationControl(payload.cancellation_allowed);
    }
    if (["rolled-back", "rollback-finalized"].includes(status)) {
      void refreshAfterRollbackFinalization();
      return;
    }
    if (TERMINAL_FAILURE_STATES.has(status)) {
      renderFailure(firstDefined(payload.error_code, status), phaseLabel(phase));
      return;
    }
    if (status === "preflight-passed") {
      session.status.preflightPassed = true;
      renderCredentials();
      return;
    }
    if (phase === "complete" || TERMINAL_SUCCESS_STATES.has(status)) {
      void verifyInstalledProof(payload);
      return;
    }
    updateProgressView(phase, percent);
  }

  function renderProgress(phase = "preflight", percent = 0) {
    session.activePhase = normalizePhase(phase);
    session.progress = normalizePercent(percent);
    setView(
      `<div class="onboarding-heading onboarding-heading--center">
        <span class="install-pulse" aria-hidden="true"><i></i></span>
        <p class="section-kicker">${session.workflowMode === "corrective" ? "RÉPARATION EN COURS" : "INSTALLATION EN COURS"}</p>
        <h1 id="onboarding-title">${session.workflowMode === "corrective" ? "Atlas répare ton système" : "Atlas prépare ton système"}</h1>
        <p>Garde l’application ouverte. Les étapes terminées sont validées une par une et un échec déclenche le retour arrière prévu.</p>
      </div>
      <div class="install-progress" role="progressbar" aria-label="Progression de l’installation" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><span id="install-progress-bar"></span></div>
      <div class="install-progress-meta"><strong id="install-progress-label">Démarrage…</strong><span id="install-progress-value">0 %</span></div>
      <ol class="installation-phases" id="installation-phases"></ol>
      <div class="info-callout"><span aria-hidden="true">i</span><p>Une demande d’authentification système peut apparaître au premier plan. Elle autorise uniquement cette release vérifiée.</p></div>
      <button class="cancel-install-button" id="cancel-install" type="button" hidden>Annuler avant modification</button>`,
      "progress",
    );
    const list = document.getElementById("installation-phases");
    for (const item of INSTALL_PHASES) {
      const row = document.createElement("li");
      row.dataset.installPhase = item.id;
      const marker = document.createElement("span");
      marker.className = "phase-marker";
      marker.setAttribute("aria-hidden", "true");
      marker.textContent = "";
      const copy = document.createElement("div");
      const strong = document.createElement("strong");
      strong.textContent = item.label;
      const small = document.createElement("small");
      small.textContent = item.detail;
      copy.append(strong, small);
      row.append(marker, copy);
      list.append(row);
    }
    document.getElementById("cancel-install")?.addEventListener("click", (event) => {
      void cancelInstallation(event.currentTarget);
    });
    updateProgressView(session.activePhase, session.progress);
    session.cancellationAllowed = Boolean(session.status?.cancellationAllowed);
    updateCancellationControl(session.cancellationAllowed);
  }

  function updateCancellationControl(allowed) {
    const button = document.getElementById("cancel-install");
    if (!button) return;
    button.hidden = !allowed;
    if (allowed) {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = "Annuler avant modification";
    }
  }

  async function cancelInstallation(button) {
    if (!session?.cancellationAllowed) return;
    setBusy(button, true, "Annulation demandée…");
    try {
      const response = await session.callNative("cancel_bootstrap");
      const status = String(firstDefined(response?.status, "not-running")).toLowerCase();
      if (status === "requested") {
        session.cancellationAllowed = false;
        updateCancellationControl(false);
        announce("Annulation demandée avant modification. Atlas attend la confirmation de l’installateur.");
        scheduleResultPoll(400);
      } else if (status === "refused-after-mutation") {
        session.cancellationAllowed = false;
        updateCancellationControl(false);
        announce("La transaction a commencé. L’annulation manuelle est désactivée; le rollback automatique reste armé en cas d’échec.");
      } else {
        updateCancellationControl(false);
        scheduleResultPoll(200);
      }
    } catch {
      session.cancellationAllowed = false;
      updateCancellationControl(false);
      announce("La demande d’annulation n’a pas pu être transmise. L’installation continue sous contrôle transactionnel.");
    }
  }

  function updateProgressView(phase, percent) {
    const progress = document.querySelector(".install-progress");
    const bar = document.getElementById("install-progress-bar");
    const label = document.getElementById("install-progress-label");
    const value = document.getElementById("install-progress-value");
    if (!progress || !bar) return;
    const normalizedPhase = normalizePhase(phase);
    const currentIndex = normalizedPhase === "complete" ? INSTALL_PHASES.length : Math.max(0, INSTALL_PHASES.findIndex((item) => item.id === normalizedPhase));
    progress.setAttribute("aria-valuenow", String(percent));
    bar.style.width = `${percent}%`;
    if (label) label.textContent = normalizedPhase === "complete" ? "Installation terminée" : INSTALL_PHASES[currentIndex]?.label ?? "Installation en cours";
    if (value) value.textContent = `${percent} %`;
    document.querySelectorAll("[data-install-phase]").forEach((row, index) => {
      row.classList.toggle("is-active", index === currentIndex);
      row.classList.toggle("is-complete", index < currentIndex || normalizedPhase === "complete");
      row.querySelector(".phase-marker").textContent = index < currentIndex || normalizedPhase === "complete" ? "✓" : index === currentIndex ? "" : "";
    });
    announce(`${normalizedPhase === "complete" ? "Installation terminée" : phaseLabel(normalizedPhase)}, ${percent} pour cent`);
  }

  function handleBootstrapResult(rawResult) {
    if (!session) return;
    const result = rawResult && typeof rawResult === "object" ? rawResult : {};
    const status = String(firstDefined(result.status, result.state, "running")).toLowerCase();
    if (["rolled-back", "rollback-finalized"].includes(status)) {
      void refreshAfterRollbackFinalization();
      return;
    }
    if (status === "preflight-passed") {
      session.status.preflightPassed = true;
      renderCredentials();
      return;
    }
    if (TERMINAL_SUCCESS_STATES.has(status) || result.success === true) {
      void verifyInstalledProof(result);
      return;
    }
    if (TERMINAL_FAILURE_STATES.has(status) || result.success === false) {
      renderFailure(firstDefined(result.error_code, status, "install_failed"), phaseLabel(firstDefined(result.phase, session.activePhase)));
      return;
    }
    handleProgressPayload(result);
    scheduleResultPoll(700);
  }

  async function refreshAfterRollbackFinalization() {
    if (!session || session.verifyingInstalledProof) return;
    session.verifyingInstalledProof = true;
    try {
      const refreshed = normalizeBootstrapStatus(await session.callNative("get_bootstrap_status"));
      session.status = refreshed;
      session.status.preflightPassed = false;
      session.status.rollbackFinalizeResume = false;
      if (bootstrapRoute(refreshed) === "atlas") {
        renderFailure("bootstrap_installed_proof_missing", "Finalisation du rollback");
      } else if (RECOVERY_REQUIRED_STATES.has(refreshed.state)) {
        renderRecoveryRequired();
      } else {
        renderFailure("rollback_complete", "Finalisation du rollback");
      }
    } catch {
      renderFailure("status_unavailable", "Finalisation du rollback");
    } finally {
      if (session) session.verifyingInstalledProof = false;
    }
  }

  function scheduleResultPoll(delay) {
    if (!session) return;
    if (session.progressTimer) window.clearTimeout(session.progressTimer);
    session.progressTimer = window.setTimeout(() => void pollBootstrapResult(), delay);
  }

  async function pollBootstrapResult() {
    if (!session) return;
    try {
      const result = await session.callNative("get_bootstrap_result");
      session.resultPollFailures = 0;
      handleBootstrapResult(result);
    } catch {
      session.resultPollFailures += 1;
      if (session.resultPollFailures >= 4) {
        renderFailure("status_unavailable", phaseLabel(session.activePhase));
      } else {
        scheduleResultPoll(1800);
      }
    }
  }

  async function verifyInstalledProof(result = {}) {
    if (!session || session.verifyingInstalledProof) return;
    session.verifyingInstalledProof = true;
    try {
      const verified = normalizeBootstrapStatus(await session.callNative("get_bootstrap_status"));
      if (
        bootstrapRoute(verified) !== "atlas" ||
        verified.state !== "installed" ||
        verified.required ||
        !verified.atlasAvailable
      ) {
        session.status = verified;
        if (RECOVERY_REQUIRED_STATES.has(verified.state)) renderRecoveryRequired();
        else renderFailure("bootstrap_installed_proof_missing", "Contrôles de fonctionnement");
        return;
      }
      session.status = verified;
      renderSuccess(result);
    } catch {
      renderFailure("bootstrap_installed_proof_missing", "Contrôles de fonctionnement");
    } finally {
      if (session) session.verifyingInstalledProof = false;
    }
  }

  function renderSuccess(result = {}) {
    clearTransientSecrets();
    if (session.progressTimer) window.clearTimeout(session.progressTimer);
    if (typeof session.unlisten === "function") session.unlisten();
    session.unlisten = null;
    const url = safeZulipUrl(firstDefined(result.zulip_url, session.status.zulipUrl));
    setView(
      `<div class="success-hero">
        <span class="success-mark" aria-hidden="true"><i>✓</i></span>
        <p class="section-kicker">INSTALLATION TERMINÉE</p>
        <h1 id="onboarding-title">Tout est opérationnel.</h1>
        <p>Atlas, Hermes, OpenBao et Zulip sont installés sur cette machine. Tu peux maintenant ajouter tes clés API depuis Atlas.</p>
      </div>
      <div class="success-access-card">
        <span class="service-glyph service-glyph--violet" aria-hidden="true">Z</span>
        <div><small>MESSAGERIE ET VALIDATIONS</small><strong>Zulip local</strong><code class="local-url" id="success-zulip-url"></code><span>Compte : <b id="success-owner-email"></b></span></div>
        <button class="mini-button" id="open-zulip" type="button">Ouvrir</button>
      </div>
      <div class="success-access-card">
        <span class="service-glyph" aria-hidden="true">◇</span>
        <div><small>GESTION DES MODÈLES</small><strong>Atlas</strong><span>Catalogue, coûts, soldes et clés API</span></div>
        <span class="verified-badge">Prêt</span>
      </div>
      <div class="privacy-callout privacy-callout--success"><span aria-hidden="true">✓</span><p><strong>Contrôles validés.</strong> Les mots de passe ont été retirés du WebView. Les fournisseurs IA restent inactifs tant que tu n’ajoutes pas leurs clés dans Atlas.</p></div>
      <div class="onboarding-actions"><button class="primary-button" id="open-atlas" type="button">Ouvrir Atlas <span aria-hidden="true">→</span></button></div>`,
      "success",
    );
    setText("success-zulip-url", url);
    setText("success-owner-email", session.status.zulipOwnerEmail);
    document.getElementById("open-zulip")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      setBusy(button, true, "Ouverture…");
      try {
        await session.callNative("open_zulip");
        setBusy(button, false, "Ouvrir");
      } catch {
        setBusy(button, false, "Indisponible");
        announce("Impossible d’ouvrir Zulip automatiquement. Utilise l’adresse locale affichée.");
      }
    });
    document.getElementById("open-atlas")?.addEventListener("click", () => void openAtlas());
    announce("Installation terminée. Tous les services locaux sont opérationnels.");
  }

  function renderFailure(errorCode, phase = "") {
    clearTransientSecrets();
    if (session?.progressTimer) window.clearTimeout(session.progressTimer);
    const code = normalizeErrorCode(errorCode);
    const description = operationalError(code, phase);
    setView(
      `<div class="failure-hero">
        <span class="failure-mark" aria-hidden="true">!</span>
        <p class="section-kicker">ACTION NÉCESSAIRE</p>
        <h1 id="onboarding-title"></h1>
        <p id="failure-message"></p>
      </div>
      <div class="failure-detail"><div><small>ÉTAPE</small><strong id="failure-phase"></strong></div><div><small>CODE LOCAL</small><code id="failure-code"></code></div></div>
      <div class="security-note"><span aria-hidden="true">⌁</span><p>Aucun mot de passe n’est conservé pour une nouvelle tentative. L’interface te demandera de les saisir à nouveau.</p></div>
      <div class="onboarding-actions"><button class="primary-button" id="failure-retry" type="button"></button></div>`,
      "error",
    );
    setText("onboarding-title", description.title);
    setText("failure-message", description.message);
    setText("failure-phase", phase || "Contrôle de l’installation");
    setText("failure-code", code);
    setText("failure-retry", description.action);
    document.getElementById("failure-retry")?.addEventListener("click", () => {
      if (session.workflowMode === "corrective") {
        void init({ callNative: session.callNative, onReady: session.onReady });
      } else if (["status_unavailable", "release_mismatch", "helper_unavailable", "rollback_failed", "bootstrap_busy", "bootstrap_installed_proof_missing"].includes(code)) {
        void init({ callNative: session.callNative, onReady: session.onReady });
      } else if (code === "preflight_failed") {
        session.status.preflightPassed = false;
        renderAccount();
      } else {
        session.status.preflightPassed = false;
        renderAccount();
      }
    });
    announce(`${description.title}. ${description.message}`);
  }

  async function openAtlas() {
    if (!session) return;
    let verified;
    try {
      verified = normalizeBootstrapStatus(await session.callNative("get_bootstrap_status"));
    } catch {
      renderFailure("status_unavailable", "Preuve d’installation");
      return;
    }
    if (
      bootstrapRoute(verified) !== "atlas" ||
      verified.state !== "installed" ||
      verified.required ||
      !verified.atlasAvailable
    ) {
      session.status = verified;
      if (RECOVERY_REQUIRED_STATES.has(verified.state)) renderRecoveryRequired();
      else renderFailure("bootstrap_installed_proof_missing", "Preuve d’installation");
      return;
    }
    session.status = verified;
    const onReady = session.onReady;
    deactivateOnboarding();
    disposeSession();
    if (typeof onReady === "function") await onReady();
  }

  function setText(id, value) {
    const element = document.getElementById(id);
    if (element) element.textContent = String(value ?? "");
  }

  function scrubSensitiveInputs(root) {
    if (!root?.querySelectorAll) return;
    root
      .querySelectorAll("input[type='password'], input[autocomplete='new-password'], input[autocomplete='current-password']")
      .forEach((input) => {
        input.value = "";
        input.type = "password";
      });
  }

  function setBusy(button, busy, label) {
    if (!button) return;
    button.disabled = busy;
    button.setAttribute("aria-busy", String(busy));
    if (label) button.textContent = label;
  }

  function phaseLabel(phase) {
    const normalized = normalizePhase(phase);
    return INSTALL_PHASES.find((item) => item.id === normalized)?.label ?? "Installation locale";
  }

  function errorCodeFrom(error, fallback) {
    if (error && typeof error === "object") {
      return normalizeErrorCode(firstDefined(error.error_code, error.code, fallback));
    }
    if (typeof error === "string" && /^[a-z0-9_-]{1,80}$/i.test(error.trim())) return normalizeErrorCode(error);
    return fallback;
  }

  return Object.freeze({
    MIN_PASSWORD_LENGTH,
    bootstrapRoute,
    confirmationDigestSuffix,
    init,
    normalizeBootstrapStatus,
    normalizeErrorCode,
    normalizePhase,
    operationalError,
    safeZulipUrl,
    validatePasswordSet,
    validateProviderCredential,
  });
});
