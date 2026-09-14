#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source-path=SCRIPTDIR
source "$script_dir/common.sh"

pull=true
start=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-pull) pull=false ;;
    --start) start=true ;;
    --help|-h)
      echo "Usage: $0 [--no-pull] [--start]"
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 64 ;;
  esac
  shift
done

require_root
require_command docker flock sleep systemctl

if [[ ! -f $ZULIP_LOCAL_RUN/secrets/postgres_password ]]; then
  systemctl start zulip-local-secrets.service
fi
lock_deployment
"$script_dir/install-ca.sh"
"$script_dir/preflight.sh" --runtime

if [[ -n $(compose ps --status running --services zulip) ]]; then
  echo "Stop the running Zulip application before applying initialization or migrations." >&2
  exit 78
fi

if [[ $pull == true ]]; then
  compose pull
fi

# `compose run` starts its dependencies. If initialization or the optional
# application start fails, return the stack to a stopped state while preserving
# all named volumes.
cleanup_failed_init() {
  local status=$?
  if (( status != 0 )); then
    compose down --remove-orphans >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup_failed_init EXIT

# Official and repeatable 12.2 initialization/migration entrypoint.
compose run --rm zulip app:init

if [[ $start == true ]]; then
  compose up --detach --wait zulip
  wait_for_zulip_health "$script_dir/health.sh"
else
  compose down --remove-orphans
fi
trap - EXIT
echo "Zulip database initialization completed successfully."
