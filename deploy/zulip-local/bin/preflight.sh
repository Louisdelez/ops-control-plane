#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source-path=SCRIPTDIR
source "$script_dir/common.sh"

mode=${1:---source}
if [[ $mode != --source && $mode != --runtime ]]; then
  echo "Usage: $0 [--source | --runtime]" >&2
  exit 64
fi

require_root
require_command awk cut docker flock getent openssl python3 selinuxenabled sha256sum sort stat uname
reject_rootless_docker
/usr/bin/docker compose version >/dev/null

[[ -f $ZULIP_LOCAL_COMPOSE && ! -L $ZULIP_LOCAL_COMPOSE ]] || {
  echo "The reviewed Compose file is unavailable." >&2
  exit 78
}

python3 - "$ZULIP_LOCAL_COMPOSE" <<'PY'
from pathlib import Path
import re
import sys

text = Path(sys.argv[1]).read_text(encoding="utf-8")
expected = {
    "ghcr.io/zulip/zulip-server:12.2-0": "765f0ab3caa49041989132ee1879d98dbab1df7695c27e713eac1f114d167755",
    "zulip/zulip-postgresql:14": "e71ba8616fa42cdc1b248f51263d9290c29681cb8c1992eb9b498af0bb656b29",
    "memcached:alpine": "c29847751abb41f4c268c84fb3087fee05d4edcbda44409ccb5086e26148e8a7",
    "rabbitmq:4.2": "15e7b5e60af2d2147f8d74eef5b93c29501cd644361ca93bc854e409c2dde624",
    "redis:alpine": "becdda6c7f4b3fb42e42fd7f120bbf5c54c4caaaf16f26da24e4563d2c1f0576",
}
images = re.findall(r'^\s*image:\s*"([^"@]+)@sha256:([0-9a-f]{64})"\s*$', text, re.MULTILINE)
if (
    dict(images) != expected
    or len(images) != len(expected)
    or len(re.findall(r"^\s*image\s*:", text, re.MULTILINE)) != len(expected)
):
    raise SystemExit("Compose image pins differ from the reviewed x86_64 set")
if text.count("platform: linux/amd64") != 5:
    raise SystemExit("Every image must be constrained to linux/amd64")
published = re.findall(r'^\s*published:\s*"?([0-9]+)"?\s*$', text, re.MULTILINE)
host_ips = re.findall(r'^\s*host_ip:\s*"?([^"\s]+)"?\s*$', text, re.MULTILINE)
if published != ["8443"] or host_ips != ["127.0.0.1"]:
    raise SystemExit("HTTPS is not constrained to the reviewed loopback port")
if re.search(r'^\s*(?:build|extends|include|privileged|network_mode|pid|ipc|devices|cap_add):', text, re.MULTILINE):
    raise SystemExit("Compose contains an unreviewed privilege or indirection")
PY

if [[ $mode == --source ]]; then
  echo "Zulip local source preflight passed; no container was changed."
  exit 0
fi

resolved=$(getent ahostsv4 zulip.ops.local | awk '{print $1}' | sort -u || true)
[[ $resolved == 127.0.0.1 ]] || {
  echo "zulip.ops.local must resolve to 127.0.0.1." >&2
  exit 78
}

validate_runtime_file "$ZULIP_LOCAL_RUN/secrets/postgres_password" 400
validate_runtime_file "$ZULIP_LOCAL_RUN/secrets/memcached_password" 444
validate_runtime_file "$ZULIP_LOCAL_RUN/secrets/rabbitmq_password" 400
validate_runtime_file "$ZULIP_LOCAL_RUN/secrets/redis_password" 400
validate_runtime_file "$ZULIP_LOCAL_RUN/tls/ca.crt" 444
validate_runtime_file "$ZULIP_LOCAL_RUN/tls/zulip.combined-chain.crt" 444
validate_runtime_file "$ZULIP_LOCAL_RUN/tls/zulip.key" 400
validate_runtime_file "$ZULIP_LOCAL_RUN/zulip-secrets.conf" 640 1000
validate_runtime_file "$ZULIP_LOCAL_RUN/admin/admin-password" 444

for container_path in \
  "$ZULIP_LOCAL_RUN" \
  "$ZULIP_LOCAL_RUN/secrets" \
  "$ZULIP_LOCAL_RUN/secrets/postgres_password" \
  "$ZULIP_LOCAL_RUN/secrets/memcached_password" \
  "$ZULIP_LOCAL_RUN/secrets/rabbitmq_password" \
  "$ZULIP_LOCAL_RUN/secrets/redis_password" \
  "$ZULIP_LOCAL_RUN/tls" \
  "$ZULIP_LOCAL_RUN/tls/ca.crt" \
  "$ZULIP_LOCAL_RUN/tls/zulip.combined-chain.crt" \
  "$ZULIP_LOCAL_RUN/tls/zulip.key" \
  "$ZULIP_LOCAL_RUN/zulip-secrets.conf" \
  "$ZULIP_LOCAL_RUN/admin" \
  "$ZULIP_LOCAL_RUN/admin/admin-password"; do
  validate_container_file_label "$container_path"
done

python3 - "$ZULIP_LOCAL_RUN/zulip-secrets.conf" "$ZULIP_LOCAL_RUN/secrets" <<'PY'
import configparser
from pathlib import Path
import sys

configuration = Path(sys.argv[1])
secret_directory = Path(sys.argv[2])
expected = {
    "avatar_salt",
    "camo_key",
    "postgres_password",
    "memcached_password",
    "rabbitmq_password",
    "redis_password",
    "secret_key",
    "shared_secret",
    "email_password",
    "zulip_org_id",
    "zulip_org_key",
}
compose_secrets = {
    "postgres_password",
    "memcached_password",
    "rabbitmq_password",
    "redis_password",
}
parser = configparser.RawConfigParser(strict=True)
try:
    with configuration.open(encoding="utf-8") as stream:
        parser.read_file(stream)
except (OSError, UnicodeError, configparser.Error) as exc:
    raise SystemExit("The rendered Zulip secrets file is invalid") from exc
if parser.sections() != ["secrets"] or set(parser["secrets"]) != expected:
    raise SystemExit("The rendered Zulip secrets file has an unexpected schema")
for key in compose_secrets:
    try:
        source = (secret_directory / key).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SystemExit("A rendered Compose secret is unavailable") from exc
    if parser["secrets"][key] != source:
        raise SystemExit("A rendered Compose secret is inconsistent")
PY

openssl verify -CAfile "$ZULIP_LOCAL_RUN/tls/ca.crt" \
  "$ZULIP_LOCAL_RUN/tls/zulip.combined-chain.crt" >/dev/null
openssl x509 -in "$ZULIP_LOCAL_RUN/tls/zulip.combined-chain.crt" \
  -noout -checkhost zulip.ops.local >/dev/null
certificate_key=$(openssl x509 -in "$ZULIP_LOCAL_RUN/tls/zulip.combined-chain.crt" -pubkey -noout |
  openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)
private_key=$(openssl pkey -in "$ZULIP_LOCAL_RUN/tls/zulip.key" -pubout -outform DER 2>/dev/null |
  sha256sum | cut -d' ' -f1)
[[ -n $certificate_key && $certificate_key == "$private_key" ]] || {
  echo "The Zulip TLS certificate and private key do not match." >&2
  exit 78
}

compose config --quiet
echo "Zulip local runtime preflight passed."
