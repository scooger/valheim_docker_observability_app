#!/usr/bin/env bash
# Full OS package update. Intended to run from cron at 03:00 America/New_York.
# Reboots only when a reboot is required AND Valheim has no active player sessions.
set -euo pipefail

LOG=/var/log/valheim-weekly-os-update.log
CONTAINER="${VALHEIM_CONTAINER:-valheim-server}"

log() {
  echo "$(date -Is) $*"
}

valheim_is_idle() {
  if ! command -v docker >/dev/null 2>&1; then
    return 0
  fi
  if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -qx true; then
    return 0
  fi
  # Image helper: exit 0 = idle, exit 1 = players connected.
  # Private servers use UDP activity heuristics (not a Steam query).
  if docker exec "$CONTAINER" /usr/local/bin/valheim-is-idle; then
    return 0
  fi
  return 1
}

maybe_reboot() {
  if [[ ! -f /var/run/reboot-required ]]; then
    log "No reboot required"
    return 0
  fi

  if ! valheim_is_idle; then
    log "Reboot required but Valheim has active sessions; deferring reboot"
    return 0
  fi

  log "Reboot required and Valheim is idle; rebooting in 60 seconds"
  /sbin/shutdown -r +1 "Valheim host weekly OS update"
}

# Mode: reboot-only is used by the deferred retry cron.
if [[ "${1:-}" == "--reboot-if-idle" ]]; then
  exec >>"$LOG" 2>&1
  log "===== deferred reboot check ====="
  maybe_reboot
  exit 0
fi

exec >>"$LOG" 2>&1
log "===== weekly OS update start ====="

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

apt-get update
apt-get -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold" dist-upgrade
apt-get -y autoremove --purge
apt-get -y autoclean

log "===== weekly OS update finished ====="
maybe_reboot
