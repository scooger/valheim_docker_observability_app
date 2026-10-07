#!/usr/bin/env bash
# Append a Valheim log line to the persistent player-events log.
# Called by ON_VALHEIM_LOG_FILTER_* hooks inside the container.
set -euo pipefail

tag="${1:-event}"
logfile="/config/player-events.log"
mkdir -p "$(dirname "$logfile")"
read -r line || true
[[ -z "${line:-}" ]] && exit 0
printf '%s\t%s\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$tag" "$line" >>"$logfile"
