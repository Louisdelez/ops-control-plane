#!/usr/bin/env bash
set -Eeuo pipefail
# Record only the script, line and exit status; never command arguments or secrets.
atlas_report_exit() {
  if [[ $1 -ne 0 ]]; then
    logger -t atlas-installer -- "script=install-orchestrator line=$2 status=$1" || true
  fi
  return 0
}
trap 'atlas_report_exit "$?" "$LINENO"' EXIT
IFS=$'\n\t'

usage() {
  printf '%s\n' \
    'Usage: scripts/install-orchestrator.sh [--enable] [--replace-api-config]' \
    '' \
    'Installs the orchestrator, a Unix-socket API, and its metrics timer.' \
    'Existing config and environment files are preserved unless the explicit' \
    '--replace-api-config migration flag is used. That flag creates a private' \
    'backup before installing the API-first defaults. --enable starts services.'
}

enable_service=0
replace_api_config=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --enable)
      enable_service=1
      ;;
    --replace-api-config)
      replace_api_config=1
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 64
      ;;
  esac
  shift
done

if [[ ${EUID} -ne 0 ]]; then
  printf '%s\n' 'Run as root.' >&2
  exit 77
fi

repository_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
source_dir="$repository_dir/orchestrator"
install_root=/opt/ops-orchestrator
venv_dir="$install_root/venv"
config_dir=/etc/ops-orchestrator
state_dir=/var/lib/ops-orchestrator
unit_dir=/etc/systemd/system
documentation_dir=/usr/share/doc/ops-orchestrator
wrapper_dir=/usr/local/libexec/ops-orchestrator
binary_dir="$install_root/bin"
openbao_ca_source=/etc/openbao.d/tls/ca.crt
openbao_policy_dir=/usr/local/share/ops-control-plane/openbao/policies
model_catalogue_dir=/usr/local/share/ops-control-plane/catalog
model_catalogue_source_dir="$model_catalogue_dir/sources"
model_key_manager=/usr/local/libexec/ops-model-key-manager
model_reload_helper=/usr/local/libexec/ops-model-credentials-reload
model_reload_sudoers=/etc/sudoers.d/ops-model-credentials-reload
credential_store=/etc/credstore.encrypted

required_commands=(flock getent install mktemp python3 runuser stat systemctl systemd-analyze systemd-sysusers systemd-tmpfiles visudo)
for command_name in "${required_commands[@]}"; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Required command is missing: %s\n' "$command_name" >&2
    exit 69
  fi
done

required_files=(
  "$source_dir/pyproject.toml"
  "$source_dir/requirements-runtime.lock"
  "$source_dir/config/orchestrator.json"
  "$source_dir/config/orchestrator.env"
  "$source_dir/config/model-promotions.json"
  "$source_dir/deploy/ops-orchestrator.sysusers"
  "$source_dir/deploy/ops-orchestrator.tmpfiles"
  "$source_dir/deploy/ops-orchestrator-mcp-codex"
  "$source_dir/deploy/ops-orchestrator-mcp-claude"
  "$source_dir/deploy/ops-orchestrator-openbao-resolve"
  "$source_dir/deploy/ops-model-credentials-reload"
  "$source_dir/deploy/ops-model-credentials-reload.sudoers"
  "$repository_dir/scripts/provision-orchestrator-openbao"
  "$repository_dir/scripts/ops-model-key-manager"
  "$repository_dir/scripts/validate-model-catalog.py"
  "$repository_dir/scripts/validate-provider-integrations.py"
  "$repository_dir/config/openbao/policies/ops-orchestrator-runtime.hcl"
  "$repository_dir/catalog/model-catalog.v2.json"
  "$repository_dir/catalog/model-catalog.v2.schema.json"
  "$repository_dir/catalog/provider-integrations.v1.json"
  "$repository_dir/catalog/provider-integrations.v1.schema.json"
  "$repository_dir/catalog/sources/catalogue_modeles_IA_API_2026.txt"
  "$repository_dir/systemd/ops-orchestrator.service"
  "$repository_dir/systemd/ops-orchestrator-hermes-facade.service"
  "$repository_dir/systemd/ops-orchestrator-secrets.service"
  "$repository_dir/systemd/ops-orchestrator-provider-finance-daemon.service"
  "$repository_dir/systemd/ops-orchestrator-metrics.service"
  "$repository_dir/systemd/ops-orchestrator-metrics.timer"
  "$repository_dir/systemd/ops-orchestrator-provider-finance.service"
  "$repository_dir/systemd/ops-orchestrator-provider-finance.timer"
  "$repository_dir/docs/orchestrator-v1.md"
)
for source_file in "${required_files[@]}"; do
  if [[ ! -f $source_file || -L $source_file ]]; then
    printf 'Required regular source file is missing or symlinked: %s\n' "$source_file" >&2
    exit 66
  fi
done

for fixed_program in /usr/bin/python3 /usr/bin/sudo /usr/bin/systemd-ask-password; do
  if [[ ! -x $fixed_program ]]; then
    printf 'Required fixed executable is missing: %s\n' "$fixed_program" >&2
    exit 69
  fi
  fixed_uid=$(stat -Lc '%u' "$fixed_program")
  fixed_mode=$(stat -Lc '%a' "$fixed_program")
  if [[ $fixed_uid != 0 ]] || (( (8#$fixed_mode & 8#22) != 0 )); then
    printf 'Required fixed executable has unsafe ownership or mode: %s\n' "$fixed_program" >&2
    exit 73
  fi
done

PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -I -c \
  'from pathlib import Path; import sys; source = Path(sys.argv[1]); compile(source.read_bytes(), str(source), "exec")' \
  "$repository_dir/scripts/ops-model-key-manager"
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -I -c \
  'from pathlib import Path; import sys; source = Path(sys.argv[1]); compile(source.read_bytes(), str(source), "exec")' \
  "$source_dir/deploy/ops-model-credentials-reload"
visudo -cf "$source_dir/deploy/ops-model-credentials-reload.sudoers" >/dev/null
/usr/bin/python3 -I "$repository_dir/scripts/validate-model-catalog.py" \
  "$repository_dir/catalog/model-catalog.v2.json" \
  --source "$repository_dir/catalog/sources/catalogue_modeles_IA_API_2026.txt" >/dev/null
/usr/bin/python3 -I "$repository_dir/scripts/validate-provider-integrations.py" \
  "$repository_dir/catalog/provider-integrations.v1.json" >/dev/null
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$source_dir/src" /usr/bin/python3 \
  -m ops_orchestrator.cli check-config \
  --config "$source_dir/config/orchestrator.json" \
  --catalogue "$repository_dir/catalog/model-catalog.v2.json" \
  --provider-integrations \
  "$repository_dir/catalog/provider-integrations.v1.json" >/dev/null

# Refuse an incompatible preserved configuration before the first managed
# filesystem mutation or service stop. Schema upgrades must use the explicit
# replacement path, which also creates the private backup below.
if [[ $replace_api_config -eq 0 && -e $config_dir/config.json ]]; then
  if [[ ! -f $config_dir/config.json || -L $config_dir/config.json ]]; then
    printf '%s\n' 'Existing configuration path is unsafe.' >&2
    exit 73
  fi
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$source_dir/src" /usr/bin/python3 \
    -m ops_orchestrator.cli check-config \
    --config "$config_dir/config.json" \
    --catalogue "$repository_dir/catalog/model-catalog.v2.json" \
    --provider-integrations \
    "$repository_dir/catalog/provider-integrations.v1.json" >/dev/null || {
      printf '%s\n' \
        'Existing configuration is incompatible; rerun with --replace-api-config.' >&2
      exit 78
    }
fi

if [[ ! -f $openbao_ca_source || -L $openbao_ca_source ]]; then
  printf 'Pinned local OpenBao CA is missing or symlinked: %s\n' "$openbao_ca_source" >&2
  exit 66
fi
openbao_ca_uid=$(stat -Lc '%u' "$openbao_ca_source")
openbao_ca_mode=$(stat -Lc '%a' "$openbao_ca_source")
if [[ $openbao_ca_uid != 0 ]] || (( (8#$openbao_ca_mode & 8#22) != 0 )); then
  printf '%s\n' 'Pinned local OpenBao CA has unsafe ownership or mode.' >&2
  exit 73
fi

for account in ops-user hermesd minecraft-ops infra-network ops-monitor backup-agent deploy-agent security-audit; do
  if ! getent passwd "$account" >/dev/null; then
    printf 'Required local account is absent: %s\n' "$account" >&2
    exit 67
  fi
done
if ! getent group node-exporter >/dev/null; then
  printf '%s\n' 'Required node-exporter group is absent.' >&2
  exit 67
fi

install -d -o root -g root -m 0755 /run/lock
exec {installation_lock}>/run/lock/ops-orchestrator-install.lock
if ! flock -n "$installation_lock"; then
  printf '%s\n' 'Another orchestrator installation is running.' >&2
  exit 75
fi

for managed_path in "$install_root" "$config_dir" "$state_dir" "$documentation_dir" "$wrapper_dir" "$openbao_policy_dir" "$model_catalogue_dir" "$model_catalogue_source_dir"; do
  if [[ -L $managed_path ]]; then
    printf 'Managed path must not be a symbolic link: %s\n' "$managed_path" >&2
    exit 73
  fi
done
for helper_target in "$model_key_manager" "$model_reload_helper" "$model_reload_sudoers"; do
  if [[ -L $helper_target ]]; then
    printf 'Credential helper target must not be a symbolic link: %s\n' "$helper_target" >&2
    exit 73
  fi
done

install -o root -g root -m 0644 "$source_dir/deploy/ops-orchestrator.sysusers" \
  /etc/sysusers.d/ops-orchestrator.conf
install -o root -g root -m 0644 "$source_dir/deploy/ops-orchestrator.tmpfiles" \
  /etc/tmpfiles.d/ops-orchestrator.conf
systemd-sysusers /etc/sysusers.d/ops-orchestrator.conf
systemd-tmpfiles --create /etc/tmpfiles.d/ops-orchestrator.conf

if ! getent passwd opsfinance >/dev/null || ! getent group opsfinance >/dev/null; then
  printf '%s\n' 'Dedicated provider-finance identity was not created.' >&2
  exit 67
fi
if [[ $(id -gn opsfinance) != opsfinance ]]; then
  printf '%s\n' 'Provider-finance primary group is invalid.' >&2
  exit 67
fi
if [[ $(id -u opsfinance) == $(id -u opsorchestrator) ]]; then
  printf '%s\n' 'Provider-finance and orchestrator UIDs must be distinct.' >&2
  exit 67
fi
finance_group_record=$(getent group opsfinance)
IFS=: read -r finance_group_name _ finance_group_gid finance_group_members \
  <<<"$finance_group_record"
if [[ $finance_group_name != opsfinance || -z $finance_group_gid ]]; then
  printf '%s\n' 'Provider-finance socket group record is malformed.' >&2
  exit 67
fi
if [[ $finance_group_members != opsorchestrator ]]; then
  printf '%s\n' 'Provider-finance socket group must contain only opsorchestrator.' >&2
  exit 67
fi
while IFS=: read -r account_name _ _ primary_gid _; do
  if [[ $primary_gid == "$finance_group_gid" && $account_name != opsfinance ]]; then
    printf 'Provider-finance group is unexpectedly primary for %s.\n' \
      "$account_name" >&2
    exit 67
  fi
done < <(getent passwd)

for group_member in ops-user hermesd opsorchestrator minecraft-ops infra-network ops-monitor backup-agent deploy-agent security-audit; do
  if ! id -nG "$group_member" | tr ' ' '\n' | grep -Fxq opsorchestrator-api; then
    printf 'Expected API group membership was not created for %s.\n' "$group_member" >&2
    exit 67
  fi
done

install -d -o root -g root -m 0755 \
  "$install_root" "$binary_dir" "$documentation_dir" "$wrapper_dir" "$openbao_policy_dir" "$model_catalogue_dir" "$model_catalogue_source_dir"
install -d -o root -g node-exporter -m 0750 /var/lib/node-exporter/textfile
install -o root -g root -m 0644 "$repository_dir/catalog/model-catalog.v2.json" \
  "$model_catalogue_dir/model-catalog.v2.json"
install -o root -g root -m 0644 "$repository_dir/catalog/model-catalog.v2.schema.json" \
  "$model_catalogue_dir/model-catalog.v2.schema.json"
install -o root -g root -m 0644 \
  "$repository_dir/catalog/provider-integrations.v1.json" \
  "$model_catalogue_dir/provider-integrations.v1.json"
install -o root -g root -m 0644 \
  "$repository_dir/catalog/provider-integrations.v1.schema.json" \
  "$model_catalogue_dir/provider-integrations.v1.schema.json"
install -o root -g root -m 0644 \
  "$repository_dir/catalog/sources/catalogue_modeles_IA_API_2026.txt" \
  "$model_catalogue_source_dir/catalogue_modeles_IA_API_2026.txt"
install -o root -g root -m 0755 "$repository_dir/scripts/ops-model-key-manager" \
  "$model_key_manager"
install -o root -g root -m 0755 \
  "$source_dir/deploy/ops-model-credentials-reload" "$model_reload_helper"
install -o root -g root -m 0440 \
  "$source_dir/deploy/ops-model-credentials-reload.sudoers" "$model_reload_sudoers"
visudo -cf "$model_reload_sudoers" >/dev/null

# This check runs before the service is stopped. Keep it as root because the
# repository may live below a private home directory that opsorchestrator
# cannot traverse; the installed copy is checked as the service account below.
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$source_dir/src" python3 \
  -m ops_orchestrator.cli check-config \
  --config "$source_dir/config/orchestrator.json" >/dev/null
if [[ $replace_api_config -eq 1 ]]; then
  for existing_path in "$config_dir/config.json" "$config_dir/orchestrator.env"; do
    if [[ -e $existing_path && (! -f $existing_path || -L $existing_path) ]]; then
      printf 'Existing configuration path is unsafe: %s\n' "$existing_path" >&2
      exit 73
    fi
  done
fi

systemctl stop ops-orchestrator-provider-finance.timer \
  ops-orchestrator-provider-finance.service hermes-gateway.service \
  ops-orchestrator-hermes-facade.service ops-orchestrator.service \
  ops-orchestrator-provider-finance-daemon.service \
  ops-orchestrator-secrets.service \
  >/dev/null 2>&1 || true
if [[ ! -x $venv_dir/bin/python ]]; then
  python3 -m venv "$venv_dir"
fi
"$venv_dir/bin/python" -m pip install \
  --disable-pip-version-check \
  --require-virtualenv \
  --upgrade \
  --no-deps \
  --only-binary=:all: \
  -r "$source_dir/requirements-runtime.lock"
# setuptools writes build metadata into its source. Keep the reviewed tree intact.
(
  build_source=$(mktemp -d)
  trap 'rm -rf -- "$build_source"' EXIT
  cp -a "$source_dir/." "$build_source/"
  "$venv_dir/bin/python" -m pip install \
    --disable-pip-version-check \
    --require-virtualenv \
    --no-build-isolation \
    --no-deps \
    --upgrade \
    "${build_source}[mcp]"
)
PYTHONDONTWRITEBYTECODE=1 "$venv_dir/bin/python" - \
  "$source_dir/requirements-runtime.lock" <<'PY'
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys

lock = Path(sys.argv[1])
for line_number, raw in enumerate(lock.read_text(encoding="utf-8").splitlines(), 1):
    requirement = raw.strip()
    if not requirement or requirement.startswith("#"):
        continue
    name, separator, expected = requirement.partition("==")
    if separator != "==" or not name or not expected or "==" in expected:
        raise SystemExit(f"invalid runtime lock line {line_number}")
    try:
        actual = version(name)
    except PackageNotFoundError:
        raise SystemExit(f"locked runtime package is absent: {name}") from None
    if actual != expected:
        raise SystemExit(
            f"locked runtime package differs: {name}={actual}, expected {expected}"
        )
PY
"$venv_dir/bin/python" -m pip check
chown -R root:root "$venv_dir"
# Code must remain readable by the service user even under bootstrap umask 077.
chmod -R a+rX,go-w "$venv_dir"
install -o root -g root -m 0755 "$source_dir/deploy/ops-orchestrator-mcp-codex" \
  "$wrapper_dir/ops-orchestrator-mcp-codex"
install -o root -g root -m 0755 "$source_dir/deploy/ops-orchestrator-mcp-claude" \
  "$wrapper_dir/ops-orchestrator-mcp-claude"
install -o root -g root -m 0755 "$source_dir/deploy/ops-orchestrator-openbao-resolve" \
  "$binary_dir/ops-orchestrator-openbao-resolve"
install -o root -g root -m 0755 "$repository_dir/scripts/provision-orchestrator-openbao" \
  /usr/local/sbin/provision-orchestrator-openbao
install -o root -g root -m 0644 \
  "$repository_dir/config/openbao/policies/ops-orchestrator-runtime.hcl" \
  "$openbao_policy_dir/ops-orchestrator-runtime.hcl"
install -o root -g root -m 0644 "$openbao_ca_source" "$config_dir/openbao-ca.crt"

if [[ $replace_api_config -eq 1 ]]; then
  backup_dir=$(mktemp -d "$config_dir/.pre-api-first.XXXXXXXX")
  chown root:root "$backup_dir"
  chmod 0700 "$backup_dir"
  if [[ -f $config_dir/config.json ]]; then
    install -o root -g root -m 0600 "$config_dir/config.json" "$backup_dir/config.json"
  fi
  if [[ -f $config_dir/orchestrator.env ]]; then
    install -o root -g root -m 0600 "$config_dir/orchestrator.env" "$backup_dir/orchestrator.env"
  fi
  install -o root -g opsorchestrator -m 0640 \
    "$source_dir/config/orchestrator.json" "$config_dir/config.json"
  install -o root -g opsorchestrator -m 0640 \
    "$source_dir/config/orchestrator.env" "$config_dir/orchestrator.env"
  printf 'Previous runtime configuration backed up in %s\n' "$backup_dir"
elif [[ ! -e $config_dir/config.json ]]; then
  install -o root -g opsorchestrator -m 0640 \
    "$source_dir/config/orchestrator.json" "$config_dir/config.json"
elif [[ ! -f $config_dir/config.json || -L $config_dir/config.json ]]; then
  printf '%s\n' 'Existing configuration path is unsafe.' >&2
  exit 73
fi
if [[ ! -e $config_dir/orchestrator.env ]]; then
  install -o root -g opsorchestrator -m 0640 \
    "$source_dir/config/orchestrator.env" "$config_dir/orchestrator.env"
elif [[ ! -f $config_dir/orchestrator.env || -L $config_dir/orchestrator.env ]]; then
  printf '%s\n' 'Existing environment path is unsafe.' >&2
  exit 73
fi
if [[ ! -e $config_dir/model-promotions.json ]]; then
  install -o root -g opsorchestrator -m 0640 \
    "$source_dir/config/model-promotions.json" "$config_dir/model-promotions.json"
elif [[ ! -f $config_dir/model-promotions.json || -L $config_dir/model-promotions.json ]]; then
  printf '%s\n' 'Existing model promotion registry path is unsafe.' >&2
  exit 73
fi

install -o root -g root -m 0644 "$repository_dir/docs/orchestrator-v1.md" \
  "$documentation_dir/orchestrator-v1.md"
install -o root -g root -m 0644 "$repository_dir/systemd/ops-orchestrator.service" \
  "$unit_dir/ops-orchestrator.service"
install -o root -g root -m 0644 \
  "$repository_dir/systemd/ops-orchestrator-hermes-facade.service" \
  "$unit_dir/ops-orchestrator-hermes-facade.service"
install -o root -g root -m 0644 \
  "$repository_dir/systemd/ops-orchestrator-secrets.service" \
  "$unit_dir/ops-orchestrator-secrets.service"
install -o root -g root -m 0644 \
  "$repository_dir/systemd/ops-orchestrator-provider-finance-daemon.service" \
  "$unit_dir/ops-orchestrator-provider-finance-daemon.service"
install -o root -g root -m 0644 "$repository_dir/systemd/ops-orchestrator-metrics.service" \
  "$unit_dir/ops-orchestrator-metrics.service"
install -o root -g root -m 0644 "$repository_dir/systemd/ops-orchestrator-metrics.timer" \
  "$unit_dir/ops-orchestrator-metrics.timer"
install -o root -g root -m 0644 \
  "$repository_dir/systemd/ops-orchestrator-provider-finance.service" \
  "$unit_dir/ops-orchestrator-provider-finance.service"
install -o root -g root -m 0644 \
  "$repository_dir/systemd/ops-orchestrator-provider-finance.timer" \
  "$unit_dir/ops-orchestrator-provider-finance.timer"

runuser -u opsorchestrator -- "$venv_dir/bin/ops-orchestrator" check-config \
  --config "$config_dir/config.json" >/dev/null
systemd-analyze verify \
  "$unit_dir/ops-orchestrator-secrets.service" \
  "$unit_dir/ops-orchestrator-provider-finance-daemon.service" \
  "$unit_dir/ops-orchestrator.service" \
  "$unit_dir/ops-orchestrator-hermes-facade.service" \
  "$unit_dir/ops-orchestrator-metrics.service" \
  "$unit_dir/ops-orchestrator-metrics.timer" \
  "$unit_dir/ops-orchestrator-provider-finance.service" \
  "$unit_dir/ops-orchestrator-provider-finance.timer" >/dev/null
systemctl daemon-reload

if [[ $enable_service -eq 1 ]]; then
  for credential_name in ops-orchestrator-openbao-role-id ops-orchestrator-openbao-secret-id; do
    credential_path="$credential_store/$credential_name"
    if [[ ! -f $credential_path || -L $credential_path ]]; then
      printf 'Activation blocked: provision encrypted OpenBao credential %s first.\n' \
        "$credential_name" >&2
      exit 78
    fi
    credential_uid=$(stat -Lc '%u' "$credential_path")
    credential_mode=$(stat -Lc '%a' "$credential_path")
    if [[ $credential_uid != 0 ]] || (( (8#$credential_mode & 8#77) != 0 )); then
      printf 'Activation blocked: encrypted credential %s has unsafe ownership or mode.\n' \
        "$credential_name" >&2
      exit 78
    fi
  done
  systemctl enable --now ops-orchestrator.service
  systemctl enable --now ops-orchestrator-metrics.timer
  systemctl enable --now ops-orchestrator-provider-finance.timer
else
  printf '%s\n' \
    'Installed but not enabled. Validate provider models, current prices and' \
    'OpenBao-backed credentials with provision-orchestrator-openbao, then run:' \
    '  systemctl enable --now ops-orchestrator.service ops-orchestrator-metrics.timer ops-orchestrator-provider-finance.timer' \
    'The budgeted Hermes facade starts only when hermes-gateway.service requires it.'
fi
