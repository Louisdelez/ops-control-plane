#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

usage() {
  cat <<'EOF'
Usage: scripts/install-post-reboot-benchmark.sh

Installs and enables the user-level, one-shot post-reboot NVIDIA/Ollama gate.
It does not start the service in the current session, change drivers, load a
module, pull a model, promote a model, or perform any root-level mutation.
EOF
}

if [[ $# -gt 0 ]]; then
  case "$1" in
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
fi

if [[ ${EUID} -eq 0 ]]; then
  echo "Run this installer as the target desktop user, not as root." >&2
  exit 77
fi

repository_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
readonly repository_dir
home_dir=${HOME:?HOME is required}
readonly home_dir
libexec_dir="$home_dir/.local/libexec"
data_dir="$home_dir/.local/share/ops-control-plane/post-reboot"
state_dir="$home_dir/.local/state/ops-control-plane/post-reboot"
unit_dir="$home_dir/.config/systemd/user"

required_commands=(install python3 stat systemctl systemd-analyze)
for command_name in "${required_commands[@]}"; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is missing: $command_name" >&2
    exit 69
  }
done

sources=(
  "$repository_dir/scripts/ops-post-reboot-benchmark"
  "$repository_dir/systemd/user/ops-post-reboot-benchmark.service"
  "$repository_dir/benchmarks/ollama_benchmark.py"
  "$repository_dir/benchmarks/prompts.json"
  "$repository_dir/benchmarks/decision.schema.json"
  "$repository_dir/config/ollama/benchmark.json"
  "$repository_dir/docs/post-reboot-benchmark.md"
)
for source_path in "${sources[@]}"; do
  if [[ ! -f $source_path || -L $source_path ]]; then
    echo "Required source is absent, not regular, or linked: $source_path" >&2
    exit 66
  fi
  source_mode=$(stat -Lc '%a' -- "$source_path")
  if (( (8#$source_mode & 022) != 0 )); then
    echo "Source is writable by group or others: $source_path" >&2
    exit 73
  fi
done

python3 -m py_compile \
  "$repository_dir/scripts/ops-post-reboot-benchmark" \
  "$repository_dir/benchmarks/ollama_benchmark.py"

install -d -m 0700 \
  "$libexec_dir" \
  "$data_dir/benchmarks/results" \
  "$data_dir/config/ollama" \
  "$data_dir/docs" \
  "$state_dir" \
  "$unit_dir"
install -m 0755 \
  "$repository_dir/scripts/ops-post-reboot-benchmark" \
  "$libexec_dir/ops-post-reboot-benchmark"
install -m 0755 \
  "$repository_dir/benchmarks/ollama_benchmark.py" \
  "$data_dir/benchmarks/ollama_benchmark.py"
install -m 0644 \
  "$repository_dir/benchmarks/prompts.json" \
  "$data_dir/benchmarks/prompts.json"
install -m 0644 \
  "$repository_dir/benchmarks/decision.schema.json" \
  "$data_dir/benchmarks/decision.schema.json"
install -m 0644 \
  "$repository_dir/config/ollama/benchmark.json" \
  "$data_dir/config/ollama/benchmark.json"
install -m 0644 \
  "$repository_dir/docs/post-reboot-benchmark.md" \
  "$data_dir/docs/post-reboot-benchmark.md"
install -m 0644 \
  "$repository_dir/systemd/user/ops-post-reboot-benchmark.service" \
  "$unit_dir/ops-post-reboot-benchmark.service"

systemd-analyze --user verify \
  "$unit_dir/ops-post-reboot-benchmark.service" >/dev/null
systemctl --user daemon-reload
systemctl --user enable ops-post-reboot-benchmark.service

echo "Post-reboot NVIDIA/Ollama gate enabled for the next user-manager start."
echo "No benchmark was started in this session."
