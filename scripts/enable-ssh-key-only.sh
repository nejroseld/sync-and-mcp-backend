#!/usr/bin/env bash
# Run by sudo on Ubuntu, after a successful key-only SSH login from the host.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo 'Run as root' >&2; exit 1; }
ROLLBACK=/root/obsi-mcp-ssh-rollback.sh
BACKUP=/root/obsi-mcp-ssh-rollback.bak
if [[ ${1:-} == --confirm ]]; then
  systemctl stop obsi-mcp-ssh-rollback.timer
  rm -f "$ROLLBACK" "$BACKUP"
  echo 'Key-only SSH login confirmed; rollback cancelled.'
  exit 0
fi
[[ $# -eq 2 && $1 =~ ^[a-z_][a-z0-9_-]*$ && $2 =~ ^[0-9a-fA-F:.]+$ ]] || { echo 'Expected SSH username and client IP' >&2; exit 1; }
SSH_USER=$1
CLIENT_IP=$2
CONFIG=/etc/ssh/sshd_config.d/00-obsi-mcp-key-only.conf
SSHD=/usr/sbin/sshd
[[ ! -e $ROLLBACK && ! -e $BACKUP ]] || { echo 'A previous SSH change is still awaiting confirmation or rollback' >&2; exit 1; }
HAD_CONFIG=0
if [[ -e $CONFIG ]]; then
  cp -p "$CONFIG" "$BACKUP"
  HAD_CONFIG=1
fi

restore() {
  if (( HAD_CONFIG )); then cp -p "$BACKUP" "$CONFIG"; else rm -f "$CONFIG"; fi
  "$SSHD" -t && systemctl reload ssh.service || true
  rm -f "$BACKUP" "$ROLLBACK"
}

install -d -m 0755 /etc/ssh/sshd_config.d
cat > "$CONFIG" <<'EOF'
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF
chmod 0644 "$CONFIG"

if ! "$SSHD" -t; then
  echo 'sshd config check failed; restoring previous config' >&2
  restore
  exit 1
fi

EFFECTIVE=$("$SSHD" -T -C "user=$SSH_USER,host=localhost,addr=$CLIENT_IP") || { restore; exit 1; }
for expected in 'pubkeyauthentication yes' 'passwordauthentication no' 'kbdinteractiveauthentication no'; do
  if ! grep -qx "$expected" <<< "$EFFECTIVE"; then
    echo "Effective sshd config does not contain: $expected; restoring" >&2
    restore
    exit 1
  fi
done

if ! systemctl reload ssh.service; then
  echo 'Could not reload SSH; restoring previous config' >&2
  restore
  exit 1
fi
cat > "$ROLLBACK" <<EOF
#!/usr/bin/env bash
set -e
if (( $HAD_CONFIG )); then cp -p '$BACKUP' '$CONFIG'; else rm -f '$CONFIG'; fi
'$SSHD' -t
systemctl reload ssh.service
rm -f '$BACKUP' '$ROLLBACK'
EOF
chmod 0700 "$ROLLBACK"
if ! systemd-run --quiet --unit=obsi-mcp-ssh-rollback --on-active=2min /bin/bash "$ROLLBACK"; then
  echo 'Could not schedule SSH rollback; restoring previous config' >&2
  restore
  exit 1
fi
echo 'Password login disabled. The change will roll back in two minutes unless key login is confirmed.'
