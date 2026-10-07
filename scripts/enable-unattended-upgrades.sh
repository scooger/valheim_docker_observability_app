#!/usr/bin/env bash
# Enable automatic security updates and a timed reboot on Ubuntu/Debian.
# Safe with Valheim: compose uses restart: unless-stopped and a 2m stop grace period.
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash scripts/enable-unattended-upgrades.sh" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y unattended-upgrades apt-listchanges

# Daily apt update + unattended-upgrade run (Ubuntu default timers also cover this).
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::Download-Upgradeable-Packages "1";
APT::Periodic::AutocleanInterval "7";
EOF

# Prefer a quiet window that matches the container RESTART_CRON (~05:10 local).
# Reboot only when a package asks for it (kernel, libc, etc.).
cat >/etc/apt/apt.conf.d/52valheim-unattended <<'EOF'
Unattended-Upgrade::Allowed-Origins {
  "${distro_id}:${distro_codename}";
  "${distro_id}:${distro_codename}-security";
  "${distro_id}ESMApps:${distro_codename}-apps-security";
  "${distro_id}ESM:${distro_codename}-infra-security";
};
Unattended-Upgrade::Package-Blacklist {
};
Unattended-Upgrade::DevRelease "auto";
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-New-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-WithUsers "true";
Unattended-Upgrade::Automatic-Reboot-Time "05:15";
Unattended-Upgrade::SyslogEnable "true";
EOF

# Align host clock labels with the Valheim container TZ when timedatectl is available.
if command -v timedatectl >/dev/null 2>&1; then
  timedatectl set-timezone America/New_York || true
fi

dpkg-reconfigure -f noninteractive unattended-upgrades
systemctl enable --now unattended-upgrades.service
systemctl enable --now apt-daily.timer apt-daily-upgrade.timer

echo
echo "Unattended upgrades enabled."
echo "  Reboot time when required: 05:15 (host local time)"
echo "  Dry-run:  unattended-upgrade --dry-run --debug"
echo "  Status:   systemctl status unattended-upgrades --no-pager"
