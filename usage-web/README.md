# Valheim usage web

Small read-only web UI + JSON API for current and historical Valheim sessions.

It reads `player-events.log` (written by the Valheim container log hooks). No Docker socket access.

- UI: `http://192.168.10.12:8088/`
- JSON: `http://192.168.10.12:8088/api/status`
- Health: `http://192.168.10.12:8088/healthz`

Keep port **8088** LAN-only. Do not forward it on UniFi.
