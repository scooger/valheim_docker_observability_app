# Valheim invite

Copy to `invite.md` (gitignored), fill in real values, then paste below the line into Discord (or wherever).

---

Valheim server is up — private, join by IP.

**In-game:** Join Game → Join IP  
**Address:** `YOUR_PUBLIC_IP:2456`  
**Password:** `YOUR_SERVER_PASS`

If you’re already on my home network, use `YOUR_LAN_IP:2456` instead.

**Usage dashboard** (sessions / latency / server load):  
**URL:** `http://YOUR_PUBLIC_IP:8088/`  
From the internet: only while someone is on the server. Empty server → looks offline. Auth depends on `USAGE_AUTH_MODE` (`server_pass` = same password as above, `override` = `USAGE_PASS`, `off` = no login). Requires `USAGE_PUBLIC_ACCESS=true` and UniFi TCP 8088 forward.

On the home network: `http://YOUR_LAN_IP:8088/` (no login, always available).

Steam only (not crossplay). Ping me if it doesn’t connect.
