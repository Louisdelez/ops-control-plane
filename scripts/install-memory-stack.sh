#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

usage() {
  printf '%s\n' \
    'Usage: sudo scripts/install-memory-stack.sh [--enable] [--skip-image-pull]' \
    '' \
    'Installs the dependency-free Python memory service and a rootful, confined' \
    'Qdrant Podman container. Services remain disabled unless --enable is used.' \
    '--skip-image-pull requires the configured Qdrant image to exist locally.'
}

enable_services=0
pull_image=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --enable) enable_services=1 ;;
    --skip-image-pull) pull_image=0 ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; usage >&2; exit 64 ;;
  esac
  shift
done

if [[ ${EUID} -ne 0 ]]; then
  printf '%s\n' 'Run as root.' >&2
  exit 77
fi

repository_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
source_dir="$repository_dir/memory"
install_root=/opt/ops-memory
config_dir=/etc/ops-memory
state_dir=/var/lib/ops-memory
qdrant_state_dir=/var/lib/ops-memory-qdrant
unit_dir=/etc/systemd/system
openbao_ca_source=/etc/openbao.d/tls/ca.crt
image_tag=$(sed -n 's/^QDRANT_IMAGE=//p' "$source_dir/deploy/qdrant.env")

required_commands=(cp find flock getent install mktemp mv podman python3 sed stat systemctl systemd-analyze systemd-creds systemd-sysusers systemd-tmpfiles)
for command_name in "${required_commands[@]}"; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Required command is missing: %s\n' "$command_name" >&2
    exit 69
  fi
done
if [[ -z $image_tag || $image_tag != docker.io/qdrant/qdrant:v* ]]; then
  printf '%s\n' 'The Qdrant image tag is missing or not pinned to a version.' >&2
  exit 65
fi

required_files=(
  "$source_dir/config/memory.toml"
  "$source_dir/config/qdrant.yaml"
  "$source_dir/requirements-mcp.txt"
  "$source_dir/deploy/ops-memory"
  "$source_dir/deploy/ops-memory-mcp"
  "$source_dir/deploy/ops-memory-openbao-resolve"
  "$source_dir/deploy/ops-memory-openbao-preflight"
  "$source_dir/deploy/ops-memory-qdrant-launcher"
  "$source_dir/deploy/ops-memory-qdrant-render-config"
  "$source_dir/deploy/ops-memory.sysusers"
  "$source_dir/deploy/ops-memory.tmpfiles"
  "$repository_dir/scripts/provision-memory-openbao"
  "$repository_dir/config/openbao/policies/ops-memory-runtime.hcl"
  "$repository_dir/systemd/ops-memory-secrets.service"
  "$repository_dir/systemd/ops-memory.service"
  "$repository_dir/systemd/ops-memory-qdrant.service"
  "$repository_dir/systemd/ops-memory-maintenance.service"
  "$repository_dir/systemd/ops-memory-maintenance.timer"
  "$repository_dir/systemd/ops-memory-health.service"
  "$repository_dir/systemd/ops-memory-health.timer"
)
for path in "${required_files[@]}"; do
  if [[ ! -f $path || -L $path ]]; then
    printf 'Required source is missing or symlinked: %s\n' "$path" >&2
    exit 66
  fi
done
if [[ ! -d $source_dir/src/ops_memory || -L $source_dir/src/ops_memory ]]; then
  printf '%s\n' 'Memory Python source directory is missing or symlinked.' >&2
  exit 66
fi
if [[ ! -f $openbao_ca_source || -L $openbao_ca_source ]] ||
   [[ $(stat -Lc '%u' "$openbao_ca_source" 2>/dev/null) != 0 ]] ||
   [[ -n $(find "$openbao_ca_source" -maxdepth 0 -perm /022 -print -quit 2>/dev/null) ]]; then
  printf 'OpenBao CA source is unsafe: %s\n' "$openbao_ca_source" >&2
  exit 73
fi
unexpected_link=$(find "$source_dir/src/ops_memory" -type l -print -quit)
if [[ -n $unexpected_link ]]; then
  printf 'Python source must not contain symlinks: %s\n' "$unexpected_link" >&2
  exit 73
fi

install -d -o root -g root -m 0755 /run/lock
exec {install_lock}>/run/lock/ops-memory-install.lock
if ! flock -n "$install_lock"; then
  printf '%s\n' 'Another memory stack installation is running.' >&2
  exit 75
fi

for managed in "$install_root" "$config_dir" "$state_dir" "$qdrant_state_dir"; do
  if [[ -L $managed ]]; then
    printf 'Managed path must not be a symlink: %s\n' "$managed" >&2
    exit 73
  fi
done

credential_store=/etc/credstore.encrypted
legacy_credential="$credential_store/ops-memory-qdrant-api-key"
role_credential="$credential_store/ops-memory-openbao-role-id"
secret_credential="$credential_store/ops-memory-openbao-secret-id"
legacy_clear_key="$config_dir/qdrant-api-key"
if [[ -e $legacy_clear_key || -L $legacy_clear_key ]]; then
  printf 'Refusing legacy plaintext Qdrant credential: %s\n' "$legacy_clear_key" >&2
  exit 73
fi
if [[ -e $legacy_credential || -L $legacy_credential ]]; then
  printf '%s\n' \
    'The legacy Qdrant key blob must first be migrated with provision-memory-openbao.' >&2
  exit 78
fi
install -d -o root -g root -m 0700 "$credential_store"
role_exists=0
secret_exists=0
if [[ -e $role_credential || -L $role_credential ]]; then role_exists=1; fi
if [[ -e $secret_credential || -L $secret_credential ]]; then secret_exists=1; fi
if [[ $role_exists -ne $secret_exists ]]; then
  printf '%s\n' 'The encrypted memory AppRole credential set is incomplete.' >&2
  exit 73
fi
for credential_path in "$role_credential" "$secret_credential"; do
  if [[ -e $credential_path || -L $credential_path ]]; then
    credential_info=$(stat -Lc '%F:%u:%a' "$credential_path" 2>/dev/null || true)
    if [[ $credential_info != "regular file:0:600" || -L $credential_path ]]; then
      printf 'Unsafe encrypted memory AppRole credential: %s\n' "$credential_path" >&2
      exit 73
    fi
  fi
done
if [[ $enable_services -eq 1 && $role_exists -eq 0 ]]; then
  printf '%s\n' \
    'Provision OpenBao first with sudo /usr/local/sbin/provision-memory-openbao.' >&2
  exit 78
fi
if podman secret exists ops-memory-qdrant-key; then
  printf '%s\n' \
    'Refusing obsolete persistent Podman secret ops-memory-qdrant-key; migrate it explicitly.' >&2
  exit 73
fi

install -o root -g root -m 0644 "$source_dir/deploy/ops-memory.sysusers" /etc/sysusers.d/ops-memory.conf
install -o root -g root -m 0644 "$source_dir/deploy/ops-memory.tmpfiles" /etc/tmpfiles.d/ops-memory.conf
systemd-sysusers /etc/sysusers.d/ops-memory.conf
systemd-tmpfiles --create /etc/tmpfiles.d/ops-memory.conf

for account in opsmemory hermesd opsbroker zulipbridge ops-user minecraft-ops infra-network ops-monitor backup-agent deploy-agent security-audit; do
  if ! getent passwd "$account" >/dev/null; then
    printf 'Required account was not found: %s\n' "$account" >&2
    exit 67
  fi
done
if ! getent group ops-memory-api >/dev/null; then
  printf '%s\n' 'ops-memory-api group was not created.' >&2
  exit 67
fi
for group_member in opsmemory hermesd opsbroker zulipbridge ops-user minecraft-ops infra-network ops-monitor backup-agent deploy-agent security-audit; do
  if ! id -nG "$group_member" | tr ' ' '\n' | grep -Fxq ops-memory-api; then
    printf 'Expected memory API group membership was not created for %s.\n' "$group_member" >&2
    exit 67
  fi
done

stage="$install_root/.src.$$.stage"
previous="$install_root/.src.$$.previous"
source_swapped=0
memory_was_active=0
qdrant_was_active=0
secrets_was_active=0
timer_was_active=0
health_timer_was_active=0
if systemctl is-active --quiet ops-memory.service 2>/dev/null; then memory_was_active=1; fi
if systemctl is-active --quiet ops-memory-qdrant.service 2>/dev/null; then qdrant_was_active=1; fi
if systemctl is-active --quiet ops-memory-secrets.service 2>/dev/null; then secrets_was_active=1; fi
if systemctl is-active --quiet ops-memory-maintenance.timer 2>/dev/null; then timer_was_active=1; fi
if systemctl is-active --quiet ops-memory-health.timer 2>/dev/null; then health_timer_was_active=1; fi
cleanup() {
  local result=$?
  trap - EXIT
  if [[ $result -ne 0 && $source_swapped -eq 1 && -d $previous ]]; then
    if [[ -d $install_root/src ]]; then
      case "$install_root/src" in
        /opt/ops-memory/src) rm -rf -- "$install_root/src" ;;
        *) printf '%s\n' 'Refusing unsafe source rollback path.' >&2 ;;
      esac
    fi
    mv -- "$previous" "$install_root/src"
  fi
  for target in "$stage" "$previous"; do
    case "$target" in
      "$install_root"/.src.*.stage|"$install_root"/.src.*.previous)
        if [[ -d $target ]]; then rm -rf -- "$target"; fi
        ;;
      *) printf 'Refusing unsafe cleanup path: %s\n' "$target" >&2 ;;
    esac
  done
  if [[ $result -ne 0 ]]; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    if [[ $secrets_was_active -eq 1 ]]; then systemctl start ops-memory-secrets.service >/dev/null 2>&1 || true; fi
    if [[ $qdrant_was_active -eq 1 ]]; then systemctl start ops-memory-qdrant.service >/dev/null 2>&1 || true; fi
    if [[ $memory_was_active -eq 1 ]]; then systemctl start ops-memory.service >/dev/null 2>&1 || true; fi
    if [[ $timer_was_active -eq 1 ]]; then systemctl start ops-memory-maintenance.timer >/dev/null 2>&1 || true; fi
    if [[ $health_timer_was_active -eq 1 ]]; then systemctl start ops-memory-health.timer >/dev/null 2>&1 || true; fi
  fi
  exit "$result"
}
trap cleanup EXIT

systemctl stop ops-memory-maintenance.timer ops-memory-health.timer >/dev/null 2>&1 || true
systemctl stop ops-memory-maintenance.service ops-memory-health.service >/dev/null 2>&1 || true
systemctl stop ops-memory.service >/dev/null 2>&1 || true
systemctl stop ops-memory-qdrant.service >/dev/null 2>&1 || true
systemctl stop ops-memory-secrets.service >/dev/null 2>&1 || true

install -d -o root -g root -m 0755 "$install_root" "$install_root/bin" "$stage" "$config_dir"
cp -a -- "$source_dir/src/." "$stage/"
chown -R root:root "$stage"
find "$stage" -type d -exec chmod 0755 {} +
find "$stage" -type f -exec chmod 0644 {} +
if [[ -d $install_root/src ]]; then mv -- "$install_root/src" "$previous"; fi
mv -- "$stage" "$install_root/src"
source_swapped=1
install -o root -g root -m 0755 "$source_dir/deploy/ops-memory" "$install_root/bin/ops-memory"
install -o root -g root -m 0755 "$source_dir/deploy/ops-memory-mcp" "$install_root/bin/ops-memory-mcp"
install -o root -g root -m 0755 "$source_dir/deploy/ops-memory-openbao-resolve" "$install_root/bin/ops-memory-openbao-resolve"
install -o root -g root -m 0755 "$source_dir/deploy/ops-memory-openbao-preflight" "$install_root/bin/ops-memory-openbao-preflight"
install -o root -g root -m 0755 "$source_dir/deploy/ops-memory-qdrant-launcher" "$install_root/bin/ops-memory-qdrant-launcher"
install -o root -g root -m 0755 "$source_dir/deploy/ops-memory-qdrant-render-config" "$install_root/bin/ops-memory-qdrant-render-config"

mcp_venv="$install_root/mcp-venv"
if [[ -e $mcp_venv && ( ! -d $mcp_venv || -L $mcp_venv ) ]]; then
  printf '%s\n' 'Existing MCP virtual environment path is unsafe.' >&2
  exit 73
fi
if [[ ! -x $mcp_venv/bin/python ]]; then
  /usr/bin/python3 -m venv "$mcp_venv"
fi
foreign_mcp_file=$(find "$mcp_venv" -xdev ! -user root -print -quit)
if [[ -n $foreign_mcp_file ]]; then
  printf '%s\n' 'MCP virtual environment contains a non-root-owned entry.' >&2
  exit 73
fi
"$mcp_venv/bin/python" -m pip install \
  --disable-pip-version-check \
  --require-virtualenv \
  --upgrade \
  -r "$source_dir/requirements-mcp.txt"

install -o root -g opsmemory -m 0640 "$source_dir/config/memory.toml" "$config_dir/memory.toml"
install -o root -g root -m 0644 "$source_dir/config/qdrant.yaml" "$config_dir/qdrant.yaml"
install -o root -g root -m 0644 "$openbao_ca_source" "$config_dir/openbao-ca.crt"
install -d -o root -g root -m 0755 /usr/local/share/ops-control-plane/openbao/policies
install -o root -g root -m 0644 \
  "$repository_dir/config/openbao/policies/ops-memory-runtime.hcl" \
  /usr/local/share/ops-control-plane/openbao/policies/ops-memory-runtime.hcl
install -o root -g root -m 0750 "$repository_dir/scripts/provision-memory-openbao" \
  /usr/local/sbin/provision-memory-openbao

if [[ $pull_image -eq 1 ]]; then
  podman pull "$image_tag"
elif ! podman image exists "$image_tag"; then
  printf 'Qdrant image is not available locally: %s\n' "$image_tag" >&2
  exit 69
fi
raw_image_id=$(podman image inspect --format '{{.Id}}' "$image_tag")
if [[ $raw_image_id =~ ^sha256:([0-9a-f]{64})$ ]]; then
  image_id=$raw_image_id
elif [[ $raw_image_id =~ ^[0-9a-f]{64}$ ]]; then
  # Podman 5 on Fedora returns the OCI config digest without its algorithm
  # prefix.  Normalize it before persisting the immutable reference.
  image_id="sha256:$raw_image_id"
else
  printf '%s\n' 'Could not resolve Qdrant image to an immutable image ID.' >&2
  exit 70
fi
if ! podman image exists "$image_id"; then
  printf '%s\n' 'Resolved Qdrant image ID is not present in the local store.' >&2
  exit 70
fi
image_env=$(mktemp --tmpdir="$config_dir" .qdrant.env.XXXXXX)
chmod 0600 "$image_env"
printf 'QDRANT_IMAGE=%s\n' "$image_id" >"$image_env"
chown root:root "$image_env"
chmod 0644 "$image_env"
mv -f -- "$image_env" "$config_dir/qdrant.env"

for unit in ops-memory-secrets.service ops-memory.service ops-memory-qdrant.service ops-memory-maintenance.service ops-memory-maintenance.timer ops-memory-health.service ops-memory-health.timer; do
  install -o root -g root -m 0644 "$repository_dir/systemd/$unit" "$unit_dir/$unit"
done
systemd-analyze verify \
  "$unit_dir/ops-memory-secrets.service" \
  "$unit_dir/ops-memory.service" \
  "$unit_dir/ops-memory-qdrant.service" \
  "$unit_dir/ops-memory-maintenance.service" \
  "$unit_dir/ops-memory-maintenance.timer" \
  "$unit_dir/ops-memory-health.service" \
  "$unit_dir/ops-memory-health.timer"
PYTHONPATH="$install_root/src" /usr/bin/python3 -m ops_memory.cli check-config --config "$config_dir/memory.toml"
systemctl daemon-reload

if [[ $enable_services -eq 1 || $secrets_was_active -eq 1 || $qdrant_was_active -eq 1 || $memory_was_active -eq 1 ]]; then
  systemctl enable --now ops-memory-secrets.service
fi
if [[ $enable_services -eq 1 || $qdrant_was_active -eq 1 ]]; then
  systemctl enable --now ops-memory-qdrant.service
fi
if [[ $enable_services -eq 1 || $memory_was_active -eq 1 ]]; then
  systemctl enable --now ops-memory.service
fi
if [[ $enable_services -eq 1 || $timer_was_active -eq 1 ]]; then
  systemctl enable --now ops-memory-maintenance.timer
fi
if [[ $enable_services -eq 1 || $health_timer_was_active -eq 1 ]]; then
  systemctl enable --now ops-memory-health.timer
fi
if [[ $enable_services -eq 0 && $memory_was_active -eq 0 && $qdrant_was_active -eq 0 && $secrets_was_active -eq 0 ]]; then
  printf '%s\n' 'Installed but not enabled. Provision OpenBao, then re-run with --enable.'
fi

printf 'Installed ops-memory 0.1.0 with Qdrant image %s (%s).\n' "$image_tag" "$image_id"
