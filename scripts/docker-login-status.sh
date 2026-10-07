#!/usr/bin/env bash
# Show running Docker containers and live connections on their published ports.
# Installed as /etc/profile.d/docker-login-status.sh (interactive logins only).

# Skip non-interactive SSH (scp, ssh host 'cmd', automation).
case $- in
  *i*) ;;
  *) return 0 2>/dev/null || exit 0 ;;
esac

if [[ -n "${DOCKER_LOGIN_STATUS_DONE:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi
export DOCKER_LOGIN_STATUS_DONE=1

if ! command -v docker >/dev/null 2>&1; then
  return 0 2>/dev/null || exit 0
fi

# Prefer rootless/group docker; fall back to sudo for users not in the docker group.
docker_cmd() {
  if docker info >/dev/null 2>&1; then
    docker "$@"
  elif command -v sudo >/dev/null 2>&1 && sudo -n docker info >/dev/null 2>&1; then
    sudo -n docker "$@"
  else
    return 1
  fi
}

echo
echo "=== Docker containers ==="

if ! docker_cmd ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null; then
  echo "(docker not available for this user)"
  echo
  return 0 2>/dev/null || exit 0
fi

# Collect host ports published by running containers (tcp/udp), including ranges.
mapfile -t published < <(
  docker_cmd ps --format '{{.Ports}}' 2>/dev/null \
    | tr ',' '\n' \
    | sed -n 's/.*:\([0-9][0-9]*\)\(-[0-9][0-9]*\)\?->.*/\1\2/p' \
    | while read -r spec; do
        if [[ "$spec" =~ ^([0-9]+)-([0-9]+)$ ]]; then
          seq "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
        elif [[ "$spec" =~ ^[0-9]+$ ]]; then
          echo "$spec"
        fi
      done \
    | sort -n -u
)

echo
echo "=== Connections on published ports ==="

if [[ ${#published[@]} -eq 0 ]]; then
  echo "(no published host ports)"
  echo
  return 0 2>/dev/null || exit 0
fi

echo "Listening: ${published[*]}"

if ! command -v ss >/dev/null 2>&1; then
  echo "(ss not installed; cannot list connections)"
  echo
  return 0 2>/dev/null || exit 0
fi

found=0
for port in "${published[@]}"; do
  # Established peers on this host port (TCP). UDP shows as UNCONN for listeners.
  while read -r line; do
    [[ -z "$line" ]] && continue
    if [[ $found -eq 0 ]]; then
      printf '%-6s %-12s %-22s %-22s %s\n' "PORT" "PROTO" "LOCAL" "PEER" "STATE"
    fi
    found=1
    printf '%-6s %s\n' "$port" "$line"
  done < <(
    ss -H -tnp "sport = :$port" 2>/dev/null | awk '{
      proto="tcp"
      local=$4; peer=$5; state=$1
      printf "%-12s %-22s %-22s %s\n", proto, local, peer, state
    }'
    # UDP "connections" are often empty; still show listeners with traffic if any.
    ss -H -unp "sport = :$port" 2>/dev/null | awk 'NR>0 {
      proto="udp"
      local=$4; peer=$5; state=$1
      if (peer == "" || peer == "*:*" || peer == "0.0.0.0:*" || peer == "[::]:*") next
      printf "%-12s %-22s %-22s %s\n", proto, local, peer, state
    }'
  )
done

if [[ $found -eq 0 ]]; then
  echo "(no active peers on published ports right now)"
fi
echo
