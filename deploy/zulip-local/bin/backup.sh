#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
# shellcheck source-path=SCRIPTDIR
source "$script_dir/common.sh"

backup_dir=/var/backups/zulip-local
recipient_file=/etc/openbao.d/recovery.age-recipient
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir) [[ $# -ge 2 ]] || exit 64; backup_dir=$2; shift ;;
    --recipient-file) [[ $# -ge 2 ]] || exit 64; recipient_file=$2; shift ;;
    --help|-h)
      echo "Usage: $0 [--output-dir DIR] [--recipient-file PUBLIC_RECIPIENT_FILE]"
      exit 0
      ;;
    *) echo "Unknown argument: $1" >&2; exit 64 ;;
  esac
  shift
done

require_root
require_command age date docker flock install realpath sha256sum stat tr
lock_deployment
umask 077
"$script_dir/health.sh" --quiet

[[ -f $recipient_file && ! -L $recipient_file ]] || {
  echo "The age recipient must be a regular, non-symlink file." >&2
  exit 78
}
recipient_file=$(realpath -e -- "$recipient_file")
validate_root_managed_file "$recipient_file"
validate_root_managed_parent_chain "$recipient_file"
recipient=$(tr -d '[:space:]' < "$recipient_file")
[[ $recipient =~ ^age1[0-9a-z]{20,}$ ]] || {
  echo "The age recipient file is invalid." >&2
  exit 78
}

if [[ -e $backup_dir || -L $backup_dir ]]; then
  [[ -d $backup_dir && ! -L $backup_dir ]] || {
    echo "The Zulip backup path is not a regular directory." >&2
    exit 78
  }
else
  install -d -m 0700 -o root -g root "$backup_dir"
fi
[[ $(stat -c '%u:%a' "$backup_dir") == 0:700 ]] || {
  echo "The Zulip backup directory has unsafe ownership or mode." >&2
  exit 78
}
backup_dir=$(realpath -e -- "$backup_dir")
validate_root_managed_parent_chain "$backup_dir"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
name="zulip-local-$stamp.tar.gz.age"
final="$backup_dir/$name"
temporary="$backup_dir/.$name.$$.tmp"
[[ ! -e $final && ! -e $final.sha256 ]] || {
  echo "A backup with this timestamp already exists." >&2
  exit 73
}
trap 'rm -f -- "$temporary"' EXIT

# app:backup is the image's official consistent pg_dump operation.
compose exec -T zulip /sbin/entrypoint.sh app:backup >/dev/null

# Docker's documented volume-backup pattern, streamed straight into age.  No
# plaintext tar is ever materialized on the host.
compose run --rm -T --no-deps zulip tar czf - \
  --exclude=./zulip-secrets.conf \
  --exclude=./certs/manual \
  --exclude=./etc-zulip/zulip-secrets.conf \
  -C /data . |
  age --encrypt --recipient "$recipient" --output "$temporary"
chmod 0400 "$temporary"
mv -- "$temporary" "$final"
(
  cd "$backup_dir"
  sha256sum "$name" > "$name.sha256"
  chmod 0400 "$name.sha256"
)
trap - EXIT
echo "Encrypted Zulip snapshot created: $final"
