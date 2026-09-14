#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root." >&2
  exit 1
fi

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

install -D -m 0644 "$repo_dir/systemd/90-ops-hardening.conf" \
  /etc/sysctl.d/90-ops-hardening.conf
install -D -m 0644 "$repo_dir/systemd/90-ops-logind.conf" \
  /etc/systemd/logind.conf.d/90-ops-control-plane.conf
install -D -m 0644 "$repo_dir/systemd/90-ops-resolved.conf" \
  /etc/systemd/resolved.conf.d/90-ops-control-plane.conf
install -D -m 0644 "$repo_dir/systemd/90-ops-coredump.conf" \
  /etc/systemd/coredump.conf.d/90-ops-control-plane.conf
install -D -m 0644 "$repo_dir/systemd/ops-users.conf" \
  /etc/sysusers.d/ops-control-plane.conf
install -D -m 0644 "$repo_dir/systemd/ops-directories.conf" \
  /etc/tmpfiles.d/ops-control-plane.conf

systemd-sysusers /etc/sysusers.d/ops-control-plane.conf
systemd-tmpfiles --create /etc/tmpfiles.d/ops-control-plane.conf

if ! firewall-cmd --permanent --get-zones | tr ' ' '\n' | grep -qx ops-control; then
  firewall-cmd --permanent --new-zone=ops-control
fi
firewall-cmd --permanent --zone=ops-control --set-target=DROP
# A newly created connection must fail closed even before NetworkManager has
# assigned it an explicit zone.  FedoraWorkstation is intentionally not kept
# as the default because it admits SSH and the high-port range.
firewall-cmd --set-default-zone=ops-control

while IFS=: read -r uuid type device; do
  [[ -n ${uuid} && -n ${device} ]] || continue
  case "$type" in
    802-11-wireless|802-3-ethernet)
      nmcli connection modify "$uuid" connection.zone ops-control
      ;;
  esac
done < <(nmcli -t -f UUID,TYPE,DEVICE connection show --active)

firewall-cmd --reload

systemctl disable --now avahi-daemon.socket avahi-daemon.service 2>/dev/null || true
systemctl mask avahi-daemon.socket avahi-daemon.service >/dev/null 2>&1 || true
systemctl disable --now wsdd.service 2>/dev/null || true
systemctl mask wsdd.service >/dev/null 2>&1 || true
systemctl stop passim.service 2>/dev/null || true
systemctl mask passim.service >/dev/null 2>&1 || true

systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null
loginctl enable-linger ops-user

# Desktop policy is applied to the local operator session. Failure is harmless
# when no graphical session exists; logind and masked sleep targets still win.
runuser -u ops-user -- gsettings set \
  org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing' 2>/dev/null || true
runuser -u ops-user -- gsettings set \
  org.gnome.settings-daemon.plugins.power sleep-inactive-ac-timeout 0 2>/dev/null || true
runuser -u ops-user -- gsettings set \
  org.gnome.system.wsdd display-mode 'disabled' 2>/dev/null || true

sysctl --system >/dev/null
systemctl daemon-reload
systemctl restart systemd-resolved

echo "Local hardening applied. Re-login may be needed for desktop power settings."
