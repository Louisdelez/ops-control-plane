#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

usage() {
  cat <<'EOF'
Usage: sudo scripts/install-monitoring-stack.sh [--replace-managed-configs]

Deploys the already-reviewed local monitoring artifacts.  node_exporter must
have been installed first with scripts/install-node-exporter.sh.  Divergent
operator-managed configuration is preserved by default; use
--replace-managed-configs only after reviewing the repository versions.

This installer does not initialize OpenBao or AIDE and does not enable Hermes
or the Zulip bridge.  Prometheus, Alertmanager, and node_exporter are required
to listen on IPv4 loopback only.
EOF
}

replace_managed_configs=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --replace-managed-configs)
      replace_managed_configs=1
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 64
      ;;
  esac
  shift
done

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 77
fi

repository_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
readonly repository_dir

required_commands=(
  amtool auditctl augenrules awk bash basename chmod cmp cp curl date dirname
  find findmnt flock getent grep id install jq logger logrotate lsblk mktemp
  mokutil mv openssl promtool python3 restorecon rm rpm runuser shellcheck
  sleep smartctl sort ss stat systemctl systemd-analyze tail tr uname xargs
)
for required_command in "${required_commands[@]}"; do
  if ! command -v "$required_command" >/dev/null 2>&1; then
    echo "Required command is missing: $required_command" >&2
    exit 69
  fi
done

required_packages=(prometheus alertmanager audit logrotate ShellCheck)
for required_package in "${required_packages[@]}"; do
  if ! rpm -q "$required_package" >/dev/null 2>&1; then
    echo "Required Fedora package is missing: $required_package" >&2
    exit 69
  fi
done

declare -a source_files=(
  "$repository_dir/monitoring/prometheus.yml"
  "$repository_dir/monitoring/alerts.yaml"
  "$repository_dir/monitoring/prometheus.env"
  "$repository_dir/monitoring/alertmanager.yml"
  "$repository_dir/monitoring/alertmanager.env"
  "$repository_dir/systemd/prometheus-hardening.conf"
  "$repository_dir/systemd/alertmanager-hardening.conf"
  "$repository_dir/systemd/90-ops-journald.conf"
  "$repository_dir/systemd/node-exporter.service"
  "$repository_dir/systemd/ops-local-metrics.service"
  "$repository_dir/systemd/ops-local-metrics.timer"
  "$repository_dir/systemd/ops-daily-report.service"
  "$repository_dir/systemd/ops-daily-report.timer"
  "$repository_dir/systemd/ops-thermal-guard.service"
  "$repository_dir/scripts/ops-local-metrics"
  "$repository_dir/scripts/ops-daily-report.py"
  "$repository_dir/scripts/ops-thermal-guard"
  "$repository_dir/config/logrotate/openbao-audit"
  "$repository_dir/config/audit/audit.rules"
  "$repository_dir/config/audit/50-ops-control-plane.rules"
  "$repository_dir/scripts/install-monitoring-stack.sh"
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

if [[ ! -x /usr/local/bin/node_exporter || -L /usr/local/bin/node_exporter ]]; then
  echo "Install node_exporter first with scripts/install-node-exporter.sh." >&2
  exit 69
fi
node_exporter_stat=$(stat -Lc '%u:%g:%a' -- /usr/local/bin/node_exporter)
if [[ $node_exporter_stat != 0:0:755 ]]; then
  echo "Unexpected node_exporter ownership or mode: $node_exporter_stat" >&2
  exit 73
fi
if ! /usr/local/bin/node_exporter --version 2>&1 | grep -Fq 'version 1.12.1'; then
  echo "The separately installed node_exporter is not the reviewed v1.12.1 build." >&2
  exit 69
fi

for service_user in prometheus node-exporter ops-monitor; do
  if ! getent passwd "$service_user" >/dev/null; then
    echo "Required service identity is missing: $service_user" >&2
    exit 67
  fi
done
for service_group in prometheus node-exporter ops-readers; do
  if ! getent group "$service_group" >/dev/null; then
    echo "Required service group is missing: $service_group" >&2
    exit 67
  fi
done
if ! id -nG ops-monitor | tr ' ' '\n' | grep -Fxq ops-readers; then
  echo "ops-monitor must be a member of ops-readers." >&2
  exit 67
fi

install -d -o root -g root -m 0755 /run/lock
exec {install_lock}>/run/lock/ops-monitoring-install.lock
if ! flock -n "$install_lock"; then
  echo "Another monitoring installation is running." >&2
  exit 75
fi

validation_dir=$(mktemp -d /run/ops-monitoring-validation.XXXXXX)
cleanup() {
  local result=$?
  trap - EXIT
  case "$validation_dir" in
    /run/ops-monitoring-validation.*)
      rm -rf -- "$validation_dir"
      ;;
    *)
      echo "Refusing to remove unexpected validation path: $validation_dir" >&2
      ;;
  esac
  exit "$result"
}
trap cleanup EXIT
chmod 0700 "$validation_dir"

bash -n \
  "$repository_dir/scripts/install-monitoring-stack.sh" \
  "$repository_dir/scripts/ops-local-metrics" \
  "$repository_dir/scripts/ops-thermal-guard" \
  "$repository_dir/monitoring/prometheus.env" \
  "$repository_dir/monitoring/alertmanager.env"
shellcheck \
  "$repository_dir/scripts/install-monitoring-stack.sh" \
  "$repository_dir/scripts/ops-local-metrics" \
  "$repository_dir/scripts/ops-thermal-guard"
python3 - "$repository_dir/scripts/ops-daily-report.py" <<'PY'
import ast
import pathlib
import sys

source = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
ast.parse(source, filename=sys.argv[1])
PY

install -m 0600 "$repository_dir/monitoring/alerts.yaml" \
  "$validation_dir/ops-alerts.yaml"
python3 - \
  "$repository_dir/monitoring/prometheus.yml" \
  "$validation_dir/prometheus.yml" \
  "$validation_dir/ops-alerts.yaml" <<'PY'
import pathlib
import sys

source_path, output_path, rules_path = map(pathlib.Path, sys.argv[1:])
source = source_path.read_text(encoding="utf-8")
needle = "/etc/prometheus/ops-alerts.yaml"
if source.count(needle) != 1:
    raise SystemExit("Prometheus config must reference the managed rules exactly once")
output_path.write_text(source.replace(needle, str(rules_path)), encoding="utf-8")
PY
promtool check rules "$repository_dir/monitoring/alerts.yaml" >/dev/null
promtool check config "$validation_dir/prometheus.yml" >/dev/null
amtool check-config "$repository_dir/monitoring/alertmanager.yml" >/dev/null
install -o root -g root -m 0600 \
  "$repository_dir/config/logrotate/openbao-audit" \
  "$validation_dir/openbao-audit.logrotate"
logrotate --debug "$validation_dir/openbao-audit.logrotate" \
  >"$validation_dir/logrotate-check.log" 2>&1

unit_sources=(
  "$repository_dir/systemd/node-exporter.service"
  "$repository_dir/systemd/ops-local-metrics.service"
  "$repository_dir/systemd/ops-local-metrics.timer"
  "$repository_dir/systemd/ops-daily-report.service"
  "$repository_dir/systemd/ops-daily-report.timer"
  "$repository_dir/systemd/ops-thermal-guard.service"
)
systemd-analyze verify "${unit_sources[@]}" \
  >"$validation_dir/systemd-source-check.log" 2>&1

assert_args_confined() {
  local environment_file=$1
  local service_kind=$2
  python3 - "$environment_file" "$service_kind" <<'PY'
import pathlib
import shlex
import sys

path = pathlib.Path(sys.argv[1])
kind = sys.argv[2]
lines = [
    line.strip()
    for line in path.read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
]
if len(lines) != 1 or not lines[0].startswith("ARGS="):
    raise SystemExit(f"{path}: expected exactly one ARGS assignment")
outer = shlex.split(lines[0].partition("=")[2], posix=True)
if len(outer) != 1:
    raise SystemExit(f"{path}: ARGS must be one quoted value")
arguments = shlex.split(outer[0], posix=True)

def values(name: str) -> list[str]:
    prefix = name + "="
    return [argument[len(prefix):] for argument in arguments if argument.startswith(prefix)]

expected = {
    "prometheus": "127.0.0.1:9090",
    "alertmanager": "127.0.0.1:9093",
}[kind]
if values("--web.listen-address") != [expected]:
    raise SystemExit(f"{path}: web listener must be exactly {expected}")
if kind == "alertmanager" and values("--cluster.listen-address") != [""]:
    raise SystemExit(f"{path}: Alertmanager clustering must remain disabled")
PY
}

assert_args_confined "$repository_dir/monitoring/prometheus.env" prometheus
assert_args_confined "$repository_dir/monitoring/alertmanager.env" alertmanager
if ! grep -Fq -- '--web.listen-address=127.0.0.1:9100' \
  "$repository_dir/systemd/node-exporter.service"; then
  echo "node-exporter.service is not confined to 127.0.0.1:9100." >&2
  exit 78
fi

rpm_file_content_is_pristine() {
  local destination=$1
  local package verification matching flags

  package=$(rpm -qf -- "$destination" 2>/dev/null) || return 1
  verification=$(rpm -V "$package" 2>/dev/null || true)
  matching=$(awk -v target="$destination" '$NF == target { print $1; exit }' \
    <<<"$verification")
  [[ -z $matching ]] && return 0
  flags=$matching
  [[ ${flags:0:1} != S && ${flags:2:1} != 5 ]]
}

declare -a managed_config_sources=(
  "$repository_dir/monitoring/prometheus.yml"
  "$repository_dir/monitoring/alerts.yaml"
  "$repository_dir/monitoring/prometheus.env"
  "$repository_dir/monitoring/alertmanager.yml"
  "$repository_dir/monitoring/alertmanager.env"
  "$repository_dir/systemd/prometheus-hardening.conf"
  "$repository_dir/systemd/alertmanager-hardening.conf"
  "$repository_dir/systemd/90-ops-journald.conf"
  "$repository_dir/config/logrotate/openbao-audit"
  "$repository_dir/config/audit/audit.rules"
  "$repository_dir/config/audit/50-ops-control-plane.rules"
)
declare -a managed_config_destinations=(
  /etc/prometheus/prometheus.yml
  /etc/prometheus/ops-alerts.yaml
  /etc/default/prometheus
  /etc/prometheus/alertmanager.yml
  /etc/default/prometheus-alertmanager
  /etc/systemd/system/prometheus.service.d/10-ops-hardening.conf
  /etc/systemd/system/prometheus-alertmanager.service.d/10-ops-hardening.conf
  /etc/systemd/journald.conf.d/90-ops.conf
  /etc/logrotate.d/openbao-audit
  /etc/audit/rules.d/00-ops-control-plane-base.rules
  /etc/audit/rules.d/50-ops-control-plane.rules
)

declare -a config_conflicts=()
for index in "${!managed_config_sources[@]}"; do
  source_file=${managed_config_sources[$index]}
  destination=${managed_config_destinations[$index]}
  if [[ -L $destination ]]; then
    echo "Managed destination must not be a symlink: $destination" >&2
    exit 73
  fi
  if [[ -e $destination && ! -f $destination ]]; then
    echo "Managed destination is not a regular file: $destination" >&2
    exit 73
  fi
  if [[ -f $destination ]] && ! cmp -s -- "$source_file" "$destination"; then
    if (( replace_managed_configs == 0 )) && \
       ! rpm_file_content_is_pristine "$destination"; then
      config_conflicts+=("$destination")
    fi
  fi
done
if (( ${#config_conflicts[@]} > 0 )); then
  echo "Divergent operator-managed configuration was preserved:" >&2
  printf '  %s\n' "${config_conflicts[@]}" >&2
  echo "Review differences, then reconcile them or rerun with --replace-managed-configs." >&2
  exit 78
fi

for managed_directory in \
  /etc/prometheus \
  /etc/systemd/system/prometheus.service.d \
  /etc/systemd/system/prometheus-alertmanager.service.d \
  /etc/systemd/journald.conf.d \
  /var/lib/prometheus \
  /var/lib/prometheus/metrics2 \
  /var/lib/prometheus/alertmanager \
  /var/lib/node-exporter \
  /var/lib/node-exporter/textfile \
  /var/lib/ops-reports \
  /opt/ops-control-plane; do
  if [[ -L $managed_directory ]]; then
    echo "Managed directory must not be a symlink: $managed_directory" >&2
    exit 73
  fi
done

# The other watch anchors are created by their component installers.  Creating
# them here could silently bypass those components' ownership policy.
audit_watch_anchors=(
  /etc/openbao.d
  /etc/credstore.encrypted
  /etc/sudoers.d
  /etc/ops-broker
  /home/ops-user/.ssh
  /opt/ops-broker
  /usr/local/libexec
)
for audit_watch_anchor in "${audit_watch_anchors[@]}"; do
  if [[ ! -d $audit_watch_anchor || -L $audit_watch_anchor ]]; then
    echo "Audit watch prerequisite is absent or unsafe: $audit_watch_anchor" >&2
    exit 69
  fi
done

install -d -o root -g prometheus -m 0750 /etc/prometheus
install -d -o root -g root -m 0755 \
  /etc/systemd/system/prometheus.service.d \
  /etc/systemd/system/prometheus-alertmanager.service.d \
  /etc/systemd/journald.conf.d
install -d -o prometheus -g prometheus -m 0750 \
  /var/lib/prometheus \
  /var/lib/prometheus/metrics2 \
  /var/lib/prometheus/alertmanager
install -d -o root -g node-exporter -m 0750 \
  /var/lib/node-exporter \
  /var/lib/node-exporter/textfile
install -d -o ops-monitor -g ops-readers -m 0750 /var/lib/ops-reports
install -d -o ops-monitor -g ops-readers -m 0750 /var/lib/ops-reports/captures
install -d -o root -g root -m 0755 /opt/ops-control-plane

prometheus_changed=0
alertmanager_changed=0
node_exporter_changed=0
local_metrics_changed=0
daily_report_changed=0
thermal_guard_changed=0
journald_changed=0
logrotate_changed=0
audit_rules_changed=0

deploy_file() {
  local source=$1
  local destination=$2
  local owner=$3
  local group=$4
  local mode=$5
  local changed_variable=$6
  local current desired temporary destination_directory destination_name

  destination_directory=$(dirname -- "$destination")
  destination_name=$(basename -- "$destination")
  if [[ -L $destination || ( -e $destination && ! -f $destination ) ]]; then
    echo "Unsafe managed destination: $destination" >&2
    exit 73
  fi

  desired="$(id -u "$owner"):$(getent group "$group" | awk -F: '{print $3}'):${mode#0}"
  current=absent
  if [[ -f $destination ]]; then
    current=$(stat -Lc '%u:%g:%a' -- "$destination")
    if cmp -s -- "$source" "$destination" && [[ $current == "$desired" ]]; then
      return 0
    fi
  fi

  temporary=$(mktemp "$destination_directory/.${destination_name}.ops.XXXXXX")
  install -o "$owner" -g "$group" -m "$mode" "$source" "$temporary"
  mv -fT -- "$temporary" "$destination"
  printf -v "$changed_variable" '%s' 1
}

deploy_file "$repository_dir/monitoring/prometheus.yml" \
  /etc/prometheus/prometheus.yml root prometheus 0640 prometheus_changed
deploy_file "$repository_dir/monitoring/alerts.yaml" \
  /etc/prometheus/ops-alerts.yaml root prometheus 0640 prometheus_changed
deploy_file "$repository_dir/monitoring/prometheus.env" \
  /etc/default/prometheus root root 0640 prometheus_changed
deploy_file "$repository_dir/systemd/prometheus-hardening.conf" \
  /etc/systemd/system/prometheus.service.d/10-ops-hardening.conf \
  root root 0644 prometheus_changed

deploy_file "$repository_dir/monitoring/alertmanager.yml" \
  /etc/prometheus/alertmanager.yml root prometheus 0640 alertmanager_changed
deploy_file "$repository_dir/monitoring/alertmanager.env" \
  /etc/default/prometheus-alertmanager root root 0640 alertmanager_changed
deploy_file "$repository_dir/systemd/alertmanager-hardening.conf" \
  /etc/systemd/system/prometheus-alertmanager.service.d/10-ops-hardening.conf \
  root root 0644 alertmanager_changed

deploy_file "$repository_dir/systemd/node-exporter.service" \
  /etc/systemd/system/node-exporter.service root root 0644 node_exporter_changed
deploy_file "$repository_dir/scripts/ops-local-metrics" \
  /usr/local/libexec/ops-local-metrics root root 0750 local_metrics_changed
deploy_file "$repository_dir/systemd/ops-local-metrics.service" \
  /etc/systemd/system/ops-local-metrics.service root root 0644 local_metrics_changed
deploy_file "$repository_dir/systemd/ops-local-metrics.timer" \
  /etc/systemd/system/ops-local-metrics.timer root root 0644 local_metrics_changed

deploy_file "$repository_dir/scripts/ops-daily-report.py" \
  /usr/local/libexec/ops-daily-report root ops-readers 0750 daily_report_changed
deploy_file "$repository_dir/systemd/ops-daily-report.service" \
  /etc/systemd/system/ops-daily-report.service root root 0644 daily_report_changed
deploy_file "$repository_dir/systemd/ops-daily-report.timer" \
  /etc/systemd/system/ops-daily-report.timer root root 0644 daily_report_changed

deploy_file "$repository_dir/scripts/ops-thermal-guard" \
  /usr/local/libexec/ops-thermal-guard root root 0750 thermal_guard_changed
deploy_file "$repository_dir/systemd/ops-thermal-guard.service" \
  /etc/systemd/system/ops-thermal-guard.service root root 0644 thermal_guard_changed
deploy_file "$repository_dir/systemd/90-ops-journald.conf" \
  /etc/systemd/journald.conf.d/90-ops.conf root root 0644 journald_changed
deploy_file "$repository_dir/config/logrotate/openbao-audit" \
  /etc/logrotate.d/openbao-audit root root 0644 logrotate_changed

audit_base_destination=/etc/audit/rules.d/00-ops-control-plane-base.rules
audit_watch_destination=/etc/audit/rules.d/50-ops-control-plane.rules
audit_base_existed=0
audit_watch_existed=0
if [[ -f $audit_base_destination ]]; then
  cp -a -- "$audit_base_destination" "$validation_dir/audit-base.previous"
  audit_base_existed=1
fi
if [[ -f $audit_watch_destination ]]; then
  cp -a -- "$audit_watch_destination" "$validation_dir/audit-watch.previous"
  audit_watch_existed=1
fi
deploy_file "$repository_dir/config/audit/audit.rules" \
  "$audit_base_destination" root root 0600 audit_rules_changed
deploy_file "$repository_dir/config/audit/50-ops-control-plane.rules" \
  "$audit_watch_destination" root root 0600 audit_rules_changed

installed_paths=(
  /etc/prometheus/prometheus.yml
  /etc/prometheus/ops-alerts.yaml
  /etc/default/prometheus
  /etc/prometheus/alertmanager.yml
  /etc/default/prometheus-alertmanager
  /etc/systemd/system/prometheus.service.d/10-ops-hardening.conf
  /etc/systemd/system/prometheus-alertmanager.service.d/10-ops-hardening.conf
  /etc/systemd/journald.conf.d/90-ops.conf
  /etc/systemd/system/node-exporter.service
  /etc/systemd/system/ops-local-metrics.service
  /etc/systemd/system/ops-local-metrics.timer
  /etc/systemd/system/ops-daily-report.service
  /etc/systemd/system/ops-daily-report.timer
  /etc/systemd/system/ops-thermal-guard.service
  /usr/local/libexec/ops-local-metrics
  /usr/local/libexec/ops-daily-report
  /usr/local/libexec/ops-thermal-guard
  /etc/logrotate.d/openbao-audit
  "$audit_base_destination"
  "$audit_watch_destination"
  /opt/ops-control-plane
)
restorecon -F "${installed_paths[@]}" >/dev/null 2>&1 || true

assert_installed_file() {
  local path=$1
  local expected=$2
  local actual
  actual=$(stat -Lc '%U:%G:%a' -- "$path")
  if [[ $actual != "$expected" ]]; then
    echo "Unsafe installed permissions for $path: $actual (expected $expected)" >&2
    exit 73
  fi
}

assert_installed_file /etc/prometheus/prometheus.yml root:prometheus:640
assert_installed_file /etc/prometheus/ops-alerts.yaml root:prometheus:640
assert_installed_file /etc/default/prometheus root:root:640
assert_installed_file /etc/prometheus/alertmanager.yml root:prometheus:640
assert_installed_file /etc/default/prometheus-alertmanager root:root:640
assert_installed_file \
  /etc/systemd/system/prometheus.service.d/10-ops-hardening.conf root:root:644
assert_installed_file \
  /etc/systemd/system/prometheus-alertmanager.service.d/10-ops-hardening.conf \
  root:root:644
assert_installed_file /etc/systemd/journald.conf.d/90-ops.conf root:root:644
assert_installed_file /etc/systemd/system/node-exporter.service root:root:644
assert_installed_file /etc/systemd/system/ops-local-metrics.service root:root:644
assert_installed_file /etc/systemd/system/ops-local-metrics.timer root:root:644
assert_installed_file /etc/systemd/system/ops-daily-report.service root:root:644
assert_installed_file /etc/systemd/system/ops-daily-report.timer root:root:644
assert_installed_file /etc/systemd/system/ops-thermal-guard.service root:root:644
assert_installed_file /usr/local/libexec/ops-local-metrics root:root:750
assert_installed_file /usr/local/libexec/ops-daily-report root:ops-readers:750
assert_installed_file /usr/local/libexec/ops-thermal-guard root:root:750
assert_installed_file /etc/logrotate.d/openbao-audit root:root:644
assert_installed_file "$audit_base_destination" root:root:600
assert_installed_file "$audit_watch_destination" root:root:600

assert_installed_directory() {
  local path=$1
  local expected=$2
  local actual
  actual=$(stat -Lc '%U:%G:%a' -- "$path")
  if [[ $actual != "$expected" ]]; then
    echo "Unsafe installed directory permissions for $path: $actual (expected $expected)" >&2
    exit 73
  fi
}

assert_installed_directory /etc/prometheus root:prometheus:750
assert_installed_directory /var/lib/prometheus prometheus:prometheus:750
assert_installed_directory /var/lib/prometheus/metrics2 prometheus:prometheus:750
assert_installed_directory /var/lib/prometheus/alertmanager prometheus:prometheus:750
assert_installed_directory /var/lib/node-exporter root:node-exporter:750
assert_installed_directory /var/lib/node-exporter/textfile root:node-exporter:750
assert_installed_directory /var/lib/ops-reports ops-monitor:ops-readers:750
assert_installed_directory /var/lib/ops-reports/captures ops-monitor:ops-readers:750
assert_installed_directory /opt/ops-control-plane root:root:755

assert_args_confined /etc/default/prometheus prometheus
assert_args_confined /etc/default/prometheus-alertmanager alertmanager
runuser -u prometheus -- promtool check rules /etc/prometheus/ops-alerts.yaml >/dev/null
runuser -u prometheus -- promtool check config /etc/prometheus/prometheus.yml >/dev/null
runuser -u prometheus -- amtool check-config /etc/prometheus/alertmanager.yml >/dev/null
logrotate --debug /etc/logrotate.d/openbao-audit \
  >"$validation_dir/logrotate-installed-check.log" 2>&1

systemctl daemon-reload
systemd-analyze verify \
  prometheus.service \
  prometheus-alertmanager.service \
  node-exporter.service \
  ops-local-metrics.service \
  ops-local-metrics.timer \
  ops-daily-report.service \
  ops-daily-report.timer \
  ops-thermal-guard.service \
  >"$validation_dir/systemd-installed-check.log" 2>&1
systemd-analyze cat-config systemd/journald.conf \
  >"$validation_dir/journald-check.log"

rollback_audit_rules() {
  if (( audit_base_existed == 1 )); then
    cp -a -- "$validation_dir/audit-base.previous" "$audit_base_destination"
  else
    rm -f -- "$audit_base_destination"
  fi
  if (( audit_watch_existed == 1 )); then
    cp -a -- "$validation_dir/audit-watch.previous" "$audit_watch_destination"
  else
    rm -f -- "$audit_watch_destination"
  fi
  restorecon -F "$audit_base_destination" "$audit_watch_destination" \
    >/dev/null 2>&1 || true
  augenrules --load >/dev/null 2>&1 || true
}

if find /etc/audit/rules.d -maxdepth 1 -type f -name '*.rules' -print0 \
  | xargs -0 grep -Eqs -- '(^|[[:space:]])-a[[:space:]]+(never,task|task,never)([[:space:]]|$)'; then
  echo "A task-never audit suppression rule is present; refusing the ineffective policy." >&2
  rollback_audit_rules
  exit 78
fi
augenrules --check >"$validation_dir/augenrules-check.log" 2>&1 || {
  echo "Audit rule merge check failed; restoring the previous managed rules." >&2
  rollback_audit_rules
  exit 78
}
audit_load_required=$audit_rules_changed
if ! auditctl -l 2>/dev/null | grep -Eq \
  '(-k[[:space:]]+ops_monitoring_config|key=ops_monitoring_config)([[:space:]]|$)'; then
  audit_load_required=1
fi
if (( audit_load_required == 1 )); then
  if ! augenrules --load >"$validation_dir/augenrules-load.log" 2>&1; then
    echo "Audit rule load failed; restoring the previous managed rules." >&2
    rollback_audit_rules
    exit 78
  fi
fi
audit_status=$(auditctl -s 2>/dev/null || true)
audit_enabled=$(awk '$1 == "enabled" { print $2; exit }' <<<"$audit_status")
if [[ ! $audit_enabled =~ ^[12]$ ]]; then
  echo "Linux Audit is not enabled after loading the policy." >&2
  exit 78
fi
loaded_audit_rules=$(auditctl -l 2>/dev/null || true)
if grep -Eq '(^|,)never,task|task,never' <<<"$loaded_audit_rules"; then
  echo "The loaded Linux Audit policy contains a task-never suppression." >&2
  exit 78
fi
for required_key in ops_openbao_config ops_credential_store ops_sudo_policy \
  ops_broker_config ops_monitoring_config ops_ssh_bootstrap \
  ops_memory_config ops_orchestrator_config ops_codex_mcp_config \
  ops_claude_mcp_config ops_claude_instructions \
  ops_broker_release ops_control_plane_release \
  ops_runbook_helpers ops_secret_provisioner; do
  if ! grep -Eq "(-k[[:space:]]+$required_key|key=$required_key)([[:space:]]|$)" \
    <<<"$loaded_audit_rules"; then
    echo "Expected Linux Audit watch is not loaded: $required_key" >&2
    exit 78
  fi
done

activate_unit() {
  local unit=$1
  local changed=$2
  systemctl enable "$unit" >/dev/null
  if (( changed == 1 )) || ! systemctl is-active --quiet "$unit"; then
    systemctl restart "$unit"
  fi
}

activate_unit node-exporter.service "$node_exporter_changed"
activate_unit prometheus-alertmanager.service "$alertmanager_changed"
activate_unit prometheus.service "$prometheus_changed"
activate_unit ops-local-metrics.timer "$local_metrics_changed"
activate_unit ops-daily-report.timer "$daily_report_changed"
activate_unit ops-thermal-guard.service "$thermal_guard_changed"
activate_unit logrotate.timer "$logrotate_changed"

if (( journald_changed == 1 )); then
  systemctl restart systemd-journald.service
fi
systemctl start ops-local-metrics.service

all_listeners_ready=0
for _ in {1..20}; do
  if [[ -n $(ss -H -ltn 'sport = :9090') ]] \
    && [[ -n $(ss -H -ltn 'sport = :9093') ]] \
    && [[ -n $(ss -H -ltn 'sport = :9100') ]]; then
    all_listeners_ready=1
    break
  fi
  sleep 1
done
if (( all_listeners_ready == 0 )); then
  echo "A local monitoring listener did not become ready." >&2
  exit 70
fi

assert_loopback_listener() {
  local port=$1
  local listeners local_address
  local -a socket_fields
  listeners=$(ss -H -ltn "sport = :$port")
  [[ -n $listeners ]] || {
    echo "No TCP listener found on expected port $port." >&2
    return 1
  }
  while IFS=$' \t' read -r -a socket_fields; do
    local_address=${socket_fields[3]:-}
    if [[ $local_address != "127.0.0.1:$port" ]]; then
      echo "Non-loopback listener found for managed port $port: $local_address" >&2
      return 1
    fi
  done <<<"$listeners"
}

assert_loopback_listener 9090
assert_loopback_listener 9093
assert_loopback_listener 9100
if [[ -n $(ss -H -ltn 'sport = :9094') ]] || \
   [[ -n $(ss -H -lun 'sport = :9094') ]]; then
  echo "Alertmanager cluster port 9094 is unexpectedly listening." >&2
  exit 78
fi

for required_unit in prometheus.service prometheus-alertmanager.service \
  node-exporter.service ops-local-metrics.timer ops-daily-report.timer \
  ops-thermal-guard.service logrotate.timer; do
  if ! systemctl is-active --quiet "$required_unit"; then
    echo "Required monitoring unit is not active: $required_unit" >&2
    exit 70
  fi
done

echo "Local monitoring stack installed and verified on 127.0.0.1 only."
echo "OpenBao and AIDE were not initialized; Hermes and Zulip were not enabled."
