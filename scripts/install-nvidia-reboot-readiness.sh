#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

usage() {
  cat <<'EOF'
Usage: pkexec scripts/install-nvidia-reboot-readiness.sh

Deploys the reviewed NVIDIA remediation broker assets and the corrected local
metrics collector. Existing managed policy is replaced only when it still
matches the previous reviewed release or this exact source tree.
EOF
}

if [[ $# -gt 0 ]]; then
  case "$1" in
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
fi

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root through the local authentication agent." >&2
  exit 77
fi

source_repository_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
readonly source_repository_dir

required_commands=(awk cmp find git grep install mktemp python3 restorecon runuser sha256sum stat systemctl systemd-analyze tar visudo)
for command_name in "${required_commands[@]}"; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is missing: $command_name" >&2
    exit 69
  }
done

if [[ $(stat -Lc '%U' -- "$source_repository_dir") != ops-user ]]; then
  echo "The source worktree is not owned by the supervised user ops-user." >&2
  exit 73
fi

# Never let root parse the user-controlled Git configuration. Git inspection
# and archive creation remain under ops-user; root only receives the archive on an
# already-open descriptor in its private staging directory.
git_as_source_user() {
  /usr/bin/runuser -u ops-user -- /usr/bin/env -i \
    PATH=/usr/bin LANG=C.UTF-8 LC_ALL=C.UTF-8 \
    GIT_ATTR_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null \
    GIT_CONFIG_SYSTEM=/dev/null GIT_NO_REPLACE_OBJECTS=1 \
    /usr/bin/git \
    -c core.fsmonitor=false \
    -c core.hooksPath=/dev/null \
    -c core.pager=cat \
    -c tar.tar.command=/usr/bin/cat \
    -c tar.umask=0022 \
    "$@"
}

git_root=''
if ! git_root=$(git_as_source_user \
  -C "$source_repository_dir" rev-parse --show-toplevel) || \
  [[ $git_root != "$source_repository_dir" ]]; then
  echo "Source directory is not the expected Git worktree root." >&2
  exit 78
fi
source_commit=''
if ! source_commit=$(git_as_source_user \
  -C "$source_repository_dir" rev-parse --verify 'HEAD^{commit}') || \
  [[ ! $source_commit =~ ^[0-9a-f]{40,64}$ ]]; then
  echo "Unable to resolve a committed source revision." >&2
  exit 78
fi
git_status=''
if ! git_status=$(git_as_source_user \
  -C "$source_repository_dir" status --porcelain=v1 --untracked-files=all); then
  echo "Unable to verify that the source tree is committed." >&2
  exit 78
fi
if [[ -n $git_status ]]; then
  echo "Refusing to deploy an uncommitted source tree." >&2
  exit 78
fi
if ! git_as_source_user -C "$source_repository_dir" \
  ls-tree -r "$source_commit" | awk '
    BEGIN { seen = 0 }
    {
      seen = 1
      if (($1 != "100644" && $1 != "100755") || $2 != "blob" ||
          $3 !~ /^[0-9a-f]+$/ || length($3) < 40 || length($3) > 64) exit 1
    }
    END { if (!seen) exit 1 }
  '; then
  echo "Committed source contains an unsupported entry type." >&2
  exit 78
fi

staging_dir=$(mktemp -d /var/tmp/ops-nvidia-readiness.XXXXXXXXXX)
readonly staging_dir
cleanup_staging() {
  if [[ $staging_dir == /var/tmp/ops-nvidia-readiness.* && \
        -d $staging_dir && ! -L $staging_dir ]]; then
    rm -rf -- "$staging_dir"
  fi
}
trap cleanup_staging EXIT
chmod 0700 "$staging_dir"
install -d -o root -g root -m 0700 "$staging_dir/tree"
git_as_source_user -C "$source_repository_dir" \
  ls-tree -r -z --full-tree "$source_commit" >"$staging_dir/tree.manifest"
git_as_source_user -C "$source_repository_dir" \
  archive --format=tar "$source_commit" >"$staging_dir/source.tar"
tar --extract --file="$staging_dir/source.tar" --directory="$staging_dir/tree" \
  --no-same-owner --no-same-permissions
if find "$staging_dir/tree" -xdev -type l -print -quit | grep -q . || \
   find "$staging_dir/tree" -xdev ! -type d ! -type f -print -quit | grep -q . || \
   find "$staging_dir/tree" -xdev \( ! -user root -o ! -group root \) \
     -print -quit | grep -q .; then
  echo "Deployment snapshot contains an unsafe filesystem entry." >&2
  exit 78
fi
/usr/bin/python3 - "$staging_dir/tree.manifest" "$staging_dir/tree" <<'PY'
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import sys


manifest_path = Path(sys.argv[1])
tree = Path(sys.argv[2])
raw_manifest = manifest_path.read_bytes()
if not raw_manifest or len(raw_manifest) > 16 * 1024 * 1024:
    raise SystemExit("invalid committed-tree manifest")

expected: dict[str, tuple[str, str]] = {}
for entry in raw_manifest.rstrip(b"\0").split(b"\0"):
    metadata, separator, raw_path = entry.partition(b"\t")
    fields = metadata.split(b" ")
    components = raw_path.split(b"/")
    if (
        not separator
        or len(fields) != 3
        or fields[0] not in {b"100644", b"100755"}
        or fields[1] != b"blob"
        or len(fields[2]) not in {40, 64}
        or any(character not in b"0123456789abcdef" for character in fields[2])
        or raw_path.startswith(b"/")
        or any(component in {b"", b".", b".."} for component in components)
    ):
        raise SystemExit("unsafe committed-tree entry")
    relative = os.fsdecode(raw_path)
    if relative in expected:
        raise SystemExit("duplicate committed-tree entry")
    expected[relative] = (fields[0].decode("ascii"), fields[2].decode("ascii"))

actual: set[str] = set()
for directory, directory_names, file_names in os.walk(tree, followlinks=False):
    for name in directory_names:
        metadata = (Path(directory) / name).lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_gid != 0:
            raise SystemExit("unsafe directory in deployment snapshot")
    for name in file_names:
        path = Path(directory) / name
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 1
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise SystemExit("unsafe file in deployment snapshot")
        relative = os.path.relpath(path, tree)
        actual.add(relative)
        if relative not in expected:
            raise SystemExit("unexpected file in deployment snapshot")
        source_mode, object_id = expected[relative]
        if bool(metadata.st_mode & stat.S_IXUSR) != (source_mode == "100755"):
            raise SystemExit("executable mode mismatch in deployment snapshot")
        digest = hashlib.sha1() if len(object_id) == 40 else hashlib.sha256()
        digest.update(f"blob {metadata.st_size}\0".encode("ascii"))
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != object_id:
            raise SystemExit("content mismatch in deployment snapshot")

if actual != set(expected):
    raise SystemExit("deployment snapshot does not match the committed tree")
PY
source_commit_after=$(git_as_source_user \
  -C "$source_repository_dir" rev-parse --verify 'HEAD^{commit}')
if [[ $source_commit_after != "$source_commit" ]]; then
  echo "Source revision changed while creating the deployment snapshot." >&2
  exit 78
fi
repository_dir="$staging_dir/tree"
readonly repository_dir

required_sources=(
  "$repository_dir/scripts/install-broker.sh"
  "$repository_dir/scripts/ops-local-metrics"
  "$repository_dir/systemd/ops-local-metrics.service"
  "$repository_dir/policies/actions.yaml"
  "$repository_dir/broker/config/rbac.yaml"
  "$repository_dir/broker/config/executables.yaml"
  "$repository_dir/broker/deploy/systemd/ops-broker.service"
  "$repository_dir/broker/deploy/helpers/nvidia-package-remediation"
  "$repository_dir/broker/deploy/helpers/nvidia-package-remediation-worker"
  "$repository_dir/broker/deploy/sudoers/ops-broker-nvidia-package-remediation"
  "$repository_dir/broker/deploy/systemd/ops-nvidia-package-remediation.service"
  "$repository_dir/runbooks/local/nvidia-package-remediation.yaml"
  "$repository_dir/runbooks/local/nvidia-package-remediation-status.yaml"
)
for source_path in "${required_sources[@]}"; do
  if [[ ! -f $source_path || -L $source_path ]]; then
    echo "Required source is absent, non-regular, or linked: $source_path" >&2
    exit 66
  fi
  source_mode=$(stat -Lc '%a' -- "$source_path")
  if (( (8#$source_mode & 022) != 0 )); then
    echo "Source is writable by group or others: $source_path" >&2
    exit 73
  fi
done

file_digest() {
  local path=$1 line
  line=$(sha256sum -- "$path")
  printf '%s\n' "${line%% *}"
}

assert_known_managed_file() {
  local destination=$1 previous_digest=$2 source=$3
  local actual_digest source_digest

  if [[ ! -e $destination ]]; then
    return 0
  fi
  if [[ ! -f $destination || -L $destination ]]; then
    echo "Managed destination is non-regular or linked: $destination" >&2
    return 73
  fi
  actual_digest=$(file_digest "$destination")
  source_digest=$(file_digest "$source")
  if [[ $actual_digest != "$previous_digest" && $actual_digest != "$source_digest" ]]; then
    echo "Refusing to overwrite divergent managed file: $destination" >&2
    return 78
  fi
}

# Digests are from the installed, reviewed 0381b3b handoff. Its actions policy
# differs from the tracked source only by one trailing blank line. The exact
# current source digest is also accepted so this installer remains idempotent.
assert_known_managed_file \
  /etc/ops-broker/actions.yaml \
  2f9c43bd0f79dd68975afcb007dc537a87ea54e5f000d9e090aa57fc0f10ebd0 \
  "$repository_dir/policies/actions.yaml"
assert_known_managed_file \
  /etc/ops-broker/rbac.yaml \
  e8230e5d756db32e7013797463dda1fa41aa669f61f8703655b71aea8dd93411 \
  "$repository_dir/broker/config/rbac.yaml"
assert_known_managed_file \
  /etc/ops-broker/executables.yaml \
  d154b88d14bc277961f7d89fe8fb68ab85c1756595d63516eeede064e0ad4f3a \
  "$repository_dir/broker/config/executables.yaml"
assert_known_managed_file \
  /usr/local/libexec/ops-local-metrics \
  8d13b1f6bb8e14428d1669c450f9e146f94f03ea9dbcc96e987ed451fb7ab460 \
  "$repository_dir/scripts/ops-local-metrics"
assert_known_managed_file \
  /etc/systemd/system/ops-local-metrics.service \
  bbf42c4d867ba9ada10d6b627093a3369097ab1a4f65dbcb753354463a65a2da \
  "$repository_dir/systemd/ops-local-metrics.service"

broker_arguments=(--replace-managed-configs)
api_should_be_enabled=0
if systemctl is-enabled --quiet ops-broker.socket 2>/dev/null; then
  api_should_be_enabled=1
  broker_arguments+=(--enable-api)
fi
"$repository_dir/scripts/install-broker.sh" "${broker_arguments[@]}"

install -o root -g root -m 0750 \
  "$repository_dir/scripts/ops-local-metrics" \
  /usr/local/libexec/ops-local-metrics
install -o root -g root -m 0644 \
  "$repository_dir/systemd/ops-local-metrics.service" \
  /etc/systemd/system/ops-local-metrics.service
restorecon -F \
  /usr/local/libexec/ops-local-metrics \
  /etc/systemd/system/ops-local-metrics.service

systemctl daemon-reload
systemd-analyze verify \
  /etc/systemd/system/ops-local-metrics.service \
  /etc/systemd/system/ops-nvidia-package-remediation.service
systemctl enable --now ops-local-metrics.timer
systemctl start ops-local-metrics.service

cmp -- "$repository_dir/scripts/ops-local-metrics" \
  /usr/local/libexec/ops-local-metrics
cmp -- "$repository_dir/systemd/ops-local-metrics.service" \
  /etc/systemd/system/ops-local-metrics.service
cmp -- "$repository_dir/policies/actions.yaml" \
  /etc/ops-broker/actions.yaml
cmp -- "$repository_dir/broker/config/rbac.yaml" \
  /etc/ops-broker/rbac.yaml
cmp -- "$repository_dir/broker/config/executables.yaml" \
  /etc/ops-broker/executables.yaml
cmp -- "$repository_dir/broker/deploy/systemd/ops-broker.service" \
  /etc/systemd/system/ops-broker.service
cmp -- "$repository_dir/broker/deploy/helpers/nvidia-package-remediation" \
  /usr/local/libexec/ops-runbooks/nvidia-package-remediation
cmp -- "$repository_dir/broker/deploy/helpers/nvidia-package-remediation-worker" \
  /usr/local/libexec/ops-runbooks/nvidia-package-remediation-worker
cmp -- "$repository_dir/broker/deploy/sudoers/ops-broker-nvidia-package-remediation" \
  /etc/sudoers.d/ops-broker-nvidia-package-remediation
cmp -- "$repository_dir/broker/deploy/systemd/ops-nvidia-package-remediation.service" \
  /etc/systemd/system/ops-nvidia-package-remediation.service
cmp -- "$repository_dir/runbooks/local/nvidia-package-remediation.yaml" \
  /opt/ops-broker/runbooks/local/nvidia-package-remediation.yaml
cmp -- "$repository_dir/runbooks/local/nvidia-package-remediation-status.yaml" \
  /opt/ops-broker/runbooks/local/nvidia-package-remediation-status.yaml
visudo -cf /etc/sudoers.d/ops-broker-nvidia-package-remediation >/dev/null
if [[ $api_should_be_enabled -eq 1 ]]; then
  if ! systemctl is-enabled --quiet ops-broker.socket || \
     ! systemctl is-active --quiet ops-broker.socket; then
    echo "The previously enabled broker socket was not preserved." >&2
    exit 1
  fi
elif systemctl is-enabled --quiet ops-broker.socket 2>/dev/null || \
     systemctl is-active --quiet ops-broker.socket 2>/dev/null || \
     systemctl is-active --quiet ops-broker.service 2>/dev/null; then
  echo "The previously disabled broker API was unexpectedly activated." >&2
  exit 1
fi
if [[ $(systemctl show -p Result --value ops-local-metrics.service) != success ]]; then
  echo "The refreshed local metrics collector did not complete successfully." >&2
  exit 1
fi
if ! grep -Fxq 'ops_nvidia_mok_enrolled 1' \
  /var/lib/node-exporter/textfile/ops.prom; then
  echo "The NVIDIA MOK metric did not validate after deployment." >&2
  exit 1
fi

echo "NVIDIA broker capability and reboot readiness metrics deployed from $source_commit."
