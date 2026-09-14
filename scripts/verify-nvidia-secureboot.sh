#!/usr/bin/env bash
set -euo pipefail

mok_subject_has_signer() {
  local signer=$1
  awk -v expected="CN=${signer}" '
    /^[[:space:]]*Subject:/ {
      subject = $0
      sub(/^[[:space:]]*Subject:[[:space:]]*/, "", subject)
      count = split(subject, attributes, ",")
      for (i = 1; i <= count; i++) {
        gsub(/^[[:space:]]+|[[:space:]]+$/, "", attributes[i])
        if (attributes[i] == expected) found = 1
      }
    }
    END { exit found ? 0 : 1 }
  '
}

kernel="${1:-$(uname -r)}"
module=$(modinfo -k "$kernel" -n nvidia 2>/dev/null || true)
if [[ -z $module || $module != /lib/modules/"$kernel"/* || ! -r $module ]]; then
  echo "MISSING: signed NVIDIA module for ${kernel}" >&2
  exit 1
fi

version=$(modinfo -k "$kernel" -F version nvidia 2>/dev/null || true)
license=$(modinfo -k "$kernel" -F license nvidia 2>/dev/null || true)
signer=$(modinfo -k "$kernel" -F signer nvidia 2>/dev/null || true)
if [[ ! $version =~ ^580([.][0-9]+){1,3}$ || $license != NVIDIA ]]; then
  echo "FAIL: expected the proprietary NVIDIA 580xx module for ${kernel}" >&2
  exit 1
fi
if [[ -z "$signer" ]]; then
  echo "FAIL: NVIDIA module is unsigned for ${kernel}" >&2
  exit 1
fi

# Match the module's embedded signer CN to an enrolled MOK subject CN. This
# avoids exposing or requiring read access to the private akmods tree.
enrolled_moks=$(mokutil --list-enrolled 2>/dev/null || true)
if mok_subject_has_signer "$signer" <<<"$enrolled_moks"; then
  enrolled=yes
else
  enrolled=no
fi

printf 'kernel=%s\nmodule=%s\ndriver_version=%s\nmodule_signer=%s\nmok_enrolled=%s\n' \
  "$kernel" "$module" "$version" "$signer" "$enrolled"

if [[ "$enrolled" != yes ]]; then
  echo "FAIL: the NVIDIA module signer is not enrolled in MOK" >&2
  exit 2
fi

if [[ "$kernel" == "$(uname -r)" ]]; then
  if [[ -d /sys/module/nouveau || ! -d /sys/module/nvidia ]]; then
    echo "FAIL: the running kernel has not switched from nouveau to NVIDIA" >&2
    exit 1
  fi
  if ! nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader \
    | awk -F, -v expected="$version" '
        NF < 3 { exit 1 }
        { gsub(/^[[:space:]]+|[[:space:]]+$/, "", $2) }
        $2 != expected { exit 1 }
        END { if (NR < 1) exit 1 }
      '; then
    echo "FAIL: signed key is enrolled but the running NVIDIA driver is unavailable" >&2
    exit 1
  fi
fi
