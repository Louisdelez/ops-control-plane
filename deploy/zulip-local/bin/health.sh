#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source-path=SCRIPTDIR
source "$script_dir/common.sh"

quiet=false
periodic=false
while [[ $# -gt 0 ]]; do
  case $1 in
    --quiet) quiet=true ;;
    --periodic) periodic=true ;;
    *) echo "Usage: $0 [--quiet] [--periodic]" >&2; exit 64 ;;
  esac
  shift
done

require_root
require_command curl date docker openssl python3 stat systemctl
"$script_dir/preflight.sh" --runtime >/dev/null

network_worker=/usr/local/libexec/ops-runbooks/control-plane-deployment-worker
[[ -f $network_worker && ! -L $network_worker && -x $network_worker && \
   $(stat -c '%u:%g:%a' "$network_worker") == 0:0:755 ]] || {
  echo "The reviewed Docker network monitor is unsafe or absent." >&2
  exit 1
}
if ! "$network_worker" network-health >/dev/null; then
  echo "The reviewed Docker network boundary is unhealthy." >&2
  if [[ $periodic == true ]]; then
    # Do not synchronously stop a unit on which this health oneshot depends.
    # Queue the stop: zulip-local.service's mandatory ExecStopPost performs a
    # Compose down (including both named networks) as soon as this check exits.
    systemctl stop --no-block zulip-local.service || true
  fi
  exit 1
fi

now=$(date +%s)
for credential in \
  /etc/credstore.encrypted/zulip-local-runtime-role-id \
  /etc/credstore.encrypted/zulip-local-runtime-secret-id \
  /etc/credstore.encrypted/zulip-local-runtime-secret-id-accessor \
  /etc/credstore.encrypted/zulip-local-bootstrap-role-id \
  /etc/credstore.encrypted/zulip-local-bootstrap-secret-id \
  /etc/credstore.encrypted/zulip-local-bootstrap-secret-id-accessor; do
  [[ -f $credential && ! -L $credential && $(stat -c '%u:%a' "$credential") == 0:400 ]] || {
    echo "A required encrypted Zulip AppRole credential is unsafe or absent." >&2
    exit 1
  }
done
for credential in \
  /etc/credstore.encrypted/zulip-local-runtime-secret-id \
  /etc/credstore.encrypted/zulip-local-bootstrap-secret-id; do
  age_seconds=$((now - $(stat -c '%Y' "$credential")))
  [[ $age_seconds -ge 0 && $age_seconds -lt 2160000 ]] || {
    echo "A Zulip AppRole credential is older than 25 days; rotate it before expiry." >&2
    exit 1
  }
done

compose ps --format json | python3 -c '
import json, sys
expected = {"database", "memcached", "rabbitmq", "redis", "zulip"}
documents = []
raw = sys.stdin.read().strip()
if not raw:
    raise SystemExit("Zulip Compose returned no running services")
try:
    parsed = json.loads(raw)
    documents = parsed if isinstance(parsed, list) else [parsed]
except json.JSONDecodeError:
    documents = [json.loads(line) for line in raw.splitlines() if line.strip()]
seen = set()
for item in documents:
    service = item.get("Service")
    if service in expected:
        seen.add(service)
        if str(item.get("State", "")).lower() != "running":
            raise SystemExit(f"Zulip service is not running: {service}")
        health = str(item.get("Health", "")).lower()
        if health and health != "healthy":
            raise SystemExit(f"Zulip service is unhealthy: {service}")
if seen != expected:
    raise SystemExit("The complete Zulip service set is not running")
'

openssl x509 -checkend 604800 -noout \
  -in "$ZULIP_LOCAL_RUN/tls/zulip.combined-chain.crt" >/dev/null || {
    echo "The Zulip TLS certificate expires in less than seven days." >&2
    exit 1
  }

curl --silent --show-error --fail --max-time 10 \
  --proto '=https' --tlsv1.2 \
  --resolve zulip.ops.local:8443:127.0.0.1 \
  --cacert "$ZULIP_LOCAL_RUN/tls/ca.crt" \
  "$ZULIP_LOCAL_ORIGIN/api/v1/server_settings" |
  python3 -c '
import json, sys
try:
    document = json.load(sys.stdin)
except (UnicodeError, json.JSONDecodeError):
    raise SystemExit("Zulip health returned invalid JSON")
if document.get("result") != "success":
    raise SystemExit("Zulip health returned an unsuccessful document")
version = document.get("zulip_version")
if not isinstance(version, str) or not (version == "12.2" or version.startswith("12.2-")):
    raise SystemExit("The running Zulip server is not the reviewed 12.2 release")
'

if [[ $quiet == false ]]; then
  echo "Zulip local HTTPS and all five Compose services are healthy."
fi
