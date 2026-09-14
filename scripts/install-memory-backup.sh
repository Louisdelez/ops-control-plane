#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

usage() {
  printf '%s\n' \
    'Usage: sudo scripts/install-memory-backup.sh [--enable] [--run-now]' \
    '' \
    'Installs deterministic SQLite/Qdrant backups and an isolated restore test.' \
    '--enable activates the daily backup and weekly restore-test timers.' \
    '--run-now performs one backup followed by a non-destructive restore test.'
}

enable_timers=0
run_now=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --enable) enable_timers=1 ;;
    --run-now) run_now=1 ;;
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
source_script="$repository_dir/memory/backup/ops_memory_backup.py"
source_config="$repository_dir/memory/backup/backup.toml"
source_sysusers="$repository_dir/memory/backup/ops-memory-backup.sysusers"
source_tmpfiles="$repository_dir/memory/backup/ops-memory-backup.tmpfiles"
source_runbook="$repository_dir/runbooks/memory/memory-backup-restore.md"
source_metric_helper="$repository_dir/memory/backup/ops_memory_job_metric.py"
install_root=/opt/ops-memory-backup
config_dir=/etc/ops-memory
unit_dir=/etc/systemd/system
document_dir=/usr/local/share/doc/ops-control-plane/runbooks
helper_dir=/usr/local/libexec/ops-memory-backup

required_commands=(flock getent install python3 systemctl systemd-analyze systemd-sysusers systemd-tmpfiles)
for command_name in "${required_commands[@]}"; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'Required command is missing: %s\n' "$command_name" >&2
    exit 69
  fi
done

required_files=(
  "$source_script"
  "$source_config"
  "$source_sysusers"
  "$source_tmpfiles"
  "$source_runbook"
  "$source_metric_helper"
  "$repository_dir/systemd/ops-memory-backup.service"
  "$repository_dir/systemd/ops-memory-backup.timer"
  "$repository_dir/systemd/ops-memory-restore-test.service"
  "$repository_dir/systemd/ops-memory-restore-test.timer"
  "$repository_dir/systemd/ops-memory-backup-metric-success.service"
  "$repository_dir/systemd/ops-memory-backup-metric-failure.service"
  "$repository_dir/systemd/ops-memory-restore-test-metric-success.service"
  "$repository_dir/systemd/ops-memory-restore-test-metric-failure.service"
)
for path in "${required_files[@]}"; do
  if [[ ! -f $path || -L $path ]]; then
    printf 'Required source is missing or symlinked: %s\n' "$path" >&2
    exit 66
  fi
done

for account in opsmemory backup-agent; do
  if ! getent passwd "$account" >/dev/null; then
    printf 'Required account was not found: %s\n' "$account" >&2
    exit 67
  fi
done
if [[ ! -f /etc/ops-memory/qdrant.env || -L /etc/ops-memory/qdrant.env ]]; then
  printf '%s\n' 'The immutable Qdrant image configuration is unavailable.' >&2
  exit 69
fi
if [[ -L /var/backups/ops-memory || -L $install_root || -L $config_dir || -L $helper_dir ]]; then
  printf '%s\n' 'A managed backup path is an unsafe symbolic link.' >&2
  exit 73
fi

install -d -o root -g root -m 0755 /run/lock
exec {install_lock}>/run/lock/ops-memory-backup-install.lock
if ! flock -n "$install_lock"; then
  printf '%s\n' 'Another ops-memory backup installation is active.' >&2
  exit 75
fi

install -o root -g root -m 0644 "$source_sysusers" /etc/sysusers.d/ops-memory-backup.conf
install -o root -g root -m 0644 "$source_tmpfiles" /etc/tmpfiles.d/ops-memory-backup.conf
systemd-sysusers /etc/sysusers.d/ops-memory-backup.conf
if ! getent group ops-memory-backup >/dev/null; then
  printf '%s\n' 'The ops-memory-backup group was not created.' >&2
  exit 67
fi
systemd-tmpfiles --create /etc/tmpfiles.d/ops-memory-backup.conf

install -d -o root -g root -m 0755 \
  "$install_root" "$install_root/bin" "$config_dir" "$document_dir" "$helper_dir"
install -o root -g root -m 0755 "$source_script" "$install_root/bin/ops-memory-backup"
install -o root -g root -m 0755 "$source_metric_helper" \
  "$helper_dir/ops-memory-job-metric"
install -o root -g opsmemory -m 0640 "$source_config" "$config_dir/backup.toml"
install -o root -g root -m 0644 "$source_runbook" "$document_dir/memory-backup-restore.md"

for unit in \
  ops-memory-backup.service \
  ops-memory-backup.timer \
  ops-memory-restore-test.service \
  ops-memory-restore-test.timer \
  ops-memory-backup-metric-success.service \
  ops-memory-backup-metric-failure.service \
  ops-memory-restore-test-metric-success.service \
  ops-memory-restore-test-metric-failure.service; do
  install -o root -g root -m 0644 "$repository_dir/systemd/$unit" "$unit_dir/$unit"
done

/usr/bin/python3 -c 'compile(open("/opt/ops-memory-backup/bin/ops-memory-backup", "rb").read(), "/opt/ops-memory-backup/bin/ops-memory-backup", "exec")'
/opt/ops-memory-backup/bin/ops-memory-backup --config "$config_dir/backup.toml" --help >/dev/null
systemd-analyze verify \
  "$unit_dir/ops-memory-backup.service" \
  "$unit_dir/ops-memory-backup.timer" \
  "$unit_dir/ops-memory-restore-test.service" \
  "$unit_dir/ops-memory-restore-test.timer" \
  "$unit_dir/ops-memory-backup-metric-success.service" \
  "$unit_dir/ops-memory-backup-metric-failure.service" \
  "$unit_dir/ops-memory-restore-test-metric-success.service" \
  "$unit_dir/ops-memory-restore-test-metric-failure.service"
systemctl daemon-reload

if [[ $enable_timers -eq 1 ]]; then
  systemctl enable --now ops-memory-backup.timer ops-memory-restore-test.timer
fi
if [[ $run_now -eq 1 ]]; then
  systemctl start ops-memory-backup.service
  systemctl start ops-memory-restore-test.service
fi
if [[ $enable_timers -eq 0 ]]; then
  printf '%s\n' 'Installed but timers remain disabled; re-run with --enable after review.'
fi
printf '%s\n' 'Installed deterministic ops-memory backup and isolated restore-test jobs.'
