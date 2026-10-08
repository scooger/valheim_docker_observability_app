# Valheim server

Docker Compose stack for a Valheim dedicated server on a remote Linux host running Docker, using [`lloesche/valheim-server`](https://hub.docker.com/r/lloesche/valheim-server). World files, backups, and config live in `state/` and survive container rebuilds.

## 1. Prepare the Docker host

Any Debian/Ubuntu machine with Docker is fine (bare metal or a VM elsewhere). Valheim wants a few fast cores and enough RAM to stay off swap — about **4 CPU** and **8 GB RAM** is a solid baseline.

Install `sudo` and `openssh-server` if needed, then confirm the host has a LAN address:

```bash
ip -4 addr
```

Copy this folder to the host (from your PC):

```bash
scp -r valheim_server you@YOUR_LAN_IP:/opt/valheim-server
```

On the host:

```bash
cd /opt/valheim-server
sudo bash scripts/setup.sh
```

The script installs Docker Engine if needed, creates a `valheim` user, writes `.env` with a random password, and starts the server. The first start downloads about 1 GB from Steam. Watch it until the log settles:

```bash
sudo docker compose logs -f
```

You want a line that the game server connected. Ctrl-C leaves the container running.

Edit `SERVER_NAME` and `WORLD_NAME` in `.env` before anyone joins, then apply with:

```bash
sudo docker compose up -d
```

`SERVER_PASS` must be at least 5 characters. Valheim will not boot with a shorter password.

### Import a world you already play

On the PC that has the world, copy the `.db` and `.fwl` files from:

`%USERPROFILE%\AppData\LocalLow\IronGate\Valheim\worlds_local`

into `state/config/worlds_local/` on the host. Set `WORLD_NAME` to that filename without the extension, then `sudo docker compose up -d`.

### Docker already installed

Skip the installer path in `setup.sh` if Docker is present — the script detects it and continues. Copy the folder over and run `sudo bash scripts/setup.sh`.

## 2. UniFi Cloud Gateway Ultra

Do this after the host has a stable address. The forward target has to stay on that address.

### Confirm the WAN is a public address

In UniFi Network, open **Settings → Internet** and select the WAN. The address there needs to be a real public IPv4, the same one a site like `https://ifconfig.me` shows from a PC on your LAN.

Port forwards do nothing when:

- The WAN address is `100.64.x.x` (carrier-grade NAT), or `10.x`, `192.168.x`, or `172.16–31.x` (the ISP modem is still routing).
- A modem/router sits in front of the Gateway Ultra and is not in bridge mode.

If the modem is still routing, put it in bridge mode so the Gateway Ultra holds the public IP, or forward the same UDP ports on the modem to the Gateway Ultra's WAN address. Two layers of NAT both have to forward.

If the ISP will not give you a public IPv4, set `CROSSPLAY=true` in `.env` and recreate the container. With crossplay the image uses PlayFab and does not need an inbound forward. Players join from the in-game crossplay browser. Leave it `false` when everyone is on Steam and the WAN is public.

Leave UPnP off. **Settings → Internet → UPnP** should stay disabled. These forwards are explicit.

### Host address

Give the Docker host a **static** LAN address (`YOUR_LAN_IP/24`, gateway `YOUR_GATEWAY_IP`), or a UniFi **Fixed IP** reservation so the forward target cannot drift.

### Forward the ports

| Name | Protocol | WAN port | Forward IP | Forward port | Required |
| --- | --- | --- | --- | --- | --- |
| Valheim game | UDP | 2456 | host fixed IP | 2456 | Yes |
| Valheim query | UDP | 2457 | host fixed IP | 2457 | Yes |
| Valheim crossplay | UDP | 2458 | host fixed IP | 2458 | Yes |
| Usage dashboard | TCP | 8088 | host fixed IP | 8088 | Only if `USAGE_PUBLIC_ACCESS=true` |

2456 is the port players type. 2457 is the Steam query port (public server list and Steam favorites). 2458 is used when `CROSSPLAY=true`, and by some mods. Forward all three UDP ports so turning crossplay on later does not need another firewall change.

**8088** is the usage / monitoring web UI. Only forward it when you want friends on the internet to open the dashboard. Leave it unforwarded for LAN-only monitoring. See [Usage web](#usage-web) for WAN vs LAN access rules.

Where to click depends on the Network application version on the Gateway Ultra:

- **Network 9.4 and later:** **Settings → Policy Engine → Port Forwarding → Create**
- **Network 9.3 / 10 if Port Forwarding is not its own page:** **Settings → Policy Engine → Policy Table → Create New Policy → Port Forwarding**
- **Older:** **Settings → Routing → Port Forwarding**

For each rule:

- **WAN:** your primary WAN (or All, if you only have one)
- **From:** Any
- **Protocol:** UDP for 2456–2458, **TCP** for 8088 (not Both)
- **Forward IP:** `YOUR_LAN_IP`
- **Forward port:** the same number as the WAN port

Saving a forward creates a matching firewall allow (External → the zone the host lives in, often Internal). Leave that allow in place. You do not add a second firewall rule for the same ports.

Do **not** forward TCP **9443** (Portainer) — keep that LAN-only.

Optional: if every friend has a stable public IP, set **From** to those addresses instead of Any. That is tighter, and it breaks the moment a friend's ISP changes their address. Any plus the server password is the usual home setup.

### Play from inside the house

People on your LAN join with `YOUR_LAN_IP:2456`. Joining your own public IP from inside the LAN (NAT hairpin) is unreliable. Outside players use the public WAN address and port 2456.

### Optional: put the host on its own VLAN

Not required. If you want it off the main LAN:

1. **Settings → Networks → New Virtual Network.** VLAN ID `30`, subnet such as `192.168.30.0/24`, DHCP on. Leave the zone as Internal if your PC should still SSH to it. A more isolated zone means you also need a LAN → that zone allow for SSH.
2. Put the Docker host on that network (switch port / NIC VLAN as appropriate for your hardware).
3. Reserve the fixed IP on that network and point the three forwards at it.

## 3. Join

In Valheim: **Join Game → Join IP**.

- Same house: `YOUR_LAN_IP:2456`
- From the internet: your public WAN address and port `2456`
- Password: `SERVER_PASS` in `.env` on the host

With `SERVER_PUBLIC=false` (the default here) the server is not listed in Steam or the community browser. Friends join only by IP and password.

Set `SERVER_PUBLIC=true` if you want it on the public list under `SERVER_NAME`. That list is slow and often misses the server anyway.

## Persistence

World data survives container and host reboots. It lives on the host disk, not inside the image:

| Host path | Container path | Contents |
| --- | --- | --- |
| `state/config/worlds_local/` | `/config/worlds_local` | World saves (`.db` / `.fwl`) |
| `state/config/backups/` | `/config/backups` | Hourly zipped world backups |
| `state/data/` | `/opt/valheim` | Downloaded dedicated server (~4 GB) |

Player characters are stored on each player's PC, not on this server. Only the shared world is server-side.

`restart: unless-stopped` and a 2 minute stop grace period bring the container back after reboot and give Valheim time to save on shutdown.

## OS updates

Weekly full OS updates run via cron on the host:

- **When:** Sundays **03:00** America/New_York
- **Enable:** `sudo bash scripts/enable-weekly-os-updates.sh`
- **Log:** `/var/log/valheim-weekly-os-update.log`

Packages always install at 03:00. A reboot runs only if the kernel/libc requires it **and** Valheim reports idle (no active player sessions via the image’s `valheim-is-idle` check). If people are online, reboot is deferred and retried every 30 minutes until 11:30. Docker brings Valheim back after reboot.

## Portainer

Portainer CE is in the same Compose file. UI: **https://YOUR_LAN_IP:9443**

On first visit, create the admin user within 5 minutes (Portainer locks the wizard after that). Data lives in `state/portainer/`. Do **not** forward TCP 9443 on UniFi — keep it LAN-only. The socket mount gives Portainer full control of Docker on this host.

## Usage web

Separate image (`usage-web/`) serves sessions from `player-events.log`, NPS RTT from `NpsMonitoring/`, plus host/Valheim CPU and memory:

- LAN UI: **http://YOUR_LAN_IP:8088/**
- WAN UI (if forwarded): **http://YOUR_PUBLIC_IP:8088/**
- JSON: `/api/status` · session detail: `/api/session/<id>`

Sessions under 2 minutes are flagged **short**. Latency appears once a client is online with NPS monitoring enabled (host self-RTT is filtered out). Resource samples live in `state/usage/metrics.jsonl`.

### LAN vs WAN access

The app classifies clients by **TCP source IP** (RFC1918 / loopback = LAN; anything else = WAN).

| Client | Behavior |
| --- | --- |
| LAN (`YOUR_LAN_IP`, home network) | Always open — **no login** |
| WAN, `USAGE_PUBLIC_ACCESS=false` | Always **404** (feature off) |
| WAN, `USAGE_PUBLIC_ACCESS=true`, nobody in Valheim | **404** — no password prompt (looks offline) |
| WAN, public on, player online, `USAGE_AUTH_MODE=server_pass` | Browser login — password = `SERVER_PASS` |
| WAN, public on, player online, `USAGE_AUTH_MODE=override` | Browser login — password = `USAGE_PASS` |
| WAN, public on, player online, `USAGE_AUTH_MODE=off` | Open — **no login** |
| WAN, wrong password | **401** |

This is presence gating (plus optional password), not per-player IP binding. While someone is in-game, anyone who can reach `:8088` and satisfy the auth mode can open the dashboard.

### Enable or disable public access

In `.env` on the host:

```bash
# false = WAN always closed (safe if you do not forward 8088)
# true  = allow internet access with the rules above
USAGE_PUBLIC_ACCESS=false

# Password for WAN when a player is online:
#   server_pass = Valheim SERVER_PASS (default)
#   override    = custom password in USAGE_PASS
#   off         = no password
USAGE_AUTH_MODE=server_pass
USAGE_PASS=
```

Then recreate the usage container:

```bash
cd /opt/valheim-server
sudo docker compose up -d usage
```

When set to `true`, also add the UniFi **TCP 8088** forward from the table above. When `false`, leave 8088 unforwarded.

```bash
sudo docker compose build usage && sudo docker compose up -d usage
```

## Login status

Interactive SSH logins source `/etc/profile.d/docker-login-status.sh`, which prints running containers and any active peers on their published ports. Non-interactive commands (`ssh host 'cmd'`) skip it.

## Live dashboard

Terminal dashboard (Docker + Valheim sessions), refreshed every 2 seconds:

```bash
ssh docker
bash /opt/valheim-server/scripts/valheim-dashboard.sh
```

It parses Valheim logs for:

| Log line | What you get |
| --- | --- |
| `Got connection SteamID …` | Steam account connecting |
| `Got character ZDOID from Name` | In-world character name |
| `Closing socket` / `ClosedByPeer` | Disconnect |
| `Connections N` | Server’s own connection count |

Valheim does **not** log client IPs (Steam networking). The dashboard can show UDP peer IPs via host `conntrack` when available. Join/leave lines are also appended to `state/config/player-events.log` by container log hooks.

## Day to day

From `/opt/valheim-server` on the host:

```bash
sudo docker compose logs -f          # live log
sudo docker compose restart          # restart, world saves on the way down
sudo docker compose pull && sudo docker compose up -d   # newer container image
sudo docker compose exec valheim supervisorctl signal HUP valheim-backup
```

Backups land in `state/config/backups/` (hourly, 7 days, at most 48 files). Worlds are in `state/config/worlds_local/`.

The container checks for a Valheim update every 30 minutes and only restarts for it when nobody is connected. It also restarts at 05:10 in `TZ` when the server is empty. Change those crons in `.env`, then `sudo docker compose up -d`.

To make someone an admin, set `ADMINLIST_IDS` in `.env` to their SteamID64 (space-separated if several). That replaces `adminlist.txt`. Recreate the container after editing.

## Ports

| Port | Protocol | Why |
| --- | --- | --- |
| 2456 | UDP | Game |
| 2457 | UDP | Steam matchmaking / query |
| 2458 | UDP | Crossplay (PlayFab) and some mod RPC |
| 8088 | TCP | Usage web — forward only when `USAGE_PUBLIC_ACCESS=true` |
| 9443 | TCP | Portainer (LAN only — do not forward) |

Do not forward TCP for the Valheim game ports (2456–2458).
