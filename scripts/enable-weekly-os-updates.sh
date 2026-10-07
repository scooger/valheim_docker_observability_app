#!/usr/bin/env bash
# Install weekly OS updates via cron at 03:00 Eastern, and disable daily unattended upgrades.
# Reboot is skipped while Valheim has players; retries every 30 minutes until noon Sunday.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash scripts/enable-weekly-os-updates.sh" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y cron

if command -v timedatectl >/dev/null 2>&1; then
  timedatectl set-timezone America/New_York
fi

install -m 0755 "$ROOT/scripts/weekly-os-update.sh" /usr/local/sbin/valheim-weekly-os-update
touch /var/log/valheim-weekly-os-update.log
chmod 644 /var/log/valheim-weekly-os-update.log

# Stop daily auto-upgrades; cron owns the schedule.
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "0";
APT::Periodic::Unattended-Upgrade "0";
APT::Periodic::Download-Upgradeable-Packages "0";
APT::Periodic::AutocleanInterval "0";
EOF

rm -f /etc/apt/apt.conf.d/52valheim-unattended
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer 2>/dev/null || true
systemctl disable --now unattended-upgrades.service 2>/dev/null || true

# Sunday 03:00 Eastern: full update.
# Sunday 03:30-11:30 every 30 minutes: reboot only if still required and Valheim is idle.
cat >/etc/cron.d/valheim-weekly-os-update <<'EOF'
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/sbin:/bin:/usr/sbin:/usr/bin
MAILTO=""

0 3 * * 0 root /usr/local/sbin/valheim-weekly-os-update
30 3-11 * * 0 root /usr/local/sbin/valheim-weekly-os-update --reboot-if-idle
0 4-11 * * 0 root /usr/local/sbin/valheim-weekly-os-update --reboot-if-idle
EOF
chmod 644 /etc/cron.d/valheim-weekly-os-update

systemctl enable --now cron.service

echo
echo "Weekly OS updates scheduled:"
echo "  Update: Sundays 03:00 America/New_York"
echo "  Reboot: only when idle; retries every 30m until 11:30"
echo "  Script: /usr/local/sbin/valheim-weekly-os-update"
echo "  Log:    /var/log/valheim-weekly-os-update.log"
timedatectl | sed -n '1,4p' || true
