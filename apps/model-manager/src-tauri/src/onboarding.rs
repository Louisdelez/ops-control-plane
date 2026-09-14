use crate::{security, CommandError};
#[cfg(target_os = "linux")]
use rustix::event::{poll, PollFd, PollFlags, Timespec};
#[cfg(target_os = "linux")]
use rustix::fs::{fcntl_add_seals, fcntl_get_seals, memfd_create, MemfdFlags, SealFlags};
#[cfg(unix)]
use rustix::fs::{open, Mode, OFlags};
#[cfg(target_os = "linux")]
use rustix::process::{kill_process_group, Pid, Signal};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs::{self, File};
#[cfg(test)]
use std::io::BufRead;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter};
use zeroize::Zeroizing;

#[cfg(unix)]
use std::os::unix::fs::{MetadataExt, PermissionsExt};
#[cfg(target_os = "linux")]
use std::os::unix::process::CommandExt;

const SOURCE_ROOT: &str = "/home/ops-user/ops-control-plane";
const TRUST_ANCHOR_ROOT: &str = "/usr/local/lib/ops-control-plane/atlas-api-zulip-2026.09.08.11";
const BOOTSTRAP_HELPER: &str =
    "/usr/local/lib/ops-control-plane/atlas-api-zulip-2026.09.08.11/control-plane-bootstrap";
const RELEASE_MANIFEST: &str =
    "/usr/local/lib/ops-control-plane/atlas-api-zulip-2026.09.08.11/release-manifest.v1.json";
const REVIEWED_ATLAS_LAUNCHER: &str =
    "/usr/local/lib/ops-control-plane/atlas-api-zulip-2026.09.08.11/launch-atlas-reviewed-rpm";
const CONSUMED_MARKER: &str = "/var/lib/ops-control-plane-bootstrap.consumed.json";
const PUBLIC_INSTALLED_PROOF: &str = "/var/lib/ops-control-plane-bootstrap.installed.json";
const BOOTSTRAP_STATE_ROOT: &str = "/var/lib/ops-control-plane-bootstrap";
const CORRECTIVE_CONSUMED_MARKER: &str =
    "/var/lib/ops-control-plane-bootstrap-corrective.consumed.json";
const CORRECTIVE_STATE_ROOT: &str = "/var/lib/ops-control-plane-bootstrap-corrective";
const CORRECTIVE_TARGET_RELEASE_ID: &str = "atlas-api-zulip-2026.09.08.11";
const CORRECTIVE_PREDECESSOR_RELEASE_ID: &str = "atlas-api-zulip-2026.09.08.10";
const CORRECTIVE_PREDECESSOR_SOURCE_SHA256: &str =
    "3ede12c444991f12c81359963cfde50bc01d757fba8bf512a56f610a095f40ef";
const CORRECTIVE_PREDECESSOR_RPM_SHA256: &str =
    "a571034b4902e951d8af2754eb904be6fe68d0a582f006d34a6b694f77cad32f";
const CORRECTIVE_PREDECESSOR_HELPER_SHA256: &str =
    "93561352ce9684ce2eb3a1cc8a9abb13375d31073c5b6a515b716cb2e1c3a9d0";
const CORRECTIVE_PREDECESSOR_MANIFEST_SHA256: &str =
    "96f137109ea240264f53e994e4e0179c9dbb90db485c595cd34bbf6a692214d2";
const INSTALLED_ATLAS: &str = "/usr/bin/ops-model-manager";
const REVIEWED_ATLAS_MEMFD: &str = "/memfd:atlas-reviewed-rpm (deleted)";
const ZULIP_URL: &str = "https://zulip.ops.local:8443";
const MAX_MANIFEST_BYTES: u64 = 2 * 1024 * 1024;
const MAX_HELPER_BYTES: u64 = 4 * 1024 * 1024;
const MAX_ATLAS_EXECUTABLE_BYTES: u64 = 64 * 1024 * 1024;
const MAX_PUBLIC_PROOF_BYTES: u64 = 16 * 1024;
const MAX_CONSUMED_MARKER_BYTES: u64 = 128 * 1024;
const MAX_EVENT_BYTES: usize = 16 * 1024;
const MAX_EVENTS: usize = 4096;
const SECRET_READINESS_PHASE: &str = "secret-channel-ready";
const SECRET_READINESS_STATUS: &str = "ready-for-secrets";
const SECRET_READINESS_MESSAGE: &str = "Canal racine scellé et release entièrement vérifiés.";
const ROLLBACK_FINALIZE_READINESS_PHASE: &str = "rollback-finalize-ready";
const ROLLBACK_FINALIZE_READINESS_STATUS: &str = "ready-to-finalize";
const ROLLBACK_FINALIZE_READINESS_MESSAGE: &str =
    "Rollback local vérifié; aucune saisie de mot de passe nécessaire.";
const MUTATION_COMMIT_PHASE: &str = "mutation-commit-ready";
const MUTATION_COMMIT_STATUS: &str = "ready-to-commit";
const MUTATION_COMMIT_MESSAGE: &str = "Transaction prête; décision finale Atlas requise.";
const CANCEL_FRAME: &[u8] = b"ATLASCTL1\n\x01";
const MUTATION_ACK_FRAME: &[u8] = b"ATLASCTL1\n\x02";
const PRE_READINESS_TIMEOUT: Duration = Duration::from_secs(600);
const PREFLIGHT_TOTAL_TIMEOUT: Duration = Duration::from_secs(1_200);
const APPLY_TOTAL_TIMEOUT: Duration = Duration::from_secs(6 * 60 * 60);
const POST_READINESS_SILENCE_TIMEOUT: Duration = Duration::from_secs(3_900);
const CHILD_EXIT_TIMEOUT: Duration = Duration::from_secs(30);

#[cfg(target_os = "linux")]
const SEALED_HELPER_FD: i32 = 2;

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct BootstrapStatus {
    pub required: bool,
    pub state: String,
    pub release_id: String,
    pub bootstrap_helper_sha256: String,
    pub source_tree_sha256: String,
    pub manifest_sha256: String,
    pub rpm_sha256: String,
    pub hermes_source_archive_sha256: String,
    pub confirmation_digest_suffix: String,
    pub zulip_url: Option<String>,
    pub atlas_available: bool,
    pub preflight_passed: bool,
    pub resume_available: bool,
    pub rollback_finalize_resume: bool,
    pub cancellation_allowed: bool,
    pub corrective_available: bool,
    pub operation_mode: Option<String>,
    pub recovery_from_release_id: Option<String>,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct BootstrapPreflightResponse {
    pub status: String,
    pub message: Option<String>,
    pub error_code: Option<String>,
    pub rollback_finalize_resume: bool,
    pub operation_mode: String,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct BootstrapStartResponse {
    pub status: String,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct BootstrapResult {
    pub status: String,
    pub phase: Option<String>,
    pub message: Option<String>,
    pub percent: Option<u8>,
    pub error_code: Option<String>,
    pub zulip_url: Option<String>,
    pub atlas_available: bool,
    pub cancellation_allowed: bool,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct BootstrapCancelResponse {
    pub status: String,
}

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct BootstrapProgress {
    pub phase: String,
    pub message: String,
    pub percent: u8,
    pub status: String,
    pub error_code: Option<String>,
    pub cancellation_allowed: bool,
}

#[derive(Clone)]
pub struct BootstrapManager {
    inner: Arc<Mutex<ManagerState>>,
}

struct ManagerState {
    lifecycle: Lifecycle,
    preflight_attestation: Option<OperationIdentity>,
    operation: Option<Operation>,
    result: BootstrapResult,
    control: Option<ChildStdin>,
    mutation_started: bool,
    cancel_requested: bool,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Lifecycle {
    Idle,
    PreflightRunning,
    PreflightPassed,
    ApplyRunning,
    Succeeded,
    Failed,
    Cancelled,
    RollbackFinalized,
}

impl Lifecycle {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Idle => "ready",
            Self::PreflightRunning => "preflight-running",
            Self::PreflightPassed => "preflight-passed",
            Self::ApplyRunning => "running",
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
            Self::RollbackFinalized => "rollback-finalized",
        }
    }
}

impl Default for BootstrapManager {
    fn default() -> Self {
        Self {
            inner: Arc::new(Mutex::new(ManagerState {
                lifecycle: Lifecycle::Idle,
                preflight_attestation: None,
                operation: None,
                result: idle_result(),
                control: None,
                mutation_started: false,
                cancel_requested: false,
            })),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct ReleaseIdentity {
    release_id: String,
    helper_sha256: String,
    manifest_sha256: String,
    source_sha256: String,
    rpm_sha256: String,
    hermes_source_archive_sha256: String,
    confirmation_digest_suffix: String,
}

impl ReleaseIdentity {
    fn same_artifacts(&self, other: &Self) -> bool {
        self.release_id == other.release_id
            && self.helper_sha256 == other.helper_sha256
            && self.manifest_sha256 == other.manifest_sha256
            && self.source_sha256 == other.source_sha256
            && self.rpm_sha256 == other.rpm_sha256
            && self.hermes_source_archive_sha256 == other.hermes_source_archive_sha256
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct OperationIdentity {
    mode: OperationMode,
    release: ReleaseIdentity,
    recovery_from_release_id: Option<String>,
    rollback_finalize_resume: bool,
}

impl OperationIdentity {
    fn same_reviewed_operation(&self, other: &Self) -> bool {
        self.mode == other.mode
            && self.release == other.release
            && self.recovery_from_release_id == other.recovery_from_release_id
    }
}

#[derive(Deserialize)]
struct ReleaseManifestProjection {
    release_id: String,
    trust_anchor: ManifestTrustAnchor,
    #[serde(default)]
    bootstrap: Option<ManifestBootstrap>,
    source: ManifestSource,
    artifacts: ManifestArtifacts,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ManifestTrustAnchor {
    version_directory: String,
    manifest_path: String,
    bootstrap_helper_path: String,
    launcher_path: String,
    manifest_mode: String,
    bootstrap_helper_mode: String,
    launcher_mode: String,
    launcher_sha256: String,
    enrollment: String,
}

#[derive(Deserialize)]
struct ManifestBootstrap {
    helper_sha256: String,
    gui_protocol: ManifestGuiProtocol,
    #[serde(default)]
    corrective: Option<ManifestCorrective>,
}

#[derive(Deserialize)]
struct ManifestGuiProtocol {
    public_operations: Vec<String>,
    launcher_mode: String,
    atlas_payload_member: String,
    atlas_payload_sha256: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ManifestCorrective {
    mode: String,
    max_attempts: u8,
    consumed_marker: String,
    state_root: String,
    history_root: String,
    recovery_helper: String,
    recovery_unit: String,
    recovery_path_unit: String,
    authority_document: String,
    recovery_from_release_id: String,
    recovery_from_source_tree_sha256: String,
    recovery_from_atlas_rpm_sha256: String,
    recovery_from_bootstrap_helper_sha256: String,
    recovery_from_manifest_sha256: String,
}

#[derive(Deserialize)]
struct ManifestSource {
    tree_sha256: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ManifestArtifacts {
    atlas_rpm: ManifestRpm,
    hermes_source_archive: ManifestHermesSourceArchive,
}

#[derive(Deserialize)]
struct ManifestRpm {
    sha256: String,
    payload_executable_path: String,
    payload_executable_sha256: String,
}

#[derive(Deserialize)]
struct ManifestHermesSourceArchive {
    sha256: String,
}

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
struct PublicInstalledProof {
    // Field order intentionally matches Python's canonical sort order.
    atlas_rpm_sha256: String,
    bootstrap_helper_sha256: String,
    completed_at: u64,
    hermes_source_archive_sha256: String,
    manifest_sha256: String,
    release_id: String,
    schema_version: u8,
    source_tree_sha256: String,
    status: String,
}

#[derive(Clone, Copy)]
struct BootstrapPaths<'a> {
    helper: &'a Path,
    manifest: &'a Path,
    consumed: &'a Path,
    public_proof: &'a Path,
    state_root: &'a Path,
    corrective_consumed: &'a Path,
    corrective_state_root: &'a Path,
    atlas: &'a Path,
}

impl BootstrapPaths<'static> {
    fn system() -> Self {
        Self {
            helper: Path::new(BOOTSTRAP_HELPER),
            manifest: Path::new(RELEASE_MANIFEST),
            consumed: Path::new(CONSUMED_MARKER),
            public_proof: Path::new(PUBLIC_INSTALLED_PROOF),
            state_root: Path::new(BOOTSTRAP_STATE_ROOT),
            corrective_consumed: Path::new(CORRECTIVE_CONSUMED_MARKER),
            corrective_state_root: Path::new(CORRECTIVE_STATE_ROOT),
            atlas: Path::new(INSTALLED_ATLAS),
        }
    }
}

enum DiskBootstrapState {
    Fresh,
    Installed(ReleaseIdentity),
    RecoveryRequired {
        corrective_eligible: bool,
        corrective_started: bool,
    },
}

enum CheckedPresence {
    Absent,
    Valid,
    Invalid,
}

enum ProofPresence {
    Absent,
    Valid(PublicInstalledProof),
    Invalid,
}

// Deliberately no Debug implementation: this value temporarily borrows the
// non-secret confirmation fields while serde writes the metadata frame.
#[derive(Serialize)]
struct ApplyMetadata<'a> {
    schema_version: u8,
    operation_mode: &'a str,
    confirmed_release_id: &'a str,
    confirmed_digest_suffix: &'a str,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct HelperEvent {
    schema_version: u8,
    event: String,
    status: String,
    phase: Option<String>,
    message: Option<String>,
    percent: Option<u8>,
    error_code: Option<String>,
    mutation_started: Option<bool>,
    cancellation_allowed: Option<bool>,
    release_id: Option<String>,
    bootstrap_helper_sha256: Option<String>,
    manifest_sha256: Option<String>,
    source_tree_sha256: Option<String>,
    atlas_rpm_sha256: Option<String>,
    hermes_source_archive_sha256: Option<String>,
    confirmation_digest_suffix: Option<String>,
    operation_mode: Option<String>,
    recovery_from_release_id: Option<String>,
    zulip_url: Option<String>,
    atlas_available: Option<bool>,
    rollback_finalize_resume: Option<bool>,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum OperationMode {
    Fresh,
    Corrective,
    Recovery,
}

impl OperationMode {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Fresh => "fresh",
            Self::Corrective => "corrective",
            Self::Recovery => "recovery",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Operation {
    FreshPreflight,
    FreshApply,
    CorrectivePreflight,
    CorrectiveApply,
    RecoveryPreflight,
    RecoveryApply,
}

impl Operation {
    const fn argument(self) -> &'static str {
        match self {
            Self::FreshPreflight => "gui-preflight",
            Self::FreshApply => "gui-apply",
            Self::CorrectivePreflight => "gui-corrective-preflight",
            Self::CorrectiveApply => "gui-corrective-apply",
            Self::RecoveryPreflight => "gui-recovery-preflight",
            Self::RecoveryApply => "gui-recovery-apply",
        }
    }

    const fn mode(self) -> OperationMode {
        match self {
            Self::FreshPreflight | Self::FreshApply => OperationMode::Fresh,
            Self::CorrectivePreflight | Self::CorrectiveApply => OperationMode::Corrective,
            Self::RecoveryPreflight | Self::RecoveryApply => OperationMode::Recovery,
        }
    }

    const fn is_preflight(self) -> bool {
        matches!(
            self,
            Self::FreshPreflight | Self::CorrectivePreflight | Self::RecoveryPreflight
        )
    }

    const fn is_apply(self) -> bool {
        matches!(
            self,
            Self::FreshApply | Self::CorrectiveApply | Self::RecoveryApply
        )
    }
}

fn idle_result() -> BootstrapResult {
    BootstrapResult {
        status: "idle".to_owned(),
        phase: None,
        message: None,
        percent: None,
        error_code: None,
        zulip_url: None,
        atlas_available: path_exists(INSTALLED_ATLAS),
        cancellation_allowed: false,
    }
}

fn path_exists(path: &str) -> bool {
    fs::symlink_metadata(path).is_ok_and(|metadata| !metadata.file_type().is_symlink())
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

fn valid_identifier(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && value.bytes().all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(byte, b'-' | b'_' | b'.')
        })
}

fn valid_message(value: &str, maximum: usize) -> bool {
    !value.is_empty()
        && value.len() <= maximum
        && !value
            .chars()
            .any(|character| character.is_control() && character != '\t')
}

#[derive(Clone, Copy)]
struct TrustedFilePolicy {
    owner_uid: u32,
    owner_gid: u32,
    mode: u32,
}

fn read_bounded_path(
    path: &Path,
    maximum: u64,
    policy: TrustedFilePolicy,
) -> Result<Vec<u8>, CommandError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| {
        CommandError::new(
            "bootstrap_release_unavailable",
            "La version d'installation locale est indisponible.",
        )
    })?;
    if metadata.file_type().is_symlink()
        || !metadata.file_type().is_file()
        || metadata.len() == 0
        || metadata.len() > maximum
    {
        return Err(CommandError::new(
            "bootstrap_release_unsafe",
            "La version d'installation locale n'est pas sûre.",
        ));
    }
    #[cfg(unix)]
    if metadata.uid() != policy.owner_uid
        || metadata.gid() != policy.owner_gid
        || metadata.nlink() != 1
        || metadata.permissions().mode() & 0o7777 != policy.mode
    {
        return Err(CommandError::new(
            "bootstrap_release_unsafe",
            "La version d'installation locale n'est pas sûre.",
        ));
    }
    #[cfg(unix)]
    let descriptor = open(
        path,
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW,
        Mode::empty(),
    )
    .map_err(|_| {
        CommandError::new(
            "bootstrap_release_unavailable",
            "La version d'installation locale est indisponible.",
        )
    })?;
    #[cfg(unix)]
    let mut file = File::from(descriptor);
    #[cfg(not(unix))]
    let mut file = File::open(path).map_err(|_| {
        CommandError::new(
            "bootstrap_release_unavailable",
            "La version d'installation locale est indisponible.",
        )
    })?;
    let opened = file.metadata().map_err(|_| {
        CommandError::new(
            "bootstrap_release_unavailable",
            "La version d'installation locale est indisponible.",
        )
    })?;
    #[cfg(unix)]
    if opened.dev() != metadata.dev()
        || opened.ino() != metadata.ino()
        || opened.len() != metadata.len()
        || opened.mtime() != metadata.mtime()
        || opened.mtime_nsec() != metadata.mtime_nsec()
        || opened.ctime() != metadata.ctime()
        || opened.ctime_nsec() != metadata.ctime_nsec()
        || opened.uid() != policy.owner_uid
        || opened.gid() != policy.owner_gid
        || opened.nlink() != 1
        || opened.permissions().mode() & 0o7777 != policy.mode
    {
        return Err(CommandError::new(
            "bootstrap_release_changed",
            "La version d'installation locale a changé.",
        ));
    }
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    Read::by_ref(&mut file)
        .take(maximum + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| {
            CommandError::new(
                "bootstrap_release_unavailable",
                "La version d'installation locale est illisible.",
            )
        })?;
    if bytes.is_empty() || bytes.len() as u64 > maximum {
        return Err(CommandError::new(
            "bootstrap_release_unsafe",
            "La version d'installation locale n'est pas sûre.",
        ));
    }
    let after = file.metadata().map_err(|_| {
        CommandError::new(
            "bootstrap_release_unavailable",
            "La version d'installation locale est indisponible.",
        )
    })?;
    #[cfg(unix)]
    if after.dev() != opened.dev()
        || after.ino() != opened.ino()
        || after.len() != opened.len()
        || after.mtime() != opened.mtime()
        || after.mtime_nsec() != opened.mtime_nsec()
        || after.ctime() != opened.ctime()
        || after.ctime_nsec() != opened.ctime_nsec()
        || after.uid() != policy.owner_uid
        || after.gid() != policy.owner_gid
        || after.nlink() != 1
        || after.permissions().mode() & 0o7777 != policy.mode
        || bytes.len() as u64 != opened.len()
    {
        return Err(CommandError::new(
            "bootstrap_release_changed",
            "La version d'installation locale a changé.",
        ));
    }
    Ok(bytes)
}

fn sha256_hex(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn load_release_projection_at(
    manifest_path: &Path,
    helper_path: &Path,
    owner_uid: u32,
    owner_gid: u32,
) -> Result<(ReleaseIdentity, ReleaseManifestProjection), CommandError> {
    let manifest_bytes = read_bounded_path(
        manifest_path,
        MAX_MANIFEST_BYTES,
        TrustedFilePolicy {
            owner_uid,
            owner_gid,
            mode: 0o444,
        },
    )?;
    let manifest_sha256 = sha256_hex(&manifest_bytes);
    let manifest: ReleaseManifestProjection =
        serde_json::from_slice(&manifest_bytes).map_err(|_| {
            CommandError::new(
                "bootstrap_manifest_invalid",
                "Le manifeste d'installation local est invalide.",
            )
        })?;
    let helper_bytes = read_bounded_path(
        helper_path,
        MAX_HELPER_BYTES,
        TrustedFilePolicy {
            owner_uid,
            owner_gid,
            mode: 0o555,
        },
    )?;
    let helper_sha256 = sha256_hex(&helper_bytes);
    let bootstrap = manifest.bootstrap.as_ref().ok_or_else(|| {
        CommandError::new(
            "bootstrap_manifest_invalid",
            "Le manifeste d'installation local est invalide.",
        )
    })?;
    if !valid_identifier(&manifest.release_id, 128)
        || manifest.trust_anchor.version_directory != TRUST_ANCHOR_ROOT
        || manifest.trust_anchor.manifest_path != RELEASE_MANIFEST
        || manifest.trust_anchor.bootstrap_helper_path != BOOTSTRAP_HELPER
        || manifest.trust_anchor.launcher_path != REVIEWED_ATLAS_LAUNCHER
        || manifest.trust_anchor.manifest_mode != "0444"
        || manifest.trust_anchor.bootstrap_helper_mode != "0555"
        || manifest.trust_anchor.launcher_mode != "0555"
        || !is_sha256(&manifest.trust_anchor.launcher_sha256)
        || manifest.trust_anchor.enrollment != "physical-polkit-tofu-v1"
        || !is_sha256(&manifest.source.tree_sha256)
        || !is_sha256(&manifest.artifacts.atlas_rpm.sha256)
        || !is_sha256(&manifest.artifacts.hermes_source_archive.sha256)
        || !is_sha256(&bootstrap.helper_sha256)
        || helper_sha256 != bootstrap.helper_sha256
        || bootstrap.gui_protocol.public_operations
            != [
                "gui-preflight",
                "gui-apply",
                "gui-corrective-preflight",
                "gui-corrective-apply",
                "gui-recovery-preflight",
                "gui-recovery-apply",
            ]
        || bootstrap.gui_protocol.launcher_mode != "sealed-rpm-payload-memfd"
        || bootstrap.gui_protocol.atlas_payload_member != INSTALLED_ATLAS
        || !is_sha256(&bootstrap.gui_protocol.atlas_payload_sha256)
        || manifest.artifacts.atlas_rpm.payload_executable_path != INSTALLED_ATLAS
        || !is_sha256(&manifest.artifacts.atlas_rpm.payload_executable_sha256)
        || manifest.artifacts.atlas_rpm.payload_executable_sha256
            != bootstrap.gui_protocol.atlas_payload_sha256
    {
        return Err(CommandError::new(
            "bootstrap_manifest_invalid",
            "Le manifeste d'installation local est invalide.",
        ));
    }
    // The complete manifest is validated again by the elevated, sealed helper
    // before any mutation; this projection only renders its public identity.
    let confirmation_digest_suffix = sha256_hex(
        format!(
            "{}:{}:{}:{}",
            helper_sha256,
            manifest_sha256,
            manifest.source.tree_sha256,
            manifest.artifacts.hermes_source_archive.sha256,
        )
        .as_bytes(),
    )[..12]
        .to_owned();
    let identity = ReleaseIdentity {
        release_id: manifest.release_id.clone(),
        helper_sha256,
        manifest_sha256,
        source_sha256: manifest.source.tree_sha256.clone(),
        rpm_sha256: manifest.artifacts.atlas_rpm.sha256.clone(),
        hermes_source_archive_sha256: manifest.artifacts.hermes_source_archive.sha256.clone(),
        confirmation_digest_suffix,
    };
    Ok((identity, manifest))
}

fn load_release_identity_at(
    manifest_path: &Path,
    helper_path: &Path,
    owner_uid: u32,
    owner_gid: u32,
) -> Result<ReleaseIdentity, CommandError> {
    load_release_projection_at(manifest_path, helper_path, owner_uid, owner_gid)
        .map(|(identity, _)| identity)
}

#[cfg(target_os = "linux")]
fn validate_current_atlas_launcher(
    manifest: &ReleaseManifestProjection,
) -> Result<(), CommandError> {
    let executable_link = fs::read_link("/proc/self/exe").map_err(|_| {
        CommandError::new(
            "bootstrap_launcher_invalid",
            "L'exécutable Atlas exact ne peut pas être authentifié.",
        )
    })?;
    let executable_name = executable_link.to_str().ok_or_else(|| {
        CommandError::new(
            "bootstrap_launcher_invalid",
            "L'exécutable Atlas exact ne peut pas être authentifié.",
        )
    })?;
    if executable_name != INSTALLED_ATLAS && executable_name != REVIEWED_ATLAS_MEMFD {
        return Err(CommandError::new(
            "bootstrap_launcher_invalid",
            "Atlas doit être lancé depuis le payload scellé du RPM revu.",
        ));
    }
    let executable = File::open("/proc/self/exe").map_err(|_| {
        CommandError::new(
            "bootstrap_launcher_invalid",
            "L'exécutable Atlas exact ne peut pas être authentifié.",
        )
    })?;
    let metadata = executable.metadata().map_err(|_| {
        CommandError::new(
            "bootstrap_launcher_invalid",
            "L'exécutable Atlas exact ne peut pas être authentifié.",
        )
    })?;
    if !metadata.file_type().is_file()
        || metadata.len() == 0
        || metadata.len() > MAX_ATLAS_EXECUTABLE_BYTES
        || metadata.permissions().mode() & 0o022 != 0
        || metadata.permissions().mode() & 0o111 == 0
        || (executable_name == REVIEWED_ATLAS_MEMFD
            && (metadata.permissions().mode() & 0o777 != 0o500
                || metadata.nlink() != 0
                || fcntl_get_seals(&executable).ok() != Some(required_memfd_seals())))
    {
        return Err(CommandError::new(
            "bootstrap_launcher_invalid",
            "L'exécutable Atlas exact ne peut pas être authentifié.",
        ));
    }
    let mut bytes = Vec::with_capacity(metadata.len() as usize);
    executable
        .take(MAX_ATLAS_EXECUTABLE_BYTES + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| {
            CommandError::new(
                "bootstrap_launcher_invalid",
                "L'exécutable Atlas exact ne peut pas être authentifié.",
            )
        })?;
    if bytes.len() as u64 != metadata.len()
        || sha256_hex(&bytes) != manifest.artifacts.atlas_rpm.payload_executable_sha256
    {
        return Err(CommandError::new(
            "bootstrap_launcher_invalid",
            "L'exécutable Atlas ne correspond pas au RPM revu.",
        ));
    }
    Ok(())
}

#[cfg(not(target_os = "linux"))]
fn validate_current_atlas_launcher(
    _manifest: &ReleaseManifestProjection,
) -> Result<(), CommandError> {
    Err(CommandError::new(
        "bootstrap_launcher_invalid",
        "Le lanceur Atlas scellé nécessite Linux.",
    ))
}

fn load_release_identity() -> Result<ReleaseIdentity, CommandError> {
    let (identity, manifest) = load_release_projection_at(
        Path::new(RELEASE_MANIFEST),
        Path::new(BOOTSTRAP_HELPER),
        0,
        0,
    )?;
    validate_current_atlas_launcher(&manifest)?;
    Ok(identity)
}

fn load_corrective_identity_at(
    manifest_path: &Path,
    helper_path: &Path,
    owner_uid: u32,
    owner_gid: u32,
) -> Result<OperationIdentity, CommandError> {
    let (mut release, manifest) =
        load_release_projection_at(manifest_path, helper_path, owner_uid, owner_gid)?;
    let corrective = manifest
        .bootstrap
        .and_then(|bootstrap| bootstrap.corrective)
        .ok_or_else(|| {
            CommandError::new(
                "bootstrap_corrective_unavailable",
                "Cette release ne contient pas de réparation corrective vérifiée.",
            )
        })?;
    if release.release_id != CORRECTIVE_TARGET_RELEASE_ID
        || corrective.mode != "single-terminal-rollback-repair"
        || corrective.max_attempts != 1
        || corrective.consumed_marker != CORRECTIVE_CONSUMED_MARKER
        || corrective.state_root != CORRECTIVE_STATE_ROOT
        || corrective.history_root != "/var/lib/ops-control-plane-bootstrap-history"
        || corrective.recovery_helper != "/usr/local/libexec/ops-control-plane-bootstrap-corrective"
        || corrective.recovery_unit != "ops-control-plane-bootstrap-corrective-recovery.service"
        || corrective.recovery_path_unit != "ops-control-plane-bootstrap-corrective-recovery.path"
        || corrective.authority_document != "AGENTS.md"
        || corrective.recovery_from_release_id != CORRECTIVE_PREDECESSOR_RELEASE_ID
        || corrective.recovery_from_source_tree_sha256 != CORRECTIVE_PREDECESSOR_SOURCE_SHA256
        || corrective.recovery_from_atlas_rpm_sha256 != CORRECTIVE_PREDECESSOR_RPM_SHA256
        || corrective.recovery_from_bootstrap_helper_sha256 != CORRECTIVE_PREDECESSOR_HELPER_SHA256
        || corrective.recovery_from_manifest_sha256 != CORRECTIVE_PREDECESSOR_MANIFEST_SHA256
    {
        return Err(CommandError::new(
            "bootstrap_corrective_manifest_invalid",
            "La déclaration de réparation corrective est invalide.",
        ));
    }
    release.confirmation_digest_suffix = sha256_hex(
        format!(
            "corrective-v1:{}:{}:{}:{}:{}:{}:{}:{}:{}:{}",
            corrective.recovery_from_release_id,
            corrective.recovery_from_source_tree_sha256,
            corrective.recovery_from_atlas_rpm_sha256,
            corrective.recovery_from_bootstrap_helper_sha256,
            corrective.recovery_from_manifest_sha256,
            release.release_id,
            release.helper_sha256,
            release.manifest_sha256,
            release.source_sha256,
            release.hermes_source_archive_sha256,
        )
        .as_bytes(),
    )[..12]
        .to_owned();
    Ok(OperationIdentity {
        mode: OperationMode::Corrective,
        release,
        recovery_from_release_id: Some(corrective.recovery_from_release_id),
        rollback_finalize_resume: false,
    })
}

fn load_corrective_identity() -> Result<OperationIdentity, CommandError> {
    let identity = load_corrective_identity_at(
        Path::new(RELEASE_MANIFEST),
        Path::new(BOOTSTRAP_HELPER),
        0,
        0,
    )?;
    let (release, manifest) = load_release_projection_at(
        Path::new(RELEASE_MANIFEST),
        Path::new(BOOTSTRAP_HELPER),
        0,
        0,
    )?;
    if !identity.release.same_artifacts(&release) {
        return Err(CommandError::new(
            "bootstrap_release_changed",
            "La version d'installation locale a changé.",
        ));
    }
    validate_current_atlas_launcher(&manifest)?;
    Ok(identity)
}

fn fresh_operation_identity(identity: ReleaseIdentity) -> OperationIdentity {
    OperationIdentity {
        mode: OperationMode::Fresh,
        release: identity,
        recovery_from_release_id: None,
        rollback_finalize_resume: false,
    }
}

fn recovery_operation_identity(identity: ReleaseIdentity) -> OperationIdentity {
    OperationIdentity {
        mode: OperationMode::Recovery,
        release: identity,
        recovery_from_release_id: None,
        rollback_finalize_resume: false,
    }
}

#[cfg(unix)]
fn same_metadata(left: &fs::Metadata, right: &fs::Metadata) -> bool {
    left.dev() == right.dev()
        && left.ino() == right.ino()
        && left.uid() == right.uid()
        && left.gid() == right.gid()
        && left.mode() == right.mode()
        && left.nlink() == right.nlink()
        && left.len() == right.len()
        && left.mtime() == right.mtime()
        && left.mtime_nsec() == right.mtime_nsec()
        && left.ctime() == right.ctime()
        && left.ctime_nsec() == right.ctime_nsec()
}

#[cfg(unix)]
fn exact_regular_metadata(
    metadata: &fs::Metadata,
    owner_uid: u32,
    owner_gid: u32,
    mode: u32,
    maximum: u64,
) -> bool {
    metadata.file_type().is_file()
        && !metadata.file_type().is_symlink()
        && metadata.uid() == owner_uid
        && metadata.gid() == owner_gid
        && metadata.mode() & 0o7777 == mode
        && metadata.nlink() == 1
        && metadata.len() > 0
        && metadata.len() <= maximum
}

#[cfg(unix)]
fn read_exact_public_file_with<F>(
    path: &Path,
    maximum: u64,
    owner_uid: u32,
    owner_gid: u32,
    mode: u32,
    after_open: F,
) -> Result<Vec<u8>, CommandError>
where
    F: FnOnce(),
{
    let before = fs::symlink_metadata(path).map_err(|_| {
        CommandError::new(
            "bootstrap_state_invalid",
            "La preuve publique d'installation est indisponible.",
        )
    })?;
    if !exact_regular_metadata(&before, owner_uid, owner_gid, mode, maximum) {
        return Err(CommandError::new(
            "bootstrap_state_invalid",
            "La preuve publique d'installation n'est pas sûre.",
        ));
    }
    let descriptor = open(
        path,
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW | OFlags::NONBLOCK,
        Mode::empty(),
    )
    .map_err(|_| {
        CommandError::new(
            "bootstrap_state_invalid",
            "La preuve publique d'installation est indisponible.",
        )
    })?;
    let mut file = File::from(descriptor);
    let opened = file.metadata().map_err(|_| {
        CommandError::new(
            "bootstrap_state_invalid",
            "La preuve publique d'installation est indisponible.",
        )
    })?;
    if !same_metadata(&before, &opened) {
        return Err(CommandError::new(
            "bootstrap_state_changed",
            "La preuve publique d'installation a changé.",
        ));
    }
    after_open();
    let mut bytes = Vec::with_capacity(opened.len() as usize);
    (&mut file)
        .take(maximum + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| {
            CommandError::new(
                "bootstrap_state_invalid",
                "La preuve publique d'installation est illisible.",
            )
        })?;
    let after_fd = file.metadata().map_err(|_| {
        CommandError::new(
            "bootstrap_state_changed",
            "La preuve publique d'installation a changé.",
        )
    })?;
    let after_path = fs::symlink_metadata(path).map_err(|_| {
        CommandError::new(
            "bootstrap_state_changed",
            "La preuve publique d'installation a changé.",
        )
    })?;
    if bytes.is_empty()
        || bytes.len() as u64 > maximum
        || bytes.len() as u64 != opened.len()
        || !same_metadata(&opened, &after_fd)
        || !same_metadata(&after_fd, &after_path)
        || !exact_regular_metadata(&after_path, owner_uid, owner_gid, mode, maximum)
    {
        return Err(CommandError::new(
            "bootstrap_state_changed",
            "La preuve publique d'installation a changé.",
        ));
    }
    Ok(bytes)
}

#[cfg(unix)]
fn read_public_installed_proof_with<F>(
    path: &Path,
    owner_uid: u32,
    owner_gid: u32,
    after_open: F,
) -> Result<PublicInstalledProof, CommandError>
where
    F: FnOnce(),
{
    let bytes = read_exact_public_file_with(
        path,
        MAX_PUBLIC_PROOF_BYTES,
        owner_uid,
        owner_gid,
        0o644,
        after_open,
    )?;
    let proof: PublicInstalledProof = serde_json::from_slice(&bytes).map_err(|_| {
        CommandError::new(
            "bootstrap_state_invalid",
            "La preuve publique d'installation est invalide.",
        )
    })?;
    let mut canonical = serde_json::to_vec(&proof).map_err(|_| {
        CommandError::new(
            "bootstrap_state_invalid",
            "La preuve publique d'installation est invalide.",
        )
    })?;
    canonical.push(b'\n');
    if canonical != bytes
        || proof.schema_version != 1
        || proof.status != "installed"
        || !valid_identifier(&proof.release_id, 128)
        || !is_sha256(&proof.bootstrap_helper_sha256)
        || !is_sha256(&proof.manifest_sha256)
        || !is_sha256(&proof.source_tree_sha256)
        || !is_sha256(&proof.atlas_rpm_sha256)
        || !is_sha256(&proof.hermes_source_archive_sha256)
        || proof.completed_at == 0
    {
        return Err(CommandError::new(
            "bootstrap_state_invalid",
            "La preuve publique d'installation est invalide.",
        ));
    }
    Ok(proof)
}

#[cfg(unix)]
fn checked_public_proof(path: &Path, owner_uid: u32, owner_gid: u32) -> ProofPresence {
    match fs::symlink_metadata(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => ProofPresence::Absent,
        Err(_) => ProofPresence::Invalid,
        Ok(_) => match read_public_installed_proof_with(path, owner_uid, owner_gid, || {}) {
            Ok(proof) => ProofPresence::Valid(proof),
            Err(_) => ProofPresence::Invalid,
        },
    }
}

#[cfg(unix)]
fn checked_exact_regular_presence(
    path: &Path,
    owner_uid: u32,
    owner_gid: u32,
    mode: u32,
    maximum: u64,
) -> CheckedPresence {
    let before = match fs::symlink_metadata(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return CheckedPresence::Absent;
        }
        Err(_) => return CheckedPresence::Invalid,
        Ok(metadata) => metadata,
    };
    let after = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(_) => return CheckedPresence::Invalid,
    };
    if exact_regular_metadata(&before, owner_uid, owner_gid, mode, maximum)
        && same_metadata(&before, &after)
    {
        CheckedPresence::Valid
    } else {
        CheckedPresence::Invalid
    }
}

#[cfg(unix)]
fn checked_state_root(path: &Path, owner_uid: u32, owner_gid: u32) -> CheckedPresence {
    let before = match fs::symlink_metadata(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return CheckedPresence::Absent;
        }
        Err(_) => return CheckedPresence::Invalid,
        Ok(metadata) => metadata,
    };
    let after = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(_) => return CheckedPresence::Invalid,
    };
    if before.file_type().is_dir()
        && !before.file_type().is_symlink()
        && before.uid() == owner_uid
        && before.gid() == owner_gid
        && before.mode() & 0o7777 == 0o700
        && same_metadata(&before, &after)
    {
        CheckedPresence::Valid
    } else {
        CheckedPresence::Invalid
    }
}

#[cfg(unix)]
fn checked_installed_atlas(path: &Path, owner_uid: u32, owner_gid: u32) -> CheckedPresence {
    let before = match fs::symlink_metadata(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return CheckedPresence::Absent;
        }
        Err(_) => return CheckedPresence::Invalid,
        Ok(metadata) => metadata,
    };
    if !before.file_type().is_file()
        || before.file_type().is_symlink()
        || before.uid() != owner_uid
        || before.gid() != owner_gid
        || before.nlink() != 1
        || before.len() == 0
        || before.mode() & 0o022 != 0
        || before.mode() & 0o7000 != 0
        || before.mode() & 0o100 == 0
    {
        return CheckedPresence::Invalid;
    }
    let descriptor = match open(
        path,
        OFlags::RDONLY | OFlags::CLOEXEC | OFlags::NOFOLLOW | OFlags::NONBLOCK,
        Mode::empty(),
    ) {
        Ok(descriptor) => descriptor,
        Err(_) => return CheckedPresence::Invalid,
    };
    let file = File::from(descriptor);
    let opened = match file.metadata() {
        Ok(metadata) => metadata,
        Err(_) => return CheckedPresence::Invalid,
    };
    let after = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(_) => return CheckedPresence::Invalid,
    };
    if same_metadata(&before, &opened) && same_metadata(&opened, &after) {
        CheckedPresence::Valid
    } else {
        CheckedPresence::Invalid
    }
}

impl PublicInstalledProof {
    fn release_identity(&self) -> ReleaseIdentity {
        let confirmation_digest_suffix = sha256_hex(
            format!(
                "{}:{}:{}:{}",
                self.bootstrap_helper_sha256,
                self.manifest_sha256,
                self.source_tree_sha256,
                self.hermes_source_archive_sha256,
            )
            .as_bytes(),
        )[..12]
            .to_owned();
        ReleaseIdentity {
            release_id: self.release_id.clone(),
            helper_sha256: self.bootstrap_helper_sha256.clone(),
            manifest_sha256: self.manifest_sha256.clone(),
            source_sha256: self.source_tree_sha256.clone(),
            rpm_sha256: self.atlas_rpm_sha256.clone(),
            hermes_source_archive_sha256: self.hermes_source_archive_sha256.clone(),
            confirmation_digest_suffix,
        }
    }
}

#[cfg(unix)]
fn assess_bootstrap_disk_state(
    paths: BootstrapPaths<'_>,
    owner_uid: u32,
    owner_gid: u32,
) -> DiskBootstrapState {
    // The immutable public proof is intentionally inspected before either
    // mutable source path. A completed installation remains usable after the
    // checkout is moved or updated.
    let proof = checked_public_proof(paths.public_proof, owner_uid, owner_gid);
    let consumed = checked_exact_regular_presence(
        paths.consumed,
        owner_uid,
        owner_gid,
        0o600,
        MAX_CONSUMED_MARKER_BYTES,
    );
    let state_root = checked_state_root(paths.state_root, owner_uid, owner_gid);
    let corrective_consumed = checked_exact_regular_presence(
        paths.corrective_consumed,
        owner_uid,
        owner_gid,
        0o600,
        MAX_CONSUMED_MARKER_BYTES,
    );
    let corrective_state_root =
        checked_state_root(paths.corrective_state_root, owner_uid, owner_gid);
    let atlas = checked_installed_atlas(paths.atlas, owner_uid, owner_gid);

    match (
        &proof,
        &consumed,
        &state_root,
        &atlas,
        &corrective_consumed,
        &corrective_state_root,
    ) {
        (
            ProofPresence::Valid(proof),
            CheckedPresence::Valid,
            CheckedPresence::Valid,
            CheckedPresence::Valid,
            CheckedPresence::Absent,
            CheckedPresence::Absent,
        )
        | (
            ProofPresence::Valid(proof),
            CheckedPresence::Valid,
            CheckedPresence::Valid,
            CheckedPresence::Valid,
            CheckedPresence::Valid,
            CheckedPresence::Valid,
        ) => DiskBootstrapState::Installed(proof.release_identity()),
        (
            ProofPresence::Absent,
            CheckedPresence::Absent,
            CheckedPresence::Absent,
            CheckedPresence::Absent,
            CheckedPresence::Absent,
            CheckedPresence::Absent,
        ) => DiskBootstrapState::Fresh,
        (
            ProofPresence::Absent,
            CheckedPresence::Valid,
            CheckedPresence::Absent | CheckedPresence::Valid,
            CheckedPresence::Absent,
            CheckedPresence::Absent,
            CheckedPresence::Absent,
        ) => DiskBootstrapState::RecoveryRequired {
            corrective_eligible: true,
            corrective_started: false,
        },
        (
            ProofPresence::Absent,
            consumed,
            state_root,
            atlas,
            CheckedPresence::Valid,
            corrective_state_root,
        ) if (matches!(consumed, CheckedPresence::Valid)
            || matches!(consumed, CheckedPresence::Absent))
            && (matches!(state_root, CheckedPresence::Valid)
                || matches!(state_root, CheckedPresence::Absent))
            && matches!(
                corrective_state_root,
                CheckedPresence::Absent | CheckedPresence::Valid
            )
            && matches!(atlas, CheckedPresence::Absent | CheckedPresence::Valid) =>
        {
            DiskBootstrapState::RecoveryRequired {
                corrective_eligible: true,
                corrective_started: true,
            }
        }
        _ => DiskBootstrapState::RecoveryRequired {
            corrective_eligible: false,
            corrective_started: false,
        },
    }
}

#[cfg(not(unix))]
fn assess_bootstrap_disk_state(
    _paths: BootstrapPaths<'_>,
    _owner_uid: u32,
    _owner_gid: u32,
) -> DiskBootstrapState {
    DiskBootstrapState::RecoveryRequired {
        corrective_eligible: false,
        corrective_started: false,
    }
}

fn empty_release_identity() -> ReleaseIdentity {
    ReleaseIdentity {
        release_id: String::new(),
        helper_sha256: String::new(),
        manifest_sha256: String::new(),
        source_sha256: String::new(),
        rpm_sha256: String::new(),
        hermes_source_archive_sha256: String::new(),
        confirmation_digest_suffix: String::new(),
    }
}

fn status_from_paths(
    manager: &BootstrapManager,
    paths: BootstrapPaths<'_>,
    owner_uid: u32,
    owner_gid: u32,
) -> Result<BootstrapStatus, CommandError> {
    let disk_state = assess_bootstrap_disk_state(paths, owner_uid, owner_gid);
    let guard = manager.inner.lock().map_err(|_| {
        CommandError::new(
            "bootstrap_state_failed",
            "L'état local de l'installation est indisponible.",
        )
    })?;
    let (
        identity,
        required,
        state,
        zulip_url,
        atlas_available,
        preflight_passed,
        corrective_available,
        operation_mode,
        recovery_from_release_id,
        resume_available,
        rollback_finalize_resume,
    ) = match disk_state {
        DiskBootstrapState::Installed(identity) => (
            identity,
            false,
            "installed".to_owned(),
            Some(ZULIP_URL.to_owned()),
            true,
            false,
            false,
            None,
            None,
            false,
            false,
        ),
        DiskBootstrapState::RecoveryRequired {
            corrective_eligible,
            corrective_started,
        } => {
            let corrective = corrective_eligible
                .then(|| {
                    load_corrective_identity_at(paths.manifest, paths.helper, owner_uid, owner_gid)
                })
                .transpose()
                .ok()
                .flatten();
            let fresh =
                load_release_identity_at(paths.manifest, paths.helper, owner_uid, owner_gid)
                    .ok()
                    .map(fresh_operation_identity);
            let attested = guard.preflight_attestation.as_ref().filter(|attested| {
                guard.lifecycle == Lifecycle::PreflightPassed
                    && match (guard.operation, attested.mode) {
                        (Some(Operation::CorrectivePreflight), OperationMode::Corrective) => {
                            corrective
                                .as_ref()
                                .is_some_and(|current| attested.same_reviewed_operation(current))
                        }
                        (Some(Operation::FreshPreflight), OperationMode::Fresh) => fresh
                            .as_ref()
                            .is_some_and(|current| attested.same_reviewed_operation(current)),
                        (Some(Operation::RecoveryPreflight), OperationMode::Fresh) => fresh
                            .as_ref()
                            .is_some_and(|current| attested.same_reviewed_operation(current)),
                        (Some(Operation::RecoveryPreflight), OperationMode::Corrective) => {
                            corrective
                                .as_ref()
                                .is_some_and(|current| attested.same_reviewed_operation(current))
                        }
                        _ => false,
                    }
            });
            let preflight_passed = attested.is_some();
            let recovery_from_release_id = attested
                .and_then(|identity| identity.recovery_from_release_id.clone())
                .or_else(|| {
                    corrective
                        .as_ref()
                        .and_then(|identity| identity.recovery_from_release_id.clone())
                });
            let identity = attested
                .map(|identity| identity.release.clone())
                .or_else(|| corrective.as_ref().map(|identity| identity.release.clone()))
                .unwrap_or_else(empty_release_identity);
            let state = if matches!(
                guard.operation,
                Some(
                    Operation::CorrectivePreflight
                        | Operation::CorrectiveApply
                        | Operation::FreshPreflight
                        | Operation::FreshApply
                        | Operation::RecoveryPreflight
                        | Operation::RecoveryApply
                )
            ) && matches!(
                guard.lifecycle,
                Lifecycle::PreflightRunning | Lifecycle::ApplyRunning
            ) {
                guard.lifecycle.as_str().to_owned()
            } else {
                "recovery-required".to_owned()
            };
            (
                identity,
                true,
                state,
                None,
                false,
                preflight_passed,
                corrective.is_some(),
                Some(
                    attested
                        .map(|identity| identity.mode)
                        .unwrap_or(OperationMode::Corrective)
                        .as_str()
                        .to_owned(),
                ),
                recovery_from_release_id,
                corrective_started && corrective.is_some(),
                attested.is_some_and(|identity| identity.rollback_finalize_resume),
            )
        }
        DiskBootstrapState::Fresh => {
            let identity =
                load_release_identity_at(paths.manifest, paths.helper, owner_uid, owner_gid)?;
            let operation_identity = fresh_operation_identity(identity.clone());
            let preflight_passed = guard
                .preflight_attestation
                .as_ref()
                .is_some_and(|attested| attested.same_reviewed_operation(&operation_identity))
                && guard.operation == Some(Operation::FreshPreflight)
                && guard.lifecycle == Lifecycle::PreflightPassed;
            (
                identity,
                true,
                guard.lifecycle.as_str().to_owned(),
                None,
                false,
                preflight_passed,
                false,
                Some(OperationMode::Fresh.as_str().to_owned()),
                None,
                false,
                false,
            )
        }
    };
    Ok(BootstrapStatus {
        required,
        state,
        release_id: identity.release_id,
        bootstrap_helper_sha256: identity.helper_sha256,
        source_tree_sha256: identity.source_sha256,
        manifest_sha256: identity.manifest_sha256,
        rpm_sha256: identity.rpm_sha256,
        hermes_source_archive_sha256: identity.hermes_source_archive_sha256,
        confirmation_digest_suffix: identity.confirmation_digest_suffix,
        zulip_url,
        atlas_available,
        preflight_passed,
        resume_available,
        rollback_finalize_resume,
        cancellation_allowed: matches!(
            guard.lifecycle,
            Lifecycle::PreflightRunning | Lifecycle::ApplyRunning
        ) && !guard.mutation_started,
        corrective_available,
        operation_mode,
        recovery_from_release_id,
    })
}

fn status_from(manager: &BootstrapManager) -> Result<BootstrapStatus, CommandError> {
    status_from_paths(manager, BootstrapPaths::system(), 0, 0)
}

pub fn get_bootstrap_status(manager: &BootstrapManager) -> Result<BootstrapStatus, CommandError> {
    status_from(manager)
}

fn set_failed(manager: &BootstrapManager, code: &'static str, message: &'static str) {
    if let Ok(mut guard) = manager.inner.lock() {
        guard.lifecycle = Lifecycle::Failed;
        guard.preflight_attestation = None;
        guard.control = None;
        guard.result = BootstrapResult {
            status: "failed".to_owned(),
            phase: guard.result.phase.clone(),
            message: Some(message.to_owned()),
            percent: guard.result.percent,
            error_code: Some(code.to_owned()),
            zulip_url: None,
            atlas_available: false,
            cancellation_allowed: false,
        };
    }
}

fn emit_progress(app: &AppHandle, progress: &BootstrapProgress) {
    let _ = app.emit("bootstrap-progress", progress);
}

fn required_preflight(operation: Operation) -> Option<Operation> {
    match operation {
        Operation::FreshApply => Some(Operation::FreshPreflight),
        Operation::CorrectiveApply => Some(Operation::CorrectivePreflight),
        Operation::RecoveryApply => Some(Operation::RecoveryPreflight),
        Operation::FreshPreflight
        | Operation::CorrectivePreflight
        | Operation::RecoveryPreflight => None,
    }
}

fn apply_has_matching_preflight(
    state: &ManagerState,
    operation: Operation,
    identity: &OperationIdentity,
) -> bool {
    operation.is_apply()
        && state.lifecycle == Lifecycle::PreflightPassed
        && state.operation == required_preflight(operation)
        && state.preflight_attestation.as_ref() == Some(identity)
}

fn start_operation(
    manager: &BootstrapManager,
    operation: Operation,
) -> Result<OperationIdentity, CommandError> {
    let disk_state = assess_bootstrap_disk_state(BootstrapPaths::system(), 0, 0);
    match (operation.mode(), disk_state) {
        (OperationMode::Fresh, DiskBootstrapState::Fresh) => {}
        (
            OperationMode::Corrective,
            DiskBootstrapState::RecoveryRequired {
                corrective_eligible: true,
                ..
            },
        ) => {}
        (
            OperationMode::Recovery,
            DiskBootstrapState::RecoveryRequired {
                corrective_eligible: true,
                ..
            },
        ) => {}
        (_, DiskBootstrapState::Installed(_)) => {
            return Err(CommandError::new(
                "bootstrap_already_installed",
                "L'installation locale est déjà terminée.",
            ));
        }
        (
            OperationMode::Corrective | OperationMode::Recovery,
            DiskBootstrapState::RecoveryRequired {
                corrective_eligible: false,
                ..
            },
        ) => {
            return Err(CommandError::new(
                "bootstrap_corrective_recovery_pending",
                "La réparation corrective a déjà commencé ou l'état local exige une reprise système.",
            ));
        }
        (OperationMode::Corrective | OperationMode::Recovery, DiskBootstrapState::Fresh) => {
            return Err(CommandError::new(
                "bootstrap_corrective_not_required",
                "Aucune transaction annulée n'est éligible à la réparation corrective.",
            ));
        }
        (OperationMode::Fresh, DiskBootstrapState::RecoveryRequired { .. }) => {
            return Err(CommandError::new(
                "bootstrap_recovery_required",
                "Une reprise racine de l'installation est requise.",
            ));
        }
    }
    let mut identity = match operation.mode() {
        OperationMode::Fresh => fresh_operation_identity(load_release_identity()?),
        OperationMode::Corrective => load_corrective_identity()?,
        OperationMode::Recovery => recovery_operation_identity(load_release_identity()?),
    };
    let mut guard = manager.inner.lock().map_err(|_| {
        CommandError::new(
            "bootstrap_state_failed",
            "L'état local de l'installation est indisponible.",
        )
    })?;
    if matches!(
        guard.lifecycle,
        Lifecycle::PreflightRunning | Lifecycle::ApplyRunning
    ) {
        return Err(CommandError::new(
            "bootstrap_busy",
            "Une opération d'installation est déjà en cours.",
        ));
    }
    if operation.is_apply() {
        if operation == Operation::RecoveryApply {
            if let Some(attested) = guard.preflight_attestation.as_ref() {
                let current = match attested.mode {
                    OperationMode::Fresh => {
                        Some(fresh_operation_identity(load_release_identity()?))
                    }
                    OperationMode::Corrective => Some(load_corrective_identity()?),
                    OperationMode::Recovery => None,
                };
                if current
                    .as_ref()
                    .is_some_and(|current| attested.same_reviewed_operation(current))
                {
                    identity = attested.clone();
                }
            }
        } else if let Some(attested) = guard.preflight_attestation.as_ref() {
            if attested.same_reviewed_operation(&identity) {
                identity.rollback_finalize_resume = attested.rollback_finalize_resume;
            }
        }
        if !apply_has_matching_preflight(&guard, operation, &identity) {
            return Err(CommandError::new(
                "bootstrap_preflight_required",
                "Le préflight graphique correspondant doit réussir juste avant l'installation.",
            ));
        }
    }
    guard.lifecycle = if operation.is_preflight() {
        Lifecycle::PreflightRunning
    } else {
        Lifecycle::ApplyRunning
    };
    if operation.is_preflight() {
        guard.preflight_attestation = None;
    }
    guard.operation = Some(operation);
    guard.mutation_started = false;
    guard.cancel_requested = false;
    guard.control = None;
    guard.result = BootstrapResult {
        status: "running".to_owned(),
        phase: Some(if operation.is_preflight() {
            if operation.mode() == OperationMode::Corrective {
                "corrective-preflight-starting".to_owned()
            } else {
                "preflight-starting".to_owned()
            }
        } else {
            "authentication-starting".to_owned()
        }),
        message: Some(if operation.is_preflight() {
            if operation.mode() == OperationMode::Corrective {
                "Vérification corrective locale en cours…".to_owned()
            } else {
                "Vérification locale en cours…".to_owned()
            }
        } else {
            "Authentification système en cours…".to_owned()
        }),
        percent: Some(0),
        error_code: None,
        zulip_url: None,
        atlas_available: path_exists(INSTALLED_ATLAS),
        cancellation_allowed: true,
    };
    Ok(identity)
}

#[cfg(target_os = "linux")]
struct SealedHelper {
    file: File,
}

#[cfg(target_os = "linux")]
fn required_memfd_seals() -> SealFlags {
    SealFlags::WRITE | SealFlags::GROW | SealFlags::SHRINK | SealFlags::SEAL
}

#[cfg(target_os = "linux")]
fn snapshot_and_seal_helper(
    path: &str,
    expected_sha256: &str,
) -> Result<SealedHelper, CommandError> {
    snapshot_and_seal_helper_at(
        path,
        expected_sha256,
        TrustedFilePolicy {
            owner_uid: 0,
            owner_gid: 0,
            mode: 0o555,
        },
    )
}

#[cfg(target_os = "linux")]
fn snapshot_and_seal_helper_at(
    path: &str,
    expected_sha256: &str,
    policy: TrustedFilePolicy,
) -> Result<SealedHelper, CommandError> {
    let bytes = read_bounded_path(Path::new(path), MAX_HELPER_BYTES, policy)?;
    if !is_sha256(expected_sha256) || sha256_hex(&bytes) != expected_sha256 {
        return Err(CommandError::new(
            "bootstrap_release_changed",
            "Le helper d'installation a changé depuis la vérification.",
        ));
    }

    let descriptor = memfd_create(
        "atlas-bootstrap-helper",
        MemfdFlags::CLOEXEC | MemfdFlags::ALLOW_SEALING,
    )
    .map_err(|_| {
        CommandError::new(
            "bootstrap_sealed_helper_failed",
            "La copie mémoire sécurisée du helper est indisponible.",
        )
    })?;
    let mut file = File::from(descriptor);
    file.set_permissions(fs::Permissions::from_mode(0o500))
        .and_then(|()| file.write_all(&bytes))
        .and_then(|()| file.flush())
        .and_then(|()| file.seek(SeekFrom::Start(0)).map(|_| ()))
        .map_err(|_| {
            CommandError::new(
                "bootstrap_sealed_helper_failed",
                "La copie mémoire sécurisée du helper a échoué.",
            )
        })?;
    let required_seals = required_memfd_seals();
    if fcntl_add_seals(&file, required_seals).is_err()
        || fcntl_get_seals(&file).ok() != Some(required_seals)
    {
        return Err(CommandError::new(
            "bootstrap_sealed_helper_failed",
            "Le helper en mémoire n'a pas pu être rendu immuable.",
        ));
    }
    let metadata = file.metadata().map_err(|_| {
        CommandError::new(
            "bootstrap_sealed_helper_failed",
            "L'identité du helper en mémoire est indisponible.",
        )
    })?;
    if metadata.len() != bytes.len() as u64 || metadata.permissions().mode() & 0o7777 != 0o500 {
        return Err(CommandError::new(
            "bootstrap_sealed_helper_failed",
            "L'identité du helper en mémoire est invalide.",
        ));
    }
    Ok(SealedHelper { file })
}

#[cfg(target_os = "linux")]
fn helper_command(operation: Operation, helper: &SealedHelper) -> Result<Command, CommandError> {
    helper_command_at(operation, helper, SOURCE_ROOT)
}

#[cfg(target_os = "linux")]
fn helper_command_at(
    operation: Operation,
    helper: &SealedHelper,
    current_directory: &str,
) -> Result<Command, CommandError> {
    let mut command = Command::new("/usr/bin/python3");
    let inherited_helper = helper.file.try_clone().map_err(|_| {
        CommandError::new(
            "bootstrap_sealed_helper_failed",
            "Le descripteur du helper en mémoire est indisponible.",
        )
    })?;
    command
        .arg("-I")
        .arg(format!("/proc/self/fd/{SEALED_HELPER_FD}"))
        .arg(operation.argument())
        .current_dir(current_directory)
        .env_clear()
        .env("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
        .env("LANG", "C.UTF-8")
        .env("LC_ALL", "C.UTF-8")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::from(inherited_helper));
    security::add_bounded_session_environment(&mut command);
    // Mapping the memfd through `Stdio` makes fd 2 inheritable only in the
    // child, atomically inside the standard library's spawn path. The helper
    // replaces stderr with /dev/null immediately after validating and reading
    // its own sealed bytes. No globally inheritable descriptor window exists.
    command.process_group(0);
    Ok(command)
}

fn terminate_child_group(child: &mut Child) {
    #[cfg(target_os = "linux")]
    if let Ok(raw_pid) = i32::try_from(child.id()) {
        if let Some(pid) = Pid::from_raw(raw_pid) {
            let _ = kill_process_group(pid, Signal::KILL);
        }
    }
    let _ = child.kill();
}

fn append_u32_frame(output: &mut Vec<u8>, value: &[u8]) -> Result<(), ()> {
    let length = u32::try_from(value.len()).map_err(|_| ())?;
    output.extend_from_slice(&length.to_be_bytes());
    output.extend_from_slice(value);
    Ok(())
}

fn build_apply_frame(
    openbao: &str,
    zulip: &str,
    operation_mode: OperationMode,
    confirmed_release: &str,
    confirmed_suffix: &str,
) -> Result<Zeroizing<Vec<u8>>, ()> {
    let metadata = ApplyMetadata {
        schema_version: 1,
        operation_mode: operation_mode.as_str(),
        confirmed_release_id: confirmed_release,
        confirmed_digest_suffix: confirmed_suffix,
    };
    let metadata_bytes = serde_json::to_vec(&metadata).map_err(|_| ())?;
    if metadata_bytes.len() > 1024 {
        return Err(());
    }
    let mut output = Zeroizing::new(Vec::with_capacity(
        11 + metadata_bytes.len() + openbao.len() + zulip.len() + 12,
    ));
    output.extend_from_slice(b"ATLASBOOT1\n");
    append_u32_frame(&mut output, &metadata_bytes)?;
    append_u32_frame(&mut output, openbao.as_bytes())?;
    append_u32_frame(&mut output, zulip.as_bytes())?;
    Ok(output)
}

#[cfg(test)]
fn bounded_line<R: BufRead>(reader: &mut R) -> std::io::Result<Option<Vec<u8>>> {
    let mut line = Vec::new();
    loop {
        let available = reader.fill_buf()?;
        if available.is_empty() {
            return if line.is_empty() {
                Ok(None)
            } else {
                Ok(Some(line))
            };
        }
        let newline = available.iter().position(|byte| *byte == b'\n');
        let take = newline.map_or(available.len(), |position| position + 1);
        if line.len().saturating_add(take) > MAX_EVENT_BYTES {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "bootstrap event exceeds the bounded protocol",
            ));
        }
        line.extend_from_slice(&available[..take]);
        reader.consume(take);
        if newline.is_some() {
            while line
                .last()
                .is_some_and(|byte| matches!(byte, b'\r' | b'\n'))
            {
                line.pop();
            }
            return Ok(Some(line));
        }
    }
}

#[cfg(target_os = "linux")]
fn bounded_child_line(
    reader: &mut ChildStdout,
    deadline: Instant,
) -> std::io::Result<Option<Vec<u8>>> {
    let mut line = Vec::new();
    loop {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .ok_or_else(|| {
                std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    "bootstrap event deadline expired",
                )
            })?;
        let timeout = Timespec::try_from(remaining).map_err(|_| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "bootstrap event deadline is invalid",
            )
        })?;
        let ready = loop {
            let mut descriptors = [PollFd::new(
                &*reader,
                PollFlags::IN | PollFlags::HUP | PollFlags::ERR,
            )];
            match poll(&mut descriptors, Some(&timeout)) {
                Ok(0) => break None,
                Ok(_) => break Some(descriptors[0].revents()),
                Err(rustix::io::Errno::INTR) => continue,
                Err(_) => {
                    return Err(std::io::Error::other("bootstrap event polling failed"));
                }
            }
        }
        .ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                "bootstrap event silence deadline expired",
            )
        })?;
        if ready.contains(PollFlags::ERR) && !ready.intersects(PollFlags::IN | PollFlags::HUP) {
            return Err(std::io::Error::other("bootstrap event pipe failed"));
        }
        let mut byte = [0_u8; 1];
        let count = loop {
            match reader.read(&mut byte) {
                Ok(count) => break count,
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(error) => return Err(error),
            }
        };
        if count == 0 {
            return if line.is_empty() {
                Ok(None)
            } else {
                Ok(Some(line))
            };
        }
        if byte[0] == b'\n' {
            while line.last().is_some_and(|value| *value == b'\r') {
                line.pop();
            }
            return Ok(Some(line));
        }
        if line.len() == MAX_EVENT_BYTES {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidData,
                "bootstrap event exceeds the bounded protocol",
            ));
        }
        line.push(byte[0]);
    }
}

fn close_control_channel(manager: &BootstrapManager) {
    if let Ok(mut guard) = manager.inner.lock() {
        guard.control = None;
    }
}

fn wait_child_bounded(child: &mut Child, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return status.success(),
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(20));
            }
            Ok(None) | Err(_) => break,
        }
    }
    terminate_child_group(child);
    let kill_deadline = Instant::now() + Duration::from_secs(5);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return false,
            Ok(None) if Instant::now() < kill_deadline => {
                std::thread::sleep(Duration::from_millis(20));
            }
            Ok(None) | Err(_) => return false,
        }
    }
}

fn validated_helper_event(line: &[u8]) -> Result<HelperEvent, ()> {
    let event: HelperEvent = serde_json::from_slice(line).map_err(|_| ())?;
    if event.schema_version != 1
        || !matches!(event.event.as_str(), "progress" | "result")
        || !valid_identifier(&event.status, 64)
        || event.percent.is_some_and(|percent| percent > 100)
        || event
            .phase
            .as_deref()
            .is_some_and(|phase| !valid_identifier(phase, 128))
        || event
            .error_code
            .as_deref()
            .is_some_and(|code| !valid_identifier(code, 128))
        || event
            .message
            .as_deref()
            .is_some_and(|message| !valid_message(message, 512))
        || event
            .operation_mode
            .as_deref()
            .is_some_and(|mode| !matches!(mode, "fresh" | "corrective" | "recovery"))
        || event
            .recovery_from_release_id
            .as_deref()
            .is_some_and(|release| !valid_identifier(release, 128))
        || (event.event == "progress"
            && matches!(
                event.status.as_str(),
                "succeeded" | "success" | "installed" | "failed" | "cancelled" | "canceled"
            ))
    {
        return Err(());
    }
    Ok(event)
}

fn event_has_identity(event: &HelperEvent) -> bool {
    event.release_id.is_some()
        || event.bootstrap_helper_sha256.is_some()
        || event.manifest_sha256.is_some()
        || event.source_tree_sha256.is_some()
        || event.atlas_rpm_sha256.is_some()
        || event.hermes_source_archive_sha256.is_some()
        || event.confirmation_digest_suffix.is_some()
        || event.rollback_finalize_resume.is_some()
}

fn event_matches_identity(event: &HelperEvent, identity: &OperationIdentity) -> bool {
    event.release_id.as_deref() == Some(identity.release.release_id.as_str())
        && event.bootstrap_helper_sha256.as_deref() == Some(identity.release.helper_sha256.as_str())
        && event.manifest_sha256.as_deref() == Some(identity.release.manifest_sha256.as_str())
        && event.source_tree_sha256.as_deref() == Some(identity.release.source_sha256.as_str())
        && event.atlas_rpm_sha256.as_deref() == Some(identity.release.rpm_sha256.as_str())
        && event.hermes_source_archive_sha256.as_deref()
            == Some(identity.release.hermes_source_archive_sha256.as_str())
        && event.confirmation_digest_suffix.as_deref()
            == Some(identity.release.confirmation_digest_suffix.as_str())
        && event.operation_mode.as_deref() == Some(identity.mode.as_str())
        && event.recovery_from_release_id.as_deref() == identity.recovery_from_release_id.as_deref()
        && event.rollback_finalize_resume == Some(identity.rollback_finalize_resume)
}

fn event_operation_context_matches(event: &HelperEvent, identity: &OperationIdentity) -> bool {
    event.operation_mode.as_deref() == Some(identity.mode.as_str())
        && event.recovery_from_release_id.as_deref() == identity.recovery_from_release_id.as_deref()
}

fn event_context_is_valid(event: &HelperEvent, identity: &OperationIdentity) -> bool {
    if !event_has_identity(event) {
        return (event.operation_mode.is_none() && event.recovery_from_release_id.is_none())
            || event_operation_context_matches(event, identity);
    }
    event_matches_identity(event, identity)
}

fn event_mentions_secret_readiness(event: &HelperEvent) -> bool {
    event.phase.as_deref() == Some(SECRET_READINESS_PHASE)
        || event.status == SECRET_READINESS_STATUS
}

fn exact_secret_readiness(event: &HelperEvent, identity: &OperationIdentity) -> bool {
    !identity.rollback_finalize_resume
        && event.event == "progress"
        && event.phase.as_deref() == Some(SECRET_READINESS_PHASE)
        && event.status == SECRET_READINESS_STATUS
        && event.message.as_deref() == Some(SECRET_READINESS_MESSAGE)
        && event.percent == Some(11)
        && event.error_code.is_none()
        && event.mutation_started == Some(false)
        && event.cancellation_allowed == Some(true)
        && event.zulip_url.is_none()
        && event.atlas_available.is_none()
        && event_matches_identity(event, identity)
}

fn event_mentions_rollback_finalize_readiness(event: &HelperEvent) -> bool {
    event.phase.as_deref() == Some(ROLLBACK_FINALIZE_READINESS_PHASE)
        || event.status == ROLLBACK_FINALIZE_READINESS_STATUS
}

fn exact_rollback_finalize_readiness(event: &HelperEvent, identity: &OperationIdentity) -> bool {
    identity.rollback_finalize_resume
        && event.event == "progress"
        && event.phase.as_deref() == Some(ROLLBACK_FINALIZE_READINESS_PHASE)
        && event.status == ROLLBACK_FINALIZE_READINESS_STATUS
        && event.message.as_deref() == Some(ROLLBACK_FINALIZE_READINESS_MESSAGE)
        && event.percent == Some(11)
        && event.error_code.is_none()
        && event.mutation_started == Some(false)
        && event.cancellation_allowed == Some(true)
        && event.zulip_url.is_none()
        && event.atlas_available.is_none()
        && event_matches_identity(event, identity)
}

fn accept_rollback_finalize_readiness(
    operation: Operation,
    event: &HelperEvent,
    identity: &OperationIdentity,
    already_seen: bool,
) -> Result<bool, ()> {
    if !event_mentions_rollback_finalize_readiness(event) {
        return Ok(false);
    }
    if !operation.is_apply() || already_seen || !exact_rollback_finalize_readiness(event, identity)
    {
        return Err(());
    }
    Ok(true)
}

fn accept_secret_readiness(
    operation: Operation,
    event: &HelperEvent,
    identity: &OperationIdentity,
    already_seen: bool,
) -> Result<bool, ()> {
    if !event_mentions_secret_readiness(event) {
        return Ok(false);
    }
    if !operation.is_apply() || already_seen || !exact_secret_readiness(event, identity) {
        return Err(());
    }
    Ok(true)
}

fn event_mentions_mutation_commit(event: &HelperEvent) -> bool {
    event.phase.as_deref() == Some(MUTATION_COMMIT_PHASE) || event.status == MUTATION_COMMIT_STATUS
}

fn exact_mutation_commit_readiness(event: &HelperEvent, identity: &OperationIdentity) -> bool {
    event.event == "progress"
        && event.phase.as_deref() == Some(MUTATION_COMMIT_PHASE)
        && event.status == MUTATION_COMMIT_STATUS
        && event.message.as_deref() == Some(MUTATION_COMMIT_MESSAGE)
        && event.percent == Some(19)
        && event.error_code.is_none()
        && event.mutation_started == Some(false)
        && event.cancellation_allowed == Some(true)
        && event.zulip_url.is_none()
        && event.atlas_available.is_none()
        && event_matches_identity(event, identity)
}

fn accept_mutation_commit_readiness(
    operation: Operation,
    event: &HelperEvent,
    identity: &OperationIdentity,
    secrets_delivered: bool,
    already_seen: bool,
) -> Result<bool, ()> {
    if !event_mentions_mutation_commit(event) {
        return Ok(false);
    }
    if !operation.is_apply()
        || !secrets_delivered
        || already_seen
        || !exact_mutation_commit_readiness(event, identity)
    {
        return Err(());
    }
    Ok(true)
}

fn write_apply_secrets_after_readiness<W: Write>(
    writer: &mut W,
    event: &HelperEvent,
    identity: &OperationIdentity,
    secrets: &mut Option<(Zeroizing<String>, Zeroizing<String>, String, String)>,
) -> Result<(), ()> {
    if !exact_secret_readiness(event, identity) {
        return Err(());
    }
    let result = secrets.as_ref().ok_or(()).and_then(
        |(openbao, zulip, confirmed_release, confirmed_suffix)| {
            let payload = build_apply_frame(
                openbao,
                zulip,
                identity.mode,
                confirmed_release,
                confirmed_suffix,
            )?;
            writer
                .write_all(&payload)
                .and_then(|()| writer.flush())
                .map_err(|_| ())
        },
    );
    // Whether delivery succeeded or failed, never retain WebView-originated
    // secrets after the single attested write attempt.
    drop(secrets.take());
    result
}

enum SecretDelivery {
    Sent,
    Cancelled,
}

fn deliver_apply_secrets(
    manager: &BootstrapManager,
    event: &HelperEvent,
    identity: &OperationIdentity,
    secrets: &mut Option<(Zeroizing<String>, Zeroizing<String>, String, String)>,
) -> Result<SecretDelivery, ()> {
    let mut guard = manager.inner.lock().map_err(|_| ())?;
    if guard.cancel_requested || guard.mutation_started {
        if let Some(mut control) = guard.control.take() {
            let _ = control
                .write_all(CANCEL_FRAME)
                .and_then(|()| control.flush());
            drop(control);
        }
        drop(secrets.take());
        return Ok(SecretDelivery::Cancelled);
    }
    let mut control = guard.control.take().ok_or(())?;
    write_apply_secrets_after_readiness(&mut control, event, identity, secrets)?;
    guard.control = Some(control);
    Ok(SecretDelivery::Sent)
}

enum MutationCommitDecision {
    Acknowledged,
    Cancelled,
}

fn decide_mutation_commit(manager: &BootstrapManager) -> Result<MutationCommitDecision, ()> {
    let mut guard = manager.inner.lock().map_err(|_| ())?;
    if guard.mutation_started {
        return Err(());
    }
    let control = guard.control.take();
    if guard.cancel_requested {
        if let Some(mut control) = control {
            let _ = control
                .write_all(CANCEL_FRAME)
                .and_then(|()| control.flush());
            drop(control);
        }
        return Ok(MutationCommitDecision::Cancelled);
    }
    let mut control = control.ok_or(())?;
    if control
        .write_all(MUTATION_ACK_FRAME)
        .and_then(|()| control.flush())
        .is_err()
    {
        drop(control);
        return Err(());
    }
    // Keep the lifecycle mutex until ACK is fully written and the pipe is
    // closed. Any concurrent cancel call linearizes strictly before this block
    // (CANCEL) or after it (refused-after-mutation).
    drop(control);
    guard.mutation_started = true;
    guard.result.cancellation_allowed = false;
    Ok(MutationCommitDecision::Acknowledged)
}

fn terminal_identity_is_valid(event: &HelperEvent, identity: &OperationIdentity) -> bool {
    let fields = [
        event.release_id.as_deref(),
        event.bootstrap_helper_sha256.as_deref(),
        event.manifest_sha256.as_deref(),
        event.source_tree_sha256.as_deref(),
        event.atlas_rpm_sha256.as_deref(),
        event.hermes_source_archive_sha256.as_deref(),
        event.confirmation_digest_suffix.as_deref(),
    ];
    (fields.iter().all(|field| field.is_none()) && event_operation_context_matches(event, identity))
        || event_matches_identity(event, identity)
}

fn exact_rollback_finalized(event: &HelperEvent, identity: &OperationIdentity) -> bool {
    identity.rollback_finalize_resume
        && event.event == "result"
        && event.status == "rolled-back"
        && event.phase.as_deref() == Some("rollback-finalized")
        && event.percent == Some(100)
        && event.error_code.is_none()
        && event.zulip_url.is_none()
        && event.atlas_available == Some(false)
        && event_matches_identity(event, identity)
}

fn update_progress(manager: &BootstrapManager, app: &AppHandle, event: &HelperEvent) {
    let phase = event.phase.as_deref().unwrap_or("working").to_owned();
    let message = event
        .message
        .as_deref()
        .unwrap_or("Installation locale en cours…")
        .to_owned();
    let percent = event.percent.unwrap_or(0);
    let mut cancellation_allowed = false;
    if let Ok(mut guard) = manager.inner.lock() {
        if event.mutation_started == Some(true) || event.cancellation_allowed == Some(false) {
            guard.mutation_started = true;
            guard.control = None;
        }
        cancellation_allowed = !guard.mutation_started
            && !guard.cancel_requested
            && event.cancellation_allowed.unwrap_or(true)
            && matches!(
                guard.lifecycle,
                Lifecycle::PreflightRunning | Lifecycle::ApplyRunning
            );
        guard.result.phase = Some(phase.clone());
        guard.result.message = Some(message.clone());
        guard.result.percent = Some(percent);
        guard.result.error_code = event.error_code.clone();
        guard.result.cancellation_allowed = cancellation_allowed;
    }
    emit_progress(
        app,
        &BootstrapProgress {
            phase,
            message,
            percent,
            status: event.status.clone(),
            error_code: event.error_code.clone(),
            cancellation_allowed,
        },
    );
}

fn installed_proof_matches(identity: &OperationIdentity) -> bool {
    matches!(
        assess_bootstrap_disk_state(BootstrapPaths::system(), 0, 0),
        DiskBootstrapState::Installed(installed)
            if installed.same_artifacts(&identity.release)
    )
}

fn finalize_operation(
    manager: &BootstrapManager,
    app: &AppHandle,
    operation: Operation,
    identity: &OperationIdentity,
    terminal_event: Option<HelperEvent>,
    exit_success: bool,
) -> BootstrapPreflightResponse {
    let accepted_event = terminal_event.as_ref().is_some_and(|event| {
        event.event == "result"
            && event_matches_identity(event, identity)
            && if operation.is_preflight() {
                event.status
                    == match operation {
                        Operation::FreshPreflight => "preflight-passed",
                        Operation::CorrectivePreflight => "corrective-preflight-passed",
                        Operation::RecoveryPreflight => "recovery-preflight-passed",
                        Operation::FreshApply
                        | Operation::CorrectiveApply
                        | Operation::RecoveryApply => unreachable!(),
                    }
            } else {
                event.status == "succeeded"
                    && event.zulip_url.as_deref() == Some(ZULIP_URL)
                    && event.atlas_available == Some(true)
            }
    });
    // A helper result is necessary but never sufficient to unlock Atlas. The
    // public proof, all root-owned state markers and the installed executable
    // must form one of the two exact installed matrices after the child exits.
    let accepted = accepted_event
        && exit_success
        && (operation.is_preflight() || installed_proof_matches(identity));
    let rollback_finalized = operation.is_apply()
        && identity.rollback_finalize_resume
        && exit_success
        && terminal_event
            .as_ref()
            .is_some_and(|event| exact_rollback_finalized(event, identity));
    let cancelled = terminal_event.as_ref().is_some_and(|event| {
        event.event == "result"
            && event.status == "cancelled"
            && terminal_identity_is_valid(event, identity)
    });
    let reported_failure = terminal_event.as_ref().is_some_and(|event| {
        event.event == "result"
            && event.status == "failed"
            && terminal_identity_is_valid(event, identity)
    });
    let error_code = terminal_event
        .as_ref()
        .and_then(|event| event.error_code.clone())
        .or_else(|| {
            (!accepted && !cancelled && !reported_failure && !rollback_finalized)
                .then(|| "bootstrap_protocol_failed".to_owned())
        });
    let message = terminal_event
        .as_ref()
        .and_then(|event| event.message.clone())
        .or_else(|| {
            Some(if rollback_finalized {
                "Rollback préproduction finalisé sans installer de composant.".to_owned()
            } else if accepted {
                if operation.is_preflight() {
                    if identity.mode == OperationMode::Corrective {
                        "Réparation corrective vérifiée.".to_owned()
                    } else {
                        "Préflight validé.".to_owned()
                    }
                } else {
                    "Installation terminée.".to_owned()
                }
            } else if cancelled {
                "Opération annulée avant modification.".to_owned()
            } else if reported_failure {
                "L'installation a échoué sans modifier de secret affiché.".to_owned()
            } else {
                "Le protocole d'installation sécurisé est indisponible ou invalide.".to_owned()
            })
        });
    let percent = terminal_event.as_ref().and_then(|event| event.percent);
    let mut lifecycle = Lifecycle::Failed;
    if accepted {
        lifecycle = if operation.is_preflight() {
            Lifecycle::PreflightPassed
        } else {
            Lifecycle::Succeeded
        };
    } else if cancelled {
        lifecycle = Lifecycle::Cancelled;
    } else if rollback_finalized {
        lifecycle = Lifecycle::RollbackFinalized;
    }
    if let Ok(mut guard) = manager.inner.lock() {
        guard.lifecycle = lifecycle;
        guard.control = None;
        if accepted && operation.is_preflight() {
            guard.preflight_attestation = Some(identity.clone());
        }
        if operation.is_apply() && (!accepted || rollback_finalized) {
            // A failed apply requires a new preflight before another attempt.
            guard.preflight_attestation = None;
        }
        guard.result = BootstrapResult {
            status: lifecycle.as_str().to_owned(),
            phase: terminal_event
                .as_ref()
                .and_then(|event| event.phase.clone()),
            message: message.clone(),
            percent,
            error_code: error_code.clone(),
            zulip_url: (accepted && operation.is_apply())
                .then(|| {
                    terminal_event
                        .as_ref()
                        .and_then(|event| event.zulip_url.clone())
                })
                .flatten()
                .or_else(|| (accepted && operation.is_apply()).then(|| ZULIP_URL.to_owned())),
            atlas_available: accepted && operation.is_apply() && installed_proof_matches(identity),
            cancellation_allowed: false,
        };
    }
    emit_progress(
        app,
        &BootstrapProgress {
            phase: terminal_event
                .as_ref()
                .and_then(|event| event.phase.clone())
                .unwrap_or_else(|| "finished".to_owned()),
            message: message.clone().unwrap_or_default(),
            percent: percent.unwrap_or(if accepted { 100 } else { 0 }),
            status: lifecycle.as_str().to_owned(),
            error_code: error_code.clone(),
            cancellation_allowed: false,
        },
    );
    BootstrapPreflightResponse {
        status: if rollback_finalized {
            Lifecycle::RollbackFinalized.as_str()
        } else if accepted {
            "passed"
        } else {
            lifecycle.as_str()
        }
        .to_owned(),
        message,
        error_code,
        rollback_finalize_resume: identity.rollback_finalize_resume,
        operation_mode: identity.mode.as_str().to_owned(),
    }
}

fn run_operation(
    manager: BootstrapManager,
    app: AppHandle,
    operation: Operation,
    mut identity: OperationIdentity,
    mut secrets: Option<(Zeroizing<String>, Zeroizing<String>, String, String)>,
) -> BootstrapPreflightResponse {
    let sealed_helper =
        match snapshot_and_seal_helper(BOOTSTRAP_HELPER, &identity.release.helper_sha256) {
            Ok(helper) => helper,
            Err(_) => {
                set_failed(
                    &manager,
                    "bootstrap_release_changed",
                    "Le helper d'installation a changé depuis la vérification.",
                );
                return BootstrapPreflightResponse {
                    status: "failed".to_owned(),
                    message: Some(
                        "Le helper d'installation a changé depuis la vérification.".to_owned(),
                    ),
                    error_code: Some("bootstrap_release_changed".to_owned()),
                    rollback_finalize_resume: identity.rollback_finalize_resume,
                    operation_mode: identity.mode.as_str().to_owned(),
                };
            }
        };
    let mut command = match helper_command(operation, &sealed_helper) {
        Ok(command) => command,
        Err(_) => {
            set_failed(
                &manager,
                "bootstrap_sealed_helper_failed",
                "Le helper sécurisé en mémoire est indisponible.",
            );
            return BootstrapPreflightResponse {
                status: "failed".to_owned(),
                message: Some("Le helper sécurisé en mémoire est indisponible.".to_owned()),
                error_code: Some("bootstrap_sealed_helper_failed".to_owned()),
                rollback_finalize_resume: identity.rollback_finalize_resume,
                operation_mode: identity.mode.as_str().to_owned(),
            };
        }
    };
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(_) => {
            set_failed(
                &manager,
                "bootstrap_start_failed",
                "Le programme d'installation sécurisé n'a pas démarré.",
            );
            return BootstrapPreflightResponse {
                status: "failed".to_owned(),
                message: Some("Le programme d'installation sécurisé n'a pas démarré.".to_owned()),
                error_code: Some("bootstrap_start_failed".to_owned()),
                rollback_finalize_resume: identity.rollback_finalize_resume,
                operation_mode: identity.mode.as_str().to_owned(),
            };
        }
    };
    // The child now owns fd 2; the parent copy must not outlive spawn.
    drop(command);
    drop(sealed_helper);
    let stdin = match child.stdin.take() {
        Some(stdin) => stdin,
        None => {
            terminate_child_group(&mut child);
            let _ = wait_child_bounded(&mut child, Duration::from_secs(5));
            set_failed(
                &manager,
                "bootstrap_protocol_failed",
                "Le canal sécurisé est indisponible.",
            );
            return BootstrapPreflightResponse {
                status: "failed".to_owned(),
                message: Some("Le canal sécurisé est indisponible.".to_owned()),
                error_code: Some("bootstrap_protocol_failed".to_owned()),
                rollback_finalize_resume: identity.rollback_finalize_resume,
                operation_mode: identity.mode.as_str().to_owned(),
            };
        }
    };
    let stdout = match child.stdout.take() {
        Some(stdout) => stdout,
        None => {
            drop(stdin);
            terminate_child_group(&mut child);
            let _ = wait_child_bounded(&mut child, Duration::from_secs(5));
            set_failed(
                &manager,
                "bootstrap_protocol_failed",
                "Le canal de progression est indisponible.",
            );
            return BootstrapPreflightResponse {
                status: "failed".to_owned(),
                message: Some("Le canal de progression est indisponible.".to_owned()),
                error_code: Some("bootstrap_protocol_failed".to_owned()),
                rollback_finalize_resume: identity.rollback_finalize_resume,
                operation_mode: identity.mode.as_str().to_owned(),
            };
        }
    };
    let control_ready = manager.inner.lock().map(|mut guard| {
        if guard.cancel_requested {
            let mut control = stdin;
            let _ = control
                .write_all(CANCEL_FRAME)
                .and_then(|()| control.flush());
            drop(control);
        } else {
            guard.control = Some(stdin);
        }
    });
    if control_ready.is_err() {
        terminate_child_group(&mut child);
        let _ = wait_child_bounded(&mut child, Duration::from_secs(5));
        set_failed(
            &manager,
            "bootstrap_state_failed",
            "L'état local de l'installation est indisponible.",
        );
        return BootstrapPreflightResponse {
            status: "failed".to_owned(),
            message: Some("L'état local de l'installation est indisponible.".to_owned()),
            error_code: Some("bootstrap_state_failed".to_owned()),
            rollback_finalize_resume: identity.rollback_finalize_resume,
            operation_mode: identity.mode.as_str().to_owned(),
        };
    }
    let mut reader = stdout;
    let mut terminal_event = None;
    let mut protocol_valid = true;
    let mut event_limit_exhausted = true;
    let mut readiness_seen = false;
    let mut delivery_sent = false;
    let mut commit_readiness_seen = false;
    let mut commit_acknowledged = false;
    let mut recovery_classification_frozen = operation.mode() != OperationMode::Recovery;
    let mut rollback_mode_frozen = operation.is_apply();
    let operation_started = Instant::now();
    let secret_readiness_deadline = operation_started + PRE_READINESS_TIMEOUT;
    let total_deadline = operation_started
        + if operation.is_preflight() {
            PREFLIGHT_TOTAL_TIMEOUT
        } else {
            APPLY_TOTAL_TIMEOUT
        };
    for _ in 0..MAX_EVENTS {
        let event_deadline = if operation.is_apply() && !readiness_seen {
            std::cmp::min(total_deadline, secret_readiness_deadline)
        } else {
            let silence_timeout = if readiness_seen {
                POST_READINESS_SILENCE_TIMEOUT
            } else {
                PRE_READINESS_TIMEOUT
            };
            std::cmp::min(total_deadline, Instant::now() + silence_timeout)
        };
        let line = match bounded_child_line(&mut reader, event_deadline) {
            Ok(Some(line)) => line,
            Ok(None) => {
                event_limit_exhausted = false;
                break;
            }
            Err(_) => {
                protocol_valid = false;
                event_limit_exhausted = false;
                break;
            }
        };
        let event = match validated_helper_event(&line) {
            Ok(event) => event,
            Err(()) => {
                protocol_valid = false;
                event_limit_exhausted = false;
                break;
            }
        };
        if !recovery_classification_frozen {
            if event_has_identity(&event) {
                if operation.is_preflight() {
                    let mut classified = match event.operation_mode.as_deref() {
                        Some("fresh") => fresh_operation_identity(identity.release.clone()),
                        Some("corrective") => match load_corrective_identity() {
                            Ok(candidate)
                                if candidate.release.same_artifacts(&identity.release) =>
                            {
                                candidate
                            }
                            _ => {
                                drop(secrets.take());
                                close_control_channel(&manager);
                                protocol_valid = false;
                                event_limit_exhausted = false;
                                break;
                            }
                        },
                        _ => {
                            drop(secrets.take());
                            close_control_channel(&manager);
                            protocol_valid = false;
                            event_limit_exhausted = false;
                            break;
                        }
                    };
                    let Some(rollback_finalize_resume) = event.rollback_finalize_resume else {
                        drop(secrets.take());
                        close_control_channel(&manager);
                        protocol_valid = false;
                        event_limit_exhausted = false;
                        break;
                    };
                    classified.rollback_finalize_resume = rollback_finalize_resume;
                    identity = classified;
                    rollback_mode_frozen = true;
                }
                recovery_classification_frozen = true;
            } else if event.operation_mode.as_deref() != Some("recovery")
                || event.recovery_from_release_id.is_some()
                || event.rollback_finalize_resume.is_some()
            {
                drop(secrets.take());
                close_control_channel(&manager);
                protocol_valid = false;
                event_limit_exhausted = false;
                break;
            }
        }
        if recovery_classification_frozen
            && operation.is_preflight()
            && !rollback_mode_frozen
            && event_has_identity(&event)
        {
            let Some(rollback_finalize_resume) = event.rollback_finalize_resume else {
                drop(secrets.take());
                close_control_channel(&manager);
                protocol_valid = false;
                event_limit_exhausted = false;
                break;
            };
            identity.rollback_finalize_resume = rollback_finalize_resume;
            rollback_mode_frozen = true;
        }
        let context_valid =
            if operation.mode() == OperationMode::Recovery && !recovery_classification_frozen {
                event.operation_mode.as_deref() == Some("recovery")
                    && event.recovery_from_release_id.is_none()
                    && !event_has_identity(&event)
            } else {
                event_context_is_valid(&event, &identity)
            };
        if !context_valid {
            drop(secrets.take());
            close_control_channel(&manager);
            protocol_valid = false;
            event_limit_exhausted = false;
            break;
        }
        if event.event == "progress" {
            let readiness = if identity.rollback_finalize_resume {
                if event_mentions_secret_readiness(&event) {
                    Err(())
                } else {
                    accept_rollback_finalize_readiness(operation, &event, &identity, readiness_seen)
                }
            } else if event_mentions_rollback_finalize_readiness(&event) {
                Err(())
            } else {
                accept_secret_readiness(operation, &event, &identity, readiness_seen)
            };
            match readiness {
                Err(()) => {
                    drop(secrets.take());
                    close_control_channel(&manager);
                    protocol_valid = false;
                    event_limit_exhausted = false;
                    break;
                }
                Ok(true) => {
                    readiness_seen = true;
                    if identity.rollback_finalize_resume {
                        if secrets.is_some() {
                            drop(secrets.take());
                            close_control_channel(&manager);
                            protocol_valid = false;
                            event_limit_exhausted = false;
                            break;
                        }
                        // The root helper attested the exact rollback-only
                        // frontier. No credential frame is written in this
                        // mode; readiness itself unlocks only the later ACK.
                        delivery_sent = true;
                    } else {
                        match deliver_apply_secrets(&manager, &event, &identity, &mut secrets) {
                            Ok(SecretDelivery::Sent) => delivery_sent = true,
                            Ok(SecretDelivery::Cancelled) => {}
                            Err(()) => {
                                drop(secrets.take());
                                close_control_channel(&manager);
                                protocol_valid = false;
                                event_limit_exhausted = false;
                                break;
                            }
                        }
                    }
                }
                Ok(false) => {
                    if operation.is_apply()
                        && !readiness_seen
                        && (event.mutation_started == Some(true)
                            || event.cancellation_allowed == Some(false))
                    {
                        drop(secrets.take());
                        close_control_channel(&manager);
                        protocol_valid = false;
                        event_limit_exhausted = false;
                        break;
                    }
                }
            }
            match accept_mutation_commit_readiness(
                operation,
                &event,
                &identity,
                delivery_sent,
                commit_readiness_seen,
            ) {
                Err(()) => {
                    close_control_channel(&manager);
                    protocol_valid = false;
                    event_limit_exhausted = false;
                    break;
                }
                Ok(true) => {
                    commit_readiness_seen = true;
                    match decide_mutation_commit(&manager) {
                        Ok(MutationCommitDecision::Acknowledged) => {
                            commit_acknowledged = true;
                        }
                        Ok(MutationCommitDecision::Cancelled) => {}
                        Err(()) => {
                            close_control_channel(&manager);
                            protocol_valid = false;
                            event_limit_exhausted = false;
                            break;
                        }
                    }
                }
                Ok(false) => {
                    if event.mutation_started == Some(true) && !commit_acknowledged {
                        close_control_channel(&manager);
                        protocol_valid = false;
                        event_limit_exhausted = false;
                        break;
                    }
                }
            }
            update_progress(&manager, &app, &event);
        } else {
            if event_mentions_secret_readiness(&event)
                || event_mentions_rollback_finalize_readiness(&event)
                || event_mentions_mutation_commit(&event)
            {
                drop(secrets.take());
                close_control_channel(&manager);
                protocol_valid = false;
                event_limit_exhausted = false;
                break;
            }
            terminal_event = Some(event);
            event_limit_exhausted = false;
            break;
        }
    }
    if event_limit_exhausted {
        protocol_valid = false;
    }
    if operation.is_apply()
        && (!delivery_sent || !commit_acknowledged)
        && terminal_event
            .as_ref()
            .is_some_and(|event| matches!(event.status.as_str(), "succeeded" | "rolled-back"))
    {
        protocol_valid = false;
    }
    // Missing, malformed or duplicated readiness always leaves any retained
    // credentials in Zeroizing storage and drops them without a pipe write.
    drop(secrets);
    drop(reader);
    close_control_channel(&manager);
    if !protocol_valid {
        // Closing both anonymous pipe ends is the reliable cancellation signal
        // even if pkexec has already created a root descendant.
        terminate_child_group(&mut child);
        terminal_event = None;
    }
    let exit_success = wait_child_bounded(&mut child, CHILD_EXIT_TIMEOUT);
    finalize_operation(
        &manager,
        &app,
        operation,
        &identity,
        terminal_event,
        exit_success,
    )
}

pub async fn run_bootstrap_preflight(
    manager: BootstrapManager,
    app: AppHandle,
) -> Result<BootstrapPreflightResponse, CommandError> {
    run_preflight(manager, app, Operation::FreshPreflight).await
}

pub async fn run_bootstrap_corrective_preflight(
    manager: BootstrapManager,
    app: AppHandle,
) -> Result<BootstrapPreflightResponse, CommandError> {
    run_preflight(manager, app, Operation::CorrectivePreflight).await
}

pub async fn run_bootstrap_recovery_preflight(
    manager: BootstrapManager,
    app: AppHandle,
) -> Result<BootstrapPreflightResponse, CommandError> {
    run_preflight(manager, app, Operation::RecoveryPreflight).await
}

async fn run_preflight(
    manager: BootstrapManager,
    app: AppHandle,
    operation: Operation,
) -> Result<BootstrapPreflightResponse, CommandError> {
    debug_assert!(operation.is_preflight());
    let identity = start_operation(&manager, operation)?;
    tauri::async_runtime::spawn_blocking(move || {
        run_operation(manager, app, operation, identity, None)
    })
    .await
    .map_err(|_| {
        CommandError::new(
            "bootstrap_worker_failed",
            "Le préflight graphique s'est interrompu.",
        )
    })
}

fn valid_bootstrap_password(value: &str) -> bool {
    (14..=100).contains(&value.chars().count())
        && value == value.trim()
        && !value.chars().any(char::is_control)
}

pub fn begin_bootstrap(
    manager: BootstrapManager,
    app: AppHandle,
    openbao_password: Option<String>,
    zulip_password: Option<String>,
    confirmed_release_id: String,
    confirmed_digest_suffix: String,
) -> Result<BootstrapStartResponse, CommandError> {
    begin_bootstrap_operation(
        manager,
        app,
        Operation::FreshApply,
        openbao_password,
        zulip_password,
        confirmed_release_id,
        confirmed_digest_suffix,
    )
}

pub fn begin_corrective_bootstrap(
    manager: BootstrapManager,
    app: AppHandle,
    openbao_password: Option<String>,
    zulip_password: Option<String>,
    confirmed_release_id: String,
    confirmed_digest_suffix: String,
) -> Result<BootstrapStartResponse, CommandError> {
    begin_bootstrap_operation(
        manager,
        app,
        Operation::CorrectiveApply,
        openbao_password,
        zulip_password,
        confirmed_release_id,
        confirmed_digest_suffix,
    )
}

pub fn begin_recovery_bootstrap(
    manager: BootstrapManager,
    app: AppHandle,
    openbao_password: Option<String>,
    zulip_password: Option<String>,
    confirmed_release_id: String,
    confirmed_digest_suffix: String,
) -> Result<BootstrapStartResponse, CommandError> {
    begin_bootstrap_operation(
        manager,
        app,
        Operation::RecoveryApply,
        openbao_password,
        zulip_password,
        confirmed_release_id,
        confirmed_digest_suffix,
    )
}

fn begin_bootstrap_operation(
    manager: BootstrapManager,
    app: AppHandle,
    operation: Operation,
    openbao_password: Option<String>,
    zulip_password: Option<String>,
    confirmed_release_id: String,
    confirmed_digest_suffix: String,
) -> Result<BootstrapStartResponse, CommandError> {
    debug_assert!(operation.is_apply());
    let openbao = openbao_password.map(Zeroizing::new);
    let zulip = zulip_password.map(Zeroizing::new);
    let identity = start_operation(&manager, operation)?;
    let secrets = if identity.rollback_finalize_resume {
        if openbao.is_some() || zulip.is_some() {
            set_failed(
                &manager,
                "bootstrap_secret_unexpected",
                "La finalisation du rollback refuse toute donnée de mot de passe.",
            );
            return Err(CommandError::new(
                "bootstrap_secret_unexpected",
                "La finalisation du rollback refuse toute donnée de mot de passe.",
            ));
        }
        None
    } else {
        let (Some(openbao), Some(zulip)) = (openbao, zulip) else {
            set_failed(
                &manager,
                "bootstrap_secret_invalid",
                "Les deux mots de passe sont requis pour cette installation.",
            );
            return Err(CommandError::new(
                "bootstrap_secret_invalid",
                "Les deux mots de passe sont requis pour cette installation.",
            ));
        };
        if !valid_bootstrap_password(&openbao)
            || !valid_bootstrap_password(&zulip)
            || openbao.as_bytes() == zulip.as_bytes()
        {
            set_failed(
                &manager,
                "bootstrap_secret_invalid",
                "Les mots de passe fournis ne respectent pas la politique locale.",
            );
            return Err(CommandError::new(
                "bootstrap_secret_invalid",
                "Les deux mots de passe doivent être différents et contenir 14 à 100 caractères valides.",
            ));
        }
        Some((
            openbao,
            zulip,
            confirmed_release_id.clone(),
            confirmed_digest_suffix.clone(),
        ))
    };
    if confirmed_release_id != identity.release.release_id
        || confirmed_digest_suffix != identity.release.confirmation_digest_suffix
    {
        set_failed(
            &manager,
            "bootstrap_confirmation_rejected",
            "La confirmation explicite ne correspond pas à la version vérifiée.",
        );
        return Err(CommandError::new(
            "bootstrap_confirmation_rejected",
            "La confirmation explicite ne correspond pas à la version vérifiée.",
        ));
    }
    let worker_manager = manager.clone();
    std::thread::Builder::new()
        .name(if operation.mode() == OperationMode::Corrective {
            "atlas-bootstrap-corrective".to_owned()
        } else {
            "atlas-bootstrap".to_owned()
        })
        .spawn(move || {
            let _ = run_operation(worker_manager, app, operation, identity, secrets);
        })
        .map_err(|_| {
            set_failed(
                &manager,
                "bootstrap_worker_failed",
                "Le worker d'installation n'a pas démarré.",
            );
            CommandError::new(
                "bootstrap_worker_failed",
                "Le worker d'installation n'a pas démarré.",
            )
        })?;
    Ok(BootstrapStartResponse {
        status: "started".to_owned(),
    })
}

pub fn get_bootstrap_result(manager: &BootstrapManager) -> Result<BootstrapResult, CommandError> {
    manager
        .inner
        .lock()
        .map(|guard| guard.result.clone())
        .map_err(|_| {
            CommandError::new(
                "bootstrap_state_failed",
                "L'état local de l'installation est indisponible.",
            )
        })
}

pub fn cancel_bootstrap(
    manager: &BootstrapManager,
) -> Result<BootstrapCancelResponse, CommandError> {
    let mut guard = manager.inner.lock().map_err(|_| {
        CommandError::new(
            "bootstrap_state_failed",
            "L'état local de l'installation est indisponible.",
        )
    })?;
    if !matches!(
        guard.lifecycle,
        Lifecycle::PreflightRunning | Lifecycle::ApplyRunning
    ) {
        return Ok(BootstrapCancelResponse {
            status: "not-running".to_owned(),
        });
    }
    if guard.mutation_started {
        return Ok(BootstrapCancelResponse {
            status: "refused-after-mutation".to_owned(),
        });
    }
    guard.cancel_requested = true;
    let Some(mut control) = guard.control.take() else {
        guard.result.message = Some("Annulation demandée avant modification…".to_owned());
        guard.result.cancellation_allowed = false;
        return Ok(BootstrapCancelResponse {
            status: "requested".to_owned(),
        });
    };
    control
        .write_all(CANCEL_FRAME)
        .and_then(|()| control.flush())
        .map_err(|_| {
            CommandError::new(
                "bootstrap_cancel_failed",
                "La demande d'annulation n'a pas été transmise.",
            )
        })?;
    drop(control);
    guard.result.message = Some("Annulation demandée avant modification…".to_owned());
    guard.result.cancellation_allowed = false;
    Ok(BootstrapCancelResponse {
        status: "requested".to_owned(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::io::Cursor;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn test_identity() -> OperationIdentity {
        fresh_operation_identity(ReleaseIdentity {
            release_id: "release-1".to_owned(),
            helper_sha256: "a".repeat(64),
            manifest_sha256: "b".repeat(64),
            source_sha256: "c".repeat(64),
            rpm_sha256: "d".repeat(64),
            hermes_source_archive_sha256: "e".repeat(64),
            confirmation_digest_suffix: "0123456789ab".to_owned(),
        })
    }

    fn readiness_event(identity: &OperationIdentity) -> HelperEvent {
        serde_json::from_value(serde_json::json!({
            "schema_version": 1,
            "event": "progress",
            "status": SECRET_READINESS_STATUS,
            "phase": SECRET_READINESS_PHASE,
            "message": SECRET_READINESS_MESSAGE,
            "percent": 11,
            "error_code": null,
            "mutation_started": false,
            "cancellation_allowed": true,
            "release_id": identity.release.release_id,
            "bootstrap_helper_sha256": identity.release.helper_sha256,
            "manifest_sha256": identity.release.manifest_sha256,
            "source_tree_sha256": identity.release.source_sha256,
            "atlas_rpm_sha256": identity.release.rpm_sha256,
            "hermes_source_archive_sha256": identity.release.hermes_source_archive_sha256,
            "confirmation_digest_suffix": identity.release.confirmation_digest_suffix,
            "operation_mode": identity.mode.as_str(),
            "recovery_from_release_id": identity.recovery_from_release_id,
            "rollback_finalize_resume": identity.rollback_finalize_resume,
            "zulip_url": null,
            "atlas_available": null
        }))
        .expect("closed readiness event")
    }

    fn rollback_finalize_readiness_event(identity: &OperationIdentity) -> HelperEvent {
        serde_json::from_value(serde_json::json!({
            "schema_version": 1,
            "event": "progress",
            "status": ROLLBACK_FINALIZE_READINESS_STATUS,
            "phase": ROLLBACK_FINALIZE_READINESS_PHASE,
            "message": ROLLBACK_FINALIZE_READINESS_MESSAGE,
            "percent": 11,
            "error_code": null,
            "mutation_started": false,
            "cancellation_allowed": true,
            "release_id": identity.release.release_id,
            "bootstrap_helper_sha256": identity.release.helper_sha256,
            "manifest_sha256": identity.release.manifest_sha256,
            "source_tree_sha256": identity.release.source_sha256,
            "atlas_rpm_sha256": identity.release.rpm_sha256,
            "hermes_source_archive_sha256": identity.release.hermes_source_archive_sha256,
            "confirmation_digest_suffix": identity.release.confirmation_digest_suffix,
            "operation_mode": identity.mode.as_str(),
            "recovery_from_release_id": identity.recovery_from_release_id,
            "rollback_finalize_resume": identity.rollback_finalize_resume,
            "zulip_url": null,
            "atlas_available": null
        }))
        .expect("closed rollback finalizer readiness event")
    }

    fn rollback_finalized_event(identity: &OperationIdentity) -> HelperEvent {
        serde_json::from_value(serde_json::json!({
            "schema_version": 1,
            "event": "result",
            "status": "rolled-back",
            "phase": "rollback-finalized",
            "message": "Rollback préproduction finalisé; aucune installation lancée.",
            "percent": 100,
            "error_code": null,
            "mutation_started": true,
            "cancellation_allowed": false,
            "release_id": identity.release.release_id,
            "bootstrap_helper_sha256": identity.release.helper_sha256,
            "manifest_sha256": identity.release.manifest_sha256,
            "source_tree_sha256": identity.release.source_sha256,
            "atlas_rpm_sha256": identity.release.rpm_sha256,
            "hermes_source_archive_sha256": identity.release.hermes_source_archive_sha256,
            "confirmation_digest_suffix": identity.release.confirmation_digest_suffix,
            "operation_mode": identity.mode.as_str(),
            "recovery_from_release_id": identity.recovery_from_release_id,
            "rollback_finalize_resume": identity.rollback_finalize_resume,
            "zulip_url": null,
            "atlas_available": false
        }))
        .expect("closed rollback finalizer result")
    }

    fn mutation_commit_event(identity: &OperationIdentity) -> HelperEvent {
        serde_json::from_value(serde_json::json!({
            "schema_version": 1,
            "event": "progress",
            "status": MUTATION_COMMIT_STATUS,
            "phase": MUTATION_COMMIT_PHASE,
            "message": MUTATION_COMMIT_MESSAGE,
            "percent": 19,
            "error_code": null,
            "mutation_started": false,
            "cancellation_allowed": true,
            "release_id": identity.release.release_id,
            "bootstrap_helper_sha256": identity.release.helper_sha256,
            "manifest_sha256": identity.release.manifest_sha256,
            "source_tree_sha256": identity.release.source_sha256,
            "atlas_rpm_sha256": identity.release.rpm_sha256,
            "hermes_source_archive_sha256": identity.release.hermes_source_archive_sha256,
            "confirmation_digest_suffix": identity.release.confirmation_digest_suffix,
            "operation_mode": identity.mode.as_str(),
            "recovery_from_release_id": identity.recovery_from_release_id,
            "rollback_finalize_resume": identity.rollback_finalize_resume,
            "zulip_url": null,
            "atlas_available": null
        }))
        .expect("closed mutation commit event")
    }

    fn unique_test_directory(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("system clock")
            .as_nanos();
        std::env::temp_dir().join(format!("atlas-{name}-{}-{nonce}", std::process::id()))
    }

    struct TestBootstrapLayout {
        root: PathBuf,
        helper: PathBuf,
        manifest: PathBuf,
        consumed: PathBuf,
        public_proof: PathBuf,
        state_root: PathBuf,
        corrective_consumed: PathBuf,
        corrective_state_root: PathBuf,
        atlas: PathBuf,
    }

    impl TestBootstrapLayout {
        fn new(name: &str) -> Self {
            let root = unique_test_directory(name);
            fs::create_dir(&root).expect("test root");
            fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).expect("test root mode");
            Self {
                helper: root.join("control-plane-bootstrap"),
                manifest: root.join("release-manifest.json"),
                consumed: root.join("consumed.json"),
                public_proof: root.join("installed.json"),
                state_root: root.join("state"),
                corrective_consumed: root.join("corrective-consumed.json"),
                corrective_state_root: root.join("corrective-state"),
                atlas: root.join("ops-model-manager"),
                root,
            }
        }

        fn paths(&self) -> BootstrapPaths<'_> {
            BootstrapPaths {
                helper: &self.helper,
                manifest: &self.manifest,
                consumed: &self.consumed,
                public_proof: &self.public_proof,
                state_root: &self.state_root,
                corrective_consumed: &self.corrective_consumed,
                corrective_state_root: &self.corrective_state_root,
                atlas: &self.atlas,
            }
        }

        fn owner(&self) -> (u32, u32) {
            let metadata = fs::metadata(&self.root).expect("test owner");
            (metadata.uid(), metadata.gid())
        }

        fn write_fresh_release(&self) {
            fs::write(&self.helper, b"#!/usr/bin/python3 -I\n").expect("test bootstrap helper");
            fs::set_permissions(&self.helper, fs::Permissions::from_mode(0o555))
                .expect("helper mode");
            let helper_sha256 = sha256_hex(b"#!/usr/bin/python3 -I\n");
            assert!(is_sha256(&helper_sha256));
            let manifest = serde_json::json!({
                "release_id": "release-1",
                "trust_anchor": {
                    "version_directory": TRUST_ANCHOR_ROOT,
                    "manifest_path": RELEASE_MANIFEST,
                    "bootstrap_helper_path": BOOTSTRAP_HELPER,
                    "launcher_path": REVIEWED_ATLAS_LAUNCHER,
                    "manifest_mode": "0444",
                    "bootstrap_helper_mode": "0555",
                    "launcher_mode": "0555",
                    "launcher_sha256": "f".repeat(64),
                    "enrollment": "physical-polkit-tofu-v1"
                },
                "bootstrap": {
                    "helper_sha256": helper_sha256,
                    "gui_protocol": {
                        "public_operations": [
                            "gui-preflight", "gui-apply",
                            "gui-corrective-preflight", "gui-corrective-apply",
                            "gui-recovery-preflight", "gui-recovery-apply"
                        ],
                        "launcher_mode": "sealed-rpm-payload-memfd",
                        "atlas_payload_member": INSTALLED_ATLAS,
                        "atlas_payload_sha256": "e".repeat(64)
                    }
                },
                "source": {"tree_sha256": "c".repeat(64)},
                "artifacts": {"atlas_rpm": {
                    "sha256": "d".repeat(64),
                    "payload_executable_path": INSTALLED_ATLAS,
                    "payload_executable_sha256": "e".repeat(64)
                }, "hermes_source_archive": {
                    "sha256": "9".repeat(64)
                }}
            });
            fs::write(
                &self.manifest,
                serde_json::to_vec(&manifest).expect("test manifest"),
            )
            .expect("manifest write");
            fs::set_permissions(&self.manifest, fs::Permissions::from_mode(0o444))
                .expect("manifest mode");
        }

        fn write_corrective_release(&self) {
            fs::write(&self.helper, b"#!/usr/bin/python3 -I\n").expect("test bootstrap helper");
            fs::set_permissions(&self.helper, fs::Permissions::from_mode(0o555))
                .expect("helper mode");
            let helper_sha256 = sha256_hex(b"#!/usr/bin/python3 -I\n");
            let manifest = serde_json::json!({
                "release_id": CORRECTIVE_TARGET_RELEASE_ID,
                "trust_anchor": {
                    "version_directory": TRUST_ANCHOR_ROOT,
                    "manifest_path": RELEASE_MANIFEST,
                    "bootstrap_helper_path": BOOTSTRAP_HELPER,
                    "launcher_path": REVIEWED_ATLAS_LAUNCHER,
                    "manifest_mode": "0444",
                    "bootstrap_helper_mode": "0555",
                    "launcher_mode": "0555",
                    "launcher_sha256": "f".repeat(64),
                    "enrollment": "physical-polkit-tofu-v1"
                },
                "bootstrap": {
                    "helper_sha256": helper_sha256,
                    "gui_protocol": {
                        "public_operations": [
                            "gui-preflight", "gui-apply",
                            "gui-corrective-preflight", "gui-corrective-apply",
                            "gui-recovery-preflight", "gui-recovery-apply"
                        ],
                        "launcher_mode": "sealed-rpm-payload-memfd",
                        "atlas_payload_member": INSTALLED_ATLAS,
                        "atlas_payload_sha256": "e".repeat(64)
                    },
                    "corrective": {
                        "mode": "single-terminal-rollback-repair",
                        "max_attempts": 1,
                        "consumed_marker": CORRECTIVE_CONSUMED_MARKER,
                        "state_root": CORRECTIVE_STATE_ROOT,
                        "history_root": "/var/lib/ops-control-plane-bootstrap-history",
                        "recovery_helper": "/usr/local/libexec/ops-control-plane-bootstrap-corrective",
                        "recovery_unit": "ops-control-plane-bootstrap-corrective-recovery.service",
                        "recovery_path_unit": "ops-control-plane-bootstrap-corrective-recovery.path",
                        "authority_document": "AGENTS.md",
                        "recovery_from_release_id": CORRECTIVE_PREDECESSOR_RELEASE_ID,
                        "recovery_from_source_tree_sha256": CORRECTIVE_PREDECESSOR_SOURCE_SHA256,
                        "recovery_from_atlas_rpm_sha256": CORRECTIVE_PREDECESSOR_RPM_SHA256,
                        "recovery_from_bootstrap_helper_sha256": CORRECTIVE_PREDECESSOR_HELPER_SHA256,
                        "recovery_from_manifest_sha256": CORRECTIVE_PREDECESSOR_MANIFEST_SHA256
                    }
                },
                "source": {"tree_sha256": "c".repeat(64)},
                "artifacts": {"atlas_rpm": {
                    "sha256": "d".repeat(64),
                    "payload_executable_path": INSTALLED_ATLAS,
                    "payload_executable_sha256": "e".repeat(64)
                }, "hermes_source_archive": {
                    "sha256": "9".repeat(64)
                }}
            });
            fs::write(
                &self.manifest,
                serde_json::to_vec(&manifest).expect("test corrective manifest"),
            )
            .expect("corrective manifest write");
            fs::set_permissions(&self.manifest, fs::Permissions::from_mode(0o444))
                .expect("manifest mode");
        }

        fn write_rolled_back_state(&self) {
            fs::create_dir(&self.state_root).expect("state root");
            fs::set_permissions(&self.state_root, fs::Permissions::from_mode(0o700))
                .expect("state mode");
            fs::write(&self.consumed, b"{\"consumed\":true}\n").expect("consumed marker");
            fs::set_permissions(&self.consumed, fs::Permissions::from_mode(0o600))
                .expect("consumed mode");
        }

        fn write_corrective_state(&self) {
            fs::create_dir(&self.corrective_state_root).expect("corrective state root");
            fs::set_permissions(
                &self.corrective_state_root,
                fs::Permissions::from_mode(0o700),
            )
            .expect("corrective state mode");
            fs::write(&self.corrective_consumed, b"{\"consumed\":true}\n")
                .expect("corrective consumed marker");
            fs::set_permissions(&self.corrective_consumed, fs::Permissions::from_mode(0o600))
                .expect("corrective consumed mode");
        }

        fn write_atlas(&self) {
            fs::write(&self.atlas, b"installed-atlas").expect("installed Atlas");
            fs::set_permissions(&self.atlas, fs::Permissions::from_mode(0o755))
                .expect("Atlas mode");
        }

        fn write_installed_files(&self) {
            self.write_rolled_back_state();
            self.write_atlas();
        }

        fn proof(&self) -> PublicInstalledProof {
            PublicInstalledProof {
                atlas_rpm_sha256: "d".repeat(64),
                bootstrap_helper_sha256: "a".repeat(64),
                completed_at: 1_789_000_000,
                hermes_source_archive_sha256: "e".repeat(64),
                manifest_sha256: "b".repeat(64),
                release_id: "release-1".to_owned(),
                schema_version: 1,
                source_tree_sha256: "c".repeat(64),
                status: "installed".to_owned(),
            }
        }

        fn write_proof(&self, proof: &PublicInstalledProof) {
            let mut payload = serde_json::to_vec(proof).expect("canonical proof");
            payload.push(b'\n');
            fs::write(&self.public_proof, payload).expect("public proof");
            fs::set_permissions(&self.public_proof, fs::Permissions::from_mode(0o644))
                .expect("proof mode");
        }
    }

    impl Drop for TestBootstrapLayout {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.root);
        }
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn repository_debug_or_test_binary_is_never_a_bootstrap_launcher() {
        let layout = TestBootstrapLayout::new("launcher-debug-rejected");
        layout.write_fresh_release();
        let (uid, gid) = layout.owner();
        let (_identity, manifest) =
            load_release_projection_at(&layout.manifest, &layout.helper, uid, gid)
                .expect("valid test release projection");
        let error = validate_current_atlas_launcher(&manifest)
            .expect_err("a repository test binary must be rejected");
        assert_eq!(error.code, "bootstrap_launcher_invalid");
    }

    #[test]
    #[cfg(unix)]
    fn release_anchor_requires_exact_owner_modes_and_single_links() {
        let layout = TestBootstrapLayout::new("root-anchor-policy");
        layout.write_fresh_release();
        let (uid, gid) = layout.owner();
        load_release_identity_at(&layout.manifest, &layout.helper, uid, gid)
            .expect("exact test anchor");

        let wrong_uid = if uid == 0 { 1 } else { 0 };
        let wrong_owner =
            load_release_identity_at(&layout.manifest, &layout.helper, wrong_uid, gid)
                .expect_err("a user-owned coherent replacement must not satisfy root policy");
        assert_eq!(wrong_owner.code, "bootstrap_release_unsafe");

        fs::set_permissions(&layout.manifest, fs::Permissions::from_mode(0o644))
            .expect("unsafe manifest mode");
        let wrong_mode = load_release_identity_at(&layout.manifest, &layout.helper, uid, gid)
            .expect_err("a writable anchor manifest must fail closed");
        assert_eq!(wrong_mode.code, "bootstrap_release_unsafe");
        fs::set_permissions(&layout.manifest, fs::Permissions::from_mode(0o444))
            .expect("restore manifest mode");

        let alias = layout.root.join("helper-hardlink");
        fs::hard_link(&layout.helper, &alias).expect("helper hard link");
        let linked = load_release_identity_at(&layout.manifest, &layout.helper, uid, gid)
            .expect_err("a multiply-linked helper must fail closed");
        assert_eq!(linked.code, "bootstrap_release_unsafe");
    }

    #[test]
    #[cfg(unix)]
    fn bootstrap_status_matrix_is_fail_closed_and_source_independent_after_install() {
        let fresh = TestBootstrapLayout::new("status-fresh");
        fresh.write_fresh_release();
        let (uid, gid) = fresh.owner();
        let fresh_status = status_from_paths(&BootstrapManager::default(), fresh.paths(), uid, gid)
            .expect("fresh status");
        assert!(fresh_status.required);
        assert_eq!(fresh_status.state, "ready");
        assert_eq!(fresh_status.release_id, "release-1");
        assert_eq!(fresh_status.hermes_source_archive_sha256, "9".repeat(64));
        assert_eq!(
            serde_json::to_value(&fresh_status).expect("serialized bootstrap status")
                ["hermes_source_archive_sha256"],
            serde_json::Value::String("9".repeat(64))
        );
        assert!(!fresh_status.resume_available);

        let partial = TestBootstrapLayout::new("status-partial");
        partial.write_installed_files();
        let (uid, gid) = partial.owner();
        let partial_status =
            status_from_paths(&BootstrapManager::default(), partial.paths(), uid, gid)
                .expect("partial status");
        assert!(partial_status.required);
        assert_eq!(partial_status.state, "recovery-required");
        assert!(!partial_status.atlas_available);
        assert!(partial_status.zulip_url.is_none());
        assert!(!partial_status.resume_available);

        let installed = TestBootstrapLayout::new("status-installed");
        installed.write_installed_files();
        installed.write_proof(&installed.proof());
        let (uid, gid) = installed.owner();
        let installed_status =
            status_from_paths(&BootstrapManager::default(), installed.paths(), uid, gid)
                .expect("installed status without source checkout");
        assert!(!installed_status.required);
        assert_eq!(installed_status.state, "installed");
        assert!(installed_status.atlas_available);
        assert_eq!(installed_status.zulip_url.as_deref(), Some(ZULIP_URL));
        assert_eq!(installed_status.release_id, "release-1");
        assert_eq!(installed_status.bootstrap_helper_sha256, "a".repeat(64));
        assert_eq!(
            installed_status.hermes_source_archive_sha256,
            "e".repeat(64)
        );
    }

    #[test]
    #[cfg(unix)]
    fn release_projection_requires_one_exact_hermes_archive_identity() {
        let layout = TestBootstrapLayout::new("hermes-archive-identity");
        layout.write_fresh_release();
        let (uid, gid) = layout.owner();
        let reviewed = fs::read(&layout.manifest).expect("reviewed manifest bytes");
        let mut missing: serde_json::Value =
            serde_json::from_slice(&reviewed).expect("reviewed manifest JSON");
        missing["artifacts"]
            .as_object_mut()
            .expect("artifacts object")
            .remove("hermes_source_archive");
        fs::set_permissions(&layout.manifest, fs::Permissions::from_mode(0o600))
            .expect("make manifest writable");
        fs::write(
            &layout.manifest,
            serde_json::to_vec(&missing).expect("manifest without Hermes identity"),
        )
        .expect("write manifest without Hermes identity");
        fs::set_permissions(&layout.manifest, fs::Permissions::from_mode(0o444))
            .expect("restore manifest mode");
        let missing_error = load_release_identity_at(&layout.manifest, &layout.helper, uid, gid)
            .expect_err("the Hermes archive identity is mandatory");
        assert_eq!(missing_error.code, "bootstrap_manifest_invalid");

        let mut unexpected: serde_json::Value =
            serde_json::from_slice(&reviewed).expect("reviewed manifest JSON");
        unexpected["artifacts"]["unreviewed_archive"] = serde_json::json!({
            "sha256": "f".repeat(64)
        });
        fs::set_permissions(&layout.manifest, fs::Permissions::from_mode(0o600))
            .expect("make manifest writable again");
        fs::write(
            &layout.manifest,
            serde_json::to_vec(&unexpected).expect("manifest with extra artifact"),
        )
        .expect("write manifest with extra artifact");
        fs::set_permissions(&layout.manifest, fs::Permissions::from_mode(0o444))
            .expect("restore manifest mode again");
        let unexpected_error = load_release_identity_at(&layout.manifest, &layout.helper, uid, gid)
            .expect_err("unreviewed artifact identities must fail closed");
        assert_eq!(unexpected_error.code, "bootstrap_manifest_invalid");
    }

    #[test]
    #[cfg(unix)]
    fn corrective_disk_boundaries_allow_only_safe_initial_and_resume_matrices() {
        let marker_only = TestBootstrapLayout::new("standard-marker-only-boundary");
        marker_only.write_corrective_release();
        fs::write(&marker_only.consumed, b"{\"consumed\":true}\n")
            .expect("standard consumed marker");
        fs::set_permissions(&marker_only.consumed, fs::Permissions::from_mode(0o600))
            .expect("standard marker mode");
        let (marker_uid, marker_gid) = marker_only.owner();
        assert!(matches!(
            assess_bootstrap_disk_state(marker_only.paths(), marker_uid, marker_gid),
            DiskBootstrapState::RecoveryRequired {
                corrective_eligible: true,
                corrective_started: false
            }
        ));

        let manager = BootstrapManager::default();
        let mut fresh_attestation = fresh_operation_identity(
            load_release_identity_at(
                &marker_only.manifest,
                &marker_only.helper,
                marker_uid,
                marker_gid,
            )
            .expect("fresh recovery identity"),
        );
        fresh_attestation.rollback_finalize_resume = true;
        {
            let mut guard = manager.inner.lock().expect("manager state");
            guard.lifecycle = Lifecycle::PreflightPassed;
            guard.operation = Some(Operation::RecoveryPreflight);
            guard.preflight_attestation = Some(fresh_attestation);
        }
        let classified = status_from_paths(&manager, marker_only.paths(), marker_uid, marker_gid)
            .expect("classified fresh recovery status");
        assert!(classified.preflight_passed);
        assert!(classified.rollback_finalize_resume);
        assert_eq!(classified.operation_mode.as_deref(), Some("fresh"));

        let layout = TestBootstrapLayout::new("corrective-boundaries");
        layout.write_corrective_release();
        layout.write_rolled_back_state();
        let (uid, gid) = layout.owner();

        assert!(matches!(
            assess_bootstrap_disk_state(layout.paths(), uid, gid),
            DiskBootstrapState::RecoveryRequired {
                corrective_eligible: true,
                corrective_started: false
            }
        ));
        let initial = status_from_paths(&BootstrapManager::default(), layout.paths(), uid, gid)
            .expect("initial corrective status");
        assert_eq!(initial.state, "recovery-required");
        assert!(initial.corrective_available);
        assert!(!initial.resume_available);
        assert!(!initial.atlas_available);
        assert_eq!(initial.release_id, CORRECTIVE_TARGET_RELEASE_ID);
        assert_eq!(
            initial.recovery_from_release_id.as_deref(),
            Some(CORRECTIVE_PREDECESSOR_RELEASE_ID)
        );

        for corrective_state in [false, true] {
            for standard_marker in [false, true] {
                for standard_state in [false, true] {
                    for atlas_present in [false, true] {
                        let boundary = TestBootstrapLayout::new("corrective-resume-boundary");
                        boundary.write_corrective_release();
                        if corrective_state {
                            boundary.write_corrective_state();
                        } else {
                            fs::write(&boundary.corrective_consumed, b"{\"consumed\":true}\n")
                                .expect("materialize corrective marker");
                            fs::set_permissions(
                                &boundary.corrective_consumed,
                                fs::Permissions::from_mode(0o600),
                            )
                            .expect("corrective marker mode");
                        }
                        if standard_marker {
                            fs::write(&boundary.consumed, b"{\"consumed\":true}\n")
                                .expect("materialize standard marker");
                            fs::set_permissions(
                                &boundary.consumed,
                                fs::Permissions::from_mode(0o600),
                            )
                            .expect("standard marker mode");
                        }
                        if standard_state {
                            fs::create_dir(&boundary.state_root)
                                .expect("materialize standard state");
                            fs::set_permissions(
                                &boundary.state_root,
                                fs::Permissions::from_mode(0o700),
                            )
                            .expect("standard state mode");
                        }
                        if atlas_present {
                            boundary.write_atlas();
                        }
                        let (boundary_uid, boundary_gid) = boundary.owner();
                        assert!(matches!(
                            assess_bootstrap_disk_state(
                                boundary.paths(),
                                boundary_uid,
                                boundary_gid,
                            ),
                            DiskBootstrapState::RecoveryRequired {
                                corrective_eligible: true,
                                corrective_started: true
                            }
                        ));
                        let status = status_from_paths(
                            &BootstrapManager::default(),
                            boundary.paths(),
                            boundary_uid,
                            boundary_gid,
                        )
                        .expect("safe corrective resume status");
                        assert!(status.corrective_available);
                        assert!(status.resume_available);
                        assert!(!status.atlas_available);
                    }
                }
            }
        }

        layout.write_corrective_state();
        fs::set_permissions(&layout.consumed, fs::Permissions::from_mode(0o644))
            .expect("make standard marker unsafe");
        assert!(matches!(
            assess_bootstrap_disk_state(layout.paths(), uid, gid),
            DiskBootstrapState::RecoveryRequired {
                corrective_eligible: false,
                ..
            }
        ));

        let partial = TestBootstrapLayout::new("corrective-partial");
        partial.write_corrective_release();
        fs::create_dir(&partial.corrective_state_root).expect("partial corrective state root");
        fs::set_permissions(
            &partial.corrective_state_root,
            fs::Permissions::from_mode(0o700),
        )
        .expect("partial corrective state mode");
        let (partial_uid, partial_gid) = partial.owner();
        assert!(matches!(
            assess_bootstrap_disk_state(partial.paths(), partial_uid, partial_gid),
            DiskBootstrapState::RecoveryRequired {
                corrective_eligible: false,
                ..
            }
        ));
        fs::remove_dir(&partial.corrective_state_root).expect("remove partial corrective state");
        fs::write(&partial.corrective_consumed, b"{\"consumed\":true}\n")
            .expect("partial corrective marker");
        fs::set_permissions(
            &partial.corrective_consumed,
            fs::Permissions::from_mode(0o600),
        )
        .expect("partial corrective marker mode");
        assert!(matches!(
            assess_bootstrap_disk_state(partial.paths(), partial_uid, partial_gid),
            DiskBootstrapState::RecoveryRequired {
                corrective_eligible: true,
                corrective_started: true
            }
        ));
        let marker_only_status = status_from_paths(
            &BootstrapManager::default(),
            partial.paths(),
            partial_uid,
            partial_gid,
        )
        .expect("marker-only corrective replay status");
        assert!(marker_only_status.corrective_available);
        assert!(marker_only_status.resume_available);
        fs::set_permissions(
            &partial.corrective_consumed,
            fs::Permissions::from_mode(0o644),
        )
        .expect("unsafe corrective marker mode");
        assert!(matches!(
            assess_bootstrap_disk_state(partial.paths(), partial_uid, partial_gid),
            DiskBootstrapState::RecoveryRequired {
                corrective_eligible: false,
                ..
            }
        ));
    }

    #[test]
    #[cfg(unix)]
    fn corrective_manifest_derives_a_distinct_fully_bound_confirmation() {
        let layout = TestBootstrapLayout::new("corrective-identity");
        layout.write_corrective_release();
        let (uid, gid) = layout.owner();
        let identity = load_corrective_identity_at(&layout.manifest, &layout.helper, uid, gid)
            .expect("strict corrective identity");
        let helper_sha256 = sha256_hex(b"#!/usr/bin/python3 -I\n");
        let manifest_sha256 =
            sha256_hex(&fs::read(&layout.manifest).expect("corrective manifest bytes"));
        let expected = sha256_hex(
            format!(
                "corrective-v1:{}:{}:{}:{}:{}:{}:{}:{}:{}:{}",
                CORRECTIVE_PREDECESSOR_RELEASE_ID,
                CORRECTIVE_PREDECESSOR_SOURCE_SHA256,
                CORRECTIVE_PREDECESSOR_RPM_SHA256,
                CORRECTIVE_PREDECESSOR_HELPER_SHA256,
                CORRECTIVE_PREDECESSOR_MANIFEST_SHA256,
                CORRECTIVE_TARGET_RELEASE_ID,
                helper_sha256,
                manifest_sha256,
                "c".repeat(64),
                "9".repeat(64),
            )
            .as_bytes(),
        )[..12]
            .to_owned();
        assert_eq!(identity.mode, OperationMode::Corrective);
        assert_eq!(identity.release.release_id, CORRECTIVE_TARGET_RELEASE_ID);
        assert_eq!(
            identity.recovery_from_release_id.as_deref(),
            Some(CORRECTIVE_PREDECESSOR_RELEASE_ID)
        );
        assert_eq!(identity.release.confirmation_digest_suffix, expected);
        assert_ne!(
            identity.release.confirmation_digest_suffix,
            load_release_identity_at(&layout.manifest, &layout.helper, uid, gid)
                .expect("fresh projection")
                .confirmation_digest_suffix
        );

        let reviewed_manifest = fs::read(&layout.manifest).expect("corrective manifest bytes");
        let mut wrong_predecessor: serde_json::Value =
            serde_json::from_slice(&reviewed_manifest).expect("corrective manifest JSON");
        wrong_predecessor["bootstrap"]["corrective"]["recovery_from_release_id"] =
            serde_json::Value::String("atlas-api-zulip-2026.09.08.09".to_owned());
        fs::set_permissions(&layout.manifest, fs::Permissions::from_mode(0o600))
            .expect("make manifest writable");
        fs::write(
            &layout.manifest,
            serde_json::to_vec(&wrong_predecessor).expect("wrong-predecessor manifest"),
        )
        .expect("wrong-predecessor manifest write");
        fs::set_permissions(&layout.manifest, fs::Permissions::from_mode(0o444))
            .expect("restore manifest mode");
        let error = load_corrective_identity_at(&layout.manifest, &layout.helper, uid, gid)
            .expect_err("corrective predecessor must be exact");
        assert_eq!(error.code, "bootstrap_corrective_manifest_invalid");

        let mut wrong_path_unit: serde_json::Value =
            serde_json::from_slice(&reviewed_manifest).expect("corrective manifest JSON");
        wrong_path_unit["bootstrap"]["corrective"]["recovery_path_unit"] =
            serde_json::Value::String("unreviewed.path".to_owned());
        fs::set_permissions(&layout.manifest, fs::Permissions::from_mode(0o600))
            .expect("make manifest writable");
        fs::write(
            &layout.manifest,
            serde_json::to_vec(&wrong_path_unit).expect("wrong-path-unit manifest"),
        )
        .expect("wrong-path-unit manifest write");
        fs::set_permissions(&layout.manifest, fs::Permissions::from_mode(0o444))
            .expect("restore manifest mode");
        let error = load_corrective_identity_at(&layout.manifest, &layout.helper, uid, gid)
            .expect_err("corrective path unit must be exact");
        assert_eq!(error.code, "bootstrap_corrective_manifest_invalid");

        let mut unexpected: serde_json::Value =
            serde_json::from_slice(&reviewed_manifest).expect("corrective manifest JSON");
        unexpected["bootstrap"]["corrective"]["unreviewed_extension"] =
            serde_json::Value::Bool(true);
        fs::set_permissions(&layout.manifest, fs::Permissions::from_mode(0o600))
            .expect("make manifest writable");
        fs::write(
            &layout.manifest,
            serde_json::to_vec(&unexpected).expect("unexpected corrective manifest"),
        )
        .expect("unexpected corrective manifest write");
        fs::set_permissions(&layout.manifest, fs::Permissions::from_mode(0o444))
            .expect("restore manifest mode");
        let error = load_corrective_identity_at(&layout.manifest, &layout.helper, uid, gid)
            .expect_err("corrective shape must reject unknown keys");
        assert_eq!(error.code, "bootstrap_manifest_invalid");
    }

    #[test]
    #[cfg(unix)]
    fn malformed_modes_symlinks_and_partial_state_never_open_atlas() {
        let layout = TestBootstrapLayout::new("status-invalid");
        layout.write_installed_files();
        layout.write_proof(&layout.proof());
        let (uid, gid) = layout.owner();

        fs::set_permissions(&layout.public_proof, fs::Permissions::from_mode(0o600))
            .expect("unsafe proof mode");
        let wrong_mode = status_from_paths(&BootstrapManager::default(), layout.paths(), uid, gid)
            .expect("wrong mode status");
        assert_eq!(wrong_mode.state, "recovery-required");
        assert!(!wrong_mode.atlas_available);

        fs::remove_file(&layout.public_proof).expect("remove wrong-mode proof");
        let target = layout.root.join("proof-target");
        let mut payload = serde_json::to_vec(&layout.proof()).expect("proof payload");
        payload.push(b'\n');
        fs::write(&target, payload).expect("proof target");
        fs::set_permissions(&target, fs::Permissions::from_mode(0o644)).expect("target mode");
        std::os::unix::fs::symlink(&target, &layout.public_proof).expect("proof symlink");
        let symlinked = status_from_paths(&BootstrapManager::default(), layout.paths(), uid, gid)
            .expect("symlink status");
        assert_eq!(symlinked.state, "recovery-required");
        assert!(!symlinked.atlas_available);

        fs::remove_file(&layout.public_proof).expect("remove proof symlink");
        layout.write_proof(&layout.proof());
        fs::set_permissions(&layout.consumed, fs::Permissions::from_mode(0o644))
            .expect("unsafe consumed mode");
        let mismatched = status_from_paths(&BootstrapManager::default(), layout.paths(), uid, gid)
            .expect("mismatched installed state");
        assert_eq!(mismatched.state, "recovery-required");
        assert!(!mismatched.atlas_available);
    }

    #[test]
    #[cfg(unix)]
    fn public_proof_path_replacement_is_detected_after_open() {
        let layout = TestBootstrapLayout::new("proof-race");
        layout.write_proof(&layout.proof());
        let replacement = layout.root.join("replacement-proof");
        let mut payload = serde_json::to_vec(&layout.proof()).expect("replacement payload");
        payload.push(b'\n');
        fs::write(&replacement, payload).expect("replacement write");
        fs::set_permissions(&replacement, fs::Permissions::from_mode(0o644))
            .expect("replacement mode");
        let (uid, gid) = layout.owner();
        let result = read_public_installed_proof_with(&layout.public_proof, uid, gid, || {
            fs::rename(&replacement, &layout.public_proof).expect("replace after open")
        });
        assert!(result.is_err());
    }

    #[test]
    #[cfg(unix)]
    fn public_proof_requires_the_hermes_archive_identity() {
        let layout = TestBootstrapLayout::new("proof-hermes-identity");
        let mut proof = serde_json::to_value(layout.proof()).expect("proof JSON");
        proof
            .as_object_mut()
            .expect("proof object")
            .remove("hermes_source_archive_sha256");
        let mut payload = serde_json::to_vec(&proof).expect("proof without Hermes identity");
        payload.push(b'\n');
        fs::write(&layout.public_proof, payload).expect("write incomplete proof");
        fs::set_permissions(&layout.public_proof, fs::Permissions::from_mode(0o644))
            .expect("proof mode");
        let (uid, gid) = layout.owner();
        let error = read_public_installed_proof_with(&layout.public_proof, uid, gid, || {})
            .expect_err("proof without Hermes identity must fail closed");
        assert_eq!(error.code, "bootstrap_state_invalid");
    }

    #[test]
    fn digest_confirmation_is_bound_to_all_reviewed_identities() {
        let helper = "a".repeat(64);
        let manifest = "b".repeat(64);
        let source = "c".repeat(64);
        let hermes = "d".repeat(64);
        let first = sha256_hex(format!("{helper}:{manifest}:{source}:{hermes}").as_bytes())[..12]
            .to_owned();
        let changed =
            sha256_hex(format!("{helper}:{manifest}:{source}:{}", "e".repeat(64)).as_bytes())[..12]
                .to_owned();
        assert_ne!(first, changed);
        assert_eq!(first.len(), 12);

        let identity = test_identity();
        let mut other = identity.release.clone();
        other.hermes_source_archive_sha256 = "f".repeat(64);
        assert!(!identity.release.same_artifacts(&other));
    }

    #[test]
    fn helper_lines_are_strict_and_bounded() {
        let valid = br#"{"schema_version":1,"event":"progress","status":"running","phase":"preflight","message":"Verification","percent":10,"error_code":null,"mutation_started":false,"cancellation_allowed":true,"release_id":null,"bootstrap_helper_sha256":null,"manifest_sha256":null,"source_tree_sha256":null,"atlas_rpm_sha256":null,"hermes_source_archive_sha256":null,"confirmation_digest_suffix":null,"zulip_url":null,"atlas_available":null}"#;
        let parsed = validated_helper_event(valid).expect("valid progress event");
        assert_eq!(parsed.percent, Some(10));
        let mut oversized = Cursor::new(vec![b'x'; MAX_EVENT_BYTES + 1]);
        assert!(bounded_line(&mut oversized).is_err());
        assert!(validated_helper_event(b"{\"event\":\"progress\",\"secret\":\"x\"}").is_err());
    }

    #[test]
    fn password_policy_is_bounded_and_distinctness_is_enforced_by_caller() {
        assert!(valid_bootstrap_password("correct horse battery staple"));
        assert!(!valid_bootstrap_password("too-short"));
        assert!(!valid_bootstrap_password(" leading-password-value"));
        assert!(!valid_bootstrap_password("password-value\n"));
        assert!(!valid_bootstrap_password(&"x".repeat(101)));
    }

    #[test]
    fn apply_frame_has_only_non_secret_json_and_bounded_binary_secret_frames() {
        let frame = build_apply_frame(
            "openbao-secret-value",
            "zulip-secret-value",
            OperationMode::Fresh,
            "atlas-api-zulip-2026.09.07.8",
            "0123456789ab",
        )
        .expect("bounded frame");
        assert!(frame.starts_with(b"ATLASBOOT1\n"));
        let metadata_length = u32::from_be_bytes(frame[11..15].try_into().expect("u32")) as usize;
        let metadata = &frame[15..15 + metadata_length];
        let metadata_text = std::str::from_utf8(metadata).expect("metadata utf8");
        assert!(metadata_text.contains("confirmed_release_id"));
        assert!(metadata_text.contains("\"operation_mode\":\"fresh\""));
        assert!(!metadata_text.contains("openbao-secret-value"));
        assert!(!metadata_text.contains("zulip-secret-value"));
    }

    #[test]
    fn secrets_are_written_only_after_one_exact_root_readiness() {
        let identity = test_identity();
        let exact = readiness_event(&identity);
        assert_eq!(
            accept_secret_readiness(Operation::FreshApply, &exact, &identity, false),
            Ok(true)
        );
        assert!(
            accept_secret_readiness(Operation::FreshPreflight, &exact, &identity, false).is_err()
        );
        assert!(accept_secret_readiness(Operation::FreshApply, &exact, &identity, true).is_err());

        let mut partial = readiness_event(&identity);
        partial.bootstrap_helper_sha256 = None;
        assert!(
            accept_secret_readiness(Operation::FreshApply, &partial, &identity, false).is_err()
        );
        let mut wrong_hermes = readiness_event(&identity);
        wrong_hermes.hermes_source_archive_sha256 = Some("f".repeat(64));
        assert!(
            accept_secret_readiness(Operation::FreshApply, &wrong_hermes, &identity, false)
                .is_err()
        );
        let mut untouched = Vec::new();
        let mut retained = Some((
            Zeroizing::new("openbao-secret-value".to_owned()),
            Zeroizing::new("zulip-secret-value".to_owned()),
            identity.release.release_id.clone(),
            identity.release.confirmation_digest_suffix.clone(),
        ));
        assert!(write_apply_secrets_after_readiness(
            &mut untouched,
            &partial,
            &identity,
            &mut retained,
        )
        .is_err());
        assert!(untouched.is_empty());

        let mut delivered = Vec::new();
        assert!(write_apply_secrets_after_readiness(
            &mut delivered,
            &exact,
            &identity,
            &mut retained,
        )
        .is_ok());
        assert!(delivered.starts_with(b"ATLASBOOT1\n"));
        assert!(delivered
            .windows(b"openbao-secret-value".len())
            .any(|window| window == b"openbao-secret-value"));
        assert!(retained.is_none());
    }

    #[test]
    fn fresh_and_corrective_protocol_contexts_are_never_interchangeable() {
        let fresh = test_identity();
        let corrective = OperationIdentity {
            mode: OperationMode::Corrective,
            release: fresh.release.clone(),
            recovery_from_release_id: Some("release-0".to_owned()),
            rollback_finalize_resume: false,
        };
        let corrective_readiness = readiness_event(&corrective);
        assert!(event_context_is_valid(&corrective_readiness, &corrective));
        assert!(!event_context_is_valid(&corrective_readiness, &fresh));
        assert_eq!(
            accept_secret_readiness(
                Operation::CorrectiveApply,
                &corrective_readiness,
                &corrective,
                false,
            ),
            Ok(true)
        );
        assert!(accept_secret_readiness(
            Operation::FreshApply,
            &corrective_readiness,
            &fresh,
            false,
        )
        .is_err());

        let mut missing_recovery = readiness_event(&corrective);
        missing_recovery.recovery_from_release_id = None;
        assert!(!event_context_is_valid(&missing_recovery, &corrective));
        assert!(accept_secret_readiness(
            Operation::CorrectiveApply,
            &missing_recovery,
            &corrective,
            false,
        )
        .is_err());

        let wrong_mode = br#"{"schema_version":1,"event":"progress","status":"running","phase":"preflight","message":"Verification","percent":10,"error_code":null,"mutation_started":false,"cancellation_allowed":true,"operation_mode":"repair"}"#;
        assert!(validated_helper_event(wrong_mode).is_err());
    }

    #[test]
    fn neutral_recovery_transport_is_distinct_and_uses_closed_commands() {
        let mut recovery = test_identity();
        recovery.mode = OperationMode::Recovery;
        let early = br#"{"schema_version":1,"event":"progress","status":"running","phase":"preflight","message":"Verification","percent":7,"error_code":null,"mutation_started":false,"cancellation_allowed":true,"operation_mode":"recovery","recovery_from_release_id":null,"zulip_url":null,"atlas_available":null}"#;
        let event = validated_helper_event(early).expect("closed recovery transport event");
        assert!(!event_has_identity(&event));
        assert!(event_context_is_valid(&event, &recovery));
        assert_eq!(
            Operation::RecoveryPreflight.argument(),
            "gui-recovery-preflight"
        );
        assert_eq!(Operation::RecoveryApply.argument(), "gui-recovery-apply");
        assert_ne!(Operation::RecoveryPreflight, Operation::FreshPreflight);
        assert_ne!(Operation::RecoveryApply, Operation::CorrectiveApply);
    }

    #[test]
    fn apply_requires_the_same_in_memory_mode_and_release_attestation() {
        let fresh = test_identity();
        let corrective = OperationIdentity {
            mode: OperationMode::Corrective,
            release: fresh.release.clone(),
            recovery_from_release_id: Some("release-0".to_owned()),
            rollback_finalize_resume: false,
        };
        let mut state = ManagerState {
            lifecycle: Lifecycle::PreflightPassed,
            preflight_attestation: Some(fresh.clone()),
            operation: Some(Operation::FreshPreflight),
            result: idle_result(),
            control: None,
            mutation_started: false,
            cancel_requested: false,
        };
        assert!(apply_has_matching_preflight(
            &state,
            Operation::FreshApply,
            &fresh
        ));
        assert!(!apply_has_matching_preflight(
            &state,
            Operation::CorrectiveApply,
            &corrective
        ));

        state.preflight_attestation = Some(corrective.clone());
        state.operation = Some(Operation::CorrectivePreflight);
        assert!(apply_has_matching_preflight(
            &state,
            Operation::CorrectiveApply,
            &corrective
        ));
        assert!(!apply_has_matching_preflight(
            &state,
            Operation::FreshApply,
            &fresh
        ));

        state.preflight_attestation = Some(fresh.clone());
        state.operation = Some(Operation::RecoveryPreflight);
        assert!(apply_has_matching_preflight(
            &state,
            Operation::RecoveryApply,
            &fresh
        ));
        let mut different_rollback_mode = fresh.clone();
        different_rollback_mode.rollback_finalize_resume = true;
        assert!(!apply_has_matching_preflight(
            &state,
            Operation::RecoveryApply,
            &different_rollback_mode,
        ));

        let mut changed = corrective.clone();
        changed.release.manifest_sha256 = "9".repeat(64);
        assert!(!apply_has_matching_preflight(
            &state,
            Operation::CorrectiveApply,
            &changed
        ));
    }

    #[test]
    fn mutation_commit_readiness_is_exact_ordered_and_one_shot() {
        let identity = test_identity();
        let exact = mutation_commit_event(&identity);
        assert_eq!(
            accept_mutation_commit_readiness(Operation::FreshApply, &exact, &identity, true, false,),
            Ok(true)
        );
        assert!(accept_mutation_commit_readiness(
            Operation::FreshApply,
            &exact,
            &identity,
            false,
            false,
        )
        .is_err());
        assert!(accept_mutation_commit_readiness(
            Operation::FreshApply,
            &exact,
            &identity,
            true,
            true,
        )
        .is_err());
        assert!(accept_mutation_commit_readiness(
            Operation::FreshPreflight,
            &exact,
            &identity,
            true,
            false,
        )
        .is_err());
        let mut partial = mutation_commit_event(&identity);
        partial.status = "running".to_owned();
        assert!(event_mentions_mutation_commit(&partial));
        assert!(accept_mutation_commit_readiness(
            Operation::FreshApply,
            &partial,
            &identity,
            true,
            false,
        )
        .is_err());
    }

    #[test]
    fn rollback_finalizer_protocol_is_exact_secretless_and_mode_bound() {
        for operation in [Operation::FreshApply, Operation::CorrectiveApply] {
            let mut identity = test_identity();
            identity.mode = operation.mode();
            identity.recovery_from_release_id =
                (operation.mode() == OperationMode::Corrective).then(|| "release-0".to_owned());
            identity.rollback_finalize_resume = true;
            let exact = rollback_finalize_readiness_event(&identity);
            assert_eq!(
                accept_rollback_finalize_readiness(operation, &exact, &identity, false),
                Ok(true)
            );
            assert!(
                accept_rollback_finalize_readiness(operation, &exact, &identity, true).is_err()
            );
            assert!(accept_secret_readiness(
                operation,
                &readiness_event(&identity),
                &identity,
                false
            )
            .is_err());

            let mut wrong_bit = exact;
            wrong_bit.rollback_finalize_resume = Some(false);
            assert!(!event_context_is_valid(&wrong_bit, &identity));
            assert!(
                accept_rollback_finalize_readiness(operation, &wrong_bit, &identity, false,)
                    .is_err()
            );

            let commit = mutation_commit_event(&identity);
            assert_eq!(
                accept_mutation_commit_readiness(operation, &commit, &identity, true, false),
                Ok(true)
            );
            let terminal = rollback_finalized_event(&identity);
            assert!(exact_rollback_finalized(&terminal, &identity));
            assert!(terminal.zulip_url.is_none());
            assert_eq!(terminal.atlas_available, Some(false));
        }

        let normal = test_identity();
        assert!(!exact_rollback_finalized(
            &rollback_finalized_event(&OperationIdentity {
                rollback_finalize_resume: true,
                ..normal.clone()
            }),
            &normal,
        ));
    }

    #[test]
    fn mutation_commit_mutex_sends_exact_ack_or_cancel_then_closes_pipe() {
        fn echo_child() -> Child {
            Command::new("/usr/bin/python3")
                .args([
                    "-I",
                    "-c",
                    "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
                ])
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                .spawn()
                .expect("echo child")
        }

        let manager = BootstrapManager::default();
        let mut acknowledged_child = echo_child();
        {
            let mut guard = manager.inner.lock().expect("manager state");
            guard.lifecycle = Lifecycle::ApplyRunning;
            guard.control = acknowledged_child.stdin.take();
        }
        assert!(matches!(
            decide_mutation_commit(&manager),
            Ok(MutationCommitDecision::Acknowledged)
        ));
        let acknowledged = acknowledged_child.wait_with_output().expect("ack output");
        assert_eq!(acknowledged.stdout, MUTATION_ACK_FRAME);
        assert!(
            manager
                .inner
                .lock()
                .expect("manager state")
                .mutation_started
        );
        assert_eq!(
            cancel_bootstrap(&manager).expect("cancel response").status,
            "refused-after-mutation"
        );

        let cancelled_manager = BootstrapManager::default();
        let mut cancelled_child = echo_child();
        {
            let mut guard = cancelled_manager.inner.lock().expect("manager state");
            guard.lifecycle = Lifecycle::ApplyRunning;
            guard.cancel_requested = true;
            guard.control = cancelled_child.stdin.take();
        }
        assert!(matches!(
            decide_mutation_commit(&cancelled_manager),
            Ok(MutationCommitDecision::Cancelled)
        ));
        let cancelled = cancelled_child.wait_with_output().expect("cancel output");
        assert_eq!(cancelled.stdout, CANCEL_FRAME);
        assert!(
            !cancelled_manager
                .inner
                .lock()
                .expect("manager state")
                .mutation_started
        );
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn helper_path_replacement_cannot_receive_any_secret_bytes() {
        let directory = unique_test_directory("helper-race");
        fs::create_dir(&directory).expect("private test directory");
        let helper_path = directory.join("control-plane-bootstrap");
        let replacement_path = directory.join("replacement");
        let marker_path = directory.join("attacker-received");
        let trusted = b"#!/usr/bin/python3 -I\nimport sys\nsys.stdin.buffer.read()\nsys.stdout.write('trusted-snapshot\\n')\n";
        fs::write(&helper_path, trusted).expect("trusted helper");
        fs::set_permissions(&helper_path, fs::Permissions::from_mode(0o555)).expect("trusted mode");
        let expected = sha256_hex(trusted);
        let owner = fs::metadata(&helper_path).expect("helper metadata");
        let sealed = snapshot_and_seal_helper_at(
            helper_path.to_str().expect("utf8 helper path"),
            &expected,
            TrustedFilePolicy {
                owner_uid: owner.uid(),
                owner_gid: owner.gid(),
                mode: 0o555,
            },
        )
        .expect("sealed helper snapshot");
        assert_eq!(
            fcntl_get_seals(&sealed.file).expect("memfd seals"),
            required_memfd_seals()
        );

        let attacker = format!(
            "#!/usr/bin/python3 -I\nimport pathlib, sys\ndata = sys.stdin.buffer.read()\npathlib.Path({:?}).write_bytes(data)\nsys.stdout.write('attacker\\n')\n",
            marker_path.to_string_lossy()
        );
        fs::write(&replacement_path, attacker).expect("attacker replacement");
        fs::set_permissions(&replacement_path, fs::Permissions::from_mode(0o700))
            .expect("replacement mode");
        fs::rename(&replacement_path, &helper_path).expect("atomic path replacement");

        let mut command = helper_command_at(
            Operation::FreshApply,
            &sealed,
            directory.to_str().expect("utf8 directory"),
        )
        .expect("sealed command");
        let mut child = command.spawn().expect("sealed helper child");
        drop(command);
        drop(sealed);
        let simulated_secret = b"secret-must-not-reach-replaced-path";
        let mut input = child.stdin.take().expect("private child pipe");
        input
            .write_all(simulated_secret)
            .expect("trusted pipe write");
        drop(input);
        let output = child.wait_with_output().expect("trusted helper exit");
        assert!(output.status.success());
        assert_eq!(output.stdout, b"trusted-snapshot\n");
        assert!(!marker_path.exists());
        assert!(!output
            .stdout
            .windows(simulated_secret.len())
            .any(|window| window == simulated_secret));
        fs::remove_dir_all(&directory).expect("test cleanup");
    }

    #[test]
    fn cancellation_is_refused_after_the_mutation_boundary() {
        let manager = BootstrapManager::default();
        {
            let mut guard = manager.inner.lock().expect("manager state");
            guard.lifecycle = Lifecycle::ApplyRunning;
            guard.mutation_started = true;
        }
        assert_eq!(
            cancel_bootstrap(&manager).expect("bounded response").status,
            "refused-after-mutation"
        );
    }

    #[test]
    fn terminal_identity_is_all_exact_or_entirely_absent() {
        let identity = test_identity();
        let mut event: HelperEvent = serde_json::from_str(
            r#"{"schema_version":1,"event":"result","status":"failed","phase":"authentication","message":"Echec","percent":100,"error_code":"enrollment_failed","mutation_started":false,"cancellation_allowed":true,"operation_mode":"fresh"}"#,
        )
        .expect("closed failure event");
        assert!(terminal_identity_is_valid(&event, &identity));
        event.release_id = Some(identity.release.release_id.clone());
        assert!(!terminal_identity_is_valid(&event, &identity));
        event.bootstrap_helper_sha256 = Some(identity.release.helper_sha256.clone());
        event.manifest_sha256 = Some(identity.release.manifest_sha256.clone());
        event.source_tree_sha256 = Some(identity.release.source_sha256.clone());
        event.atlas_rpm_sha256 = Some(identity.release.rpm_sha256.clone());
        event.hermes_source_archive_sha256 =
            Some(identity.release.hermes_source_archive_sha256.clone());
        event.confirmation_digest_suffix =
            Some(identity.release.confirmation_digest_suffix.clone());
        event.rollback_finalize_resume = Some(false);
        assert!(terminal_identity_is_valid(&event, &identity));
    }
}
