#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

usage() {
  cat <<'EOF'
Usage: sudo scripts/install-ops-v1.sh [--enable-observation] [--replace-managed-configs]

Installs the reviewed, local Ops V1 primitives. Remote management, release
targets, configuration targets and retention remain disabled because the
repository registries contain no enabled target. --enable-observation enables
only the read-only check and daily-report timers.
EOF
}

enable_observation=0
replace_managed_configs=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --enable-observation) enable_observation=1 ;;
    --replace-managed-configs) replace_managed_configs=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
  shift
done

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 77
fi

repository_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
readonly repository_dir

required_commands=(flock getent install python3 stat systemctl systemd-analyze)
for command_name in "${required_commands[@]}"; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is missing: $command_name" >&2
    exit 69
  }
done

sources=(
  "$repository_dir/scripts/ops-v1"
  "$repository_dir/runbooks/contracts/catalog.json"
  "$repository_dir/inventory/ops-v1.json"
  "$repository_dir/inventory/release-targets.json"
  "$repository_dir/inventory/config-targets.json"
  "$repository_dir/inventory/mission-priorities.json"
  "$repository_dir/monitoring/ops-v1-retention.json"
  "$repository_dir/config/hermes/skill-registry.json"
  "$repository_dir/systemd/ops-v1-observe.service"
  "$repository_dir/systemd/ops-v1-observe.timer"
  "$repository_dir/systemd/ops-v1-report.service"
  "$repository_dir/systemd/ops-v1-report.timer"
)
for source_path in "${sources[@]}"; do
  if [[ ! -f $source_path || -L $source_path ]]; then
    echo "Required source is absent, not regular, or linked: $source_path" >&2
    exit 66
  fi
  source_mode=$(stat -Lc '%a' -- "$source_path")
  if (( (8#$source_mode & 022) != 0 )); then
    echo "Source is writable by group or others: $source_path" >&2
    exit 73
  fi
done

for identity in ops-monitor node-exporter; do
  getent passwd "$identity" >/dev/null || {
    echo "Required service identity is missing: $identity" >&2
    exit 67
  }
done

python3 -m py_compile "$repository_dir/scripts/ops-v1"
python3 "$repository_dir/scripts/ops-v1" contracts-validate \
  --catalog "$repository_dir/runbooks/contracts/catalog.json" >/dev/null
python3 "$repository_dir/scripts/ops-v1" inventory-validate \
  --inventory "$repository_dir/inventory/ops-v1.json" >/dev/null
python3 "$repository_dir/scripts/ops-v1" skills-validate \
  --registry "$repository_dir/config/hermes/skill-registry.json" \
  --skills-root "$repository_dir/config/hermes/skills" \
  --repository-root "$repository_dir" >/dev/null
python3 "$repository_dir/scripts/ops-v1" retention-plan \
  --policy "$repository_dir/monitoring/ops-v1-retention.json" >/dev/null
install -d -o root -g root -m 0755 /run/lock
exec {install_lock}>/run/lock/ops-v1-install.lock
if ! flock -n "$install_lock"; then
  echo "Another Ops V1 installation is running." >&2
  exit 75
fi

for managed in /etc/ops-v1 /opt/ops-v1 /var/lib/ops-v1; do
  if [[ -L $managed ]]; then
    echo "Managed path must not be a symbolic link: $managed" >&2
    exit 73
  fi
done

install -d -o root -g root -m 0755 /etc/ops-v1 /opt/ops-v1 /opt/ops-v1/contracts
install -d -o root -g ops-monitor -m 0750 /var/lib/ops-v1
install -d -o ops-monitor -g ops-monitor -m 0750 /var/lib/ops-v1/checks
install -d -o root -g root -m 0700 /var/lib/ops-v1/audit /var/lib/ops-v1/releases /var/lib/ops-v1/incoming
install -d -o root -g root -m 0750 /var/lib/ops-v1/reports /var/lib/ops-v1/quarantine
install -d -o root -g root -m 0700 /run/ops-v1 /run/ops-v1/approved /run/ops-v1/locks

install -o root -g root -m 0755 "$repository_dir/scripts/ops-v1" /usr/local/libexec/ops-v1
install -o root -g root -m 0644 "$repository_dir/runbooks/contracts/catalog.json" /opt/ops-v1/contracts/catalog.json
systemd-analyze verify \
  "$repository_dir/systemd/ops-v1-observe.service" \
  "$repository_dir/systemd/ops-v1-observe.timer" \
  "$repository_dir/systemd/ops-v1-report.service" \
  "$repository_dir/systemd/ops-v1-report.timer" >/dev/null

install_config() {
  local source_path=$1
  local destination_path=$2
  if [[ -L $destination_path ]]; then
    echo "Configuration destination is a symbolic link: $destination_path" >&2
    return 73
  fi
  if [[ ! -e $destination_path || $replace_managed_configs -eq 1 ]]; then
    install -o root -g root -m 0644 "$source_path" "$destination_path"
  else
    echo "Preserved existing operator configuration: $destination_path"
  fi
}

install_config "$repository_dir/inventory/ops-v1.json" /etc/ops-v1/inventory.json
install_config "$repository_dir/inventory/release-targets.json" /etc/ops-v1/release-targets.json
install_config "$repository_dir/inventory/config-targets.json" /etc/ops-v1/config-targets.json
install_config "$repository_dir/inventory/mission-priorities.json" /etc/ops-v1/mission-priorities.json
install_config "$repository_dir/monitoring/ops-v1-retention.json" /etc/ops-v1/retention.json
install_config "$repository_dir/config/hermes/skill-registry.json" /etc/ops-v1/skill-registry.json

for unit in ops-v1-observe.service ops-v1-observe.timer ops-v1-report.service ops-v1-report.timer; do
  install -o root -g root -m 0644 "$repository_dir/systemd/$unit" "/etc/systemd/system/$unit"
done
systemctl daemon-reload

if [[ $enable_observation -eq 1 ]]; then
  systemctl enable --now ops-v1-observe.timer ops-v1-report.timer
else
  systemctl disable ops-v1-observe.timer ops-v1-report.timer >/dev/null 2>&1 || true
  echo "Installed dormant. Use --enable-observation after reviewing /etc/ops-v1/inventory.json."
fi

echo "Ops V1 deterministic primitives installed. Remote targets remain disabled."
