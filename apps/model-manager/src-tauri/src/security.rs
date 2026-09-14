use crate::{catalogue, CommandError};
use serde::{Deserialize, Serialize};
use std::fs;
use std::io::{Read, Write};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use zeroize::Zeroizing;

#[cfg(unix)]
use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};

const KEY_MANAGER: &str = "/usr/local/libexec/ops-model-key-manager";
const XDG_OPEN: &str = "/usr/bin/xdg-open";
const ZULIP_URL: &str = "https://zulip.ops.local:8443";
const TRUSTED_DIRECTORIES: [&str; 3] = ["/usr", "/usr/local", "/usr/local/libexec"];
const MAX_HELPER_RESULT_BYTES: u64 = 256;
static CREDENTIAL_CHANGE_RUNNING: AtomicBool = AtomicBool::new(false);

#[derive(Debug, Serialize)]
pub struct SaveProviderCredentialStatus {
    status: &'static str,
    provider_account_id: Option<String>,
    error_code: Option<&'static str>,
}

#[derive(Debug, Serialize)]
pub struct OpenZulipStatus {
    status: &'static str,
}

struct CredentialOperationGuard;

impl Drop for CredentialOperationGuard {
    fn drop(&mut self) {
        CREDENTIAL_CHANGE_RUNNING.store(false, Ordering::Release);
    }
}

fn valid_openbao_password(value: &str) -> bool {
    (14..=100).contains(&value.chars().count())
        && value == value.trim()
        && !value.chars().any(char::is_control)
}

fn valid_api_key(value: &str) -> bool {
    (8..=4096).contains(&value.len()) && value.bytes().all(|byte| (0x21..=0x7e).contains(&byte))
}

fn append_key_frame(output: &mut Vec<u8>, value: &[u8]) -> Result<(), CommandError> {
    let length = u32::try_from(value.len()).map_err(|_| {
        CommandError::new(
            "credential_invalid",
            "Les identifiants saisis ne respectent pas les limites locales.",
        )
    })?;
    output.extend_from_slice(&length.to_be_bytes());
    output.extend_from_slice(value);
    Ok(())
}

fn build_key_frame(
    openbao_password: &str,
    api_key: &str,
) -> Result<Zeroizing<Vec<u8>>, CommandError> {
    let mut frame = Zeroizing::new(Vec::with_capacity(
        10 + openbao_password.len() + api_key.len() + 8,
    ));
    frame.extend_from_slice(b"ATLASKEY1\n");
    append_key_frame(&mut frame, openbao_password.as_bytes())?;
    append_key_frame(&mut frame, api_key.as_bytes())?;
    Ok(frame)
}

pub async fn save_provider_credential(
    provider_account_id: String,
    openbao_password: String,
    api_key: String,
) -> Result<SaveProviderCredentialStatus, CommandError> {
    let openbao_password = Zeroizing::new(openbao_password);
    let api_key = Zeroizing::new(api_key);
    if !is_provider_identifier(&provider_account_id)
        || !(catalogue::provider_exists(&provider_account_id) || matches!(provider_account_id.as_str(), "siliconflow" | "voyage" | "jina"))
    {
        return Err(CommandError::new(
            "invalid_provider_account_id",
            "Ce compte fournisseur n'est pas valide.",
        ));
    }
    if !valid_openbao_password(&openbao_password) || !valid_api_key(&api_key) {
        return Err(CommandError::new(
            "credential_invalid",
            "Les identifiants saisis ne respectent pas les limites locales.",
        ));
    }

    #[cfg(not(unix))]
    return Ok(SaveProviderCredentialStatus {
        status: "failed",
        provider_account_id: None,
        error_code: Some("unsupported_platform"),
    });

    #[cfg(unix)]
    {
        match verify_helper() {
            HelperState::Missing => {
                return Ok(SaveProviderCredentialStatus {
                    status: "failed",
                    provider_account_id: None,
                    error_code: Some("helper_unavailable"),
                });
            }
            HelperState::Unsafe => {
                return Ok(SaveProviderCredentialStatus {
                    status: "failed",
                    provider_account_id: None,
                    error_code: Some("helper_unsafe"),
                });
            }
            HelperState::Ready => {}
        }
        if CREDENTIAL_CHANGE_RUNNING
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return Ok(SaveProviderCredentialStatus {
                status: "failed",
                provider_account_id: Some(provider_account_id),
                error_code: Some("already_running"),
            });
        }
        let provider_for_worker = provider_account_id.clone();
        tauri::async_runtime::spawn_blocking(move || {
            let _operation_guard = CredentialOperationGuard;
            let frame = build_key_frame(&openbao_password, &api_key)?;
            let mut command = Command::new(KEY_MANAGER);
            command
                .arg(if cfg!(feature="native-desktop") {"store-stdin"} else {"set-stdin"})
                .arg(&provider_for_worker)
                .env_clear()
                .env("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
                .env("LANG", "C.UTF-8")
                .env("LC_ALL", "C.UTF-8")
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null());
            add_bounded_session_environment(&mut command);
            let mut child = command.spawn().map_err(|_| {
                CommandError::new(
                    "credential_helper_failed",
                    "Le gestionnaire local de clés n'a pas démarré.",
                )
            })?;
            let write_result = child
                .stdin
                .take()
                .ok_or_else(|| {
                    CommandError::new(
                        "credential_helper_failed",
                        "Le canal local sécurisé est indisponible.",
                    )
                })
                .and_then(|mut stdin| {
                    stdin.write_all(&frame).map_err(|_| {
                        CommandError::new(
                            "credential_helper_failed",
                            "Le canal local sécurisé a échoué.",
                        )
                    })
                });
            drop(frame);
            // Do not retain either WebView-originated secret while OpenBao and
            // the consumer reload complete in the helper.
            drop(openbao_password);
            drop(api_key);
            if let Err(error) = write_result {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
            let mut output = Vec::new();
            let read_result = child
                .stdout
                .take()
                .ok_or_else(|| {
                    CommandError::new(
                        "credential_helper_failed",
                        "Le résultat local est indisponible.",
                    )
                })
                .and_then(|stdout| {
                    stdout
                        .take(MAX_HELPER_RESULT_BYTES + 1)
                        .read_to_end(&mut output)
                        .map_err(|_| {
                            CommandError::new(
                                "credential_helper_failed",
                                "Le résultat local est illisible.",
                            )
                        })
                });
            if read_result.is_err() || output.len() as u64 > MAX_HELPER_RESULT_BYTES {
                let _ = child.kill();
                let _ = child.wait();
                return Ok(SaveProviderCredentialStatus {
                    status: "failed",
                    provider_account_id: Some(provider_for_worker),
                    error_code: Some("invalid_helper_result"),
                });
            }
            let exit_code = child.wait().ok().and_then(|status| status.code());
            let status = parse_helper_result_for(&output, exit_code, &provider_for_worker);
            Ok(SaveProviderCredentialStatus {
                status: if status == "rotated" || status == "stored" {
                    "saved"
                } else {
                    "failed"
                },
                provider_account_id: Some(provider_for_worker),
                error_code: match status {
                    "rotated" | "stored" => None,
                    "cancelled" => Some("cancelled"),
                    "failed" => Some("credential_write_failed"),
                    _ => Some("invalid_helper_result"),
                },
            })
        })
        .await
        .map_err(|_| {
            CREDENTIAL_CHANGE_RUNNING.store(false, Ordering::Release);
            CommandError::new(
                "credential_worker_failed",
                "Le gestionnaire local de clés s'est interrompu.",
            )
        })?
    }
}

pub fn open_zulip() -> Result<OpenZulipStatus, CommandError> {
    #[cfg(not(unix))]
    return Ok(OpenZulipStatus {
        status: "unsupported_platform",
    });

    #[cfg(unix)]
    {
        let metadata = fs::symlink_metadata(XDG_OPEN).map_err(|_| {
            CommandError::new(
                "browser_unavailable",
                "Le navigateur système est indisponible.",
            )
        })?;
        if metadata.file_type().is_symlink()
            || !metadata.file_type().is_file()
            || metadata.uid() != 0
            || metadata.permissions().mode() & 0o022 != 0
            || metadata.permissions().mode() & 0o111 == 0
        {
            return Err(CommandError::new(
                "browser_unsafe",
                "Le lanceur du navigateur système n'est pas sûr.",
            ));
        }
        let mut command = Command::new(XDG_OPEN);
        command
            .arg(ZULIP_URL)
            .env_clear()
            .env("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")
            .env("LANG", "C.UTF-8")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null());
        add_bounded_session_environment(&mut command);
        let mut child = command.spawn().map_err(|_| {
            CommandError::new(
                "browser_start_failed",
                "Le navigateur système n'a pas démarré.",
            )
        })?;
        let _ = std::thread::Builder::new()
            .name("atlas-browser-launch".to_owned())
            .spawn(move || {
                let _ = child.wait();
            });
        Ok(OpenZulipStatus { status: "opened" })
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct HelperResult {
    status: String,
    provider_account_id: Option<String>,
}

fn parse_helper_result_for(
    output: &[u8],
    exit_code: Option<i32>,
    expected_provider: &str,
) -> &'static str {
    if output.is_empty() || output.len() > MAX_HELPER_RESULT_BYTES as usize {
        return "invalid_helper_result";
    }
    let Ok(result) = serde_json::from_slice::<HelperResult>(output) else {
        return "invalid_helper_result";
    };
    if result.provider_account_id.as_deref() != Some(expected_provider) {
        return "invalid_helper_result";
    }
    match (exit_code, result.status.as_str()) {
        (Some(0), "rotated") => "rotated",
        (Some(0), "stored") if cfg!(feature="native-desktop") => "stored",
        (Some(2), "cancelled") => "cancelled",
        (Some(1), "failed") => "failed",
        _ => "invalid_helper_result",
    }
}

#[cfg(unix)]
#[derive(Clone, Copy)]
enum HelperState {
    Ready,
    Missing,
    Unsafe,
}

#[cfg(unix)]
fn verify_helper() -> HelperState {
    for path in TRUSTED_DIRECTORIES {
        let Ok(metadata) = fs::symlink_metadata(path) else {
            return HelperState::Missing;
        };
        if metadata.file_type().is_symlink()
            || !metadata.file_type().is_dir()
            || metadata.uid() != 0
            || metadata.permissions().mode() & 0o022 != 0
        {
            return HelperState::Unsafe;
        }
    }

    let metadata = match fs::symlink_metadata(KEY_MANAGER) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return HelperState::Missing,
        Err(_) => return HelperState::Unsafe,
    };
    if metadata.file_type().is_symlink()
        || !metadata.file_type().is_file()
        || metadata.file_type().is_socket()
        || metadata.uid() != 0
        || metadata.permissions().mode() & 0o022 != 0
        || metadata.permissions().mode() & 0o111 == 0
    {
        return HelperState::Unsafe;
    }
    HelperState::Ready
}

#[cfg(unix)]
pub(crate) fn add_bounded_session_environment(command: &mut Command) {
    for name in ["DISPLAY", "WAYLAND_DISPLAY"] {
        if let Ok(value) = std::env::var(name) {
            if !value.is_empty()
                && value.len() <= 128
                && !value
                    .bytes()
                    .any(|byte| matches!(byte, b'\0' | b'\r' | b'\n'))
            {
                command.env(name, value);
            }
        }
    }

    // Derive the session runtime directory from the kernel-owned proc entry,
    // never from an inherited UID or HOME value.
    if let Ok(process_metadata) = fs::metadata("/proc/self") {
        let uid = process_metadata.uid();
        let runtime_directory = format!("/run/user/{uid}");
        if let Ok(metadata) = fs::symlink_metadata(&runtime_directory) {
            if !metadata.file_type().is_symlink()
                && metadata.file_type().is_dir()
                && metadata.uid() == uid
                && metadata.permissions().mode() & 0o077 == 0
            {
                command.env("XDG_RUNTIME_DIR", &runtime_directory);
                let bus = format!("{runtime_directory}/bus");
                if fs::symlink_metadata(&bus).is_ok_and(|value| value.file_type().is_socket()) {
                    command.env("DBUS_SESSION_BUS_ADDRESS", format!("unix:path={bus}"));
                }
            }
        }
    }
}

pub(crate) fn is_provider_identifier(value: &str) -> bool {
    let bytes = value.as_bytes();
    (1..=128).contains(&bytes.len())
        && (bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
        && bytes.iter().skip(1).all(|byte| {
            byte.is_ascii_lowercase() || byte.is_ascii_digit() || matches!(*byte, b'_' | b'-')
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn provider_argument_is_a_single_bounded_identifier() {
        assert!(is_provider_identifier("mistral-ai"));
        assert!(!is_provider_identifier("Mistral"));
        assert!(!is_provider_identifier("../../bin/sh"));
        assert!(!is_provider_identifier("deepseek --help"));
        assert!(!is_provider_identifier("deepseek\nopenai"));
    }

    #[test]
    fn only_catalogued_providers_can_reach_the_helper_boundary() {
        assert!(catalogue::provider_exists("deepseek"));
        assert!(!catalogue::provider_exists("unlisted-provider"));
    }

    #[test]
    fn key_frame_is_exact_and_has_no_text_serialization() {
        let frame = build_key_frame("openbao-secret-value", "api-key-secret-value")
            .expect("bounded key frame");
        assert!(frame.starts_with(b"ATLASKEY1\n"));
        let password_length = u32::from_be_bytes(frame[10..14].try_into().expect("u32"));
        assert_eq!(password_length, 20);
        assert!(!std::str::from_utf8(&frame).is_ok_and(|value| value.starts_with('{')));
    }

    #[test]
    fn credential_fields_share_the_reviewed_bounds() {
        assert!(valid_openbao_password("correct horse battery staple"));
        assert!(!valid_openbao_password("too-short"));
        assert!(!valid_openbao_password(" leading-password-value"));
        assert!(valid_api_key("api-key-12345678"));
        assert!(!valid_api_key("contains space"));
        assert!(!valid_api_key(&"x".repeat(4097)));
    }

    #[test]
    fn stdin_result_must_echo_the_exact_provider() {
        assert_eq!(
            parse_helper_result_for(
                b"{\"provider_account_id\":\"openai\",\"status\":\"rotated\"}\n",
                Some(0),
                "openai"
            ),
            "rotated"
        );
        assert_eq!(
            parse_helper_result_for(
                b"{\"provider_account_id\":\"deepseek\",\"status\":\"rotated\"}\n",
                Some(0),
                "openai"
            ),
            "invalid_helper_result"
        );
    }
}
