#!/usr/bin/env bash
set -euo pipefail
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
PLUGIN_DIR=/opt/valheim-server/state/config/bepinex/plugins
mkdir -p "$PLUGIN_DIR"

install_pkg() {
  local ns="$1" name="$2" ver="$3"
  local url="https://thunderstore.io/package/download/${ns}/${name}/${ver}/"
  local zip="$TMP/${ns}-${name}-${ver}.zip"
  local dest="$TMP/ex-$name"
  echo "Downloading $ns $name $ver..."
  curl -fsSL -A 'Mozilla/5.0 ValheimSetup' -o "$zip" "$url"
  rm -rf "$dest"
  mkdir -p "$dest"
  python3 - "$zip" "$dest" "$PLUGIN_DIR" "$name" <<'PY'
import sys, zipfile, shutil
from pathlib import Path
zf, dest, plugins, name = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
with zipfile.ZipFile(zf) as z:
    for info in z.infolist():
        rel = info.filename.replace("\\", "/")
        if not rel or rel.endswith("/"):
            if rel:
                (dest / rel).mkdir(parents=True, exist_ok=True)
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with z.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)

def copytree(src: Path, dst: Path):
    if dst.exists():
        shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)

for cand in [dest / "plugins", dest / "BepInEx" / "plugins"]:
    if cand.is_dir():
        for item in cand.iterdir():
            copytree(item, plugins / item.name)
        print("installed from", cand)
        break
else:
    pkg = plugins / name
    skip = {"manifest.json", "README.md", "icon.png", "CHANGELOG.md", "LICENSE"}
    if pkg.exists():
        shutil.rmtree(pkg)
    pkg.mkdir(parents=True)
    for item in dest.iterdir():
        if item.name in skip:
            continue
        copytree(item, pkg / item.name)
    print("installed package tree into", pkg)
PY
}

install_pkg ValheimModding Jotunn 2.30.2
install_pkg MidnightMods NetworkPerformanceSystem 1.15.1

puid=$(sed -n 's/^PUID=//p' /opt/valheim-server/.env | head -n1)
pgid=$(sed -n 's/^PGID=//p' /opt/valheim-server/.env | head -n1)
chown -R "${puid}:${pgid}" /opt/valheim-server/state/config/bepinex
echo "Installed files:"
find "$PLUGIN_DIR" -type f | sed 's/^/  /'
cd /opt/valheim-server
docker compose restart valheim
echo "Waiting for boot..."
sleep 60
docker compose logs --tail 300 valheim 2>&1 | grep -iE 'bepinex|jotunn|networkperformance|nps|Loaded plugin|Exception|Error loading' | tail -60 || true
echo "--- NPS config candidates ---"
find /opt/valheim-server/state/config/bepinex -iname '*Network*' -o -iname '*NPS*' 2>/dev/null | sed 's/^/  /' || true
