"use strict";

const GLib = imports.gi.GLib;

function read(path) {
  const [ok, bytes] = GLib.file_get_contents(path);
  if (!ok) throw new Error(`Impossible de lire ${path}`);
  return new TextDecoder().decode(bytes);
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function equal(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(`${message}: attendu ${JSON.stringify(expected)}, reçu ${JSON.stringify(actual)}`);
  }
}

if (ARGV.length !== 2) {
  throw new Error("usage: gjs onboarding.test.js ONBOARDING_JS APP_JS");
}

const onboardingSource = read(ARGV[0]);
eval(onboardingSource);
const onboarding = window.AtlasOnboarding;
assert(onboarding, "onboarding.js doit exposer AtlasOnboarding");

equal(onboarding.MIN_PASSWORD_LENGTH, 14, "la politique minimale doit rester explicite");

const tooShort = onboarding.validatePasswordSet({
  openBaoPassword: "1234567890123",
  openBaoConfirmation: "1234567890123",
  zulipPassword: "abcdefghijklmn",
  zulipConfirmation: "abcdefghijklmn",
});
equal(tooShort.valid, false, "un mot de passe de 13 caractères doit être refusé");
equal(tooShort.checks.openBaoLength, false, "le contrôle OpenBao doit signaler la longueur");

const valid = onboarding.validatePasswordSet({
  openBaoPassword: "phrase-openbao-2026",
  openBaoConfirmation: "phrase-openbao-2026",
  zulipPassword: "phrase-zulip-2026!",
  zulipConfirmation: "phrase-zulip-2026!",
});
equal(valid.valid, true, "deux mots de passe conformes et différents doivent être acceptés");

const same = onboarding.validatePasswordSet({
  openBaoPassword: "mot-de-passe-identique",
  openBaoConfirmation: "mot-de-passe-identique",
  zulipPassword: "mot-de-passe-identique",
  zulipConfirmation: "mot-de-passe-identique",
});
equal(same.valid, false, "les mots de passe des deux services doivent différer");
equal(same.checks.passwordsDiffer, false, "le critère de séparation doit être visible");

const outerSpace = onboarding.validatePasswordSet({
  openBaoPassword: " phrase-openbao-2026",
  openBaoConfirmation: " phrase-openbao-2026",
  zulipPassword: "phrase-zulip-2026!",
  zulipConfirmation: "phrase-zulip-2026!",
});
equal(outerSpace.valid, false, "un espace extérieur doit être refusé");
equal(outerSpace.checks.openBaoBounds, false, "le format OpenBao doit signaler l’espace extérieur");

const controlCharacter = onboarding.validatePasswordSet({
  openBaoPassword: "phrase-openbao-2026",
  openBaoConfirmation: "phrase-openbao-2026",
  zulipPassword: "phrase-zulip\n2026!",
  zulipConfirmation: "phrase-zulip\n2026!",
});
equal(controlCharacter.valid, false, "un caractère de contrôle doit être refusé");

const validProviderCredential = onboarding.validateProviderCredential({
  openBaoPassword: "phrase-openbao-2026",
  apiKey: "sk-provider_ABC-123456",
});
equal(validProviderCredential.valid, true, "une paire coffre/clé conforme doit être acceptée");

const spacedApiKey = onboarding.validateProviderCredential({
  openBaoPassword: "phrase-openbao-2026",
  apiKey: "sk-provider ABC-123456",
});
equal(spacedApiKey.valid, false, "une clé API contenant un espace doit être refusée");
equal(spacedApiKey.checks.apiKeyFormat, false, "le format API doit suivre exactement les octets 0x21..0x7e");

const unicodeApiKey = onboarding.validateProviderCredential({
  openBaoPassword: "phrase-openbao-2026",
  apiKey: "clé-provider-123456",
});
equal(unicodeApiKey.valid, false, "une clé API non ASCII doit être refusée");

const oversizedApiKey = onboarding.validateProviderCredential({
  openBaoPassword: "phrase-openbao-2026",
  apiKey: "x".repeat(4097),
});
equal(oversizedApiKey.valid, false, "une clé API dépassant 4096 octets doit être refusée");

const digest = "a".repeat(64);
const withoutCompositeSuffix = onboarding.normalizeBootstrapStatus({
  required: true,
  release_id: "release-test",
  bootstrap_helper_sha256: "d".repeat(64),
  manifest_sha256: digest,
  source_tree_sha256: "b".repeat(64),
  rpm_sha256: "c".repeat(64),
  hermes_source_archive_sha256: "e".repeat(64),
});
equal(
  withoutCompositeSuffix.confirmationDigestSuffix,
  "",
  "le frontend ne doit jamais inventer le suffixe composite depuis un digest",
);

const withCompositeSuffix = onboarding.normalizeBootstrapStatus({
  required: true,
  release_id: "release-test",
  bootstrap_helper_sha256: "d".repeat(64),
  manifest_sha256: digest,
  source_tree_sha256: "b".repeat(64),
  rpm_sha256: "c".repeat(64),
  hermes_source_archive_sha256: "e".repeat(64),
  confirmation_digest_suffix: "12abcdef3456",
});
equal(withCompositeSuffix.confirmationDigestSuffix, "12abcdef3456", "le suffixe backend exact doit être conservé");
equal(
  withCompositeSuffix.hermesSourceArchiveSha256,
  "e".repeat(64),
  "l’empreinte de l’archive Hermes attestée par le backend doit être conservée",
);
equal(
  onboarding.normalizeBootstrapStatus({ hermes_source_archive_sha256: "invalide" }).hermesSourceArchiveSha256,
  "",
  "une empreinte d’archive Hermes invalide doit être supprimée",
);
equal(
  onboarding.normalizeBootstrapStatus({ rollback_finalize_resume: true }).rollbackFinalizeResume,
  true,
  "le bit root de finalisation rollback doit être conservé sans conversion implicite",
);

const installed = onboarding.normalizeBootstrapStatus({ state: "installed" });
equal(installed.required, false, "un état déjà installé doit ouvrir Atlas sans onboarding");
const readyToInstall = onboarding.normalizeBootstrapStatus({ state: "ready", required: true });
equal(readyToInstall.required, true, "l’état backend ready doit afficher l’onboarding et non ouvrir Atlas");
equal(onboarding.bootstrapRoute(readyToInstall), "welcome", "ready doit rester sur le parcours initial");
equal(
  onboarding.normalizePhase("secret-channel-ready"),
  "secrets",
  "l’attestation du canal racine doit rester dans la phase de création des accès",
);
equal(
  onboarding.normalizePhase("mutation-commit-ready"),
  "packages",
  "la préparation du commit doit rester dans la phase d’installation des composants",
);
equal(
  onboarding.normalizePhase("broker-quiesced"),
  "packages",
  "un échec après mise en maintenance du broker doit rester dans la phase d’installation",
);
const recoveryRequired = onboarding.normalizeBootstrapStatus({
  state: "recovery-required",
  required: false,
  release_id: "release-recovery-test",
  atlas_available: true,
});
equal(
  onboarding.bootstrapRoute(recoveryRequired),
  "recovery",
  "recovery-required doit primer sur required=false et sur la présence du binaire Atlas",
);
equal(
  onboarding.bootstrapRoute(onboarding.normalizeBootstrapStatus({ state: "recovery_required", required: true })),
  "recovery",
  "la variante sérialisée recovery_required doit rester bloquante",
);
equal(
  onboarding.bootstrapRoute(onboarding.normalizeBootstrapStatus({ state: "installed", required: false })),
  "atlas",
  "seul l’état natif installé peut sortir de l’onboarding",
);
equal(onboarding.safeZulipUrl("https://evil.example"), "https://zulip.ops.local:8443", "une URL distante doit être refusée");
equal(
  onboarding.safeZulipUrl("https://zulip.ops.local:8443"),
  "https://zulip.ops.local:8443",
  "l’URL Zulip locale doit être acceptée",
);

for (const code of ["step_failed", "step_timeout"]) {
  assert(onboarding.operationalError(code).title === "L’installation s’est arrêtée", "un échec d’installation ne doit pas être présenté comme un service malade");
}
assert(onboarding.operationalError("health_failed").title === "Un service ne répond pas", "seul le contrôle de santé doit signaler un service malade");
const knownError = onboarding.operationalError("release_mismatch");
assert(knownError.title.includes("Release"), "une erreur connue doit être actionnable");
const unknownError = onboarding.operationalError("inconnu", "Installation");
assert(unknownError.message.includes("Installation"), "une erreur inconnue doit indiquer l’étape sans données brutes");

for (const forbidden of ["localStorage", "sessionStorage", "console.log", "target=\"_blank\""]) {
  assert(!onboardingSource.includes(forbidden), `onboarding.js ne doit pas contenir ${forbidden}`);
}
assert(onboardingSource.includes('id="recovery-action"'), "la reprise doit proposer une action graphique bornée");
assert(
  onboardingSource.includes("Atlas ne relance jamais une première installation"),
  "l’écran de reprise doit interdire explicitement le parcours fresh",
);
assert(
  onboardingSource.includes('"run_bootstrap_recovery_preflight"') &&
    onboardingSource.includes('"begin_recovery_bootstrap"'),
  "le parcours de reprise doit utiliser le préflight et l’apply natifs de classification",
);
assert(
  onboardingSource.indexOf('if (route === "recovery")') < onboardingSource.indexOf('if (route === "atlas")'),
  "le verrou recovery doit être évalué avant l’ouverture d’Atlas",
);

class FakeClassList {
  constructor(value = "") {
    this.replace(value);
  }

  replace(value) {
    this.names = new Set(String(value).split(/\s+/).filter(Boolean));
  }

  add(...names) {
    names.forEach((name) => this.names.add(name));
  }

  remove(...names) {
    names.forEach((name) => this.names.delete(name));
  }

  contains(name) {
    return this.names.has(name);
  }

  toggle(name, force) {
    const enabled = force === undefined ? !this.names.has(name) : Boolean(force);
    if (enabled) this.names.add(name);
    else this.names.delete(name);
    return enabled;
  }

  toString() {
    return [...this.names].join(" ");
  }
}

class FakeElement {
  constructor(ownerDocument, tagName = "div") {
    this.ownerDocument = ownerDocument;
    this.tagName = String(tagName).toUpperCase();
    this.attributes = new Map();
    this.children = [];
    this.listeners = new Map();
    this.dataset = {};
    this.style = {};
    this.classList = new FakeClassList();
    this.id = "";
    this.value = "";
    this.type = "";
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.inert = false;
    this.textContent = "";
    this._innerHTML = "";
    this.replacesView = false;
  }

  get innerHTML() {
    return this._innerHTML;
  }

  set innerHTML(value) {
    this._innerHTML = String(value);
    if (this.replacesView) this.ownerDocument.replaceView(this, this._innerHTML);
  }

  get className() {
    return this.classList.toString();
  }

  set className(value) {
    this.classList.replace(value);
    this.attributes.set("class", String(value));
  }

  setAttribute(name, value) {
    const normalizedName = String(name);
    const normalizedValue = String(value);
    this.attributes.set(normalizedName, normalizedValue);
    if (normalizedName === "id") this.id = normalizedValue;
    if (normalizedName === "class") this.classList.replace(normalizedValue);
    if (normalizedName === "type") this.type = normalizedValue;
    if (normalizedName === "hidden") this.hidden = true;
    if (normalizedName === "disabled") this.disabled = true;
    if (normalizedName === "checked") this.checked = true;
    if (normalizedName.startsWith("data-")) {
      const key = normalizedName.slice(5).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
      this.dataset[key] = normalizedValue;
    }
  }

  getAttribute(name) {
    const normalizedName = String(name);
    if (this.attributes.has(normalizedName)) return this.attributes.get(normalizedName);
    if (normalizedName.startsWith("data-")) {
      const key = normalizedName.slice(5).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
      if (Object.hasOwn(this.dataset, key)) return String(this.dataset[key]);
    }
    return null;
  }

  removeAttribute(name) {
    const normalizedName = String(name);
    this.attributes.delete(normalizedName);
    if (normalizedName === "hidden") this.hidden = false;
    if (normalizedName === "disabled") this.disabled = false;
    if (normalizedName === "checked") this.checked = false;
  }

  addEventListener(type, callback) {
    const callbacks = this.listeners.get(type) ?? [];
    callbacks.push(callback);
    this.listeners.set(type, callbacks);
  }

  removeEventListener(type, callback) {
    const callbacks = this.listeners.get(type) ?? [];
    this.listeners.set(type, callbacks.filter((candidate) => candidate !== callback));
  }

  dispatch(type) {
    const event = {
      type,
      target: this,
      currentTarget: this,
      defaultPrevented: false,
      preventDefault() {
        this.defaultPrevented = true;
      },
    };
    for (const callback of this.listeners.get(type) ?? []) callback(event);
    return event;
  }

  click() {
    if (!this.disabled) this.dispatch("click");
  }

  focus() {
    this.ownerDocument.activeElement = this;
  }

  reset() {
    for (const input of this.querySelectorAll("input")) {
      input.value = "";
      input.checked = false;
    }
  }

  append(...children) {
    this.children.push(...children);
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? null;
  }

  querySelectorAll(selector) {
    return this.ownerDocument.queryWithin(this, selector);
  }
}

class FakeDocument {
  constructor() {
    this.hidden = false;
    this.activeElement = null;
    this.listeners = new Map();
    this.body = new FakeElement(this, "body");
    this.root = new FakeElement(this, "section");
    this.root.setAttribute("id", "bootstrap-onboarding");
    this.root.hidden = true;
    this.content = new FakeElement(this, "main");
    this.content.setAttribute("id", "onboarding-content");
    this.content.replacesView = true;
    this.appShell = new FakeElement(this, "div");
    this.appShell.setAttribute("id", "atlas-app-shell");
    this.live = new FakeElement(this, "div");
    this.live.setAttribute("id", "onboarding-live");
    const markers = ["welcome", "account", "credentials", "review", "install"].map((step) => {
      const marker = new FakeElement(this, "li");
      marker.setAttribute("data-onboarding-marker", step);
      return marker;
    });
    this.root.append(...markers, this.live, this.content);
    this.body.append(this.root, this.appShell);
  }

  createElement(tagName) {
    return new FakeElement(this, tagName);
  }

  getElementById(id) {
    return this.walk(this.body).find((element) => element.id === id) ?? null;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] ?? null;
  }

  querySelectorAll(selector) {
    return this.walk(this.body).filter((element) => this.matches(element, selector));
  }

  queryWithin(root, selector) {
    return root.children.flatMap((child) => this.walk(child)).filter((element) => this.matches(element, selector));
  }

  walk(root) {
    return [root, ...root.children.flatMap((child) => this.walk(child))];
  }

  matches(element, selector) {
    return String(selector).split(",").some((part) => {
      const candidate = part.trim();
      if (!candidate) return false;
      if (candidate.startsWith("#")) return element.id === candidate.slice(1);
      if (candidate.startsWith(".")) return element.classList.contains(candidate.slice(1));
      const attribute = candidate.match(/^([a-z0-9-]+)?\[([^=\]]+)(?:=['\"]?([^'\"\]]+)['\"]?)?\]$/i);
      if (attribute) {
        const [, tagName, name, expected] = attribute;
        if (tagName && element.tagName !== tagName.toUpperCase()) return false;
        const actual = element.getAttribute(name);
        return actual !== null && (expected === undefined || actual === expected);
      }
      return element.tagName === candidate.toUpperCase();
    });
  }

  replaceView(content, markup) {
    content.children = [];
    const startTag = /<([a-z][a-z0-9-]*)\b([^>]*)>/gi;
    let match;
    while ((match = startTag.exec(markup)) !== null) {
      const element = new FakeElement(this, match[1]);
      const attributes = match[2];
      const attribute = /([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g;
      let item;
      while ((item = attribute.exec(attributes)) !== null) {
        element.setAttribute(item[1], item[2] ?? item[3] ?? item[4] ?? "");
      }
      content.append(element);
    }
  }

  addEventListener(type, callback) {
    const callbacks = this.listeners.get(type) ?? [];
    callbacks.push(callback);
    this.listeners.set(type, callbacks);
  }

  removeEventListener(type, callback) {
    const callbacks = this.listeners.get(type) ?? [];
    this.listeners.set(type, callbacks.filter((candidate) => candidate !== callback));
  }
}

async function settlePromises() {
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
}

async function exerciseCompleteGraphicalFlow() {
  const fakeDocument = new FakeDocument();
  globalThis.document = fakeDocument;
  window.requestAnimationFrame = (callback) => {
    callback();
    return 1;
  };
  window.clearTimeout = () => {};

  const openBaoSecret = "phrase-openbao-integration-2026";
  const zulipSecret = "phrase-zulip-integration-2026!";
  const calls = [];
  let finishBootstrap;
  let beginArgumentReference;
  let atlasReady = 0;
  let preflightPassed = false;
  let installedProofPublished = false;
  const callNative = (command, args) => {
    const snapshot = args === undefined ? undefined : JSON.parse(JSON.stringify(args));
    calls.push({ command, args: snapshot });
    if (command === "get_bootstrap_status") {
      if (installedProofPublished) {
        return Promise.resolve({
          state: "installed",
          required: false,
          atlas_available: true,
          zulip_url: "https://zulip.ops.local:8443",
          zulip_owner_email: "admin@ops.local",
        });
      }
      return Promise.resolve({
        state: "ready",
        required: true,
        release_id: "atlas-integration-release",
        bootstrap_helper_sha256: "d".repeat(64),
        manifest_sha256: "a".repeat(64),
        source_tree_sha256: "b".repeat(64),
        rpm_sha256: "c".repeat(64),
        hermes_source_archive_sha256: "e".repeat(64),
        confirmation_digest_suffix: "12abcdef3456",
        preflight_passed: preflightPassed,
        operation_mode: "fresh",
        local_username: "ops-user",
        zulip_owner_email: "admin@ops.local",
        zulip_url: "https://zulip.ops.local:8443",
      });
    }
    if (command === "run_bootstrap_preflight") {
      preflightPassed = true;
      return Promise.resolve({ status: "preflight-passed", preflight_passed: true });
    }
    if (command === "begin_bootstrap") {
      beginArgumentReference = args;
      return new Promise((resolve) => {
        finishBootstrap = resolve;
      });
    }
    if (command === "open_zulip") return Promise.resolve({ status: "opened" });
    return Promise.reject(new Error(`commande native inattendue: ${command}`));
  };

  await onboarding.init({ callNative, onReady: async () => { atlasReady += 1; } });
  assert(fakeDocument.getElementById("onboarding-start"), "l’écran bienvenue doit être rendu par init");
  fakeDocument.getElementById("onboarding-start").click();
  assert(fakeDocument.getElementById("account-continue"), "le bouton bienvenue doit atteindre l’écran compte");
  equal(fakeDocument.getElementById("zulip-owner-email").textContent, "admin@ops.local", "le compte propriétaire doit être visible");

  fakeDocument.getElementById("account-continue").click();
  await settlePromises();
  equal(calls[1].command, "run_bootstrap_preflight", "le compte doit lancer le préflight natif");
  assert(fakeDocument.getElementById("credentials-form"), "un préflight réussi doit atteindre réellement l’écran des mots de passe");

  const fieldIds = ["openbao-password", "openbao-confirmation", "zulip-password", "zulip-confirmation"];
  for (const id of fieldIds) {
    const input = fakeDocument.getElementById(id);
    assert(input, `le champ graphique ${id} doit exister`);
    equal(input.type, "password", `le champ ${id} doit être masqué`);
    equal(input.getAttribute("autocomplete"), "new-password", `le champ ${id} ne doit pas être mémorisé comme identifiant`);
  }
  const enteredFields = fieldIds.map((id) => fakeDocument.getElementById(id));
  enteredFields[0].value = openBaoSecret;
  enteredFields[1].value = openBaoSecret;
  enteredFields[2].value = zulipSecret;
  enteredFields[3].value = zulipSecret;
  enteredFields.forEach((input) => input.dispatch("input"));
  equal(fakeDocument.getElementById("credentials-continue").disabled, false, "quatre saisies conformes doivent autoriser la validation");
  fakeDocument.getElementById("credentials-form").dispatch("submit");

  assert(fakeDocument.getElementById("install-start"), "la validation des mots de passe doit atteindre la revue de release");
  equal(
    fakeDocument.getElementById("review-hermes").textContent,
    "e".repeat(64),
    "la revue doit afficher l’archive Hermes liée à la confirmation",
  );
  enteredFields.forEach((input) => equal(input.value, "", "chaque champ secret doit être vidé avant la revue"));
  assert(!fakeDocument.content.innerHTML.includes(openBaoSecret), "le mot de passe OpenBao ne doit jamais entrer dans le HTML");
  assert(!fakeDocument.content.innerHTML.includes(zulipSecret), "le mot de passe Zulip ne doit jamais entrer dans le HTML");

  const confirmation = fakeDocument.getElementById("release-confirmation");
  confirmation.checked = true;
  confirmation.dispatch("change");
  equal(fakeDocument.getElementById("install-start").disabled, false, "la confirmation graphique doit autoriser l’installation");
  fakeDocument.getElementById("install-start").click();
  await settlePromises();

  assert(fakeDocument.getElementById("installation-phases"), "begin_bootstrap doit afficher la progression graphique");
  const beginCall = calls.find((call) => call.command === "begin_bootstrap");
  assert(beginCall, "la revue doit invoquer begin_bootstrap");
  equal(beginCall.args.openBaoPassword, openBaoSecret, "OpenBao doit être transmis à begin_bootstrap");
  equal(beginCall.args.zulipPassword, zulipSecret, "Zulip doit être transmis à begin_bootstrap");
  equal(
    Object.keys(beginCall.args).sort().join(","),
    "confirmedDigestSuffix,confirmedReleaseId,openBaoPassword,zulipPassword",
    "l’invoke bootstrap ne doit recevoir que les secrets et la confirmation de release",
  );
  const secretBearingCalls = calls.filter((call) => {
    const serialized = JSON.stringify(call.args ?? {});
    return serialized.includes(openBaoSecret) || serialized.includes(zulipSecret);
  });
  equal(secretBearingCalls.length, 1, "les secrets ne doivent traverser qu’un seul appel natif");
  equal(secretBearingCalls[0].command, "begin_bootstrap", "seul invoke begin_bootstrap doit porter les secrets");
  equal(beginArgumentReference.openBaoPassword, "", "la référence de requête OpenBao doit être effacée après invoke");
  equal(beginArgumentReference.zulipPassword, "", "la référence de requête Zulip doit être effacée après invoke");

  installedProofPublished = true;
  finishBootstrap({
    status: "succeeded",
    success: true,
    zulip_url: "https://zulip.ops.local:8443",
  });
  await settlePromises();
  assert(fakeDocument.getElementById("open-zulip"), "un résultat réussi doit atteindre l’écran de succès Zulip");
  equal(fakeDocument.getElementById("success-owner-email").textContent, "admin@ops.local", "le login Zulip doit être affiché au succès");
  equal(fakeDocument.getElementById("success-zulip-url").textContent, "https://zulip.ops.local:8443", "l’URL Zulip locale doit être affichée");
  assert(!fakeDocument.content.innerHTML.includes(openBaoSecret), "le succès ne doit contenir aucun secret OpenBao");
  assert(!fakeDocument.content.innerHTML.includes(zulipSecret), "le succès ne doit contenir aucun secret Zulip");

  fakeDocument.getElementById("open-zulip").click();
  await settlePromises();
  equal(calls.at(-1).command, "open_zulip", "Zulip doit être ouvert uniquement par la commande native dédiée");
  fakeDocument.getElementById("open-atlas").click();
  await settlePromises();
  equal(atlasReady, 1, "le bouton final doit ouvrir Atlas");
  equal(fakeDocument.root.hidden, true, "l’onboarding doit être masqué après le succès");
}

async function exerciseCompleteCorrectiveFlow() {
  const fakeDocument = new FakeDocument();
  globalThis.document = fakeDocument;
  window.requestAnimationFrame = (callback) => {
    callback();
    return 1;
  };
  window.clearTimeout = () => {};

  const openBaoSecret = "phrase-openbao-corrective-2026";
  const zulipSecret = "phrase-zulip-corrective-2026!";
  const calls = [];
  let correctivePreflightPassed = false;
  let installedProofPublished = false;
  let finishBootstrap;
  let atlasReady = 0;
  const correctiveStatus = () => ({
    state: "recovery-required",
    required: true,
    release_id: "atlas-api-zulip-2026.09.08.11",
    recovery_from_release_id: "atlas-api-zulip-2026.09.08.10",
    bootstrap_helper_sha256: "1".repeat(64),
    manifest_sha256: "2".repeat(64),
    source_tree_sha256: "3".repeat(64),
    rpm_sha256: "4".repeat(64),
    hermes_source_archive_sha256: "5".repeat(64),
    confirmation_digest_suffix: "abcdef123456",
    corrective_available: true,
    resume_available: true,
    preflight_passed: correctivePreflightPassed,
    operation_mode: "corrective",
    atlas_available: false,
    local_username: "ops-user",
    zulip_owner_email: "admin@ops.local",
  });
  const callNative = (command, args) => {
    calls.push({
      command,
      args: args === undefined ? undefined : JSON.parse(JSON.stringify(args)),
    });
    if (command === "get_bootstrap_status") {
      return Promise.resolve(
        installedProofPublished
          ? {
              state: "installed",
              required: false,
              atlas_available: true,
              zulip_url: "https://zulip.ops.local:8443",
              zulip_owner_email: "admin@ops.local",
            }
          : correctiveStatus(),
      );
    }
    if (command === "run_bootstrap_recovery_preflight") {
      correctivePreflightPassed = true;
      return Promise.resolve({
        status: "corrective-preflight-passed",
        preflight_passed: true,
        rollback_finalize_resume: false,
        operation_mode: "corrective",
      });
    }
    if (command === "begin_recovery_bootstrap") {
      return new Promise((resolve) => {
        finishBootstrap = resolve;
      });
    }
    return Promise.reject(new Error(`commande corrective inattendue: ${command}`));
  };

  await onboarding.init({ callNative, onReady: async () => { atlasReady += 1; } });
  assert(fakeDocument.getElementById("recovery-action"), "recovery-required doit afficher l’action corrective");
  assert(
    fakeDocument.content.innerHTML.includes("Reprendre la réparation"),
    "un marker correctif valide après crash doit proposer une reprise",
  );
  assert(!fakeDocument.getElementById("credentials-form"), "aucun formulaire secret ne doit précéder l’attestation root");
  assert(!fakeDocument.getElementById("openbao-password"), "aucun champ OpenBao ne doit précéder l’attestation root");
  assert(!fakeDocument.getElementById("zulip-password"), "aucun champ Zulip ne doit précéder l’attestation root");
  equal(
    fakeDocument.getElementById("recovery-from-release").textContent,
    "atlas-api-zulip-2026.09.08.10",
    "la release annulée doit être visible",
  );
  equal(
    fakeDocument.getElementById("recovery-target-release").textContent,
    "atlas-api-zulip-2026.09.08.11",
    "la release corrective doit être visible",
  );

  fakeDocument.getElementById("recovery-action").click();
  await settlePromises();
  equal(calls[1].command, "run_bootstrap_recovery_preflight", "la reprise doit utiliser le préflight racine de classification");
  assert(fakeDocument.getElementById("credentials-form"), "l’attestation corrective doit seule débloquer les quatre champs");

  const fieldIds = ["openbao-password", "openbao-confirmation", "zulip-password", "zulip-confirmation"];
  const fields = fieldIds.map((id) => fakeDocument.getElementById(id));
  fields[0].value = openBaoSecret;
  fields[1].value = openBaoSecret;
  fields[2].value = zulipSecret;
  fields[3].value = zulipSecret;
  fields.forEach((input) => input.dispatch("input"));
  fakeDocument.getElementById("credentials-form").dispatch("submit");
  equal(
    fakeDocument.getElementById("review-recovery-from").textContent,
    "atlas-api-zulip-2026.09.08.10",
    "la revue doit montrer explicitement .10 comme origine",
  );
  equal(
    fakeDocument.getElementById("review-recovery-target").textContent,
    "atlas-api-zulip-2026.09.08.11",
    "la revue doit montrer explicitement la nouvelle release",
  );
  equal(
    fakeDocument.getElementById("review-hermes").textContent,
    "5".repeat(64),
    "la revue corrective doit montrer l’archive Hermes attestée",
  );
  fields.forEach((input) => equal(input.value, "", "les champs correctifs doivent être vidés avant la revue"));

  const confirmation = fakeDocument.getElementById("release-confirmation");
  confirmation.checked = true;
  confirmation.dispatch("change");
  fakeDocument.getElementById("install-start").click();
  await settlePromises();
  const apply = calls.find((call) => call.command === "begin_recovery_bootstrap");
  assert(apply, "la revue corrective doit utiliser begin_recovery_bootstrap");
  assert(!calls.some((call) => call.command === "begin_bootstrap"), "le parcours correctif ne doit jamais retomber sur apply fresh");
  equal(apply.args.openBaoPassword, openBaoSecret, "le secret OpenBao doit traverser un seul invoke correctif");
  equal(apply.args.zulipPassword, zulipSecret, "le secret Zulip doit traverser un seul invoke correctif");

  installedProofPublished = true;
  finishBootstrap({ status: "succeeded", success: true, zulip_url: "https://zulip.ops.local:8443" });
  await settlePromises();
  assert(fakeDocument.getElementById("open-atlas"), "la preuve installed doit débloquer l’écran de succès correctif");
  fakeDocument.getElementById("open-atlas").click();
  await settlePromises();
  equal(atlasReady, 1, "Atlas ne doit s’ouvrir qu’après une seconde lecture de la preuve installed");
}

async function exerciseSecretlessRollbackFinalizerFlow() {
  const fakeDocument = new FakeDocument();
  globalThis.document = fakeDocument;
  window.requestAnimationFrame = (callback) => {
    callback();
    return 1;
  };
  window.clearTimeout = () => {};

  const calls = [];
  let preflightPassed = false;
  let finalized = false;
  let atlasReady = 0;
  const pendingStatus = () => ({
    state: "recovery-required",
    required: true,
    release_id: "atlas-api-zulip-2026.09.08.11",
    recovery_from_release_id: "atlas-api-zulip-2026.09.08.10",
    bootstrap_helper_sha256: "1".repeat(64),
    manifest_sha256: "2".repeat(64),
    source_tree_sha256: "3".repeat(64),
    rpm_sha256: "4".repeat(64),
    hermes_source_archive_sha256: "5".repeat(64),
    confirmation_digest_suffix: "abcdef123456",
    corrective_available: true,
    resume_available: true,
    preflight_passed: preflightPassed,
    rollback_finalize_resume: preflightPassed,
    operation_mode: "corrective",
    atlas_available: false,
  });
  const callNative = (command, args) => {
    calls.push({ command, args: args === undefined ? undefined : JSON.parse(JSON.stringify(args)) });
    if (command === "get_bootstrap_status") {
      return Promise.resolve(finalized
        ? {
            state: "recovery-required",
            required: true,
            corrective_available: false,
            resume_available: false,
            rollback_finalize_resume: false,
            atlas_available: false,
          }
        : pendingStatus());
    }
    if (command === "run_bootstrap_recovery_preflight") {
      preflightPassed = true;
      return Promise.resolve({
        status: "corrective-preflight-passed",
        preflight_passed: true,
        rollback_finalize_resume: true,
        operation_mode: "corrective",
      });
    }
    if (command === "begin_recovery_bootstrap") {
      finalized = true;
      return Promise.resolve({ status: "rollback-finalized" });
    }
    return Promise.reject(new Error(`commande finalizer inattendue: ${command}`));
  };

  await onboarding.init({ callNative, onReady: async () => { atlasReady += 1; } });
  fakeDocument.getElementById("recovery-action").click();
  await settlePromises();
  assert(fakeDocument.getElementById("rollback-finalize-confirmation"), "le bit root doit afficher la revue rollback dédiée");
  equal(
    fakeDocument.getElementById("rollback-review-hermes").textContent,
    "5".repeat(64),
    "la finalisation rollback doit afficher l’archive Hermes liée au suffixe",
  );
  assert(!fakeDocument.getElementById("credentials-form"), "la finalisation rollback ne doit afficher aucun formulaire secret");
  assert(fakeDocument.content.innerHTML.includes("Aucun mot de passe"), "l’interface doit annoncer explicitement la voie sans secret");

  const confirmation = fakeDocument.getElementById("rollback-finalize-confirmation");
  confirmation.checked = true;
  confirmation.dispatch("change");
  fakeDocument.getElementById("rollback-finalize-start").click();
  await settlePromises();

  const apply = calls.find((call) => call.command === "begin_recovery_bootstrap");
  assert(apply, "le finalizer doit invoquer l’apply attesté");
  equal(
    Object.keys(apply.args).sort().join(","),
    "confirmedDigestSuffix,confirmedReleaseId",
    "la voie rollback ne doit transmettre que la confirmation non secrète",
  );
  assert(!JSON.stringify(calls).includes("Password"), "aucun champ de mot de passe ne doit traverser la voie rollback");
  equal(atlasReady, 0, "un rollback finalisé ne doit jamais déverrouiller Atlas");
  assert(fakeDocument.content.innerHTML.includes("Revérifier l’état"), "après finalisation, l’interface doit rester sur le verrou de récupération");
}

async function exerciseFreshRecoveryClassification() {
  for (const rollbackFinalizeResume of [true, false]) {
    const fakeDocument = new FakeDocument();
    globalThis.document = fakeDocument;
    window.requestAnimationFrame = (callback) => {
      callback();
      return 1;
    };
    window.clearTimeout = () => {};

    const calls = [];
    let preflightPassed = false;
    const status = () => ({
      state: "recovery-required",
      required: true,
      release_id: "atlas-api-zulip-2026.09.08.11",
      recovery_from_release_id: preflightPassed ? undefined : "atlas-api-zulip-2026.09.08.10",
      bootstrap_helper_sha256: "1".repeat(64),
      manifest_sha256: "2".repeat(64),
      source_tree_sha256: "3".repeat(64),
      rpm_sha256: "4".repeat(64),
      hermes_source_archive_sha256: "5".repeat(64),
      confirmation_digest_suffix: "123456abcdef",
      corrective_available: true,
      resume_available: true,
      preflight_passed: preflightPassed,
      rollback_finalize_resume: preflightPassed && rollbackFinalizeResume,
      operation_mode: preflightPassed ? "fresh" : "corrective",
      atlas_available: false,
    });
    const callNative = (command, args) => {
      calls.push({ command, args: args === undefined ? undefined : JSON.parse(JSON.stringify(args)) });
      if (command === "get_bootstrap_status") return Promise.resolve(status());
      if (command === "run_bootstrap_recovery_preflight") {
        preflightPassed = true;
        return Promise.resolve({
          status: "recovery-preflight-passed",
          preflight_passed: true,
          rollback_finalize_resume: rollbackFinalizeResume,
          operation_mode: "fresh",
        });
      }
      if (command === "begin_recovery_bootstrap") {
        return Promise.resolve({ status: "started" });
      }
      return Promise.reject(new Error(`commande fresh-recovery inattendue: ${command}`));
    };

    await onboarding.init({ callNative, onReady: async () => {} });
    fakeDocument.getElementById("recovery-action").click();
    await settlePromises();
    if (!rollbackFinalizeResume) {
      assert(fakeDocument.getElementById("credentials-form"), "un resume fresh avant pending doit demander les mots de passe après attestation");
      assert(!fakeDocument.getElementById("rollback-finalize-confirmation"), "un resume fresh normal ne doit pas emprunter le finalizer");
      continue;
    }
    assert(fakeDocument.getElementById("rollback-finalize-confirmation"), "un pending fresh doit emprunter le finalizer sans secret");
    const confirmation = fakeDocument.getElementById("rollback-finalize-confirmation");
    confirmation.checked = true;
    confirmation.dispatch("change");
    fakeDocument.getElementById("rollback-finalize-start").click();
    await settlePromises();
    const apply = calls.find((call) => call.command === "begin_recovery_bootstrap");
    assert(apply, "le pending fresh doit conserver l’apply recovery neutre");
    equal(Object.keys(apply.args).sort().join(","), "confirmedDigestSuffix,confirmedReleaseId", "le pending fresh ne doit transmettre aucun secret");
  }
}

async function exerciseMissingHermesIdentityFailsClosedAfterPreflight() {
  const fakeDocument = new FakeDocument();
  globalThis.document = fakeDocument;
  window.requestAnimationFrame = (callback) => {
    callback();
    return 1;
  };
  window.clearTimeout = () => {};

  let preflightPassed = false;
  const calls = [];
  const statusWithoutHermes = () => ({
    state: "ready",
    required: true,
    release_id: "release-without-hermes-identity",
    bootstrap_helper_sha256: "1".repeat(64),
    manifest_sha256: "2".repeat(64),
    source_tree_sha256: "3".repeat(64),
    rpm_sha256: "4".repeat(64),
    confirmation_digest_suffix: "abcdef123456",
    preflight_passed: preflightPassed,
    operation_mode: "fresh",
  });
  const callNative = (command) => {
    calls.push(command);
    if (command === "get_bootstrap_status") return Promise.resolve(statusWithoutHermes());
    if (command === "run_bootstrap_preflight") {
      preflightPassed = true;
      return Promise.resolve({
        status: "preflight-passed",
        preflight_passed: true,
        rollback_finalize_resume: false,
        operation_mode: "fresh",
      });
    }
    return Promise.reject(new Error(`commande inattendue sans identité Hermes: ${command}`));
  };

  await onboarding.init({ callNative, onReady: async () => {} });
  fakeDocument.getElementById("onboarding-start").click();
  fakeDocument.getElementById("account-continue").click();
  await settlePromises();
  equal(
    fakeDocument.getElementById("failure-code").textContent,
    "bootstrap_preflight_attestation_invalid",
    "le statut relu après préflight doit exiger l’identité de l’archive Hermes",
  );
  assert(!fakeDocument.getElementById("credentials-form"), "une identité Hermes absente ne doit exposer aucun secret");
  equal(
    calls.join(","),
    "get_bootstrap_status,run_bootstrap_preflight,get_bootstrap_status",
    "le verrou doit échouer immédiatement après la relecture attestée",
  );
}

async function exerciseMissingHermesIdentityBlocksRollbackReview() {
  const fakeDocument = new FakeDocument();
  globalThis.document = fakeDocument;
  window.requestAnimationFrame = (callback) => {
    callback();
    return 1;
  };
  window.clearTimeout = () => {};

  const calls = [];
  const callNative = (command) => {
    calls.push(command);
    if (command === "get_bootstrap_status") {
      return Promise.resolve({
        state: "recovery-required",
        required: true,
        release_id: "atlas-api-zulip-2026.09.08.11",
        recovery_from_release_id: "atlas-api-zulip-2026.09.08.10",
        bootstrap_helper_sha256: "1".repeat(64),
        manifest_sha256: "2".repeat(64),
        source_tree_sha256: "3".repeat(64),
        rpm_sha256: "4".repeat(64),
        confirmation_digest_suffix: "abcdef123456",
        corrective_available: true,
        resume_available: true,
        preflight_passed: true,
        rollback_finalize_resume: true,
        operation_mode: "corrective",
      });
    }
    return Promise.reject(new Error(`le helper ne doit pas être relancé: ${command}`));
  };

  await onboarding.init({ callNative, onReady: async () => {} });
  fakeDocument.getElementById("recovery-action").click();
  await settlePromises();
  equal(
    fakeDocument.getElementById("failure-code").textContent,
    "bootstrap_preflight_attestation_invalid",
    "la revue rollback doit exiger l’identité de l’archive Hermes",
  );
  assert(
    !fakeDocument.getElementById("rollback-finalize-confirmation"),
    "la confirmation rollback doit rester inaccessible sans identité Hermes",
  );
  equal(calls.join(","), "get_bootstrap_status", "aucun apply ne doit être lancé depuis une identité incomplète");
}

async function exerciseMissingHermesIdentityDisablesReleaseReview() {
  const fakeDocument = new FakeDocument();
  globalThis.document = fakeDocument;
  window.requestAnimationFrame = (callback) => {
    callback();
    return 1;
  };
  window.clearTimeout = () => {};

  const calls = [];
  const callNative = (command) => {
    calls.push(command);
    if (command === "get_bootstrap_status") {
      return Promise.resolve({
        state: "ready",
        required: true,
        release_id: "release-without-hermes-review",
        bootstrap_helper_sha256: "1".repeat(64),
        manifest_sha256: "2".repeat(64),
        source_tree_sha256: "3".repeat(64),
        rpm_sha256: "4".repeat(64),
        confirmation_digest_suffix: "abcdef123456",
        preflight_passed: true,
        operation_mode: "fresh",
      });
    }
    return Promise.reject(new Error(`aucune autre commande ne doit être appelée: ${command}`));
  };

  await onboarding.init({ callNative, onReady: async () => {} });
  fakeDocument.getElementById("onboarding-start").click();
  fakeDocument.getElementById("account-continue").click();
  await settlePromises();
  const values = [
    "phrase-openbao-review-2026",
    "phrase-openbao-review-2026",
    "phrase-zulip-review-2026!",
    "phrase-zulip-review-2026!",
  ];
  ["openbao-password", "openbao-confirmation", "zulip-password", "zulip-confirmation"].forEach((id, index) => {
    const input = fakeDocument.getElementById(id);
    input.value = values[index];
    input.dispatch("input");
  });
  fakeDocument.getElementById("credentials-form").dispatch("submit");

  assert(fakeDocument.getElementById("release-confirmation").disabled, "la confirmation doit être désactivée sans identité Hermes");
  assert(fakeDocument.getElementById("install-start").disabled, "l’installation doit rester désactivée sans identité Hermes");
  assert(!fakeDocument.getElementById("release-warning").hidden, "la revue doit expliquer que les empreintes sont incomplètes");
  equal(
    fakeDocument.getElementById("review-hermes").textContent,
    "Empreinte indisponible",
    "la carte de revue doit rendre explicite l’identité Hermes absente",
  );
  equal(calls.join(","), "get_bootstrap_status", "la revue incomplète ne doit lancer aucune installation");
}

async function exerciseAtlasProofGate() {
  const fakeDocument = new FakeDocument();
  globalThis.document = fakeDocument;
  window.requestAnimationFrame = (callback) => {
    callback();
    return 1;
  };
  let atlasReady = 0;
  const callNative = () =>
    Promise.resolve({ state: "installed", required: false, atlas_available: false });
  await onboarding.init({ callNative, onReady: async () => { atlasReady += 1; } });
  equal(atlasReady, 0, "un état installed contradictoire sans binaire validé ne doit jamais ouvrir Atlas");
  assert(fakeDocument.getElementById("failure-retry"), "l’absence de preuve/binaire cohérent doit rester dans l’onboarding");
}

async function exerciseCorrectiveIdentityGate() {
  const fakeDocument = new FakeDocument();
  globalThis.document = fakeDocument;
  window.requestAnimationFrame = (callback) => {
    callback();
    return 1;
  };
  const calls = [];
  const callNative = (command) => {
    calls.push(command);
    return Promise.resolve({
      state: "recovery-required",
      required: true,
      release_id: "atlas-api-zulip-2026.09.08.11",
      recovery_from_release_id: "atlas-api-zulip-2026.09.08.09",
      bootstrap_helper_sha256: "1".repeat(64),
      manifest_sha256: "2".repeat(64),
      source_tree_sha256: "3".repeat(64),
      rpm_sha256: "4".repeat(64),
      hermes_source_archive_sha256: "5".repeat(64),
      confirmation_digest_suffix: "abcdef123456",
      corrective_available: true,
      operation_mode: "corrective",
      atlas_available: false,
    });
  };
  await onboarding.init({ callNative, onReady: async () => {} });
  assert(
    fakeDocument.content.innerHTML.includes("Revérifier l’état"),
    "une origine corrective différente de .10 doit rester verrouillée",
  );
  assert(
    !fakeDocument.content.innerHTML.includes("Vérifier et réparer") &&
      !fakeDocument.content.innerHTML.includes("Reprendre la réparation"),
    "une identité corrective non reconnue ne doit proposer aucune opération corrective",
  );
  assert(!fakeDocument.getElementById("credentials-form"), "une origine corrective invalide ne doit exposer aucun secret");
  equal(calls.join(","), "get_bootstrap_status", "le verrou identitaire ne doit lancer aucun helper");
}

async function exerciseNeutralPreflightFailurePreservesRootError() {
  const fakeDocument = new FakeDocument();
  globalThis.document = fakeDocument;
  window.requestAnimationFrame = (callback) => {
    callback();
    return 1;
  };
  window.clearTimeout = () => {};

  const calls = [];
  const callNative = (command) => {
    calls.push(command);
    if (command === "get_bootstrap_status") {
      return Promise.resolve({
        state: "recovery-required",
        required: true,
        release_id: "atlas-api-zulip-2026.09.08.11",
        recovery_from_release_id: "atlas-api-zulip-2026.09.08.10",
        bootstrap_helper_sha256: "1".repeat(64),
        manifest_sha256: "2".repeat(64),
        source_tree_sha256: "3".repeat(64),
        rpm_sha256: "4".repeat(64),
        hermes_source_archive_sha256: "5".repeat(64),
        confirmation_digest_suffix: "abcdef123456",
        corrective_available: true,
        resume_available: true,
        preflight_passed: false,
        operation_mode: "corrective",
        atlas_available: false,
      });
    }
    if (command === "run_bootstrap_recovery_preflight") {
      return Promise.resolve({
        status: "failed",
        preflight_passed: false,
        error_code: "network_unavailable",
        operation_mode: "recovery",
      });
    }
    return Promise.reject(new Error(`commande inattendue après échec du préflight neutre: ${command}`));
  };

  await onboarding.init({ callNative, onReady: async () => {} });
  fakeDocument.getElementById("recovery-action").click();
  await settlePromises();

  equal(
    calls.join(","),
    "get_bootstrap_status,run_bootstrap_recovery_preflight",
    "un échec root ne doit déclencher aucune lecture de statut supplémentaire",
  );
  equal(
    fakeDocument.getElementById("failure-code").textContent,
    "network_unavailable",
    "le code root doit être affiché avant toute validation du mode encore neutre",
  );
  assert(!fakeDocument.getElementById("credentials-form"), "un préflight en échec ne doit afficher aucun formulaire secret");
  assert(!fakeDocument.getElementById("openbao-password"), "un préflight en échec ne doit afficher aucun secret OpenBao");
  assert(!fakeDocument.getElementById("zulip-password"), "un préflight en échec ne doit afficher aucun secret Zulip");
}

for (const forbidden of [
  "localStorage",
  "sessionStorage",
  "window.open(",
  "fetch(",
  "XMLHttpRequest",
  "__TAURI__.shell",
  "plugin-shell",
]) {
  assert(!onboardingSource.includes(forbidden), `le parcours bootstrap ne doit pas utiliser ${forbidden}`);
}

let integrationError = null;
const integrationLoop = GLib.MainLoop.new(null, false);
Promise.resolve()
  .then(exerciseCompleteGraphicalFlow)
  .then(exerciseCompleteCorrectiveFlow)
  .then(exerciseSecretlessRollbackFinalizerFlow)
  .then(exerciseFreshRecoveryClassification)
  .then(exerciseMissingHermesIdentityFailsClosedAfterPreflight)
  .then(exerciseMissingHermesIdentityBlocksRollbackReview)
  .then(exerciseMissingHermesIdentityDisablesReleaseReview)
  .then(exerciseCorrectiveIdentityGate)
  .then(exerciseNeutralPreflightFailurePreservesRootError)
  .then(exerciseAtlasProofGate)
  .then(
  () => integrationLoop.quit(),
  (error) => {
    integrationError = error;
    integrationLoop.quit();
  },
  );
integrationLoop.run();
if (integrationError) throw integrationError;

globalThis.document = { addEventListener() {} };
const appSource = read(ARGV[1]);
eval(appSource);
for (const forbidden of ["begin_credential_change", "localStorage", "sessionStorage", "console.log"]) {
  assert(!appSource.includes(forbidden), `app.js ne doit pas contenir ${forbidden}`);
}
assert(appSource.includes('callNative("save_provider_credential", request)'), "le modal doit appeler save_provider_credential");
assert(appSource.includes('maxlength="4096"'), "la clé API doit être bornée à 4096 caractères");
assert(appSource.includes("request.openBaoPassword = \"\""), "la référence de mot de passe doit être écrasée après invoke");
assert(appSource.includes("request.apiKey = \"\""), "la référence de clé doit être écrasée après invoke");

print("frontend onboarding tests: ok");
