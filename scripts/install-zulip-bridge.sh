#!/usr/bin/env bash
set -Eeuo pipefail
# Record only the script, line and exit status; never command arguments or secrets.
atlas_report_exit() {
  if [[ $1 -ne 0 ]]; then
    logger -t atlas-installer -- "script=install-zulip-bridge line=$2 status=$1" || true
  fi
  return 0
}
trap 'atlas_report_exit "$?" "$LINENO"' EXIT
IFS=$'\n\t'

usage() {
  cat <<'EOF'
Usage: sudo scripts/install-zulip-bridge.sh [--check | --activate]

With no option, installs the root-owned bridge and its systemd units, then
leaves the bridge and its private Alertmanager query socket disabled/inactive.

  --check     Read-only, offline validation of source and installed state.
  --activate  Install, then enable/start only after the host+TPM2 encrypted
              OpenBao AppRole set is complete and fresh, and the broker Unix
              socket is active and accessible. Alertmanager is reached through
              a group-restricted Unix socket proxy; no bridge TCP listener exists.
EOF
}

mode=install
while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      [[ $mode == install ]] || { usage >&2; exit 64; }
      mode=check
      ;;
    --activate)
      [[ $mode == install ]] || { usage >&2; exit 64; }
      mode=activate
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 64
      ;;
  esac
  shift
done

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root (including --check, which validates root-only runtime files)." >&2
  exit 77
fi

repository_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
source_dir="$repository_dir/bridges/zulip"
install_root=/opt/ops-control-plane/bridges/zulip
venv_dir="$install_root/.venv"
state_dir=/var/lib/zulip-approval-bridge
unit_source="$source_dir/systemd/zulip-approval-bridge.service"
unit_target=/etc/systemd/system/zulip-approval-bridge.service
query_service_source="$source_dir/systemd/zulip-alertmanager-query.service"
query_service_target=/etc/systemd/system/zulip-alertmanager-query.service
query_socket_source="$source_dir/systemd/zulip-alertmanager-query.socket"
query_socket_target=/etc/systemd/system/zulip-alertmanager-query.socket
launcher_source="$repository_dir/scripts/zulip-openbao-launcher"
launcher_target=/usr/local/libexec/zulip-openbao-launcher
provisioner_source="$repository_dir/scripts/provision-zulip-openbao"
provisioner_target=/usr/local/sbin/provision-zulip-openbao
credstore=/etc/credstore.encrypted
credential_names=(
  zulip-openbao-role-id
  zulip-openbao-secret-id
  zulip-openbao-secret-id-accessor
)
broker_socket=/run/ops-broker/api.sock
alertmanager_socket=/run/zulip-alertmanager-query/api.sock
socket_proxyd=/usr/lib/systemd/systemd-socket-proxyd
service_name=zulip-approval-bridge.service
query_service_name=zulip-alertmanager-query.service
query_socket_name=zulip-alertmanager-query.socket

common_commands=(cmp date find getent grep id python3 runuser stat systemctl systemd-analyze systemd-creds)
install_commands=(flock groupadd install useradd usermod)
for required_command in "${common_commands[@]}"; do
  if ! command -v "$required_command" >/dev/null 2>&1; then
    echo "Required command is missing: $required_command" >&2
    exit 69
  fi
done
if [[ $mode != check ]]; then
  for required_command in "${install_commands[@]}"; do
    if ! command -v "$required_command" >/dev/null 2>&1; then
      echo "Required install command is missing: $required_command" >&2
      exit 69
    fi
  done
fi

validate_requirements() {
  local requirements_file=$1
  local line
  local count=0
  while IFS= read -r line || [[ -n $line ]]; do
    [[ -z $line || $line == \#* ]] && continue
    if [[ ! $line =~ ^[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+$ ]]; then
      echo "Runtime requirement is not an exact package pin." >&2
      return 1
    fi
    count=$((count + 1))
  done < "$requirements_file"
  if [[ $count -eq 0 ]]; then
    echo "Runtime requirements are empty." >&2
    return 1
  fi
}

validate_source() {
  local required_files=(
    "$source_dir/README.md"
    "$source_dir/pyproject.toml"
    "$source_dir/requirements.txt"
    "$source_dir/src/zulip_approval_bridge/__init__.py"
    "$source_dir/src/zulip_approval_bridge/bridge.py"
    "$source_dir/src/zulip_approval_bridge/cli.py"
    "$source_dir/src/zulip_approval_bridge/clients.py"
    "$source_dir/src/zulip_approval_bridge/config.py"
    "$source_dir/src/zulip_approval_bridge/errors.py"
    "$source_dir/src/zulip_approval_bridge/mobile.py"
    "$source_dir/src/zulip_approval_bridge/publishers.py"
    "$source_dir/src/zulip_approval_bridge/runtime.py"
    "$source_dir/src/zulip_approval_bridge/state.py"
    "$source_dir/config/example.env"
    "$unit_source"
    "$query_service_source"
    "$query_socket_source"
    "$launcher_source"
    "$provisioner_source"
  )
  local required_file
  for required_file in "${required_files[@]}"; do
    if [[ ! -f $required_file || -L $required_file ]]; then
      echo "Required regular source file is missing or symlinked: $required_file" >&2
      return 1
    fi
  done
  validate_requirements "$source_dir/requirements.txt"

  # Parse Python syntax without generating bytecode or touching the source tree.
  python3 - "$source_dir/src" <<'PY'
import ast
from pathlib import Path
import sys

root = Path(sys.argv[1])
files = sorted(root.rglob("*.py"))
if not files:
    raise SystemExit("no Python sources found")
for path in files:
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
PY

  python3 - "$launcher_source" "$provisioner_source" <<'PY'
import ast
from pathlib import Path
import sys

for raw_path in sys.argv[1:]:
    path = Path(raw_path)
    ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
PY

  grep -Fq 'ExecStart=/usr/local/libexec/zulip-openbao-launcher' "$unit_source"
  grep -Fq 'LoadCredentialEncrypted=zulip-openbao-role-id:/etc/credstore.encrypted/zulip-openbao-role-id' "$unit_source"
  grep -Fq 'LoadCredentialEncrypted=zulip-openbao-secret-id:/etc/credstore.encrypted/zulip-openbao-secret-id' "$unit_source"
  if grep -Fq 'EnvironmentFile=' "$unit_source"; then
    echo "Reviewed bridge unit must not read a plaintext environment file." >&2
    return 1
  fi
  grep -Fq 'ExecCondition=/usr/bin/test -S /run/ops-broker/api.sock' "$unit_source"
  grep -Fq 'ExecCondition=/usr/bin/test -S /run/zulip-alertmanager-query/api.sock' "$unit_source"
  grep -Fq 'SupplementaryGroups=opsbroker-api ops-readers' "$unit_source"
  grep -Fq 'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6' "$unit_source"
  grep -Fq 'ListenStream=/run/zulip-alertmanager-query/api.sock' "$query_socket_source"
  grep -Fq 'SocketGroup=zulipbridge' "$query_socket_source"
  grep -Fq 'SocketMode=0660' "$query_socket_source"
  grep -Fq 'ExecStart=/usr/lib/systemd/systemd-socket-proxyd --exit-idle-time=30s 127.0.0.1:9093' "$query_service_source"
  grep -Fq 'IPAddressDeny=any' "$query_service_source"
  grep -Fq 'IPAddressAllow=localhost' "$query_service_source"
  if [[ ! -x $socket_proxyd || -L $socket_proxyd ]]; then
    echo "Required systemd socket proxy is absent, non-executable, or symlinked." >&2
    return 1
  fi
  systemd-analyze security --offline=yes --no-pager "$unit_source" >/dev/null
  systemd-analyze security --offline=yes --no-pager "$query_service_source" >/dev/null
  # The reviewed bridge unit deliberately uses an absolute production launcher
  # path.  On a first install that path does not exist yet, so validate the
  # socket proxy units now and validate the complete installed unit set after
  # the launcher has been installed below.
  systemd-analyze verify "$query_service_source" "$query_socket_source" >/dev/null
  if [[ -x $launcher_target && ! -L $launcher_target ]]; then
    systemd-analyze verify "$unit_source" "$query_service_source" "$query_socket_source" >/dev/null
  fi
}

verify_installed_pins() {
  "$venv_dir/bin/python" - "$install_root/requirements.txt" <<'PY'
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys

for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    name, expected = line.split("==", 1)
    try:
        actual = version(name)
    except PackageNotFoundError as exc:
        raise SystemExit(f"missing pinned runtime package: {name}") from exc
    if actual != expected:
        raise SystemExit(f"runtime package version mismatch: {name}")
if version("setuptools") != "80.9.0":
    raise SystemExit("build backend version mismatch: setuptools")
PY
}

check_installed_state() {
  local foreign_owner
  local writable_entry
  if [[ ! -e $install_root ]]; then
    echo "Installed tree: absent (source preflight only)."
  else
    if [[ ! -d $install_root || -L $install_root ]]; then
      echo "Installed bridge root is not a real directory." >&2
      return 1
    fi
    foreign_owner=$(find "$install_root" -xdev ! -user root -print -quit)
    if [[ -n $foreign_owner ]]; then
      echo "Installed bridge contains a non-root-owned entry." >&2
      return 1
    fi
    writable_entry=$(find "$install_root" -xdev ! -type l \( -perm -0020 -o -perm -0002 \) -print -quit)
    if [[ -n $writable_entry ]]; then
      echo "Installed bridge contains a group/other-writable entry." >&2
      return 1
    fi
    if [[ ! -x $venv_dir/bin/zulip-approval-bridge ]]; then
      echo "Installed production entry point is absent." >&2
      return 1
    fi
    "$venv_dir/bin/python" -m pip check >/dev/null
    verify_installed_pins
    echo "Installed tree: root-owned, non-writable, dependencies coherent."
  fi

  local unit_pair source_unit installed_unit
  for unit_pair in \
    "$unit_source|$unit_target" \
    "$query_service_source|$query_service_target" \
    "$query_socket_source|$query_socket_target" \
    "$launcher_source|$launcher_target" \
    "$provisioner_source|$provisioner_target"; do
    source_unit=${unit_pair%%|*}
    installed_unit=${unit_pair#*|}
    if [[ -e $installed_unit ]]; then
      if [[ ! -f $installed_unit || -L $installed_unit ]]; then
        echo "Installed reviewed file is not a regular non-symlink file: $installed_unit" >&2
        return 1
      fi
      if ! cmp -s "$source_unit" "$installed_unit"; then
        echo "Installed reviewed file differs from its source: $installed_unit" >&2
        return 1
      fi
    fi
  done

  if systemctl is-enabled --quiet "$service_name" 2>/dev/null; then
    echo "Service state: enabled."
  else
    echo "Service state: disabled."
  fi
  if systemctl is-active --quiet "$service_name" 2>/dev/null; then
    echo "Service runtime: active."
  else
    echo "Service runtime: inactive."
  fi
  if systemctl is-enabled --quiet "$query_socket_name" 2>/dev/null; then
    echo "Alertmanager query socket: enabled."
  else
    echo "Alertmanager query socket: disabled."
  fi
  if systemctl is-active --quiet "$query_socket_name" 2>/dev/null; then
    echo "Alertmanager query socket runtime: active."
  else
    echo "Alertmanager query socket runtime: inactive."
  fi
}

validate_encrypted_credentials() {
  local credential
  local name
  local owner
  local permissions
  local now
  local modified
  local age

  for name in "${credential_names[@]}"; do
    credential="$credstore/$name"
    if [[ ! -f $credential || -L $credential ]]; then
      echo "Activation blocked: encrypted Zulip AppRole credential is absent or symlinked: $name" >&2
      return 1
    fi
    owner=$(stat -c '%u' "$credential")
    permissions=$(stat -c '%a' "$credential")
    if [[ $owner -ne 0 || $permissions != 600 ]]; then
      echo "Activation blocked: encrypted Zulip AppRole credentials must be root-owned mode 0600." >&2
      return 1
    fi
    if ! systemd-creds decrypt --refuse-null --name="$name" "$credential" - 2>/dev/null |
      python3 -c 'import re, sys
raw = sys.stdin.buffer.read(257)
if len(raw) > 256:
    raise SystemExit(1)
try:
    value = raw.rstrip(b"\r\n").decode("ascii", errors="strict")
except UnicodeError:
    raise SystemExit(1)
raise SystemExit(0 if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{15,127}", value) else 1)' \
        >/dev/null; then
      echo "Activation blocked: an encrypted Zulip AppRole credential cannot be safely decrypted." >&2
      return 1
    fi
  done

  now=$(date +%s)
  modified=$(stat -c '%Y' "$credstore/zulip-openbao-secret-id")
  age=$((now - modified))
  if (( age < 0 || age >= 2592000 )); then
    echo "Activation blocked: the 30-day Zulip SecretID is expired or has an invalid local timestamp." >&2
    return 1
  fi
}

activation_ready() {
  local blocked=0
  validate_encrypted_credentials || blocked=1
  if [[ ! -x $launcher_target || -L $launcher_target ]] \
    || [[ ! -x $provisioner_target || -L $provisioner_target ]]; then
    echo "Activation blocked: reviewed OpenBao launcher/provisioner is not installed." >&2
    blocked=1
  fi
  if ! systemctl is-active --quiet openbao.service 2>/dev/null; then
    echo "Activation blocked: openbao.service is inactive." >&2
    blocked=1
  fi
  if ! getent passwd zulipbridge >/dev/null; then
    echo "Activation blocked: zulipbridge service identity is absent." >&2
    blocked=1
  elif ! id -nG zulipbridge | tr ' ' '\n' | grep -Fxq opsbroker-api \
    || ! id -nG zulipbridge | tr ' ' '\n' | grep -Fxq ops-readers; then
    echo "Activation blocked: zulipbridge lacks an exact required supplementary group." >&2
    blocked=1
  fi
  if ! systemctl is-active --quiet ops-broker.socket 2>/dev/null || [[ ! -S $broker_socket ]]; then
    echo "Activation blocked: ops-broker.socket is not active at the reviewed Unix path." >&2
    blocked=1
  elif getent passwd zulipbridge >/dev/null \
    && ! runuser -u zulipbridge -- /usr/bin/test -w "$broker_socket"; then
    echo "Activation blocked: zulipbridge cannot connect to the broker Unix socket." >&2
    blocked=1
  fi
  if ! systemctl is-active --quiet prometheus-alertmanager.service 2>/dev/null; then
    echo "Activation blocked: prometheus-alertmanager.service is inactive." >&2
    blocked=1
  fi
  if [[ ! -x $socket_proxyd || -L $socket_proxyd ]]; then
    echo "Activation blocked: reviewed systemd-socket-proxyd is unavailable." >&2
    blocked=1
  fi
  [[ $blocked -eq 0 ]]
}

validate_source

if [[ $mode == check ]]; then
  check_installed_state
  if activation_ready; then
    echo "Activation readiness: ready (no network request was made)."
  else
    echo "Activation readiness: blocked as reported above (expected before secret provisioning)."
  fi
  echo "Offline check complete."
  exit 0
fi

install -d -o root -g root -m 0755 /run/lock
exec {install_lock}>/run/lock/zulip-approval-bridge-install.lock
if ! flock -n "$install_lock"; then
  echo "Another Zulip bridge installation is running." >&2
  exit 75
fi

# A failed update must never leave the bridge enabled or running.
keep_inactive_on_failure() {
  local result=$1
  atlas_report_exit "$result" "$2"
  trap - EXIT
  if [[ $result -ne 0 ]]; then
    systemctl disable --now "$service_name" >/dev/null 2>&1 || true
    systemctl disable --now "$query_socket_name" >/dev/null 2>&1 || true
    systemctl stop "$query_service_name" >/dev/null 2>&1 || true
  fi
  exit "$result"
}
trap 'keep_inactive_on_failure "$?" "$LINENO"' EXIT

systemctl disable --now "$service_name" >/dev/null 2>&1 || true
systemctl disable --now "$query_socket_name" >/dev/null 2>&1 || true
systemctl stop "$query_service_name" >/dev/null 2>&1 || true

for managed_parent in /opt/ops-control-plane /opt/ops-control-plane/bridges "$install_root" "$state_dir"; do
  if [[ -L $managed_parent ]]; then
    echo "Managed path must not be a symbolic link: $managed_parent" >&2
    exit 73
  fi
done

for bridge_group in zulipbridge opsbroker-api ops-readers; do
  if ! getent group "$bridge_group" >/dev/null; then
    groupadd --system "$bridge_group"
  fi
done
if ! getent passwd zulipbridge >/dev/null; then
  useradd --system --gid zulipbridge --home-dir "$state_dir" --no-create-home \
    --shell /usr/sbin/nologin --comment "Zulip Ops bridge" zulipbridge
fi
bridge_uid=$(id -u zulipbridge)
IFS=: read -r _ _ _ _ _ bridge_home bridge_shell < <(getent passwd zulipbridge)
if [[ $bridge_uid -eq 0 || $bridge_uid -ge 1000 || ($bridge_shell != /usr/sbin/nologin && $bridge_shell != /sbin/nologin && $bridge_shell != /bin/false) ]]; then
  echo "Refusing a privileged or interactive zulipbridge identity." >&2
  exit 73
fi
if [[ $bridge_home != "$state_dir" ]]; then
  if [[ $bridge_home != /var/lib/zulip-bridge ]]; then
    echo "Refusing an unexpected existing zulipbridge home: $bridge_home" >&2
    exit 73
  fi
  usermod --home "$state_dir" zulipbridge
fi
for required_group in opsbroker-api ops-readers; do
  if ! id -nG zulipbridge | tr ' ' '\n' | grep -Fxq "$required_group"; then
    usermod --append --groups "$required_group" zulipbridge
  fi
  if ! id -nG zulipbridge | tr ' ' '\n' | grep -Fxq "$required_group"; then
    echo "Failed to grant zulipbridge its required $required_group membership." >&2
    exit 73
  fi
done

install -d -o root -g root -m 0755 /opt/ops-control-plane /opt/ops-control-plane/bridges
install -d -o root -g root -m 0755 "$install_root"
install -d -o zulipbridge -g zulipbridge -m 0700 "$state_dir"

state_link=$(find "$state_dir" -xdev -type l -print -quit)
state_foreign_owner=$(find "$state_dir" -xdev ! -user zulipbridge -print -quit)
state_exposed=$(find "$state_dir" -xdev ! -type l -perm /0077 -print -quit)
if [[ -n $state_link || -n $state_foreign_owner || -n $state_exposed ]]; then
  echo "Existing bridge state contains an unsafe owner, mode, or symbolic link." >&2
  exit 73
fi

if [[ -e $install_root/.env ]]; then
  echo "Refusing an unexpected environment file inside the production code tree." >&2
  exit 73
fi
for managed_file in \
  "$unit_target" "$query_service_target" "$query_socket_target" \
  "$launcher_target" "$provisioner_target"; do
  if [[ -L $managed_file ]]; then
    echo "Installed reviewed path must not be a symbolic link: $managed_file" >&2
    exit 73
  fi
done
foreign_owner=$(find "$install_root" -xdev ! -user root -print -quit)
if [[ -n $foreign_owner ]]; then
  echo "Production tree contains a non-root-owned entry; refusing update." >&2
  exit 73
fi
unexpected_link=$(find "$install_root" -xdev -path "$venv_dir" -prune -o -type l -print -quit)
if [[ -n $unexpected_link ]]; then
  echo "Production source tree contains an unexpected symbolic link; refusing update." >&2
  exit 73
fi

copy_source_file() {
  local source_file=$1
  local relative_path=$2
  local destination="$install_root/$relative_path"
  if [[ -L $destination ]]; then
    echo "Refusing symlink destination: $destination" >&2
    return 1
  fi
  install -D -o root -g root -m 0644 "$source_file" "$destination"
}

for top_file in README.md pyproject.toml requirements.txt; do
  copy_source_file "$source_dir/$top_file" "$top_file"
done
copy_source_file "$source_dir/config/example.env" config/example.env
copy_source_file "$unit_source" systemd/zulip-approval-bridge.service
copy_source_file "$query_service_source" systemd/zulip-alertmanager-query.service
copy_source_file "$query_socket_source" systemd/zulip-alertmanager-query.socket
while IFS= read -r -d '' python_source; do
  relative_source=${python_source#"$source_dir/"}
  copy_source_file "$python_source" "$relative_source"
done < <(find "$source_dir/src" -xdev -type f -name '*.py' -print0)

if [[ -e $venv_dir && (! -d $venv_dir || -L $venv_dir) ]]; then
  echo "Production venv path is not a real directory." >&2
  exit 73
fi
if [[ ! -x $venv_dir/bin/python ]]; then
  python3 -m venv "$venv_dir"
fi
"$venv_dir/bin/python" -m pip install \
  --disable-pip-version-check --require-virtualenv --no-cache-dir --upgrade \
  --only-binary=:all: --no-deps --requirement "$install_root/requirements.txt"
"$venv_dir/bin/python" -m pip install \
  --disable-pip-version-check --require-virtualenv --no-cache-dir --upgrade \
  --only-binary=:all: setuptools==83.0.0
"$venv_dir/bin/python" -m pip install \
  --disable-pip-version-check --require-virtualenv --no-cache-dir --upgrade \
  --no-build-isolation --no-deps "$install_root"
"$venv_dir/bin/python" -m pip check
verify_installed_pins
test -x "$venv_dir/bin/zulip-approval-bridge"

chown -R root:root "$install_root"
# This tree contains installed code; private state and credentials live elsewhere.
chmod -R a+rX,go-w "$install_root"
install -d -o root -g root -m 0755 /usr/local/libexec /usr/local/sbin
install -o root -g root -m 0755 "$launcher_source" "$launcher_target"
install -o root -g root -m 0755 "$provisioner_source" "$provisioner_target"
install -o root -g root -m 0644 "$unit_source" "$unit_target"
install -o root -g root -m 0644 "$query_service_source" "$query_service_target"
install -o root -g root -m 0644 "$query_socket_source" "$query_socket_target"

# This is the authoritative whole-unit validation: every absolute executable
# referenced by the bridge unit is now present at its reviewed production path.
systemd-analyze verify "$unit_target" "$query_service_target" "$query_socket_target" >/dev/null

if command -v restorecon >/dev/null 2>&1; then
  restorecon -RF \
    "$install_root" "$state_dir" "$launcher_target" "$provisioner_target" \
    "$unit_target" "$query_service_target" "$query_socket_target" || {
    echo "Warning: restorecon reported an error; inspect SELinux labels before activation." >&2
  }
fi

systemctl daemon-reload
systemctl disable --now "$service_name" >/dev/null 2>&1 || true
systemctl disable --now "$query_socket_name" >/dev/null 2>&1 || true
systemctl stop "$query_service_name" >/dev/null 2>&1 || true

if [[ $mode == activate ]]; then
  if ! activation_ready; then
    echo "Bridge installed but left disabled and inactive." >&2
    exit 78
  fi
  # Reconcile a parent left by an older socket unit (root:root 0750).
  # systemd DirectoryMode only applies when it creates the directory.
  query_directory=${alertmanager_socket%/*}
  if [[ -L $query_directory || (-e $query_directory && ! -d $query_directory) ]]; then
    echo "Private Alertmanager query directory is not a real directory." >&2
    exit 73
  fi
  install -d -o root -g root -m 0755 "$query_directory"
  if ! systemctl enable --now "$query_socket_name"; then
    systemctl disable --now "$query_socket_name" >/dev/null 2>&1 || true
    echo "Alertmanager query socket activation failed; all bridge units remain dormant." >&2
    exit 1
  fi
  if [[ ! -S $alertmanager_socket ]] \
    || ! runuser -u zulipbridge -- /usr/bin/test -w "$alertmanager_socket"; then
    systemctl disable --now "$query_socket_name" >/dev/null 2>&1 || true
    echo "Private Alertmanager query socket is not accessible to zulipbridge." >&2
    exit 1
  fi
  if ! systemctl enable --now "$service_name"; then
    systemctl disable --now "$service_name" >/dev/null 2>&1 || true
    systemctl disable --now "$query_socket_name" >/dev/null 2>&1 || true
    systemctl stop "$query_service_name" >/dev/null 2>&1 || true
    echo "Activation failed; bridge returned to disabled/inactive state." >&2
    exit 1
  fi
  if ! systemctl is-enabled --quiet "$service_name" \
    || ! systemctl is-active --quiet "$service_name" \
    || ! systemctl is-enabled --quiet "$query_socket_name" \
    || ! systemctl is-active --quiet "$query_socket_name"; then
    systemctl disable --now "$service_name" >/dev/null 2>&1 || true
    systemctl disable --now "$query_socket_name" >/dev/null 2>&1 || true
    systemctl stop "$query_service_name" >/dev/null 2>&1 || true
    echo "Activation did not reach a healthy systemd state; bridge disabled again." >&2
    exit 1
  fi
  echo "Zulip Ops bridge and private Alertmanager query socket explicitly activated."
else
  echo "Zulip Ops bridge installed; bridge and query socket are disabled and inactive."
  echo "Populate the reviewed Zulip KV object and run provision-zulip-openbao; then review --check before --activate."
fi
