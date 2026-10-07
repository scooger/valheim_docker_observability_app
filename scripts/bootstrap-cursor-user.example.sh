#!/usr/bin/env bash
# Create the cursor automation user with SSH key login and passwordless sudo.
# Copy to .scratch/ (gitignored), paste your public key, then on the host:
#   sudo bash bootstrap-cursor-user.sh
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with: sudo bash $0" >&2
  exit 1
fi

# Paste the full line from your workstation *.pub file (one line).
PUBKEY='ssh-ed25519 AAAA...REPLACE_ME comment'

if [[ "$PUBKEY" == *REPLACE_ME* ]]; then
  echo "Edit PUBKEY in this script before running." >&2
  exit 1
fi

if ! id cursor >/dev/null 2>&1; then
  adduser --disabled-password --gecos 'Cursor agent' cursor
fi

usermod -aG sudo cursor
install -d -m 700 -o cursor -g cursor /home/cursor/.ssh
printf '%s\n' "$PUBKEY" > /home/cursor/.ssh/authorized_keys
chown cursor:cursor /home/cursor/.ssh/authorized_keys
chmod 600 /home/cursor/.ssh/authorized_keys

cat >/etc/sudoers.d/cursor <<'EOF'
cursor ALL=(ALL) NOPASSWD:ALL
EOF
chmod 440 /etc/sudoers.d/cursor
visudo -cf /etc/sudoers.d/cursor

echo "cursor user ready"
id cursor
