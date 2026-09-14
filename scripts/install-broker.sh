#!/usr/bin/env bash
set -Eeuo pipefail
# Record only the script, line and exit status; never command arguments or secrets.
atlas_report_exit() {
  if [[ $1 -ne 0 ]]; then
    logger -t atlas-installer -- "script=install-broker line=$2 status=$1" || true
  fi
  return 0
}
trap 'atlas_report_exit "$?" "$LINENO"' EXIT
IFS=$'\n\t'

usage() {
  cat <<'EOF'
Usage: sudo scripts/install-broker.sh [--enable-api] [--replace-managed-configs]

Installs the root-owned broker runtime under /opt/ops-broker, private state
under /var/lib/ops-broker, and audit/backup timers. The HTTP API socket is
disabled by default. --enable-api explicitly enables its Unix socket for the
zulipbridge-only group; no TCP listener is installed. The replacement flag is
only for repository-managed policy files after their diff has been reviewed.
EOF
}

enable_api=0
replace_managed_configs=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --enable-api)
      enable_api=1
      ;;
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
broker_source="$repository_dir/broker"
install_root=/opt/ops-broker
venv_dir="$install_root/venv"
runbooks_dir="$install_root/runbooks"
defaults_dir="$install_root/defaults"
config_dir=/etc/ops-broker
state_dir=/var/lib/ops-broker
backup_dir=/var/backups/ops-broker
helper_dir=/usr/local/libexec/ops-runbooks
wrapper_dir=/usr/local/libexec/ops-broker
unit_dir=/etc/systemd/system
deployment_source="$repository_dir/deploy/control-plane"
deployment_share=/usr/local/share/ops-control-plane/deployment

required_commands=(
  cmp flock getent install python3 runuser systemctl systemd-analyze systemd-inhibit
  rpm systemd-sysusers systemd-tmpfiles visudo
)
for required_command in "${required_commands[@]}"; do
  if ! command -v "$required_command" >/dev/null 2>&1; then
    echo "Required command is missing: $required_command" >&2
    exit 69
  fi
done

required_files=(
  "$broker_source/pyproject.toml"
  "$broker_source/requirements-mcp.txt"
  "$broker_source/config/rbac.yaml"
  "$broker_source/config/executables.yaml"
  "$repository_dir/policies/actions.yaml"
  "$broker_source/deploy/broker.env"
  "$broker_source/deploy/ops-broker.sysusers"
  "$broker_source/deploy/ops-broker.tmpfiles"
  "$broker_source/deploy/helpers/locate-prod"
  "$broker_source/deploy/helpers/locate-prod-worker"
  "$broker_source/deploy/sudoers/ops-broker-locate-prod"
  "$broker_source/deploy/systemd/ops-locate-prod.service"
  "$broker_source/deploy/helpers/remote-preflight"
  "$broker_source/deploy/helpers/remote-preflight-worker"
  "$broker_source/deploy/sudoers/ops-broker-remote-preflight"
  "$broker_source/deploy/systemd/ops-remote-preflight@.service"
  "$broker_source/deploy/helpers/local-health"
  "$broker_source/deploy/helpers/service-status"
  "$broker_source/deploy/helpers/restart-service"
  "$broker_source/deploy/helpers/nvidia-package-remediation"
  "$broker_source/deploy/helpers/nvidia-package-remediation-worker"
  "$broker_source/deploy/helpers/control-plane-deployment"
  "$broker_source/deploy/helpers/minecraft-crash-triage"
  "$broker_source/deploy/helpers/ops-broker-backup-metric"
  "$broker_source/deploy/helpers/ops-broker-mcp-codex"
  "$broker_source/deploy/helpers/ops-broker-mcp-claude"
  "$broker_source/deploy/sudoers/ops-broker-restart"
  "$broker_source/deploy/sudoers/ops-broker-nvidia-package-remediation"
  "$broker_source/deploy/sudoers/ops-broker-control-plane-deployment"
  "$broker_source/deploy/sudoers/ops-broker-codex"
  "$broker_source/deploy/sudoers/ops-broker-claude"
  "$broker_source/deploy/systemd/ops-nvidia-package-remediation.service"
  "$deployment_source/bin/control-plane-deployment-worker"
  "$repository_dir/scripts/reconcile-broker-zulip-approver"
  "$deployment_source/release-manifest.v1.json"
  "$deployment_source/release-manifest.v1.schema.json"
  "$deployment_source/README.md"
  "$deployment_source/ops-control-plane-deployment.tmpfiles"
)
for required_file in "${required_files[@]}"; do
  if [[ ! -f $required_file || -L $required_file ]]; then
    echo "Required regular source file is missing or symlinked: $required_file" >&2
    exit 66
  fi
done

for required_directory in "$repository_dir/runbooks" "$broker_source/deploy/systemd"; do
  if [[ ! -d $required_directory || -L $required_directory ]]; then
    echo "Required source directory is missing or symlinked: $required_directory" >&2
    exit 66
  fi
done

# Validate the complete release snapshot before the broker or its policy is
# touched. This imports code only from the reviewed path and executes no
# deployment operation.
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -I - \
  "$deployment_source/bin/control-plane-deployment-worker" \
  "$deployment_source/release-manifest.v1.json" <<'PY'
import json
from pathlib import Path
import runpy
import sys

worker = runpy.run_path(sys.argv[1])
manifest_path = Path(sys.argv[2])
document = json.loads(manifest_path.read_text(encoding="utf-8"))
worker["validate_manifest"](document)
source = document["source"]
repository_root = manifest_path.parents[2]
release_marker = repository_root.parent / ".deployment-release.json"
if release_marker.exists():
    marker = json.loads(release_marker.read_text(encoding="utf-8"))
    expected_marker = {
        "schema_version": 1,
        "release_id": document["release_id"],
        "source_tree_sha256": source["tree_sha256"],
        "atlas_rpm_sha256": document["artifacts"]["atlas_rpm"]["sha256"],
        "hermes_source_archive_sha256": document["artifacts"]["hermes_source_archive"]["sha256"],
    }
    if marker != expected_marker:
        raise SystemExit("Staged control-plane marker differs from the manifest.")
    root = repository_root
    atlas_path = repository_root.parent / "artifacts" / Path(
        document["artifacts"]["atlas_rpm"]["source_path"]
    ).name
else:
    root = Path(source["root"])
    atlas_path = Path(document["artifacts"]["atlas_rpm"]["source_path"])
actual = worker["source_tree_digest"](root, source["roots"])
if actual != source["tree_sha256"]:
    raise SystemExit("Control-plane source tree differs from the reviewed manifest.")
worker["validate_component_declarations"](root, document)
atlas = document["artifacts"]["atlas_rpm"]
worker["validate_atlas_rpm"](atlas_path, atlas)
PY

if ! getent passwd ops-user >/dev/null; then
  echo "The supervised local user 'ops-user' does not exist; refusing sudoers install." >&2
  exit 67
fi

if [[ $replace_managed_configs -eq 0 ]]; then
  for policy_pair in \
    "$broker_source/config/rbac.yaml|$config_dir/rbac.yaml" \
    "$broker_source/config/executables.yaml|$config_dir/executables.yaml"; do
    policy_source=${policy_pair%%|*}
    policy_target=${policy_pair#*|}
    if [[ -e $policy_target ]] && ! cmp -s -- "$policy_source" "$policy_target"; then
      echo "Reviewed runbooks require the matching managed broker policies." >&2
      echo "Re-run the separately approved broker update with --replace-managed-configs." >&2
      exit 78
    fi
  done
fi

install -d -o root -g root -m 0755 /run/lock
exec {install_lock}>/run/lock/ops-broker-install.lock
if ! flock -n "$install_lock"; then
  echo "Another ops-broker installation is running." >&2
  exit 75
fi

runbooks_stage=""
runbooks_previous=""
runbooks_swapped=0
api_was_active=0
installation_complete=0

if systemctl is-active --quiet ops-broker.service 2>/dev/null || \
   systemctl is-active --quiet ops-broker.socket 2>/dev/null; then
  api_was_active=1
fi

safe_remove_staging_directory() {
  local target=$1
  case "$target" in
    "$install_root"/.runbooks.*|"$runbooks_dir")
      rm -rf -- "$target"
      ;;
    *)
      echo "Refusing to remove unexpected staging path: $target" >&2
      return 1
      ;;
  esac
}

finish_installation() {
  local result=$1
  atlas_report_exit "$result" "$2"
  trap - EXIT
  if [[ -n $runbooks_stage && -d $runbooks_stage ]]; then
    safe_remove_staging_directory "$runbooks_stage" || true
  fi
  if [[ $result -ne 0 && -n $runbooks_previous && -d $runbooks_previous ]]; then
    if [[ $runbooks_swapped -eq 1 && -d $runbooks_dir ]]; then
      safe_remove_staging_directory "$runbooks_dir"
    fi
    if [[ ! -e $runbooks_dir ]]; then
      mv -- "$runbooks_previous" "$runbooks_dir"
    fi
  fi
  if [[ $result -ne 0 && $api_was_active -eq 1 ]]; then
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl start ops-broker.socket >/dev/null 2>&1 || true
  fi
  if [[ $installation_complete -eq 1 && -n $runbooks_previous && -d $runbooks_previous ]]; then
    safe_remove_staging_directory "$runbooks_previous"
  fi
  exit "$result"
}
trap 'finish_installation "$?" "$LINENO"' EXIT

for managed_path in "$install_root" "$config_dir" "$state_dir" "$backup_dir" \
  "$deployment_share" /var/lib/ops-control-plane-deployment \
  /var/backups/ops-control-plane-deployment; do
  if [[ -L $managed_path ]]; then
    echo "Managed path must not be a symbolic link: $managed_path" >&2
    exit 73
  fi
done

install -o root -g root -m 0644 "$broker_source/deploy/ops-broker.sysusers" \
  /etc/sysusers.d/ops-broker.conf
install -o root -g root -m 0644 "$broker_source/deploy/ops-broker.tmpfiles" \
  /etc/tmpfiles.d/ops-broker.conf
systemd-sysusers /etc/sysusers.d/ops-broker.conf

api_group_record=$(getent group opsbroker-api) || {
  echo "The opsbroker-api group was not created." >&2
  exit 67
}
IFS=: read -r api_group_name _ api_group_gid api_group_members <<<"$api_group_record"
if [[ $api_group_name != opsbroker-api || -z $api_group_gid ]]; then
  echo "The opsbroker-api group record is malformed." >&2
  exit 67
fi
IFS=, read -r -a api_members <<<"$api_group_members"
for api_member in "${api_members[@]}"; do
  if [[ -n $api_member && $api_member != zulipbridge ]]; then
    echo "Unexpected supplementary member in opsbroker-api: $api_member" >&2
    exit 73
  fi
done
while IFS=: read -r account_name _ _ primary_gid _; do
  if [[ $primary_gid == "$api_group_gid" ]]; then
    echo "The dedicated opsbroker-api group is a primary group for $account_name." >&2
    exit 73
  fi
done < <(getent passwd)

for runtime_path in "$state_dir" "$backup_dir" /run/ops-broker; do
  if [[ -L $runtime_path ]]; then
    echo "Runtime path must not be a symbolic link: $runtime_path" >&2
    exit 73
  fi
done
systemd-tmpfiles --create /etc/tmpfiles.d/ops-broker.conf
install -o root -g root -m 0644 \
  "$deployment_source/ops-control-plane-deployment.tmpfiles" \
  /etc/tmpfiles.d/ops-control-plane-deployment.conf
systemd-tmpfiles --create /etc/tmpfiles.d/ops-control-plane-deployment.conf

if [[ -e $venv_dir ]]; then
  if [[ ! -d $venv_dir || -L $venv_dir ]]; then
    echo "Existing venv path is not a real directory: $venv_dir" >&2
    exit 73
  fi
  foreign_owner=$(find "$venv_dir" -xdev ! -user root -print -quit)
  if [[ -n $foreign_owner ]]; then
    echo "Existing production venv contains a non-root-owned entry; refusing update." >&2
    exit 73
  fi
fi

for sudoers_source in "$broker_source"/deploy/sudoers/*; do
  visudo -cf "$sudoers_source" >/dev/null
done

systemctl stop ops-broker.socket >/dev/null 2>&1 || true
systemctl stop ops-broker.service >/dev/null 2>&1 || true

for code_directory in "$install_root" "$defaults_dir" "$helper_dir" "$wrapper_dir"; do
  if [[ -L $code_directory ]]; then
    echo "Executable/configuration directory must not be a symbolic link: $code_directory" >&2
    exit 73
  fi
done
install -d -o root -g root -m 0755 "$install_root" "$defaults_dir"
if [[ ! -x $venv_dir/bin/python ]]; then
  python3 -m venv "$venv_dir"
fi
"$venv_dir/bin/python" -m pip install \
  --disable-pip-version-check \
  --require-virtualenv \
  --upgrade \
  --no-deps \
  --only-binary=:all: \
  -r "$broker_source/requirements-mcp.txt"
# setuptools writes build metadata into its source. Keep the reviewed tree intact.
(
  build_source=$(mktemp -d)
  trap 'rm -rf -- "$build_source"' EXIT
  cp -a "$broker_source/." "$build_source/"
  "$venv_dir/bin/python" -m pip install \
    --disable-pip-version-check \
    --require-virtualenv \
    --upgrade \
    --no-build-isolation \
    --no-deps \
    "$build_source"
)
PYTHONDONTWRITEBYTECODE=1 "$venv_dir/bin/python" - \
  "$broker_source/requirements-mcp.txt" <<'PY'
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
install -o root -g root -m 0644 "$broker_source/README.md" "$install_root/README.md"

runbooks_stage=$(mktemp -d "$install_root/.runbooks.new.XXXXXX")
chmod 0755 "$runbooks_stage"
while IFS= read -r -d '' source_runbook; do
  relative_runbook=${source_runbook#"$repository_dir/runbooks/"}
  install -D -o root -g root -m 0644 \
    "$source_runbook" "$runbooks_stage/$relative_runbook"
done < <(find "$repository_dir/runbooks" -xdev -type f -name '*.yaml' -print0)
if [[ -e $runbooks_dir ]]; then
  if [[ ! -d $runbooks_dir || -L $runbooks_dir ]]; then
    echo "Installed runbooks path is not a real directory." >&2
    exit 73
  fi
  runbooks_previous=$(mktemp -d "$install_root/.runbooks.previous.XXXXXX")
  rmdir -- "$runbooks_previous"
  mv -- "$runbooks_dir" "$runbooks_previous"
fi
mv -- "$runbooks_stage" "$runbooks_dir"
runbooks_stage=""
runbooks_swapped=1

install -o root -g root -m 0644 "$repository_dir/policies/actions.yaml" \
  "$defaults_dir/actions.yaml"
install -o root -g root -m 0644 "$broker_source/config/rbac.yaml" \
  "$defaults_dir/rbac.yaml"
install -o root -g root -m 0644 "$broker_source/config/executables.yaml" \
  "$defaults_dir/executables.yaml"

install -d -o root -g opsbroker -m 0750 "$config_dir"
seed_config() {
  local source_file=$1
  local destination_file=$2
  if [[ -L $destination_file ]]; then
    echo "Configuration path must not be a symbolic link: $destination_file" >&2
    return 73
  fi
  if [[ ! -e $destination_file || $replace_managed_configs -eq 1 ]]; then
    install -o root -g opsbroker -m 0640 "$source_file" "$destination_file"
  elif [[ ! -f $destination_file ]]; then
    echo "Configuration path is not a regular file: $destination_file" >&2
    return 73
  else
    chown root:opsbroker "$destination_file"
    chmod 0640 "$destination_file"
  fi
}
seed_config "$repository_dir/policies/actions.yaml" "$config_dir/actions.yaml"
seed_config "$broker_source/config/rbac.yaml" "$config_dir/rbac.yaml"
seed_config "$broker_source/config/executables.yaml" "$config_dir/executables.yaml"
install -o root -g opsbroker -m 0640 "$broker_source/deploy/broker.env" \
  "$config_dir/broker.env"

install -d -o root -g root -m 0755 "$helper_dir" "$wrapper_dir"
for helper_name in \
  local-health service-status restart-service nvidia-package-remediation \
  nvidia-package-remediation-worker minecraft-crash-triage \
  control-plane-deployment remote-preflight remote-preflight-worker locate-prod locate-prod-worker; do
  helper_target="$helper_dir/$helper_name"
  if [[ -L $helper_target ]]; then
    echo "Helper destination must not be a symbolic link: $helper_target" >&2
    exit 73
  fi
  install -o root -g root -m 0755 \
    "$broker_source/deploy/helpers/$helper_name" "$helper_target"
done
wrapper_target="$wrapper_dir/ops-broker-mcp-codex"
if [[ -L $wrapper_target ]]; then
  echo "Wrapper destination must not be a symbolic link: $wrapper_target" >&2
  exit 73
fi
install -o root -g root -m 0755 \
  "$broker_source/deploy/helpers/ops-broker-mcp-codex" "$wrapper_target"
claude_wrapper_target="$wrapper_dir/ops-broker-mcp-claude"
if [[ -L $claude_wrapper_target ]]; then
  echo "Wrapper destination must not be a symbolic link: $claude_wrapper_target" >&2
  exit 73
fi
install -o root -g root -m 0755 \
  "$broker_source/deploy/helpers/ops-broker-mcp-claude" "$claude_wrapper_target"
metric_helper_target="$wrapper_dir/ops-broker-backup-metric"
if [[ -L $metric_helper_target ]]; then
  echo "Metric helper destination must not be a symbolic link: $metric_helper_target" >&2
  exit 73
fi
install -o root -g root -m 0755 \
  "$broker_source/deploy/helpers/ops-broker-backup-metric" "$metric_helper_target"
approver_reconciler_target="$helper_dir/reconcile-zulip-approver"
if [[ -L $approver_reconciler_target ]]; then
  echo "Approver reconciler destination must not be a symbolic link: $approver_reconciler_target" >&2
  exit 73
fi
install -o root -g root -m 0755 \
  "$repository_dir/scripts/reconcile-broker-zulip-approver" "$approver_reconciler_target"
deployment_worker_target="$helper_dir/control-plane-deployment-worker"
if [[ -L $deployment_worker_target ]]; then
  echo "Deployment worker destination must not be a symbolic link: $deployment_worker_target" >&2
  exit 73
fi
install -o root -g root -m 0755 \
  "$deployment_source/bin/control-plane-deployment-worker" "$deployment_worker_target"
install -d -o root -g root -m 0755 "$deployment_share"
install -o root -g root -m 0644 \
  "$deployment_source/release-manifest.v1.json" \
  "$deployment_share/release-manifest.v1.json"
install -o root -g root -m 0644 \
  "$deployment_source/release-manifest.v1.schema.json" \
  "$deployment_share/release-manifest.v1.schema.json"
install -o root -g root -m 0644 \
  "$deployment_source/README.md" "$deployment_share/README.md"

for sudoers_name in \
  ops-broker-restart ops-broker-nvidia-package-remediation \
  ops-broker-control-plane-deployment ops-broker-codex ops-broker-claude ops-broker-remote-preflight ops-broker-locate-prod; do
  sudoers_target="/etc/sudoers.d/$sudoers_name"
  if [[ -L $sudoers_target ]]; then
    echo "Sudoers destination must not be a symbolic link: $sudoers_target" >&2
    exit 73
  fi
  install -o root -g root -m 0440 \
    "$broker_source/deploy/sudoers/$sudoers_name" "$sudoers_target"
done
visudo -c >/dev/null

for unit_source in "$broker_source"/deploy/systemd/*; do
  unit_name=$(basename "$unit_source")
  unit_target="$unit_dir/$unit_name"
  if [[ -L $unit_target ]]; then
    echo "Unit destination must not be a symbolic link: $unit_target" >&2
    exit 73
  fi
  install -o root -g root -m 0644 "$unit_source" "$unit_target"
done
if command -v restorecon >/dev/null 2>&1; then
  if ! restorecon -RF \
    "$install_root" "$config_dir" "$state_dir" "$backup_dir" \
    "$helper_dir" "$wrapper_dir" /run/ops-broker \
    /etc/sysusers.d/ops-broker.conf /etc/tmpfiles.d/ops-broker.conf \
    /etc/sudoers.d/ops-broker-restart /etc/sudoers.d/ops-broker-codex \
    /etc/sudoers.d/ops-broker-claude \
    /etc/sudoers.d/ops-broker-nvidia-package-remediation \
    /etc/sudoers.d/ops-broker-control-plane-deployment \
    /etc/tmpfiles.d/ops-control-plane-deployment.conf \
    "$deployment_share" "$deployment_worker_target" \
    "$unit_dir"/ops-broker* "$unit_dir"/ops-nvidia-package-remediation.service; then
    echo "Warning: restorecon reported an error; inspect SELinux labels before enabling API." >&2
  fi
fi

systemd-analyze verify "$unit_dir"/ops-broker.socket "$unit_dir"/ops-broker.service \
  "$unit_dir"/ops-broker-audit-verify.service \
  "$unit_dir"/ops-broker-audit-verify.timer \
  "$unit_dir"/ops-broker-backup.service "$unit_dir"/ops-broker-backup.timer \
  "$unit_dir"/ops-broker-backup-metric-success.service \
  "$unit_dir"/ops-broker-backup-metric-failure.service \
  "$unit_dir"/ops-nvidia-package-remediation.service

run_as_broker() {
  runuser -u opsbroker -- /usr/bin/env -i \
    PATH=/usr/sbin:/usr/bin \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    OPS_BROKER_DATABASE="$state_dir/state.db" \
    OPS_BROKER_RUNBOOKS="$runbooks_dir" \
    OPS_BROKER_ACTION_POLICY="$config_dir/actions.yaml" \
    OPS_BROKER_RBAC_POLICY="$config_dir/rbac.yaml" \
    OPS_BROKER_EXECUTABLES_POLICY="$config_dir/executables.yaml" \
    "$@"
}
state_database="$state_dir/state.db"
if [[ -L $state_database || ( -e $state_database && ! -f $state_database ) ]]; then
  echo "Broker state database must be a regular, non-symlink file." >&2
  exit 73
fi
if [[ -e $state_database && $(stat -c '%U:%G' "$state_database") != opsbroker:opsbroker ]]; then
  echo "Existing broker state has unexpected ownership; refusing to take it over." >&2
  exit 73
fi
run_as_broker "$venv_dir/bin/python" -c \
  'from ops_broker.factory import build_service; service = build_service(); assert service.database.verify_audit_chain().valid'
chmod 0600 "$state_database"
run_as_broker "$venv_dir/bin/ops-broker-verify" --database "$state_database"
if [[ -f /var/lib/ops-control-plane-bootstrap/zulip-approver.json ]]; then
  "$approver_reconciler_target" apply >/dev/null
fi

systemctl daemon-reload
systemctl enable --now ops-broker-audit-verify.timer ops-broker-backup.timer
systemctl start ops-broker-audit-verify.service
systemctl start ops-broker-backup.service

# The service is socket-activated only; never leave a direct boot enablement.
systemctl disable ops-broker.service >/dev/null 2>&1 || true
if [[ $enable_api -eq 1 ]]; then
  systemctl enable --now ops-broker.socket
  echo "Unix API enabled at /run/ops-broker/api.sock (group opsbroker-api)."
else
  systemctl disable --now ops-broker.socket ops-broker.service >/dev/null 2>&1 || true
  echo "Unix API remains disabled. Re-run with --enable-api only after the bridge is ready."
fi

installation_complete=1
echo "ops-broker installed. State and backups remain readable only by opsbroker/root."
