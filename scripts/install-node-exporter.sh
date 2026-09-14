#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi
if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /path/to/node_exporter-1.12.1.linux-amd64.tar.gz" >&2
  exit 1
fi

archive=$1
expected=b51d8a76aa2a9156a55d501aca6276fae09e262259a5e4e831d2c2222f084e63
actual=$(sha256sum "$archive" | awk '{print $1}')
[[ $actual == "$expected" ]] || { echo "node_exporter checksum mismatch." >&2; exit 1; }

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
tmp_dir=$(mktemp -d)
trap 'rm -rf -- "$tmp_dir"' EXIT
tar -xzf "$archive" -C "$tmp_dir"

install -m 0644 "$repo_dir/systemd/ops-users.conf" /etc/sysusers.d/ops-control-plane.conf
install -m 0644 "$repo_dir/systemd/ops-directories.conf" /etc/tmpfiles.d/ops-control-plane.conf
systemd-sysusers /etc/sysusers.d/ops-control-plane.conf
systemd-tmpfiles --create /etc/tmpfiles.d/ops-control-plane.conf

install -m 0755 "$tmp_dir/node_exporter-1.12.1.linux-amd64/node_exporter" \
  /usr/local/bin/node_exporter
install -m 0755 "$repo_dir/scripts/ops-local-metrics" /usr/local/libexec/ops-local-metrics
install -m 0644 "$repo_dir/systemd/node-exporter.service" /etc/systemd/system/node-exporter.service
install -m 0644 "$repo_dir/systemd/ops-local-metrics.service" /etc/systemd/system/ops-local-metrics.service
install -m 0644 "$repo_dir/systemd/ops-local-metrics.timer" /etc/systemd/system/ops-local-metrics.timer
restorecon -RF /usr/local/bin/node_exporter /usr/local/libexec/ops-local-metrics \
  /var/lib/node-exporter >/dev/null 2>&1 || true

systemctl daemon-reload
systemctl enable --now node-exporter.service ops-local-metrics.timer
systemctl start ops-local-metrics.service

echo "node_exporter v1.12.1 installed on 127.0.0.1:9100."
