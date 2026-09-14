#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

usage() {
  cat <<'EOF'
Usage: sudo scripts/install-openbao-backup.sh [--enable] [--run-now]

Installs the reviewed OpenBao Raft backup components.  By default the timer is
left disabled.  Provision credentials interactively with
`sudo /usr/local/sbin/provision-openbao-backup`, then rerun with --enable.

--run-now implies --enable and starts one backup after installation.
EOF
}

enable_timer=0
run_now=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --enable) enable_timer=1 ;;
    --run-now) enable_timer=1; run_now=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
  shift
done

if [[ $EUID -ne 0 ]]; then
  echo "Run as root." >&2
  exit 77
fi

repository_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
readonly repository_dir

required_commands=(age bash flock getent install python3 restorecon stat systemctl systemd-analyze systemd-creds systemd-sysusers systemd-tmpfiles)
for command_name in "${required_commands[@]}"; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is missing: $command_name" >&2
    exit 69
  }
done

source_files=(
  "$repository_dir/openbao/backup/openbao_raft_backup.py"
  "$repository_dir/openbao/backup/openbao_backup_metric.py"
  "$repository_dir/openbao/backup/backup.toml"
  "$repository_dir/openbao/backup/openbao-backup.sysusers"
  "$repository_dir/openbao/backup/openbao-backup.tmpfiles"
  "$repository_dir/config/openbao/policies/openbao-backup-runtime.hcl"
  "$repository_dir/config/openbao/recovery.age-recipient"
  "$repository_dir/scripts/provision-openbao-backup"
  "$repository_dir/runbooks/openbao/openbao-raft-backup.md"
  "$repository_dir/systemd/openbao-raft-backup.service"
  "$repository_dir/systemd/openbao-raft-backup.timer"
  "$repository_dir/systemd/openbao-raft-backup-metric-success.service"
  "$repository_dir/systemd/openbao-raft-backup-metric-failure.service"
)
for source_file in "${source_files[@]}"; do
  if [[ ! -f $source_file || -L $source_file ]]; then
    echo "Required source is absent, not regular, or symlinked: $source_file" >&2
    exit 66
  fi
  source_mode=$(stat -Lc '%a' -- "$source_file")
  if (( (8#$source_mode & 022) != 0 )); then
    echo "Source is writable by group or others: $source_file" >&2
    exit 73
  fi
done

install -d -o root -g root -m 0755 /run/lock
exec {install_lock}>/run/lock/openbao-backup-install.lock
if ! flock -n "$install_lock"; then
  echo "Another OpenBao backup installation is running." >&2
  exit 75
fi

reject_symlink() {
  local managed_path=$1
  if [[ -L $managed_path ]]; then
    echo "Managed OpenBao backup path must not be a symbolic link: $managed_path" >&2
    exit 73
  fi
}
for managed_path in \
  /etc/sysusers.d/openbao-backup.conf /etc/tmpfiles.d/openbao-backup.conf \
  /opt/openbao-raft-backup /opt/openbao-raft-backup/bin/openbao-raft-backup \
  /etc/openbao-backup /etc/openbao-backup/backup.toml \
  /etc/openbao-backup/recovery.age-recipient \
  /var/backups/openbao /usr/local/libexec/openbao-raft-backup \
  /usr/local/sbin/provision-openbao-backup; do
  reject_symlink "$managed_path"
done

python3 - "${source_files[0]}" "${source_files[1]}" "${source_files[7]}" <<'PY'
import ast
import pathlib
import sys
for name in sys.argv[1:]:
    ast.parse(pathlib.Path(name).read_text(encoding="utf-8"), filename=name)
PY
bash -n "$repository_dir/scripts/install-openbao-backup.sh"

install -o root -g root -m 0644 "$repository_dir/openbao/backup/openbao-backup.sysusers" /etc/sysusers.d/openbao-backup.conf
systemd-sysusers /etc/sysusers.d/openbao-backup.conf
getent passwd openbao-backup >/dev/null || { echo "openbao-backup identity was not created." >&2; exit 67; }
getent group node-exporter >/dev/null || {
  echo "Install the monitoring stack and node_exporter identity first." >&2
  exit 67
}

install -o root -g root -m 0644 "$repository_dir/openbao/backup/openbao-backup.tmpfiles" /etc/tmpfiles.d/openbao-backup.conf
systemd-tmpfiles --create /etc/tmpfiles.d/openbao-backup.conf
for private_directory in /var/lib/openbao-backup /var/backups/openbao; do
  if [[ ! -d $private_directory || -L $private_directory \
      || $(stat -Lc '%U:%G:%a' -- "$private_directory") != openbao-backup:openbao-backup:700 ]]; then
    echo "OpenBao backup directory is missing or unsafe: $private_directory" >&2
    exit 73
  fi
done
if [[ ! -d /var/lib/node-exporter/textfile || -L /var/lib/node-exporter/textfile || $(stat -Lc '%U:%G:%a' -- /var/lib/node-exporter/textfile) != root:node-exporter:750 ]]; then
  echo "The node_exporter textfile directory is missing or unsafe." >&2
  exit 73
fi

install -d -o root -g root -m 0755 /opt/openbao-raft-backup/bin
install -o root -g root -m 0755 "$repository_dir/openbao/backup/openbao_raft_backup.py" /opt/openbao-raft-backup/bin/openbao-raft-backup
install -d -o root -g root -m 0755 /usr/local/libexec/openbao-raft-backup
install -o root -g root -m 0755 "$repository_dir/openbao/backup/openbao_backup_metric.py" /usr/local/libexec/openbao-raft-backup/openbao-backup-metric
install -o root -g root -m 0755 "$repository_dir/scripts/provision-openbao-backup" /usr/local/sbin/provision-openbao-backup

install -d -o root -g openbao-backup -m 0750 /etc/openbao-backup
install -o root -g openbao-backup -m 0640 "$repository_dir/openbao/backup/backup.toml" /etc/openbao-backup/backup.toml
install -o root -g openbao-backup -m 0640 "$repository_dir/config/openbao/recovery.age-recipient" /etc/openbao-backup/recovery.age-recipient
install -d -o root -g root -m 0755 /usr/local/share/ops-control-plane/openbao/policies
install -o root -g root -m 0644 "$repository_dir/config/openbao/policies/openbao-backup-runtime.hcl" /usr/local/share/ops-control-plane/openbao/policies/openbao-backup-runtime.hcl
install -d -o root -g root -m 0755 /usr/local/share/doc/ops-control-plane/runbooks
install -o root -g root -m 0644 "$repository_dir/runbooks/openbao/openbao-raft-backup.md" /usr/local/share/doc/ops-control-plane/runbooks/openbao-raft-backup.md

for unit in openbao-raft-backup.service openbao-raft-backup.timer openbao-raft-backup-metric-success.service openbao-raft-backup-metric-failure.service; do
  install -o root -g root -m 0644 "$repository_dir/systemd/$unit" "/etc/systemd/system/$unit"
done
restorecon -RF /opt/openbao-raft-backup /etc/openbao-backup /usr/local/libexec/openbao-raft-backup /usr/local/sbin/provision-openbao-backup /etc/systemd/system/openbao-raft-backup.service /etc/systemd/system/openbao-raft-backup.timer /etc/systemd/system/openbao-raft-backup-metric-success.service /etc/systemd/system/openbao-raft-backup-metric-failure.service >/dev/null
systemd-analyze verify \
  /etc/systemd/system/openbao-raft-backup.service \
  /etc/systemd/system/openbao-raft-backup.timer \
  /etc/systemd/system/openbao-raft-backup-metric-success.service \
  /etc/systemd/system/openbao-raft-backup-metric-failure.service >/dev/null
systemctl daemon-reload

if (( enable_timer )); then
  credential_names=(openbao-backup-role-id openbao-backup-secret-id openbao-backup-secret-id-accessor)
  for credential_name in "${credential_names[@]}"; do
    credential_path="/etc/credstore.encrypted/$credential_name"
    if [[ ! -f $credential_path || -L $credential_path || $(stat -Lc '%u:%g:%a' -- "$credential_path") != 0:0:600 ]]; then
      echo "Provision the complete encrypted OpenBao backup credential set before enabling." >&2
      exit 78
    fi
    if ! systemd-creds decrypt --name="$credential_name" "$credential_path" - >/dev/null 2>&1; then
      echo "An OpenBao backup credential is not decryptable on this host and TPM." >&2
      exit 78
    fi
  done
  systemctl enable --now openbao-raft-backup.timer
fi

if (( run_now )); then
  systemctl start openbao-raft-backup.service
  systemctl is-active --quiet openbao-raft-backup.timer
fi

echo "OpenBao Raft backup components installed."
if (( ! enable_timer )); then
  echo "Timer left disabled; run the interactive provisioner before enabling it."
fi
