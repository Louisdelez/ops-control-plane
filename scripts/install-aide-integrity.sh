#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
readonly repo_dir
readonly aide_config=/etc/aide/ops-control-plane.conf
readonly aide_database=/var/lib/aide/aide.db.gz
readonly metric_dir=/var/lib/node-exporter/textfile
readonly metric_file=$metric_dir/aide.prom

if ! rpm -q aide >/dev/null 2>&1; then
  dnf -y install aide
fi

if [[ ! -f /etc/aide.conf || -L /etc/aide.conf ]]; then
  echo "Fedora AIDE configuration is absent or unsafe: /etc/aide.conf" >&2
  exit 1
fi
base_owner=$(stat -Lc '%u' -- /etc/aide.conf)
base_mode=$(stat -Lc '%a' -- /etc/aide.conf)
if [[ $base_owner != 0 ]] || (( (8#$base_mode & 022) != 0 )); then
  echo "Fedora AIDE configuration is not root-owned or is writable by an untrusted principal." >&2
  exit 1
fi

if ! getent passwd node-exporter >/dev/null || ! getent group node-exporter >/dev/null; then
  echo "The node-exporter service identity is missing; install node_exporter first." >&2
  exit 1
fi

install -d -o root -g root -m 0750 /etc/aide
install -m 0600 "$repo_dir/config/aide/ops-control-plane.conf" "$aide_config"

# This validates the existing Fedora policy plus our exclusions.  It does not
# initialize, update, or read an AIDE database.
/usr/bin/aide --config="$aide_config" --config-check

assert_excluded() {
  local probe=$1
  local path_check_rc

  set +e
  /usr/bin/aide --config="$aide_config" --path-check="$probe" >/dev/null 2>&1
  path_check_rc=$?
  set -e
  if [[ $path_check_rc -ne 1 ]]; then
    echo "AIDE safety exclusion is ineffective for $probe (exit $path_check_rc)." >&2
    exit 1
  fi
}

assert_excluded 'd:/home/ops-user'
assert_excluded 'f:/home/ops-user/.ops-aide-probe'
assert_excluded 'f:/run/credentials/ops-aide-probe'
assert_excluded 'f:/tmp/ops-aide-probe'
assert_excluded 'f:/var/lib/openbao/ops-aide-probe'
assert_excluded 'f:/var/lib/hermes/workspace/ops-aide-probe'
assert_excluded 'f:/var/lib/hermes/profiles/minecraft-ops/workspace/ops-aide-probe'

set +e
/usr/bin/aide --config="$aide_config" \
  --path-check='f:/var/lib/hermes/hermes-agent/pyproject.toml' >/dev/null 2>&1
hermes_path_check_rc=$?
set -e
if [[ $hermes_path_check_rc -ne 0 ]]; then
  echo "AIDE does not cover the root-owned Hermes runtime (exit $hermes_path_check_rc)." >&2
  exit 1
fi

install -d -o root -g node-exporter -m 0750 /var/lib/node-exporter
install -d -o root -g node-exporter -m 0750 "$metric_dir"
install -m 0755 "$repo_dir/scripts/ops-aide-check" /usr/local/libexec/ops-aide-check
install -m 0644 "$repo_dir/systemd/ops-aide-check.service" /etc/systemd/system/ops-aide-check.service
install -m 0644 "$repo_dir/systemd/ops-aide-check.timer" /etc/systemd/system/ops-aide-check.timer

restorecon -RF /etc/aide /usr/local/libexec/ops-aide-check \
  /var/lib/node-exporter /etc/systemd/system/ops-aide-check.service \
  /etc/systemd/system/ops-aide-check.timer >/dev/null 2>&1 || true

# Seed an explicit fail-closed/never-run state once.  Re-running the installer
# never destroys a genuine result produced by the checker.
if [[ -L $metric_file ]]; then
  echo "Refusing unsafe AIDE metric symlink: $metric_file" >&2
  exit 1
fi
if [[ ! -e $metric_file ]]; then
  metric_tmp=$(mktemp "$metric_dir/.aide.prom.XXXXXX")
  trap 'rm -f -- "$metric_tmp"' EXIT
  database_present=0
  initial_status=3
  if [[ -f $aide_database && ! -L $aide_database ]]; then
    database_present=1
    initial_status=4
  fi
  {
    echo '# HELP ops_aide_database_present Whether the trusted AIDE input database is present as a regular file.'
    echo '# TYPE ops_aide_database_present gauge'
    printf 'ops_aide_database_present %d\n' "$database_present"
    echo '# HELP ops_aide_last_check_timestamp_seconds Unix timestamp of the last attempted AIDE check; zero means never run.'
    echo '# TYPE ops_aide_last_check_timestamp_seconds gauge'
    echo 'ops_aide_last_check_timestamp_seconds 0'
    echo '# HELP ops_aide_last_check_status Last check status: 0 clean, 1 changes, 2 error, 3 database missing, 4 never run.'
    echo '# TYPE ops_aide_last_check_status gauge'
    printf 'ops_aide_last_check_status %d\n' "$initial_status"
    echo '# HELP ops_aide_last_check_exit_code Raw AIDE exit code; minus one means AIDE was not invoked.'
    echo '# TYPE ops_aide_last_check_exit_code gauge'
    echo 'ops_aide_last_check_exit_code -1'
    echo '# HELP ops_aide_last_check_clean Whether the last attempted AIDE check completed with no differences.'
    echo '# TYPE ops_aide_last_check_clean gauge'
    echo 'ops_aide_last_check_clean 0'
    echo '# HELP ops_aide_last_check_changes_detected Whether AIDE reported added, removed, or changed files.'
    echo '# TYPE ops_aide_last_check_changes_detected gauge'
    echo 'ops_aide_last_check_changes_detected 0'
    echo '# HELP ops_aide_last_check_error Whether integrity verification is unavailable due to an error, missing baseline, or no completed check.'
    echo '# TYPE ops_aide_last_check_error gauge'
    echo 'ops_aide_last_check_error 1'
  } >"$metric_tmp"
  chown root:node-exporter "$metric_tmp"
  chmod 0640 "$metric_tmp"
  mv -f -- "$metric_tmp" "$metric_file"
  trap - EXIT
fi

systemctl daemon-reload
systemctl enable --now ops-aide-check.timer

echo "AIDE daily checking is installed; no database was initialized or updated."
if [[ ! -f $aide_database ]]; then
  echo "Checks remain fail-closed until $aide_database is promoted manually."
fi
