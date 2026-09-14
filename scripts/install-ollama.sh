#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi
if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /path/to/ollama-linux-amd64-v0.30.8.tar.zst" >&2
  exit 1
fi

archive=$1
expected=ffe2b2c2f2f5f5b30c081ec353c2e0bb2d9ead516064a8e22663b24b8fd8dca0
actual=$(sha256sum "$archive" | awk '{print $1}')
if [[ $actual != "$expected" ]]; then
  echo "Ollama archive checksum mismatch." >&2
  exit 1
fi

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
install -m 0644 "$repo_dir/systemd/ops-users.conf" \
  /etc/sysusers.d/ops-control-plane.conf
install -m 0644 "$repo_dir/systemd/ops-directories.conf" \
  /etc/tmpfiles.d/ops-control-plane.conf
systemd-sysusers /etc/sysusers.d/ops-control-plane.conf
systemd-tmpfiles --create /etc/tmpfiles.d/ops-control-plane.conf

tar --zstd -xf "$archive" -C /usr/local --no-same-owner
chown -R root:root /usr/local/lib/ollama /usr/local/bin/ollama
chmod 0755 /usr/local/bin/ollama

install -m 0644 "$repo_dir/systemd/ollama.service" /etc/systemd/system/ollama.service
restorecon -RF /usr/local/bin/ollama /usr/local/lib/ollama /var/lib/ollama \
  /etc/systemd/system/ollama.service >/dev/null 2>&1 || true
systemctl daemon-reload
systemctl enable --now ollama.service

echo "Ollama v0.30.8 installed from the verified upstream archive."
