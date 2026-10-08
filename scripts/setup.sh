#!/usr/bin/env bash
# Install Docker (if needed) and start the Valheim server on a Debian/Ubuntu Docker host.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash scripts/setup.sh" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This script supports Debian and Ubuntu. Install Docker, then run: docker compose up -d" >&2
  exit 1
fi

apt-get update
apt-get install -y ca-certificates curl openssl

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Installing Docker Engine..."
  curl -fsSL https://get.docker.com | sh
fi

systemctl enable --now docker

if ! id valheim >/dev/null 2>&1; then
  useradd \
    --system \
    --user-group \
    --create-home \
    --home-dir /var/lib/valheim \
    --shell /usr/sbin/nologin \
    valheim
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  password="$(openssl rand -hex 12)"
  sed -i "s|^SERVER_PASS=.*|SERVER_PASS=${password}|" .env
  sed -i "s|^PUID=.*|PUID=$(id -u valheim)|" .env
  sed -i "s|^PGID=.*|PGID=$(id -g valheim)|" .env
  echo
  echo "Created .env"
  echo "  SERVER_PASS=${password}"
  echo "Edit SERVER_NAME and WORLD_NAME in .env if you want different names, then re-run this script."
  echo
else
  echo "Using existing .env"
fi

password="$(sed -n 's/^SERVER_PASS=//p' .env | head -n1 | tr -d '"' | tr -d "'")"
if [[ "${#password}" -lt 5 || "$password" == "change-me" ]]; then
  echo "Set SERVER_PASS in .env to at least 5 characters (not change-me), then re-run." >&2
  exit 1
fi

puid="$(sed -n 's/^PUID=//p' .env | head -n1 | tr -d '"')"
pgid="$(sed -n 's/^PGID=//p' .env | head -n1 | tr -d '"')"
puid="${puid:-$(id -u valheim)}"
pgid="${pgid:-$(id -g valheim)}"

mkdir -p state/config/worlds_local state/config/backups state/data
chown -R "${puid}:${pgid}" state

docker compose pull
docker compose up -d

lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"

echo
echo "Valheim is starting. The first launch downloads the dedicated server from Steam and can take several minutes."
echo "Follow progress with:  docker compose logs -f"
echo
echo "LAN address: ${lan_ip:-unknown}:2456"
echo "Password is SERVER_PASS in ${ROOT}/.env"
echo
echo "Next: on the UniFi Gateway Ultra, reserve ${lan_ip:-the host IP} and forward UDP 2456-2458 to it."
echo "Steps are in README.md."
