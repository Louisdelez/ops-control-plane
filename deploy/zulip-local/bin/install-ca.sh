#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source-path=SCRIPTDIR
source "$script_dir/common.sh"

require_root
require_command awk getent install openssl sort stat update-ca-trust
validate_runtime_file "$ZULIP_LOCAL_RUN/tls/ca.crt" 444

validate_root_managed_file /etc/hosts

resolved=$(getent ahostsv4 zulip.ops.local | awk '{print $1}' | sort -u || true)
if [[ -n $resolved && $resolved != 127.0.0.1 ]]; then
  echo "Refusing to override a non-loopback zulip.ops.local mapping." >&2
  exit 78
fi
if [[ -z $resolved ]]; then
  printf '%s\n' '127.0.0.1 zulip.ops.local' >> /etc/hosts
fi

install -d -m 0755 -o root -g root /etc/pki/ca-trust/source/anchors
[[ ! -L /etc/pki/ca-trust/source/anchors/zulip-ops-local-ca.crt ]] || {
  echo "Refusing to replace a symlinked Zulip trust anchor." >&2
  exit 78
}
install -m 0644 -o root -g root "$ZULIP_LOCAL_RUN/tls/ca.crt" \
  /etc/pki/ca-trust/source/anchors/zulip-ops-local-ca.crt
if command -v restorecon >/dev/null 2>&1; then
  restorecon -F /etc/pki/ca-trust/source/anchors/zulip-ops-local-ca.crt
fi
update-ca-trust extract
echo "The public Zulip local CA and loopback hostname mapping are installed."
