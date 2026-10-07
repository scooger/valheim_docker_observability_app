#!/usr/bin/env bash
# Enable BepInEx and install Jotunn + NetworkPerformanceSystem on the Valheim host.
set -euo pipefail

ROOT="${ROOT:-/opt/valheim-server}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "Missing $ROOT/.env" >&2
  exit 1
fi

# Resolve latest Thunderstore package versions + download URLs.
curl -fsSL -A 'Mozilla/5.0 ValheimSetup' \
  -o "$TMP/thunderstore-index.json" \
  'https://thunderstore.io/c/valheim/api/v1/package/'

python3 - "$TMP/thunderstore-index.json" >"$TMP/pkgs.txt" <<'PY'
import json, sys
pkgs = json.load(open(sys.argv[1]))
wanted = (("ValheimModding", "Jotunn"), ("MidnightMods", "NetworkPerformanceSystem"))
found = {}
for p in pkgs:
    key = (p.get("owner"), p.get("name"))
    if key in wanted and key not in found:
        ver = p["versions"][0]
        url = ver.get("download_url") or (
            f"https://thunderstore.io/package/download/{key[0]}/{key[1]}/{ver['version_number']}/"
        )
        found[key] = (ver["version_number"], url)
missing = [k for k in wanted if k not in found]
if missing:
    raise SystemExit(f"packages not found: {missing}")
for key in wanted:
    ver, url = found[key]
    print(f"{key[0]}|{key[1]}|{ver}|{url}")
PY

echo "Resolved packages:"
cat "$TMP/pkgs.txt"

# Enable BepInEx in .env
if grep -q '^BEPINEX=' .env; then
  sed -i 's/^BEPINEX=.*/BEPINEX=true/' .env
else
  printf '\nBEPINEX=true\n' >> .env
fi
if grep -q '^VALHEIM_PLUS=' .env; then
  sed -i 's/^VALHEIM_PLUS=.*/VALHEIM_PLUS=false/' .env
else
  printf 'VALHEIM_PLUS=false\n' >> .env
fi

echo "Recreating Valheim with BEPINEX=true (first boot installs BepInExPack)..."
docker compose up -d valheim

# Wait for bepinex config tree
for i in $(seq 1 60); do
  if [[ -d state/config/bepinex ]]; then
    break
  fi
  sleep 5
done

mkdir -p state/config/bepinex/plugins
PLUGIN_DIR="$ROOT/state/config/bepinex/plugins"

while IFS='|' read -r ns name ver url; do
  zip="$TMP/${ns}-${name}-${ver}.zip"
  echo "Downloading ${ns}-${name}-${ver}..."
  curl -fsSL -A 'Mozilla/5.0 ValheimSetup' -o "$zip" "$url"
  mkdir -p "$TMP/extract-$name"
  if command -v unzip >/dev/null 2>&1; then
    unzip -qo "$zip" -d "$TMP/extract-$name"
  else
    python3 - "$zip" "$TMP/extract-$name" <<'PY'
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PY
  fi
  # Thunderstore zips usually contain plugins/ or a flat DLL tree under BepInEx/plugins
  if [[ -d "$TMP/extract-$name/plugins" ]]; then
    cp -a "$TMP/extract-$name/plugins/." "$PLUGIN_DIR/"
  elif [[ -d "$TMP/extract-$name/BepInEx/plugins" ]]; then
    cp -a "$TMP/extract-$name/BepInEx/plugins/." "$PLUGIN_DIR/"
  else
    # Copy everything that looks like plugin content into a named folder
    dest="$PLUGIN_DIR/$name"
    mkdir -p "$dest"
    cp -a "$TMP/extract-$name/." "$dest/"
    # Remove thunderstore metadata if present
    rm -f "$dest/manifest.json" "$dest/README.md" "$dest/icon.png" "$dest/CHANGELOG.md" 2>/dev/null || true
  fi
  echo "Installed $name $ver"
done <"$TMP/pkgs.txt"

# Ownership for container PUID/PGID
puid="$(sed -n 's/^PUID=//p' .env | head -n1)"
pgid="$(sed -n 's/^PGID=//p' .env | head -n1)"
puid="${puid:-0}"
pgid="${pgid:-0}"
chown -R "${puid}:${pgid}" state/config/bepinex

echo "Restarting Valheim to load plugins..."
docker compose restart valheim
sleep 8

echo
echo "Plugins directory:"
find state/config/bepinex/plugins -maxdepth 3 -type f | sed 's/^/  /'
echo
echo "Recent BepInEx/NPS log lines:"
docker compose logs --tail 200 valheim 2>&1 | grep -iE 'bepinex|jotunn|networkperformance|nps|plugin' | tail -40 || true
echo
echo "Done. After first full boot, config appears under state/config/bepinex/config/"
echo "For disconnect debugging, set Enable Network Monitoring = true in MidnightsFX.NetworkPerformanceSystem.cfg"
