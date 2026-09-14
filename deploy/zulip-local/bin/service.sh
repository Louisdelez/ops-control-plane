#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source-path=SCRIPTDIR
source "$script_dir/common.sh"

[[ $# -eq 1 && ( $1 == start || $1 == stop ) ]] || {
  echo "Usage: $0 start | stop" >&2
  exit 64
}
action=$1

require_root
require_command docker flock sleep
lock_deployment

if [[ $action == start ]]; then
  "$script_dir/preflight.sh" --runtime
  compose up --detach --wait zulip
  wait_for_zulip_health "$script_dir/health.sh"
else
  compose down --remove-orphans
fi
