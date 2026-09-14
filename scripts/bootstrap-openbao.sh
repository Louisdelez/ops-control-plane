#!/usr/bin/env bash
set -euo pipefail

script_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ -d $script_root/config/openbao/policies ]]; then
  policy_dir="$script_root/config/openbao/policies"
else
  policy_dir=/usr/local/share/ops-control-plane/openbao/policies
fi
[[ -d $policy_dir ]] || { echo "OpenBao policy directory is missing." >&2; exit 1; }
export BAO_ADDR=${BAO_ADDR:-https://127.0.0.1:8200}
export BAO_CACERT=${BAO_CACERT:-/etc/pki/ca-trust/source/anchors/openbao-local.crt}

if [[ -z ${BAO_TOKEN:-} ]]; then
  read -r -s -p 'Initial root token (not stored): ' BAO_TOKEN
  echo
  export BAO_TOKEN
fi
trap 'unset BAO_TOKEN admin_password admin_password_2' EXIT

if ! bao audit list -format=json | jq -e 'has("file/")' >/dev/null; then
  echo "Required declarative file audit device is not active; refusing bootstrap." >&2
  exit 1
fi

for mount in minecraft infra-shared network-shared monitoring-shared backup-shared; do
  if ! bao secrets list -format=json | jq -e --arg p "kv-${mount}/" 'has($p)' >/dev/null; then
    bao secrets enable -path="kv-${mount}" -version=2 kv
  fi
done

if ! bao auth list -format=json | jq -e 'has("approle/")' >/dev/null; then
  bao auth enable approle
fi
if ! bao auth list -format=json | jq -e 'has("userpass/")' >/dev/null; then
  bao auth enable userpass
fi

for policy_file in "$policy_dir"/*.hcl; do
  policy_name=$(basename "$policy_file" .hcl)
  # Handle the retired name explicitly below so it can never be confused with
  # a runtime role policy during bootstrap.
  if [[ $policy_name == deepseek-client ]]; then
    continue
  fi
  bao policy write "$policy_name" "$policy_file"
done

# Quarantine both legacy policy names before deleting their AppRoles.  ACL
# policy updates affect existing tokens, so even a token issued before this
# migration can no longer read a provider key while it ages out.
bao policy write deepseek-client "$policy_dir/deepseek-client.hcl"
for legacy_role in deepseek-client hermes-coordinator; do
  bao delete "auth/approle/role/${legacy_role}" >/dev/null
done

for role in minecraft-monitor minecraft-deploy infra-network backup-agent; do
  bao write "auth/approle/role/${role}" \
    token_policies="token-self,${role}" \
    bind_secret_id=true \
    secret_id_bound_cidrs=127.0.0.1/32 \
    token_bound_cidrs=127.0.0.1/32 \
    token_period=20m \
    token_explicit_max_ttl=24h \
    token_no_default_policy=true
done

legacy_roles=$(bao list -format=json auth/approle/role)
for legacy_role in deepseek-client hermes-coordinator; do
  if jq -e --arg role "$legacy_role" '.[] | select(. == $role)' \
    <<<"$legacy_roles" >/dev/null; then
    echo "Legacy AppRole still exists after quarantine: $legacy_role" >&2
    exit 1
  fi
done
unset legacy_roles

# The Zulip launcher performs exactly one KV read followed by revoke-self,
# then execs the bridge with a clean validated environment.  Its reusable
# SecretID is bounded to 1024 starts and 30 days.  Two token uses are exactly
# sufficient for the read and the mandatory revocation.
bao write "auth/approle/role/zulip-bridge" \
  token_policies="zulip-bridge" \
  bind_secret_id=true \
  secret_id_bound_cidrs=127.0.0.1/32 \
  token_bound_cidrs=127.0.0.1/32 \
  secret_id_num_uses=1024 \
  secret_id_ttl=720h \
  token_ttl=60s \
  token_max_ttl=2m \
  token_explicit_max_ttl=2m \
  token_num_uses=2 \
  token_no_default_policy=true \
  token_type=service

# The Hermes secret helper logs in for one bounded gateway read and revokes its token
# before emitting any dotenv value.  Its reusable SecretID expires after 30
# days and is persisted only as a systemd host+TPM2 encrypted credential by
# provision-hermes-openbao.
bao write "auth/approle/role/hermes-runtime" \
  token_policies="hermes-runtime" \
  bind_secret_id=true \
  secret_id_bound_cidrs=127.0.0.1/32 \
  token_bound_cidrs=127.0.0.1/32 \
  secret_id_num_uses=1024 \
  secret_id_ttl=720h \
  token_ttl=60s \
  token_max_ttl=2m \
  token_explicit_max_ttl=2m \
  token_num_uses=2 \
  token_no_default_policy=true \
  token_type=service

# The memory resolver receives a token with exactly two uses: one exact read
# of the Qdrant key and the mandatory revoke-self operation.  The reusable
# SecretID is bounded and only its host+TPM2 encrypted form persists locally.
bao write "auth/approle/role/ops-memory-runtime" \
  token_policies="ops-memory-runtime" \
  bind_secret_id=true \
  secret_id_bound_cidrs=127.0.0.1/32 \
  token_bound_cidrs=127.0.0.1/32 \
  secret_id_num_uses=1024 \
  secret_id_ttl=720h \
  token_ttl=60s \
  token_max_ttl=2m \
  token_explicit_max_ttl=2m \
  token_num_uses=2 \
  token_no_default_policy=true \
  token_type=service

# The orchestrator resolver performs 22 exact reads (21 optional provider
# accounts and the mandatory Hermes facade token), then revokes before
# atomically publishing the bundle.
# Only the reusable AppRole identifiers persist, encrypted with host+TPM2.
bao write "auth/approle/role/ops-orchestrator-runtime" \
  token_policies="ops-orchestrator-runtime" \
  bind_secret_id=true \
  secret_id_bound_cidrs=127.0.0.1/32 \
  token_bound_cidrs=127.0.0.1/32 \
  secret_id_num_uses=1024 \
  secret_id_ttl=720h \
  token_ttl=60s \
  token_max_ttl=2m \
  token_explicit_max_ttl=2m \
  token_num_uses=23 \
  token_no_default_policy=true \
  token_type=service

read -r -s -p 'Choose the OpenBao password for human user ops-user: ' admin_password
echo
read -r -s -p 'Repeat password: ' admin_password_2
echo
if [[ $admin_password != "$admin_password_2" || ${#admin_password} -lt 10 ]]; then
  echo "Passwords differ or are shorter than 10 characters." >&2
  exit 1
fi
printf '%s' "$admin_password" | bao write auth/userpass/users/ops-user password=- \
  token_policies=token-self,human-admin token_ttl=1h token_max_ttl=8h

if ! printf '%s' "$admin_password" | \
  bao login -no-store -format=json -method=userpass username=ops-user password=- \
  >/dev/null; then
  echo "The human recovery login failed; the initial root token was NOT revoked." >&2
  exit 1
fi

# BAO_TOKEN still contains the initial root token in this process.  Revoke it
# only after the independent, non-persisted human-admin login succeeded.
bao token revoke -self >/dev/null
unset BAO_TOKEN admin_password admin_password_2

echo "Bootstrap complete. Human login was verified without storing a token."
echo "The initial root token has been revoked. Use only an ephemeral, non-persisted"
echo "human login workflow; do not leave an OpenBao token on this unencrypted disk."
