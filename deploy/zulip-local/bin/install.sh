#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

usage() {
  cat <<'EOF'
Usage: sudo ./bin/install.sh --check | --install [--enable]

  --check    Validate source and host prerequisites; change nothing.
  --install  Install root-owned files and units; start nothing.
  --enable   With --install, enable (but do not start) stack/health/backup units.
EOF
}

mode=
enable=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check|--install)
      [[ -z $mode ]] || { usage >&2; exit 64; }
      mode=$1
      ;;
    --enable) enable=true ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
  shift
done
[[ -n $mode ]] || { usage >&2; exit 64; }
[[ $mode == --install || $enable == false ]] || { usage >&2; exit 64; }
[[ ${EUID} -eq 0 ]] || { echo "Run this installer as root." >&2; exit 77; }

source_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
target_root=/opt/ops-control-plane/deploy/zulip-local
unit_root=/etc/systemd/system

for command_name in age bash chcon chmod docker find flock getent install mkdir openssl python3 selinuxenabled stat systemctl systemd-analyze systemd-creds update-ca-trust; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is missing: $command_name" >&2
    exit 69
  }
done
[[ $(uname -m) == x86_64 ]] || { echo "This digest set is x86_64-only." >&2; exit 78; }
security_options=$(docker info --format '{{json .SecurityOptions}}' 2>/dev/null) || {
  echo "The rootful Docker daemon is unavailable." >&2
  exit 69
}
[[ $security_options != *rootless* ]] || {
  echo "Zulip's official Docker image does not support rootless mode." >&2
  exit 78
}
docker compose version >/dev/null

required=(
  README.md compose.yaml config/defaults.env
  openbao/zulip-local-runtime.hcl openbao/zulip-local-bootstrap.hcl
  lib/zulip_local.py
  bin/common.sh bin/preflight.sh bin/install.sh bin/install-ca.sh bin/init.sh
  bin/service.sh
  bin/provision-openbao.py bin/render-secrets.py bin/provision-zulip.py
  bin/backup.sh bin/restore.sh bin/rollback.sh bin/health.sh
  bin/validate-backup-stream.py
  systemd/zulip-local-secrets.service systemd/zulip-local.service
  systemd/zulip-local-init.service systemd/zulip-local-health.service
  systemd/zulip-local-health.timer systemd/zulip-local-backup.service
  systemd/zulip-local-backup.timer
)
for relative in "${required[@]}"; do
  [[ -f $source_root/$relative && ! -L $source_root/$relative ]] || {
    echo "Required source file is absent or symlinked: $relative" >&2
    exit 78
  }
done

for shell_source in "$source_root"/bin/*.sh; do
  bash -n "$shell_source"
done
python3 - "$source_root" <<'PY'
import ast
from pathlib import Path
import sys

root = Path(sys.argv[1])
for path in sorted(root.rglob("*.py")):
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
PY
ZULIP_LOCAL_ROOT=$source_root bash "$source_root/bin/preflight.sh" --source

if [[ $mode == --check ]]; then
  echo "Zulip local installation preflight passed; nothing was changed."
  exit 0
fi

# source_root was canonicalized above.
# shellcheck disable=SC1091
source "$source_root/bin/common.sh"
lock_deployment
if systemctl is-active --quiet zulip-local.service; then
  echo "Stop zulip-local.service before replacing its installed deployment files." >&2
  exit 78
fi

if [[ -e $target_root ]]; then
  [[ -d $target_root && ! -L $target_root ]] || {
    echo "The installed Zulip package root has an unsafe type." >&2
    exit 78
  }
  unsafe_entry=$(find "$target_root" -xdev \( -type l -o ! -user root -o -perm -0022 \) -print -quit)
  [[ -z $unsafe_entry ]] || {
    echo "The installed Zulip package tree has unsafe metadata." >&2
    exit 78
  }
fi

install -d -m 0755 -o root -g root "$target_root"
for relative in "${required[@]}"; do
  mode_bits=0644
  case "$relative" in
    bin/*.sh|bin/*.py) mode_bits=0750 ;;
  esac
  install -D -m "$mode_bits" -o root -g root \
    "$source_root/$relative" "$target_root/$relative"
done
install -d -m 0700 -o root -g root /etc/credstore.encrypted /var/backups/zulip-local
install -d -m 0755 -o root -g root /usr/local/share/ops-control-plane/openbao/policies
for policy in "$source_root"/openbao/*.hcl; do
  install -m 0644 -o root -g root "$policy" \
    "/usr/local/share/ops-control-plane/openbao/policies/$(basename "$policy")"
done
for unit in "$source_root"/systemd/*; do
  [[ ! -L $unit_root/$(basename "$unit") ]] || {
    echo "Refusing to overwrite a symlinked Zulip systemd unit." >&2
    exit 78
  }
  install -m 0644 -o root -g root "$unit" "$unit_root/$(basename "$unit")"
done
systemctl daemon-reload
systemd-analyze verify "$unit_root"/zulip-local-*.service "$unit_root"/zulip-local-*.timer

if [[ $enable == true ]]; then
  for credential in \
    zulip-local-runtime-role-id zulip-local-runtime-secret-id \
    zulip-local-runtime-secret-id-accessor \
    zulip-local-bootstrap-role-id zulip-local-bootstrap-secret-id \
    zulip-local-bootstrap-secret-id-accessor; do
    credential_path=/etc/credstore.encrypted/$credential
    [[ -f $credential_path && ! -L $credential_path && \
      $(stat -c '%u:%a' "$credential_path") == 0:400 ]] || {
      echo "Cannot enable: provision safe encrypted OpenBao AppRoles first." >&2
      exit 78
    }
  done
  systemctl enable zulip-local-secrets.service zulip-local.service \
    zulip-local-health.timer zulip-local-backup.timer
fi

echo "Zulip local files were installed. No service or container was started."
