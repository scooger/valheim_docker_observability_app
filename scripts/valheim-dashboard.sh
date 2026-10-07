#!/usr/bin/env bash
# Live terminal dashboard: Docker containers + Valheim sessions from server logs.
#
#   bash /opt/valheim-server/scripts/valheim-dashboard.sh
#   INTERVAL=1 bash /opt/valheim-server/scripts/valheim-dashboard.sh
#
# Valheim logs SteamID on connect and character name on spawn. It does not log
# client IPs (Steam networking). Optional host conntrack shows UDP peer IPs.
set -euo pipefail

CONTAINER="${VALHEIM_CONTAINER:-valheim-server}"
INTERVAL="${INTERVAL:-2}"
EVENTS_HOST="${EVENTS_HOST:-/opt/valheim-server/state/config/player-events.log}"

docker_cmd() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
  elif command -v sudo >/dev/null 2>&1; then
    sudo docker "$@"
  else
    return 1
  fi
}

build_sessions() {
  local logs
  logs="$(docker_cmd logs --since 24h "$CONTAINER" 2>/dev/null || true)"

  declare -A steam_name=()
  declare -A online=()
  declare -a order=()
  local last_steam=""
  local connections="?"
  local line char sid seen s

  while IFS= read -r line; do
    if [[ "$line" =~ Player\ history\ entry.*:\ \ +([^[:space:]]+)\ \(Steam_([0-9]+) ]]; then
      steam_name["${BASH_REMATCH[2]}"]="${BASH_REMATCH[1]}"
    fi
  done <<<"$logs"

  while IFS= read -r line; do
    if [[ "$line" =~ Got\ connection\ SteamID\ ([0-9]+) ]]; then
      last_steam="${BASH_REMATCH[1]}"
      online["$last_steam"]=1
      [[ -z "${steam_name[$last_steam]:-}" ]] && steam_name["$last_steam"]="(joining…)"
      seen=0
      for s in ${order[@]+"${order[@]}"}; do
        [[ "$s" == "$last_steam" ]] && { seen=1; break; }
      done
      [[ $seen -eq 0 ]] && order+=("$last_steam")
    elif [[ "$line" =~ Got\ character\ ZDOID\ from\ (.+)\ : ]]; then
      char="${BASH_REMATCH[1]}"
      if [[ -n "$last_steam" && -n "${online[$last_steam]:-}" ]]; then
        steam_name["$last_steam"]="$char"
      else
        for s in ${order[@]+"${order[@]}"}; do
          if [[ -n "${online[$s]:-}" && "${steam_name[$s]:-}" == "(joining…)" ]]; then
            steam_name["$s"]="$char"
            break
          fi
        done
      fi
    elif [[ "$line" =~ Closing\ socket\ ([0-9]+) ]]; then
      unset "online[${BASH_REMATCH[1]}]"
    elif [[ "$line" =~ ClosedByPeer ]] && [[ "$line" =~ ([0-9]{17}) ]]; then
      unset "online[${BASH_REMATCH[1]}]"
    elif [[ "$line" =~ Connections\ ([0-9]+) ]]; then
      connections="${BASH_REMATCH[1]}"
    fi
  done <<<"$logs"

  SESSION_CONNECTIONS="$connections"
  SESSION_LINES=()
  local count=0
  for s in ${order[@]+"${order[@]}"}; do
    [[ -n "${online[$s]:-}" ]] || continue
    SESSION_LINES+=("$(printf '%-20s  SteamID %s' "${steam_name[$s]:-unknown}" "$s")")
    count=$((count + 1))
  done
  if [[ $count -eq 0 ]]; then
    SESSION_LINES=("(no active sessions parsed from logs)")
  fi
}

udp_peers() {
  if command -v conntrack >/dev/null 2>&1; then
    local out
    out="$(sudo -n conntrack -L -p udp 2>/dev/null \
      | awk '/dport=(2456|2457|2458)/ {
          src="";
          for (i=1;i<=NF;i++) {
            if ($i ~ /^src=/ && $i !~ /src=192\.168\.10\.12/ && $i !~ /src=127\./ && $i !~ /src=172\.(1[7-9]|2[0-9]|3[0-1])\./)
              src=substr($i,5);
          }
          if (src != "") print src;
        }' \
      | sort -u)"
    if [[ -n "$out" ]]; then
      echo "$out"
    else
      echo "(no UDP peers in conntrack right now)"
    fi
  else
    echo "(optional: sudo apt install conntrack — shows peer IPs for UDP 2456-2458)"
  fi
}

render() {
  if [[ -t 1 ]]; then
    clear
  else
    echo
  fi
  echo "Valheim live dashboard  |  $(date '+%Y-%m-%d %H:%M:%S %Z')  |  every ${INTERVAL}s  |  Ctrl-C quit"
  echo "================================================================================"
  echo
  echo "Docker"
  echo "------"
  docker_cmd ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null \
    || echo "(docker unavailable)"
  echo
  echo "Valheim sessions"
  echo "----------------"
  echo "Server-reported connections: ${SESSION_CONNECTIONS}"
  echo
  for line in "${SESSION_LINES[@]}"; do
    echo "  $line"
  done
  echo
  echo "Recent player-events.log"
  echo "------------------------"
  if [[ -r "$EVENTS_HOST" ]]; then
    tail -n 10 "$EVENTS_HOST" | sed 's/^/  /'
  else
    echo "  (none yet — log hooks write here after container recreate)"
  fi
  echo
  echo "UDP peer IPs (host)"
  echo "-------------------"
  while IFS= read -r peer; do
    echo "  $peer"
  done < <(udp_peers)
  echo
}

trap 'echo; exit 0' INT TERM

if [[ "${1:-}" == "--once" ]]; then
  build_sessions
  render
  exit 0
fi

while true; do
  build_sessions
  render
  sleep "$INTERVAL"
done
