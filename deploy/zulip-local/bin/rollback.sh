#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)

if [[ ${1:-} == --stop-only && $# -eq 1 ]]; then
  # Stopping is repeatable and preserves every named volume.
  # shellcheck source-path=SCRIPTDIR
  source "$script_dir/common.sh"
  require_root
  require_command systemctl
  if systemctl is-active --quiet zulip-local.service; then
    # The unit's ExecStopPost owns the lifecycle lock.
    systemctl stop zulip-local.service
  else
    lock_deployment
    compose down --remove-orphans
  fi
  echo "Zulip containers were stopped; all named volumes were preserved."
  exit 0
fi

# A data rollback is exactly the reviewed, checksum-verified restore workflow;
# it deliberately has no shortcut around the destructive confirmation.
exec "$script_dir/restore.sh" "$@"
