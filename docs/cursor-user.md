# Cursor sudo user on a Docker host

Steps to create a `cursor` user with SSH key login and passwordless sudo on a Linux host. Use this when standing up a machine that Cursor will manage over SSH.

**Target host for this setup:** `192.168.10.12` (new Docker server)

## Prerequisites

- Ubuntu or Debian on the host
- OpenSSH server installed and listening on port 22
- An existing admin account that can `sudo` (created during OS install)
- On your Windows PC, the Ed25519 key already generated for this lab:

  - Private: `C:\Users\travi\.ssh\valheim_ed25519`
  - Public: `C:\Users\travi\.ssh\valheim_ed25519.pub`

If the key is missing, generate it on the PC:

```powershell
ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\valheim_ed25519 -C "valheim@$(hostname)" -N '""'
Get-Content $env:USERPROFILE\.ssh\valheim_ed25519.pub
```

## 1. Confirm you can reach the host

From the PC (replace `YOUR_ADMIN` with the account you created on the Docker server):

```powershell
ssh YOUR_ADMIN@192.168.10.12
```

Log in with that user’s password once so the host key is accepted.

## 2. Prepare the bootstrap script (under `.scratch/`)

From the repo on the PC:

```powershell
cd c:\Users\travi\Development\valheim_server
copy scripts\bootstrap-cursor-user.example.sh .scratch\bootstrap-cursor-user.sh
```

Edit `.scratch/bootstrap-cursor-user.sh` and set `PUBKEY` to the full line from `valheim_ed25519.pub`:

```powershell
Get-Content $env:USERPROFILE\.ssh\valheim_ed25519.pub
```

The whole `.scratch/` folder is gitignored; only `scripts/bootstrap-cursor-user.example.sh` is tracked.

## 3. Copy the bootstrap script to the host

From the PC, in PowerShell:

```powershell
scp -i $env:USERPROFILE\.ssh\valheim_ed25519 `
  c:\Users\travi\Development\valheim_server\.scratch\bootstrap-cursor-user.sh `
  YOUR_ADMIN@192.168.10.12:~/bootstrap-cursor-user.sh
```

If key login for `YOUR_ADMIN` is not set up yet, use password auth:

```powershell
scp c:\Users\travi\Development\valheim_server\.scratch\bootstrap-cursor-user.sh YOUR_ADMIN@192.168.10.12:~/
```

Or paste the script onto the host with an editor / the Proxmox or physical console.

## 4. Run the bootstrap as root

On the host (SSH session as `YOUR_ADMIN`):

```bash
chmod +x ~/bootstrap-cursor-user.sh
sudo bash ~/bootstrap-cursor-user.sh
```

Enter your admin password when `sudo` asks.

What the script does:

1. Creates user `cursor` with no password login
2. Adds `cursor` to the `sudo` group
3. Installs the workstation public key in `/home/cursor/.ssh/authorized_keys`
4. Grants passwordless sudo via `/etc/sudoers.d/cursor`
5. Validates the sudoers file with `visudo -cf`

Expected output ends with something like:

```text
cursor user ready
uid=1001(cursor) gid=1001(cursor) groups=1001(cursor),27(sudo)
```

## 5. Add an SSH config alias on the PC

Append (or create) `C:\Users\travi\.ssh\config`:

```sshconfig
Host docker
  HostName 192.168.10.12
  User cursor
  IdentityFile ~/.ssh/valheim_ed25519
  IdentitiesOnly yes
```

Keep the existing `valheim` host entry for `192.168.10.30` if you still use that guest.

## 6. Verify

From the PC:

```powershell
ssh docker "hostname; whoami; id; sudo -n true && echo SUDO_OK"
```

You should see user `cursor`, and `SUDO_OK` with no password prompt.

Optional: allow `cursor` to run Docker without root after Docker is installed:

```bash
sudo usermod -aG docker cursor
```

Log out and back in (or start a new SSH session) for the group to apply.

## 7. Cleanup

On the host, you can remove the bootstrap copy:

```bash
rm ~/bootstrap-cursor-user.sh
```

Keep your personal admin account; use `cursor` for Cursor automation.

## Security notes

- `cursor` has **passwordless full sudo**. Anyone with the matching private key can control the host.
- Protect `C:\Users\travi\.ssh\valheim_ed25519` (do not commit it; do not copy it into the repo).
- Prefer key-only SSH for `cursor` (the account is created with `--disabled-password`).
- To rotate access: replace the line in `/home/cursor/.ssh/authorized_keys`, or remove `/etc/sudoers.d/cursor` and the user.

Use the same public key you already trust for the Valheim guest (`192.168.10.30`), from `valheim_ed25519.pub` on the PC.
