# Valheim server

Docker Compose stack for a Valheim dedicated server on a Proxmox guest, using [`lloesche/valheim-server`](https://hub.docker.com/r/lloesche/valheim-server). World files, backups, and config live in `state/` and survive container rebuilds.

Run the game server in a VM (or an existing Docker guest). Leave Docker off the Proxmox host itself.

## 1. Create the Proxmox guest

A small VM is the reliable option. Valheim wants a few fast cores and enough RAM to stay off swap.

| Setting | Value |
| --- | --- |
| OS | Debian 12 or Ubuntu 24.04 Server |
| Machine | q35 |
| BIOS | SeaBIOS (OVMF also works) |
| Disk | 32 GB VirtIO SCSI, Discard enabled |
| CPU | `host`, 4 cores |
| Memory | 8192 MB, ballooning device **off** |
| Network | VirtIO on `vmbr0` (the bridge that faces your UniFi LAN) |
| QEMU Guest Agent | Enabled |
| Firewall | Off on the VM (the Gateway Ultra is the firewall) |

Install the OS, then install `sudo` and `openssh-server` if the installer did not. Confirm the guest got an address from UniFi:

```bash
ip -4 addr
```

Copy this folder to the guest (from your PC):

```bash
scp -r valheim_server you@GUEST_IP:/opt/valheim-server
```

On the guest:

```bash
cd /opt/valheim-server
sudo bash scripts/setup.sh
```

The script installs Docker Engine, creates a `valheim` user, writes `.env` with a random password, and starts the server. The first start downloads about 1 GB from Steam. Watch it until the log settles:

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

into `state/config/worlds_local/` on the guest. Set `WORLD_NAME` to that filename without the extension, then `sudo docker compose up -d`.

### Already running Docker on a guest

Skip the VM section. Copy the folder over and run `sudo bash scripts/setup.sh`.

An LXC only works if it is privileged and has `nesting=1` (and usually `keyctl=1`). An unprivileged container will fail when Docker starts. A VM avoids that.

## 2. UniFi Cloud Gateway Ultra

Do this after the guest has an address. The forward target has to stay on that address.

### Confirm the WAN is a public address

In UniFi Network, open **Settings → Internet** and select the WAN. The address there needs to be a real public IPv4, the same one a site like `https://ifconfig.me` shows from a PC on your LAN.

Port forwards do nothing when:

- The WAN address is `100.64.x.x` (carrier-grade NAT), or `10.x`, `192.168.x`, or `172.16–31.x` (the ISP modem is still routing).
- A modem/router sits in front of the Gateway Ultra and is not in bridge mode.

If the modem is still routing, put it in bridge mode so the Gateway Ultra holds the public IP, or forward the same UDP ports on the modem to the Gateway Ultra's WAN address. Two layers of NAT both have to forward.

If the ISP will not give you a public IPv4, set `CROSSPLAY=true` in `.env` and recreate the container. With crossplay the image uses PlayFab and does not need an inbound forward. Players join from the in-game crossplay browser. Leave it `false` when everyone is on Steam and the WAN is public.

Leave UPnP off. **Settings → Internet → UPnP** should stay disabled. These forwards are explicit.

### Guest address

This host uses a **static** address (`YOUR_LAN_IP/24`), gateway `YOUR_GATEWAY_IP`. You do **not** need a UniFi DHCP reservation.

If you ever rebuild with DHCP instead, set a **Fixed IP** in UniFi (**Client Devices** → the VM) so the forward target cannot drift.

### Forward the game ports

Valheim listens on UDP only.

| Name | Protocol | WAN port | Forward IP | Forward port |
| --- | --- | --- | --- | --- |
| Valheim game | UDP | 2456 | guest fixed IP | 2456 |
| Valheim query | UDP | 2457 | guest fixed IP | 2457 |
| Valheim crossplay | UDP | 2458 | guest fixed IP | 2458 |

2456 is the port players type. 2457 is the Steam query port (public server list and Steam favorites). 2458 is used when `CROSSPLAY=true`, and by some mods. Forward all three so turning crossplay on later does not need another firewall change.

Where to click depends on the Network application version on the Gateway Ultra:

- **Network 9.4 and later:** **Settings → Policy Engine → Port Forwarding → Create**
- **Network 9.3 / 10 if Port Forwarding is not its own page:** **Settings → Policy Engine → Policy Table → Create New Policy → Port Forwarding**
- **Older:** **Settings → Routing → Port Forwarding**

For each rule:

- **WAN:** your primary WAN (or All, if you only have one)
- **From:** Any
- **Protocol:** UDP (not Both)
- **Forward IP:** `YOUR_LAN_IP`
- **Forward port:** the same number as the WAN port

Saving a forward creates a matching firewall allow (External → the zone the guest lives in, often Internal). Leave that allow in place. You do not add a second firewall rule for the same ports.

Optional: if every friend has a stable public IP, set **From** to those addresses instead of Any. That is tighter, and it breaks the moment a friend's ISP changes their address. Any plus the server password is the usual home setup.

### Play from inside the house

People on your LAN join with `YOUR_LAN_IP:2456`. Joining your own public IP from inside the LAN (NAT hairpin) is unreliable. Outside players use the public WAN address and port 2456.

If you previously forwarded to the old VM (`YOUR_OLD_LAN_IP`), update those UniFi rules to `YOUR_LAN_IP`.

### Optional: put the guest on its own VLAN

Not required. If you want it off the main LAN:

1. **Settings → Networks → New Virtual Network.** VLAN ID `30`, subnet such as `192.168.30.0/24`, DHCP on. Leave the zone as Internal if your PC should still SSH to it. A more isolated zone means you also need a LAN → that zone allow for SSH.
2. On the Proxmox VM NIC, set VLAN tag `30`. `vmbr0` must be VLAN-aware, or the tag never leaves the host.
3. Reserve the fixed IP on that network and point the three forwards at it.

## 3. Join

In Valheim: **Join Game → Join IP**.

- Same house: `YOUR_LAN_IP:2456`
- From the internet: your public WAN address and port `2456`
- Password: `SERVER_PASS` in `.env` on the guest

With `SERVER_PUBLIC=false` (the default here) the server is not listed in Steam or the community browser. Friends join only by IP and password.

Set `SERVER_PUBLIC=true` if you want it on the public list under `SERVER_NAME`. That list is slow and often misses the server anyway.

## Persistence

World data survives container and VM reboots. It lives on the guest disk, not inside the image:

| Host path | Container path | Contents |
| --- | --- | --- |
| `state/config/worlds_local/` | `/config/worlds_local` | World saves (`.db` / `.fwl`) |
| `state/config/backups/` | `/config/backups` | Hourly zipped world backups |
| `state/data/` | `/opt/valheim` | Downloaded dedicated server (~4 GB) |

Player characters are stored on each player's PC, not on this server. Only the shared world is server-side.

`restart: unless-stopped` and a 2 minute stop grace period bring the container back after reboot and give Valheim time to save on shutdown.

## OS updates

Weekly full OS updates run via cron on the guest:

- **When:** Sundays **03:00** America/New_York
- **Enable:** `sudo bash scripts/enable-weekly-os-updates.sh`
- **Log:** `/var/log/valheim-weekly-os-update.log`

Packages always install at 03:00. A reboot runs only if the kernel/libc requires it **and** Valheim reports idle (no active player sessions via the image’s `valheim-is-idle` check). If people are online, reboot is deferred and retried every 30 minutes until 11:30. Docker brings Valheim back after reboot.

## Portainer

Portainer CE is in the same Compose file. UI: **https://YOUR_LAN_IP:9443**

On first visit, create the admin user within 5 minutes (Portainer locks the wizard after that). Data lives in `state/portainer/`. Do **not** forward TCP 9443 on UniFi — keep it LAN-only. The socket mount gives Portainer full control of Docker on this host.

## Usage web

Separate image (`usage-web/`) serves sessions from `player-events.log`, NPS RTT from `NpsMonitoring/`, plus host/Valheim CPU and memory:

- UI: **http://YOUR_LAN_IP:8088/** — summary min/avg/max for duration and latency; click a session for events + RTT chart
- JSON: **http://YOUR_LAN_IP:8088/api/status** · session detail: `/api/session/<id>`

Sessions under 2 minutes are flagged **short**. Latency appears once a client is online with NPS monitoring enabled (host self-RTT is filtered out). Resource samples live in `state/usage/metrics.jsonl`.

**Public access** (`PUBLIC_PASSWORD_GATE` + `PUBLIC_SESSION_GATE`): LAN clients always see the UI with no login. Internet is **default-deny** (plain `404`) while nobody is in Valheim — no password prompt. When at least one player is online, WAN gets HTTP Basic Auth (`SERVER_PASS`, any username). Wrong password → 401. Optional `ACCESS_TOKEN` is an alternate WAN unlock.

```bash
cd /opt/valheim-server
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

From `/opt/valheim-server` on the guest:

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

Nothing in this stack listens on TCP. Do not forward TCP for these ports.
