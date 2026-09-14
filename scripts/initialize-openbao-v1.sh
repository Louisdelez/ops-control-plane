#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi
if [[ ${1:-} != --initialize ]]; then
  echo "Usage: $0 --initialize" >&2
  exit 2
fi

script_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ -r $script_root/config/openbao/recovery.age-recipient ]]; then
  recipient_file="$script_root/config/openbao/recovery.age-recipient"
  auto_unseal="$script_root/scripts/openbao-auto-unseal"
  bootstrap="$script_root/scripts/bootstrap-openbao.sh"
else
  recipient_file=/etc/openbao.d/recovery.age-recipient
  auto_unseal=/usr/local/libexec/openbao-auto-unseal
  bootstrap=/usr/local/libexec/bootstrap-openbao
fi
export BAO_ADDR=https://127.0.0.1:8200
export BAO_CACERT=/etc/pki/ca-trust/source/anchors/openbao-local.crt
credstore=/etc/credstore.encrypted
recovery_dir=/var/lib/openbao/recovery
tmp_dir=$(mktemp -d /dev/shm/openbao-initialize.XXXXXX)
complete=false
sensitive_material=false

cleanup() {
  unset BAO_TOKEN
  if [[ $complete == true || $sensitive_material == false ]]; then
    rm -rf -- "$tmp_dir"
  else
    echo "Initialization did not complete; root-only transient material remains at $tmp_dir" >&2
    echo "Resolve the error in this boot session, then securely remove that directory." >&2
  fi
}
trap cleanup EXIT
umask 077

for command in age bao curl jq systemctl systemd-analyze systemd-creds install sha256sum; do
  command -v "$command" >/dev/null || { echo "Missing command: $command" >&2; exit 1; }
done
[[ -r $recipient_file ]] || { echo "Missing recovery recipient file." >&2; exit 1; }
recipient=$(tr -d '[:space:]' < "$recipient_file")
[[ $recipient =~ ^age1[0-9a-z]{20,}$ ]] || { echo "Invalid age recovery recipient." >&2; exit 1; }
systemd-analyze has-tpm2 --quiet || { echo "A working TPM2 is required." >&2; exit 1; }

health=$(curl --silent --show-error --fail-with-body --cacert "$BAO_CACERT" \
  "$BAO_ADDR/v1/sys/health" 2>/dev/null || true)
initialized=$(jq -r 'if has("initialized") then .initialized else empty end' <<<"$health")
if [[ $initialized != false ]]; then
  echo "Refusing initialization: OpenBao is already initialized or health is unavailable." >&2
  exit 1
fi
for name in openbao-unseal-share-1 openbao-unseal-share-2; do
  [[ ! -e $credstore/$name ]] || { echo "Refusing to overwrite $credstore/$name" >&2; exit 1; }
done

init_json="$tmp_dir/openbao-init.json"
bao operator init -format=json -key-shares=3 -key-threshold=2 > "$init_json"
sensitive_material=true
jq -e '.unseal_keys_b64 | length == 3' "$init_json" >/dev/null
jq -e '.root_token | type == "string" and length > 10' "$init_json" >/dev/null

stamp=$(date -u +%Y%m%dT%H%M%SZ)
install -d -m 0700 -o root -g root "$recovery_dir" "$credstore"
recovery_tmp="$tmp_dir/openbao-recovery-$stamp.json.age"
age --encrypt --recipient "$recipient" --output "$recovery_tmp" "$init_json"
install -m 0400 -o root -g root "$recovery_tmp" \
  "$recovery_dir/openbao-recovery-$stamp.json.age"
sha256sum "$recovery_dir/openbao-recovery-$stamp.json.age" \
  > "$recovery_dir/openbao-recovery-$stamp.json.age.sha256"
chmod 0400 "$recovery_dir/openbao-recovery-$stamp.json.age.sha256"

systemd-creds setup >/dev/null
for index in 1 2; do
  name="openbao-unseal-share-$index"
  jq -r ".unseal_keys_b64[$((index - 1))]" "$init_json" > "$tmp_dir/$name"
  systemd-creds encrypt --with-key=host+tpm2 --name="$name" \
    "$tmp_dir/$name" "$tmp_dir/$name.cred" >/dev/null
  install -m 0400 -o root -g root "$tmp_dir/$name.cred" "$credstore/$name"
done

# Use the same in-memory credential interface as the boot service for the first
# unseal.  No share crosses argv or the persistent environment.
CREDENTIALS_DIRECTORY="$tmp_dir" "$auto_unseal"

# The bootstrap script keeps the initial root token only in this process tree,
# verifies the independent human login, and revokes the token before returning.
BAO_TOKEN=$(jq -r '.root_token' "$init_json")
export BAO_TOKEN
"$bootstrap"
unset BAO_TOKEN

restorecon -RF "$credstore" "$recovery_dir" >/dev/null 2>&1 || true
systemctl daemon-reload
systemctl enable openbao-unseal.service >/dev/null
systemctl restart openbao-unseal.service

complete=true
echo "OpenBao V1 initialized, boot auto-unseal enabled, and initial root token revoked."
echo "Encrypted recovery bundle: $recovery_dir/openbao-recovery-$stamp.json.age"
echo "Copy that age-encrypted bundle and checksum to offline storage."
