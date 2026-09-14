#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

# shellcheck disable=SC2034  # Shared by the scripts that source this library.
ZULIP_LOCAL_ROOT=${ZULIP_LOCAL_ROOT:-/opt/ops-control-plane/deploy/zulip-local}
ZULIP_LOCAL_COMPOSE="$ZULIP_LOCAL_ROOT/compose.yaml"
# shellcheck disable=SC2034  # Consumed by sourcing scripts.
ZULIP_LOCAL_RUN=/run/zulip-local
ZULIP_LOCAL_LOCK_DIR=/run/lock/zulip-local
ZULIP_LOCAL_LOCK="$ZULIP_LOCAL_LOCK_DIR/operation.lock"
# shellcheck disable=SC2034  # Consumed by sourcing scripts.
ZULIP_LOCAL_ORIGIN=https://zulip.ops.local:8443
ZULIP_RESTART_SUPPRESS_MARKER=/var/lib/ops-control-plane-deployment/docker-restart-suppressed.json

require_root() {
  if [[ ${EUID} -ne 0 ]]; then
    echo "This Zulip operation must run as root." >&2
    return 77
  fi
}

require_command() {
  local command_name
  for command_name in "$@"; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
      echo "Required command is missing: $command_name" >&2
      return 69
    fi
  done
}

compose() {
  local restart_policy=unless-stopped
  if [[ -e $ZULIP_RESTART_SUPPRESS_MARKER || -L $ZULIP_RESTART_SUPPRESS_MARKER ]]; then
    [[ -f $ZULIP_RESTART_SUPPRESS_MARKER && ! -L $ZULIP_RESTART_SUPPRESS_MARKER ]] || {
      echo "The Docker restart-suppression marker has an unsafe type." >&2
      return 78
    }
    [[ $(stat -c '%u:%g:%a' "$ZULIP_RESTART_SUPPRESS_MARKER") == 0:0:600 ]] || {
      echo "The Docker restart-suppression marker has unsafe metadata." >&2
      return 78
    }
    [[ $(stat -c '%u:%g:%a' "$(dirname -- "$ZULIP_RESTART_SUPPRESS_MARKER")") == 0:0:700 ]] || {
      echo "The Docker restart-suppression marker parent has unsafe metadata." >&2
      return 78
    }
    /usr/bin/python3 - "$ZULIP_RESTART_SUPPRESS_MARKER" <<'PY'
import json
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
try:
    document = json.loads(path.read_bytes())
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise SystemExit("The Docker restart-suppression marker is invalid.") from exc
if not isinstance(document, dict) or set(document) != {
    "release_id", "schema_version", "status", "transaction_id"
}:
    raise SystemExit("The Docker restart-suppression marker is invalid.")
identifier = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,191}")
if (
    document["schema_version"] != 1
    or document["status"] != "suppressed"
    or not isinstance(document["release_id"], str)
    or not isinstance(document["transaction_id"], str)
    or not identifier.fullmatch(document["release_id"])
    or not identifier.fullmatch(document["transaction_id"])
):
    raise SystemExit("The Docker restart-suppression marker is invalid.")
PY
    restart_policy=no
  fi
  ZULIP_LOCAL_RESTART_POLICY=$restart_policy \
    /usr/bin/docker compose --project-name zulip-local --file "$ZULIP_LOCAL_COMPOSE" "$@"
}

lock_deployment() {
  if [[ ! -e $ZULIP_LOCAL_LOCK_DIR ]]; then
    mkdir --mode=0700 -- "$ZULIP_LOCAL_LOCK_DIR"
  fi
  if [[ ! -d $ZULIP_LOCAL_LOCK_DIR || -L $ZULIP_LOCAL_LOCK_DIR || \
        $(stat -c '%u:%a' "$ZULIP_LOCAL_LOCK_DIR") != 0:700 ]]; then
    echo "The Zulip lifecycle lock directory has unsafe metadata." >&2
    return 78
  fi
  if [[ -e $ZULIP_LOCAL_LOCK && ( ! -f $ZULIP_LOCAL_LOCK || -L $ZULIP_LOCAL_LOCK ) ]]; then
    echo "The Zulip lifecycle lock file has an unsafe type." >&2
    return 78
  fi
  exec 9>"$ZULIP_LOCAL_LOCK"
  chmod 0600 "$ZULIP_LOCAL_LOCK"
  /usr/bin/flock -n 9 || {
    echo "Another Zulip deployment operation holds the lock." >&2
    return 75
  }
}

reject_rootless_docker() {
  local security_options
  security_options=$(/usr/bin/docker info --format '{{json .SecurityOptions}}' 2>/dev/null) || {
    echo "The rootful Docker daemon is unavailable." >&2
    return 69
  }
  if [[ $security_options == *rootless* ]]; then
    echo "Zulip's official image does not support rootless Docker." >&2
    return 78
  fi
  if [[ $(uname -m) != x86_64 ]]; then
    echo "This deployment is digest-pinned specifically for x86_64." >&2
    return 78
  fi
}

validate_runtime_file() {
  local path=$1
  local expected_mode=$2
  local expected_gid=${3:-0}
  [[ -f $path && ! -L $path ]] || {
    echo "Required runtime file is absent or symlinked: $path" >&2
    return 78
  }
  [[ $(stat -c '%u:%g:%a' "$path") == "0:$expected_gid:$expected_mode" ]] || {
    echo "Required runtime file has unsafe ownership or mode: $path" >&2
    return 78
  }
}

validate_root_managed_file() {
  local path=$1
  local mode
  [[ -f $path && ! -L $path ]] || {
    echo "Required root-managed file is absent or symlinked: $path" >&2
    return 78
  }
  mode=$(stat -c '%a' "$path")
  if [[ $(stat -c '%u' "$path") != 0 ]] || (( (8#$mode & 0022) != 0 )); then
    echo "Required root-managed file has unsafe ownership or mode: $path" >&2
    return 78
  fi
}

validate_root_managed_parent_chain() {
  local path=$1
  local parent
  local mode
  parent=$(dirname -- "$path")
  while true; do
    [[ -d $parent && ! -L $parent ]] || {
      echo "A parent of the root-managed path is unsafe: $path" >&2
      return 78
    }
    mode=$(stat -c '%a' "$parent")
    if [[ $(stat -c '%u' "$parent") != 0 ]] || (( (8#$mode & 0022) != 0 )); then
      echo "A parent of the root-managed path is writable by non-root: $path" >&2
      return 78
    fi
    [[ $parent == / ]] && break
    parent=$(dirname -- "$parent")
  done
}

validate_container_file_label() {
  local path=$1
  local context
  if /usr/bin/selinuxenabled; then
    context=$(stat -c '%C' "$path")
    [[ $context == *:container_file_t:* ]] || {
      echo "Required runtime path lacks the SELinux container file type: $path" >&2
      return 78
    }
  fi
}

wait_for_zulip_health() {
  local health_script=$1
  local attempt
  # None of the five pinned images declares an OCI HEALTHCHECK. Compose's
  # --wait therefore proves only that their processes are running. Gate every
  # successful start on the actual local HTTPS and service-set check instead.
  for ((attempt = 1; attempt <= 120; attempt++)); do
    if "$health_script" --quiet >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  "$health_script" --quiet || true
  echo "Zulip did not become healthy within ten minutes." >&2
  return 1
}
