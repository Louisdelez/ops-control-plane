#!/usr/bin/env bash
set -Eeuo pipefail
# Record only the script, line and exit status; never command arguments or secrets.
atlas_report_exit() {
  if [[ $1 -ne 0 ]]; then
    logger -t atlas-installer -- "script=install-hermes line=$2 status=$1" || true
  fi
  return 0
}
trap 'atlas_report_exit "$?" "$LINENO"' EXIT
IFS=$'\n\t'
umask 077

readonly hermes_version=0.21.0
readonly hermes_tag=v2026.8.31
readonly hermes_commit=29112bef099274229cadff79cdff7bf7b99c4b77
readonly uv_version=0.12.0
readonly archive_sha256=76b99a8be9b77d66833c3cfe2b35c6d6f6a58e4ff9637ef8effcfc1f420ab35a
readonly archive_compressed_size=69442866
source_archive=

usage() {
  cat <<'EOF'
Usage: sudo scripts/install-hermes.sh --archive /ABSOLUTE/PATH/TO/HERMES.tar.gz

Installs or verifies the pinned Hermes 0.21.0 vendored source, deploys root-owned
profiles, five procedural skills, three fixed-identity MCP wrappers and a
dormant system unit. It never creates the activation marker and never enables
or starts hermes-gateway.service.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --archive)
      [[ $# -ge 2 ]] || { usage >&2; exit 64; }
      source_archive=$2
      shift 2
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
done

if [[ -z $source_archive || $source_archive != /* ]]; then
  echo "--archive with an absolute path is required." >&2
  usage >&2
  exit 64
fi

if [[ $EUID -ne 0 ]]; then
  echo "Run as root." >&2
  exit 77
fi

repository_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
readonly repository_dir
readonly hermes_home=/var/lib/hermes
readonly install_dir=$hermes_home/hermes-agent
readonly hermes_execution_path=$hermes_home/bin:$hermes_home/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
readonly source_staging=$hermes_home/.atlas-api-zulip-2026.09.08.11-hermes-source-staging
readonly upstream_tmp=$hermes_home/.atlas-api-zulip-2026.09.08.11-hermes-upstream-tmp
readonly uv_config_staging=$hermes_home/.atlas-api-zulip-2026.09.08.11-hermes-uv-config
readonly unit_name=hermes-gateway.service
readonly activation_marker=/etc/hermes/hermes-gateway.enabled
readonly openbao_policy_dir=/usr/local/share/ops-control-plane/openbao/policies
readonly -a hermes_openbao_policies=(
  hermes-runtime.hcl
  hermes-coordinator.hcl
  deepseek-client.hcl
)

required_commands=(install runuser systemctl systemd-analyze systemd-sysusers systemd-tmpfiles systemd-creds visudo python3 curl sleep stat cmp flock getent chown chmod grep find basename id tr sha256sum sync mkdir mv rm)
for command_name in "${required_commands[@]}"; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is missing: $command_name" >&2
    exit 69
  }
done

if [[ ! -f $source_archive || -L $source_archive ]]; then
  echo "The vendored Hermes archive is unavailable or symlinked." >&2
  exit 66
fi
archive_metadata=$(stat -c '%u:%g:%a:%h:%s' -- "$source_archive")
if [[ $archive_metadata != "0:0:444:1:$archive_compressed_size" ]]; then
  echo "The vendored Hermes archive metadata differs from the staged release." >&2
  exit 65
fi
archive_observed=$(sha256sum -- "$source_archive")
if [[ ${archive_observed%% *} != "$archive_sha256" ]]; then
  echo "The vendored Hermes archive checksum differs from review." >&2
  exit 65
fi

required_files=(
  "$repository_dir/config/hermes/default/config.yaml"
  "$repository_dir/config/hermes/default/SOUL.md"
  "$repository_dir/config/hermes/ops-broker-mcp-profile.sudoers"
  "$repository_dir/config/hermes/ops-local-mcp-profile.sudoers"
  "$repository_dir/config/openbao/policies/hermes-runtime.hcl"
  "$repository_dir/config/openbao/policies/hermes-coordinator.hcl"
  "$repository_dir/config/openbao/policies/deepseek-client.hcl"
  "$repository_dir/scripts/hermes-secrets-env"
  "$repository_dir/scripts/hermes-gateway-preflight"
  "$repository_dir/scripts/extract-hermes-source.py"
  "$repository_dir/scripts/ops-broker-mcp-profile"
  "$repository_dir/scripts/ops-orchestrator-mcp-profile"
  "$repository_dir/scripts/ops-memory-mcp-profile"
  "$repository_dir/scripts/verify-hermes-mcp-live.py"
  "$repository_dir/scripts/provision-hermes-openbao"
  "$repository_dir/scripts/validate-hermes-config.py"
  "$repository_dir/scripts/verify-hermes-sync-check.py"
  "$repository_dir/scripts/vendor/uv-0.12.0-installer.sh"
  "$repository_dir/systemd/hermes-gateway.service"
  "$repository_dir/systemd/ops-orchestrator-hermes-facade.service"
  "$repository_dir/systemd/ops-users.conf"
  "$repository_dir/systemd/ops-directories.conf"
  "$repository_dir/orchestrator/deploy/ops-orchestrator.sysusers"
  "$repository_dir/memory/deploy/ops-memory.sysusers"
)
for source_file in "${required_files[@]}"; do
  [[ -f $source_file && ! -L $source_file ]] || {
    echo "Required regular source file is missing or symlinked: $source_file" >&2
    exit 66
  }
done
for policy_source_dir in \
  "$repository_dir/config/openbao" \
  "$repository_dir/config/openbao/policies"; do
  if [[ ! -d $policy_source_dir || -L $policy_source_dir ]]; then
    echo "OpenBao policy source directory is missing or symlinked: $policy_source_dir" >&2
    exit 66
  fi
done

if [[ ! -x /opt/ops-orchestrator/venv/bin/ops-orchestrator-hermes-facade \
  || -L /opt/ops-orchestrator/venv/bin/ops-orchestrator-hermes-facade \
  || ! -f /etc/systemd/system/ops-orchestrator-hermes-facade.service \
  || -L /etc/systemd/system/ops-orchestrator-hermes-facade.service ]]; then
  echo "Install the API-first orchestrator and its Hermes facade before Hermes." >&2
  exit 66
fi

"$repository_dir/scripts/validate-hermes-config.py"
visudo -cf "$repository_dir/config/hermes/ops-broker-mcp-profile.sudoers" >/dev/null
visudo -cf "$repository_dir/config/hermes/ops-local-mcp-profile.sudoers" >/dev/null

if [[ -e $activation_marker || -L $activation_marker ]]; then
  echo "Activation marker already exists; refusing a supposedly dormant install." >&2
  echo "Stop Hermes and remove $activation_marker deliberately before reinstalling." >&2
  exit 73
fi
if systemctl is-active --quiet "$unit_name" 2>/dev/null; then
  echo "$unit_name is running; stop it explicitly before installation." >&2
  exit 73
fi

install -d -o root -g root -m 0755 /run/lock
exec {install_lock}>/run/lock/hermes-install.lock
flock -n "$install_lock" || { echo "Another Hermes installation is running." >&2; exit 75; }

reject_symlink() {
  local managed_path=$1
  if [[ -L $managed_path ]]; then
    echo "Managed path must not be a symbolic link: $managed_path" >&2
    exit 73
  fi
}

for managed_path in \
  "$hermes_home" "$install_dir" "$source_staging" "$upstream_tmp" \
  "$uv_config_staging" /etc/hermes \
  /etc/credstore.encrypted \
  /usr/local/share/ops-control-plane \
  /usr/local/share/ops-control-plane/openbao \
  "$openbao_policy_dir" \
  "$openbao_policy_dir/hermes-runtime.hcl" \
  "$openbao_policy_dir/hermes-coordinator.hcl" \
  "$openbao_policy_dir/deepseek-client.hcl" \
  /etc/sysusers.d/ops-control-plane.conf /etc/tmpfiles.d/ops-control-plane.conf \
  /usr/local/libexec/ops-broker-mcp-profile /usr/local/libexec/hermes-gateway-preflight \
  /usr/local/libexec/ops-orchestrator-mcp-profile /usr/local/libexec/ops-memory-mcp-profile \
  /usr/local/libexec/hermes-secrets-env /usr/local/sbin/provision-hermes-openbao \
  /usr/local/libexec/verify-hermes-mcp-live \
  /etc/sudoers.d/hermes-ops-broker-mcp /etc/sudoers.d/hermes-local-mcp \
  /etc/systemd/system/hermes-gateway.service; do
  reject_symlink "$managed_path"
done
if find "$repository_dir/config/hermes" -type l -print -quit | grep -q .; then
  echo "Hermes source configuration must not contain symbolic links." >&2
  exit 73
fi

install -o root -g root -m 0644 "$repository_dir/systemd/ops-users.conf" /etc/sysusers.d/ops-control-plane.conf
install -o root -g root -m 0644 "$repository_dir/systemd/ops-directories.conf" /etc/tmpfiles.d/ops-control-plane.conf
install -d -o root -g root -m 0700 /etc/credstore.encrypted
systemd-sysusers /etc/sysusers.d/ops-control-plane.conf
systemd-sysusers \
  "$repository_dir/orchestrator/deploy/ops-orchestrator.sysusers" \
  "$repository_dir/memory/deploy/ops-memory.sysusers"
systemd-tmpfiles --create /etc/tmpfiles.d/ops-control-plane.conf
if ! getent passwd hermesd >/dev/null; then
  echo "Required service accounts were not created." >&2
  exit 67
fi
if ! getent passwd opsbroker >/dev/null; then
  echo "Required service accounts were not created." >&2
  exit 67
fi
for api_group in opsorchestrator-api ops-memory-api; do
  getent group "$api_group" >/dev/null || {
    echo "Required local API group was not created: $api_group" >&2
    exit 67
  }
done
for service_account in hermesd minecraft-ops infra-network ops-monitor backup-agent deploy-agent security-audit; do
  for api_group in opsorchestrator-api ops-memory-api; do
    if ! id -nG "$service_account" | tr ' ' '\n' | grep -Fxq "$api_group"; then
      echo "$service_account is not a member of $api_group." >&2
      exit 67
    fi
  done
done

if [[ ! -e $install_dir ]]; then
  if [[ $(stat -c '%d' /var/lib) != "$(stat -c '%d' "$hermes_home")" ]]; then
    echo "Hermes home is not on the reviewed atomic staging filesystem." >&2
    exit 73
  fi
  extraction_root=$source_staging
  if ! mkdir -m 0700 -- "$extraction_root"; then
    echo "The release-bound Hermes source staging path is not empty." >&2
    exit 73
  fi
  cleanup_extraction() {
    if [[ -n ${extraction_root:-} && $extraction_root == "$source_staging" ]]; then
      rm -rf -- "$extraction_root"
    elif [[ -n ${extraction_root:-} ]]; then
      echo "Unsafe Hermes extraction path." >&2
    fi
  }
  trap 'atlas_exit_status=$?; atlas_report_exit "$atlas_exit_status" "$LINENO"; cleanup_extraction; exit "$atlas_exit_status"' EXIT
  chown root:root "$extraction_root"
  chmod 0700 "$extraction_root"
  /usr/bin/python3 -I -S -B "$repository_dir/scripts/extract-hermes-source.py" \
    "$source_archive" "$extraction_root"
  chown -R hermesd:hermesd "$extraction_root"
  # Extraction fsyncs every file and directory before returning. Persist the
  # subsequent ownership transition before publishing the tree, then persist
  # the atomic rename itself. `sync -f` maps to syncfs(2) for this filesystem.
  sync -f "$extraction_root"
  [[ ! -e $install_dir && ! -L $install_dir ]] || {
    echo "Hermes install path appeared during source staging." >&2
    exit 73
  }
  mv -T -- "$extraction_root" "$install_dir"
  sync -f "$hermes_home"
  extraction_root=
  trap 'atlas_report_exit "$?" "$LINENO"' EXIT
elif [[ ! -d $install_dir || -L $install_dir ]]; then
  echo "Existing Hermes install path is unsafe." >&2
  exit 73
fi

receipt=$install_dir/.atlas-source.json
if [[ ! -f $receipt || -L $receipt ]]; then
  echo "Hermes source provenance receipt is absent or unsafe." >&2
  exit 65
fi
/usr/bin/python3 -I -S -B "$repository_dir/scripts/extract-hermes-source.py" \
  --verify-installed "$install_dir"
grep -Eq '^version = "0\.21\.0"$' "$install_dir/pyproject.toml" || {
  echo "Pinned source does not declare Hermes 0.21.0." >&2
  exit 65
}

runtime_ready=0
if [[ -x $install_dir/venv/bin/python ]]; then
  version_output=$(runuser -u hermesd -- env HOME="$hermes_home" HERMES_HOME="$hermes_home" \
    PATH="$hermes_execution_path" UV_NO_MODIFY_PATH=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    "$install_dir/venv/bin/python" -m hermes_cli.main --version 2>/dev/null || true)
  [[ $version_output == *"0.21.0"* ]] && runtime_ready=1
fi

upstream_tmp_created=0
uv_config_created=0
cleanup_runtime_staging() {
  if [[ $uv_config_created -eq 1 ]]; then
    rm -rf -- "$uv_config_staging"
  fi
  if [[ $upstream_tmp_created -eq 1 ]]; then
    rm -rf -- "$upstream_tmp"
  fi
}
trap 'atlas_exit_status=$?; atlas_report_exit "$atlas_exit_status" "$LINENO"; cleanup_runtime_staging; exit "$atlas_exit_status"' EXIT
if ! mkdir -m 0700 -- "$upstream_tmp"; then
  echo "The release-bound Hermes upstream staging path is not empty." >&2
  exit 73
fi
upstream_tmp_created=1
chown hermesd:hermesd "$upstream_tmp"

# Install the pinned, checksum-verifying uv installer before upstream can
# download its moving "latest" URL. The vendored script is covered by the
# reviewed source-tree digest and embeds the release archive SHA-256 values.
managed_uv="$hermes_home/bin/uv"
reviewed_uv_present() {
  local reported
  [[ -x $managed_uv && ! -L $managed_uv ]] || return 1
  reported=$(runuser -u hermesd -- "$managed_uv" --version) || return 1
  [[ $reported == "uv $uv_version" || $reported == "uv $uv_version ("*")" ]]
}
if ! reviewed_uv_present; then
  install -d -o hermesd -g hermesd -m 0750 "$hermes_home/bin"
  runuser -u hermesd -- env HOME="$hermes_home" \
    UV_UNMANAGED_INSTALL="$hermes_home/bin" UV_NO_MODIFY_PATH=1 \
    /usr/bin/sh < "$repository_dir/scripts/vendor/uv-0.12.0-installer.sh"
fi
reviewed_uv_present || { echo "Pinned uv installation failed verification." >&2; exit 65; }

if [[ $runtime_ready -eq 0 ]]; then
  chown -R hermesd:hermesd "$install_dir"
  # Only bootstrap uv/Python and the venv here. Dependency installation is
  # performed exactly once below with the committed lock; the upstream
  # python-deps fallback tiers are deliberately never entered.
  runuser -u hermesd -- env HOME="$hermes_home" HERMES_HOME="$hermes_home" \
    PATH="$hermes_execution_path" UV_NO_MODIFY_PATH=1 \
    TMPDIR="$upstream_tmp" \
    UV_CACHE_DIR="$hermes_home/.cache/uv" \
    UV_PYTHON_INSTALL_DIR="$hermes_home/.local/share/uv/python" \
    UV_PYTHON_BIN_DIR="$hermes_home/.local/share/uv/bin" \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_VERSION="$uv_version" \
    /usr/bin/bash "$install_dir/scripts/install.sh" \
    --stage venv --dir "$install_dir" --hermes-home "$hermes_home" \
    --commit "$hermes_commit" --skip-setup --skip-browser \
    --skip-computer-use --no-skills --non-interactive
fi

# The upstream installer has availability fallbacks that may resolve directly
# from PyPI. They are not acceptable for this managed deployment: always finish
# with the committed, hash-bearing uv.lock and fail if that exact sync cannot be
# reproduced. This also verifies a pre-existing runtime rather than trusting its
# installation history.
if ! reviewed_uv_present; then
  echo "Hermes managed uv is not the reviewed version $uv_version." >&2
  exit 65
fi
if [[ ! -f $install_dir/uv.lock || -L $install_dir/uv.lock ]]; then
  echo "Hermes source has no safe committed uv.lock." >&2
  exit 65
fi
if ! mkdir -m 0700 -- "$uv_config_staging"; then
  echo "The release-bound Hermes uv configuration staging path is not empty." >&2
  exit 73
fi
uv_config_created=1
chown hermesd:hermesd "$uv_config_staging"
uv_sync_mode=()
if [[ $runtime_ready -eq 1 ]]; then
  # The installed tree is root-owned after a successful run. `--check` proves
  # exact lock synchronization without granting the service account a write
  # window merely because the installer was rerun.
  uv_sync_mode=(--check)
fi
if ! uv_sync_report=$(runuser -u hermesd -- env -u UV_NO_CONFIG -u UV_CONFIG_FILE \
  HOME="$hermes_home" HERMES_HOME="$hermes_home" \
  PATH="$hermes_execution_path" UV_NO_MODIFY_PATH=1 \
  TMPDIR="$upstream_tmp" \
  PYTHONDONTWRITEBYTECODE=1 \
  XDG_CONFIG_HOME="$uv_config_staging" \
  XDG_CONFIG_DIRS="$uv_config_staging" \
  UV_CACHE_DIR="$hermes_home/.cache/uv" \
  UV_PYTHON_INSTALL_DIR="$hermes_home/.local/share/uv/python" \
  UV_PYTHON_BIN_DIR="$hermes_home/.local/share/uv/bin" \
  UV_PROJECT_ENVIRONMENT="$install_dir/venv" \
  UV_PYTHON="$install_dir/venv/bin/python" \
  "$managed_uv" sync --project "$install_dir" --extra all --locked \
  "${uv_sync_mode[@]}" --output-format json); then
  # Sealing ownership changes filesystem freshness without changing source
  # bytes. Permit only a rebuild of this exact editable project; every other
  # proposed package change still fails the strict lock verification.
  if [[ $runtime_ready -ne 1 ]] || ! printf '%s' "$uv_sync_report" | \
    /usr/bin/python3 -I -S -B "$repository_dir/scripts/verify-hermes-sync-check.py" "$install_dir"; then
    echo "Hermes hash-locked dependency synchronization failed." >&2
    exit 65
  fi
fi

rm -rf -- "$uv_config_staging" "$upstream_tmp"
uv_config_created=0
upstream_tmp_created=0
sync -f "$hermes_home"
trap 'atlas_report_exit "$?" "$LINENO"' EXIT

version_output=$(runuser -u hermesd -- env HOME="$hermes_home" HERMES_HOME="$hermes_home" \
  PATH="$hermes_execution_path" UV_NO_MODIFY_PATH=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  "$install_dir/venv/bin/python" -m hermes_cli.main --version)
[[ $version_output == *"0.21.0"* ]] || { echo "Hermes runtime version check failed." >&2; exit 65; }
reject_symlink "$install_dir/.install_method"
install -o hermesd -g hermesd -m 0644 /dev/null "$install_dir/.install_method"
printf 'unknown\n' > "$install_dir/.install_method"
chown -R root:hermesd "$install_dir"
chmod -R u=rwX,g=rX,o= "$install_dir"
/usr/bin/python3 -I -S -B "$repository_dir/scripts/extract-hermes-source.py" \
  --verify-installed "$install_dir"
sync -f "$install_dir"
version_output=$(runuser -u hermesd -- env HOME="$hermes_home" HERMES_HOME="$hermes_home" \
  PATH="$hermes_execution_path" UV_NO_MODIFY_PATH=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  "$install_dir/venv/bin/python" -m hermes_cli.main --version)
[[ $version_output == *"0.21.0"* ]] || { echo "Hermes sealed runtime version check failed." >&2; exit 65; }

install -d -o root -g hermesd -m 0750 /etc/hermes
install -d -o hermesd -g hermesd -m 0700 "$hermes_home/profiles" "$hermes_home/workspace"
for managed_path in "$hermes_home/config.yaml" "$hermes_home/SOUL.md" "$hermes_home/.no-bundled-skills"; do
  reject_symlink "$managed_path"
done
install -o root -g hermesd -m 0640 "$repository_dir/config/hermes/default/config.yaml" "$hermes_home/config.yaml"
install -o root -g hermesd -m 0640 "$repository_dir/config/hermes/default/SOUL.md" "$hermes_home/SOUL.md"
install -o root -g hermesd -m 0640 /dev/null "$hermes_home/.no-bundled-skills"

profiles=(minecraft-ops infra-shared network-shared monitoring-shared backup-shared deploy-ops security-ops)
for profile in "${profiles[@]}"; do
  profile_source="$repository_dir/config/hermes/profiles/$profile"
  profile_target="$hermes_home/profiles/$profile"
  [[ -f $profile_source/config.yaml && -f $profile_source/SOUL.md ]] || {
    echo "Incomplete profile source: $profile" >&2
    exit 66
  }
  reject_symlink "$profile_target"
  reject_symlink "$profile_target/config.yaml"
  reject_symlink "$profile_target/SOUL.md"
  reject_symlink "$profile_target/.no-bundled-skills"
  install -d -o hermesd -g hermesd -m 0700 "$profile_target" "$profile_target/workspace"
  install -o root -g hermesd -m 0640 "$profile_source/config.yaml" "$profile_target/config.yaml"
  install -o root -g hermesd -m 0640 "$profile_source/SOUL.md" "$profile_target/SOUL.md"
  install -o root -g hermesd -m 0640 /dev/null "$profile_target/.no-bundled-skills"
done

mapfile -t skills < <(
  python3 - "$repository_dir/config/hermes/skill-registry.json" <<'PY'
import json
from pathlib import Path
import sys

document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for entry in document["skills"]:
    print(entry["name"])
PY
)
[[ ${#skills[@]} -eq 5 ]] || {
  echo "The reviewed Hermes V1 registry must contain exactly five skills." >&2
  exit 65
}
for skill in "${skills[@]}"; do
  [[ $skill =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]] || {
    echo "Unsafe skill name in the reviewed registry." >&2
    exit 65
  }
done
homes=("$hermes_home")
for profile in "${profiles[@]}"; do homes+=("$hermes_home/profiles/$profile"); done
for profile_home in "${homes[@]}"; do
  skill_target="$profile_home/skills"
  if [[ -L $skill_target ]]; then
    echo "Skill directory must not be a symbolic link: $skill_target" >&2
    exit 73
  fi
  if [[ -d $skill_target ]]; then
    while IFS= read -r existing; do
      existing_name=$(basename "$existing")
      managed=0
      for skill in "${skills[@]}"; do
        [[ $existing_name == "$skill" ]] && managed=1
      done
      if [[ $managed -ne 1 ]]; then
        echo "Unmanaged installed skill requires review: $existing" >&2
        exit 73
      fi
    done < <(find "$skill_target" -mindepth 1 -maxdepth 1 -type d -print)
  fi
  install -d -o root -g hermesd -m 0750 "$skill_target"
  for skill in "${skills[@]}"; do
    reject_symlink "$skill_target/$skill"
    reject_symlink "$skill_target/$skill/SKILL.md"
    install -d -o root -g hermesd -m 0750 "$skill_target/$skill"
    install -o root -g hermesd -m 0640 \
      "$repository_dir/config/hermes/skills/$skill/SKILL.md" "$skill_target/$skill/SKILL.md"
  done
done

install -o root -g root -m 0755 "$repository_dir/scripts/ops-broker-mcp-profile" /usr/local/libexec/ops-broker-mcp-profile
install -o root -g root -m 0755 "$repository_dir/scripts/ops-orchestrator-mcp-profile" /usr/local/libexec/ops-orchestrator-mcp-profile
install -o root -g root -m 0755 "$repository_dir/scripts/ops-memory-mcp-profile" /usr/local/libexec/ops-memory-mcp-profile
install -o root -g root -m 0755 "$repository_dir/scripts/verify-hermes-mcp-live.py" /usr/local/libexec/verify-hermes-mcp-live
install -o root -g root -m 0755 "$repository_dir/scripts/hermes-secrets-env" /usr/local/libexec/hermes-secrets-env
install -o root -g root -m 0755 "$repository_dir/scripts/hermes-gateway-preflight" /usr/local/libexec/hermes-gateway-preflight
for policy_install_dir in \
  /usr/local/share/ops-control-plane \
  /usr/local/share/ops-control-plane/openbao \
  "$openbao_policy_dir"; do
  reject_symlink "$policy_install_dir"
  install -d -o root -g root -m 0755 "$policy_install_dir"
  if [[ -L $policy_install_dir \
    || $(stat -c '%U:%G:%a' -- "$policy_install_dir") != root:root:755 ]]; then
    echo "OpenBao policy directory has unsafe ownership, mode or type: $policy_install_dir" >&2
    exit 73
  fi
done
for policy_name in "${hermes_openbao_policies[@]}"; do
  policy_source="$repository_dir/config/openbao/policies/$policy_name"
  policy_target="$openbao_policy_dir/$policy_name"
  reject_symlink "$policy_target"
  install -o root -g root -m 0644 "$policy_source" "$policy_target"
  if [[ ! -f $policy_target || -L $policy_target \
    || $(stat -c '%U:%G:%a' -- "$policy_target") != root:root:644 \
    || ! -s $policy_target ]]; then
    echo "Installed OpenBao policy has unsafe ownership, mode or type: $policy_target" >&2
    exit 73
  fi
  if ! cmp -s -- "$policy_source" "$policy_target"; then
    echo "Installed OpenBao policy differs from its reviewed source: $policy_target" >&2
    exit 73
  fi
done
install -o root -g root -m 0755 "$repository_dir/scripts/provision-hermes-openbao" /usr/local/sbin/provision-hermes-openbao
install -o root -g root -m 0440 "$repository_dir/config/hermes/ops-broker-mcp-profile.sudoers" /etc/sudoers.d/hermes-ops-broker-mcp
install -o root -g root -m 0440 "$repository_dir/config/hermes/ops-local-mcp-profile.sudoers" /etc/sudoers.d/hermes-local-mcp
visudo -c >/dev/null

install -o root -g root -m 0644 "$repository_dir/systemd/hermes-gateway.service" /etc/systemd/system/hermes-gateway.service
if command -v restorecon >/dev/null 2>&1; then
  restorecon -F /usr/local/libexec/ops-broker-mcp-profile \
    /usr/local/libexec/ops-orchestrator-mcp-profile /usr/local/libexec/ops-memory-mcp-profile \
    /usr/local/libexec/hermes-gateway-preflight \
    /usr/local/libexec/hermes-secrets-env /usr/local/sbin/provision-hermes-openbao \
    /usr/local/libexec/verify-hermes-mcp-live \
    "$openbao_policy_dir/hermes-runtime.hcl" \
    "$openbao_policy_dir/hermes-coordinator.hcl" \
    "$openbao_policy_dir/deepseek-client.hcl" \
    /etc/sudoers.d/hermes-ops-broker-mcp /etc/sudoers.d/hermes-local-mcp \
    /etc/systemd/system/hermes-gateway.service >/dev/null 2>&1 || true
fi
systemd-analyze verify \
  /etc/systemd/system/ops-orchestrator-hermes-facade.service \
  /etc/systemd/system/hermes-gateway.service
systemctl daemon-reload

echo "Hermes $hermes_version ($hermes_tag, $hermes_commit) configured."
echo "The gateway remains dormant: no activation marker was created and the unit was not enabled or started."
