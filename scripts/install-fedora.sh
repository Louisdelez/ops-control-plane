#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

dnf upgrade --refresh -y
dnf install -y \
  openbao age yq ansible-core restic wireguard-tools lm_sensors \
  smartmontools tpm2-tools policycoreutils-python-utils \
  fapolicyd aide python3-pip pipx

echo "Fedora base installed. Reboot is deferred until NVIDIA/MOK setup is ready."
