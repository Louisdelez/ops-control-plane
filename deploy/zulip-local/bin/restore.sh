#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source-path=SCRIPTDIR
source "$script_dir/common.sh"

backup=
identity_file=
admin_password_file=
confirmation=
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup) [[ $# -ge 2 ]] || exit 64; backup=$2; shift ;;
    --identity-file) [[ $# -ge 2 ]] || exit 64; identity_file=$2; shift ;;
    --admin-password-file) [[ $# -ge 2 ]] || exit 64; admin_password_file=$2; shift ;;
    --confirm) [[ $# -ge 2 ]] || exit 64; confirmation=$2; shift ;;
    --help|-h)
      echo "Usage: $0 --backup FILE.age --identity-file /run/... --admin-password-file /run/... --confirm RESTORE-ZULIP-LOCAL"
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 64 ;;
  esac
  shift
done

require_root
require_command age docker flock python3 realpath sha256sum sleep stat systemctl tr
[[ $confirmation == RESTORE-ZULIP-LOCAL ]] || {
  echo "Restore requires the exact confirmation RESTORE-ZULIP-LOCAL." >&2
  exit 64
}
[[ -n $backup && -n $identity_file && -n $admin_password_file ]] || {
  echo "Backup, age identity and administrator password file are required." >&2
  exit 64
}
[[ -f $backup && ! -L $backup && -f $identity_file && ! -L $identity_file ]] || {
  echo "The backup and age identity must be regular, non-symlink files." >&2
  exit 78
}
[[ -f $admin_password_file && ! -L $admin_password_file ]] || {
  echo "The administrator password file must be regular and non-symlinked." >&2
  exit 78
}
backup=$(realpath -e -- "$backup")
identity_file=$(realpath -e -- "$identity_file")
admin_password_file=$(realpath -e -- "$admin_password_file")
validate_root_managed_parent_chain "$backup"
validate_root_managed_parent_chain "$identity_file"
validate_root_managed_parent_chain "$admin_password_file"
[[ $backup =~ /zulip-local-[0-9]{8}T[0-9]{6}Z\.tar\.gz\.age$ ]] || {
  echo "The backup filename is outside the reviewed format." >&2
  exit 78
}
[[ -f $backup && ! -L $backup && -f $backup.sha256 && ! -L $backup.sha256 ]] || {
  echo "The backup or its checksum is unavailable." >&2
  exit 78
}
[[ $(stat -c '%u:%a' "$backup") == 0:400 && $(stat -c '%u:%a' "$backup.sha256") == 0:400 ]] || {
  echo "The encrypted backup or checksum has unsafe ownership or mode." >&2
  exit 78
}
[[ $identity_file == /run/* && -f $identity_file && ! -L $identity_file ]] || {
  echo "The age identity must be a regular, non-symlink file under /run." >&2
  exit 78
}
[[ $(stat -c '%u:%a' "$identity_file") == 0:400 ]] || {
  echo "The age identity has unsafe ownership or mode." >&2
  exit 78
}
[[ $admin_password_file == /run/* && $(stat -c '%u:%a' "$admin_password_file") == 0:400 ]] || {
  echo "The administrator password file must be root-only under /run." >&2
  exit 78
}
password_size=$(stat -c '%s' "$admin_password_file")
[[ $password_size -ge 12 && $password_size -le 4096 ]] || {
  echo "The administrator password file has an invalid size." >&2
  exit 78
}
"$script_dir/provision-zulip.py" --validate-password-file-only \
  --admin-password-file "$admin_password_file"
checksum_line=$(<"$backup.sha256")
expected_name=$(basename "$backup")
[[ $checksum_line =~ ^([0-9a-f]{64})\ \ \*$expected_name$ || $checksum_line =~ ^([0-9a-f]{64})\ \ $expected_name$ ]] || {
  echo "The encrypted backup checksum file has an invalid format." >&2
  exit 78
}
expected_checksum=${BASH_REMATCH[1]}
actual_checksum=$(sha256sum "$backup" | cut -d' ' -f1)
[[ $actual_checksum == "$expected_checksum" ]] || {
  echo "The encrypted backup checksum is invalid." >&2
  exit 78
}

systemctl is-active --quiet zulip-local.service || {
  echo "Restore requires the active systemd-managed Zulip stack." >&2
  exit 78
}

lock_deployment
"$script_dir/preflight.sh" --runtime >/dev/null
systemctl is-active --quiet zulip-local.service || {
  echo "The systemd-managed Zulip stack changed state before restore." >&2
  exit 75
}

# Validate names, types, member count, expanded size and dump presence before
# touching a volume. Decryption is streamed and leaves no plaintext file.
age --decrypt --identity "$identity_file" "$backup" |
  python3 "$script_dir/validate-backup-stream.py"

# A running bridge keeps its API key in memory. Stop it before replacing the
# database, and restart it only after the OpenBao bridge object is reconciled.
bridge_was_active=false
if systemctl is-active --quiet zulip-approval-bridge.service; then
  systemctl stop zulip-approval-bridge.service
  bridge_was_active=true
fi

cleanup_failed_restore() {
  local status=$?
  trap - EXIT
  if (( status != 0 )); then
    # Preserve whatever data remains for diagnosis, but never leave partial
    # application/dependency containers behind or claim an active unit.
    compose down --remove-orphans >/dev/null 2>&1 || true
    exec 9>&-
    systemctl stop zulip-local.service >/dev/null 2>&1 || true
    echo "Restore failed; the Zulip stack and bridge remain stopped." >&2
  fi
  exit "$status"
}
trap cleanup_failed_restore EXIT

# This is the one intentionally destructive operation and is gated by the exact
# confirmation above plus checksum and structural validation.
compose down --volumes --remove-orphans

age --decrypt --identity "$identity_file" "$backup" |
  compose run --rm -T --no-deps zulip tar xzf - -C /data \
    --no-same-owner --no-same-permissions \
    --exclude=./zulip-secrets.conf --exclude=zulip-secrets.conf \
    --exclude=./certs/manual --exclude=certs/manual \
    --exclude=./etc-zulip/zulip-secrets.conf \
    --exclude=etc-zulip/zulip-secrets.conf

dump_output=$(compose run --rm -T --no-deps zulip sh -euc \
  'find /data/backups -maxdepth 1 -type f -name "backup-*.sql" -printf "%f\n" | sort | tail -n 1')
dump=$(printf '%s' "$dump_output" | tr -d '\r\n')
[[ $dump =~ ^backup-[A-Za-z0-9_.:+-]+\.sql$ ]] || {
  echo "No unambiguous official database dump was restored." >&2
  exit 78
}
compose run --rm -T zulip app:restore "$dump"
compose up --detach --wait zulip
wait_for_zulip_health "$script_dir/health.sh"
# The database dump contains the bot key and numeric IDs as of the snapshot.
# Reconcile the exact OpenBao bridge object before declaring success. Descriptor
# 9 is inherited, so no lifecycle operation can enter between restore and
# bridge reconciliation.
"$script_dir/provision-zulip.py" --reset-admin-password \
  --deployment-lock-fd 9 \
  --admin-password-file "$admin_password_file"
if [[ $bridge_was_active == true ]]; then
  systemctl start zulip-approval-bridge.service
  systemctl is-active --quiet zulip-approval-bridge.service || {
    echo "The restored bridge did not become active with its reconciled identity." >&2
    exit 1
  }
fi
"$script_dir/health.sh" --quiet
systemctl is-active --quiet zulip-local.service || {
  echo "The Zulip systemd unit changed state during restore." >&2
  exit 75
}
trap - EXIT
echo "The encrypted Zulip snapshot was restored and passed health checks."
