#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cred_file=/etc/credstore.encrypted/openbao-tls.key
rotate=${1:-}

install -d -m 0750 -o root -g openbao /etc/openbao.d/tls
install -d -m 0700 -o openbao -g openbao /var/lib/openbao/raft /var/log/openbao
install -D -m 0640 -o root -g openbao "$repo_dir/config/openbao/openbao.hcl" \
  /etc/openbao.d/openbao.hcl
install -d -m 0755 /etc/systemd/system/openbao.service.d
install -d -m 0700 -o root -g root /etc/credstore.encrypted
install -m 0644 "$repo_dir/config/openbao/openbao.service.d-hardening.conf" \
  /etc/systemd/system/openbao.service.d/90-ops-hardening.conf
install -m 0755 -o root -g root "$repo_dir/scripts/openbao-auto-unseal" \
  /usr/local/libexec/openbao-auto-unseal
install -m 0750 -o root -g root "$repo_dir/scripts/bootstrap-openbao.sh" \
  /usr/local/libexec/bootstrap-openbao
install -m 0750 -o root -g root "$repo_dir/scripts/initialize-openbao-v1.sh" \
  /usr/local/sbin/initialize-openbao-v1
install -m 0750 -o root -g root "$repo_dir/scripts/provision-memory-openbao" \
  /usr/local/sbin/provision-memory-openbao
install -m 0750 -o root -g root "$repo_dir/scripts/provision-orchestrator-openbao" \
  /usr/local/sbin/provision-orchestrator-openbao
install -m 0644 -o root -g openbao "$repo_dir/config/openbao/recovery.age-recipient" \
  /etc/openbao.d/recovery.age-recipient
install -d -m 0755 -o root -g root /usr/local/share/ops-control-plane/openbao/policies
for policy_file in "$repo_dir"/config/openbao/policies/*.hcl; do
  install -m 0644 -o root -g root "$policy_file" \
    "/usr/local/share/ops-control-plane/openbao/policies/$(basename "$policy_file")"
done
install -m 0644 -o root -g root "$repo_dir/systemd/openbao-unseal.service" \
  /etc/systemd/system/openbao-unseal.service

if [[ ! -f $cred_file || $rotate == --rotate-tls ]]; then
  tmp_dir=$(mktemp -d /dev/shm/openbao-tls.XXXXXX)
  trap 'rm -rf -- "$tmp_dir"' EXIT
  umask 077
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
    -out "$tmp_dir/ca.key" >/dev/null 2>&1
  openssl req -x509 -new -sha256 -days 397 -key "$tmp_dir/ca.key" \
    -subj '/CN=Dell Ops Local CA' \
    -addext 'basicConstraints=critical,CA:TRUE' \
    -addext 'keyUsage=critical,keyCertSign,cRLSign' \
    -out "$tmp_dir/ca.crt" >/dev/null 2>&1
  openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 \
    -out "$tmp_dir/tls.key" >/dev/null 2>&1
  openssl req -new -sha256 -key "$tmp_dir/tls.key" \
    -subj '/CN=OpenBao Dell Ops Local' \
    -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' \
    -addext 'basicConstraints=critical,CA:FALSE' \
    -addext 'keyUsage=critical,digitalSignature,keyEncipherment' \
    -addext 'extendedKeyUsage=serverAuth' \
    -out "$tmp_dir/server.csr" >/dev/null 2>&1
  openssl x509 -req -sha256 -days 397 -in "$tmp_dir/server.csr" \
    -CA "$tmp_dir/ca.crt" -CAkey "$tmp_dir/ca.key" -CAcreateserial \
    -copy_extensions copy -out "$tmp_dir/server.crt" >/dev/null 2>&1
  systemd-creds setup >/dev/null
  systemd-creds encrypt --with-key=host+tpm2 --name=tls.key \
    "$tmp_dir/tls.key" "$cred_file" >/dev/null
  install -m 0644 -o root -g openbao "$tmp_dir/server.crt" \
    /etc/openbao.d/tls/server.crt
  install -m 0644 -o root -g openbao "$tmp_dir/ca.crt" \
    /etc/openbao.d/tls/ca.crt
  install -m 0644 -o root -g root "$tmp_dir/ca.crt" \
    /etc/pki/ca-trust/source/anchors/openbao-local.crt
  update-ca-trust
fi

restorecon -RF /etc/openbao.d /var/lib/openbao /var/log/openbao \
  /etc/credstore.encrypted >/dev/null 2>&1 || true
systemctl daemon-reload
systemctl enable openbao.service
systemctl enable openbao-unseal.service
systemctl restart openbao.service

echo "OpenBao configuration installed on https://127.0.0.1:8200."
