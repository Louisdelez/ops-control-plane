#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'
umask 022

readonly claude_version=2.1.236
readonly claude_release=1
readonly claude_arch=x86_64
readonly anthropic_key_package=gpg-pubkey-31ddde24ddfab679f42d7bd2baa929ff1a7ecace-69caef70

if [[ ${EUID} -ne 0 ]]; then
  echo 'Run as root.' >&2
  exit 77
fi

repository_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
readonly repository_dir
readonly source_repo=$repository_dir/config/fedora/claude-code.repo
readonly source_mcp=$repository_dir/config/claude/managed-mcp.json
readonly source_instructions=$repository_dir/config/claude/CLAUDE.md

for command_name in dnf getent install python3 rpm runuser stat; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is missing: $command_name" >&2
    exit 69
  }
done
getent passwd ops-user >/dev/null || {
  echo 'The supervised operator account ops-user is unavailable.' >&2
  exit 67
}

for source_file in "$source_repo" "$source_mcp" "$source_instructions"; do
  if [[ ! -f $source_file || -L $source_file ]]; then
    echo "Required regular source file is missing or symlinked: $source_file" >&2
    exit 66
  fi
  source_mode=$(stat -Lc '%a' -- "$source_file")
  if (( (8#$source_mode & 022) != 0 )); then
    echo "Source is writable by group or others: $source_file" >&2
    exit 73
  fi
done

python3 - "$source_mcp" <<'PY'
import json
from pathlib import Path
import sys

document = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if set(document) != {"mcpServers"} or set(document["mcpServers"]) != {
    "ops-broker", "ops-orchestrator", "ops-memory"
}:
    raise SystemExit("managed Claude MCP configuration has an unexpected shape")
expected = {
    "ops-broker": "/usr/bin/sudo",
    "ops-orchestrator": "/usr/local/libexec/ops-orchestrator/ops-orchestrator-mcp-claude",
    "ops-memory": "/opt/ops-memory/bin/ops-memory-mcp",
}
for name, command in expected.items():
    server = document["mcpServers"][name]
    if server.get("command") != command or server.get("type") != "stdio":
        raise SystemExit(f"managed Claude MCP server is unsafe: {name}")
PY

for destination in \
  /etc/yum.repos.d/claude-code.repo \
  /etc/claude-code /etc/claude-code/managed-mcp.json \
  /home/ops-user/.claude /home/ops-user/.claude/CLAUDE.md; do
  if [[ -L $destination ]]; then
    echo "Managed Claude path must not be a symbolic link: $destination" >&2
    exit 73
  fi
done

install -o root -g root -m 0644 "$source_repo" /etc/yum.repos.d/claude-code.repo
dnf -y install "claude-code-${claude_version}-${claude_release}.${claude_arch}"

rpm -q --quiet "$anthropic_key_package" || {
  echo 'The exact reviewed Anthropic signing key is not installed.' >&2
  exit 65
}
installed_nevra=$(rpm -q --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}' claude-code)
expected_nevra="claude-code-${claude_version}-${claude_release}.${claude_arch}"
if [[ $installed_nevra != "$expected_nevra" ]]; then
  echo "Unexpected Claude Code package: $installed_nevra" >&2
  exit 65
fi

install -d -o root -g root -m 0755 /etc/claude-code
install -o root -g root -m 0644 "$source_mcp" /etc/claude-code/managed-mcp.json
install -d -o ops-user -g ops-user -m 0700 /home/ops-user/.claude
install -o ops-user -g ops-user -m 0644 "$source_instructions" /home/ops-user/.claude/CLAUDE.md

runuser -u ops-user -- /usr/bin/claude --version | grep -Fq "$claude_version" || {
  echo 'Claude Code runtime version check failed.' >&2
  exit 65
}

echo "Claude Code $claude_version and its three supervised MCP definitions are installed."
echo 'Account authentication remains an explicit human browser action: claude auth login'
