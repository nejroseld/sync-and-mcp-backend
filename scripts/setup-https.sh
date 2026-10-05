#!/usr/bin/env bash
# Run by root on Ubuntu: publish obsi-mcp at https://DOMAIN through Caddy (automatic Let's Encrypt).
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo 'Run as root' >&2; exit 1; }
[[ $# -eq 2 ]] || { echo 'Usage: setup-https.sh DOMAIN PORT' >&2; exit 1; }
DOMAIN=$1
PORT=$2
[[ $DOMAIN =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || { echo 'Invalid domain' >&2; exit 1; }
if [[ ! $PORT =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then echo 'Invalid port' >&2; exit 1; fi
CADDYFILE=/etc/caddy/Caddyfile
MARKER='# managed by obsi-mcp'
export DEBIAN_FRONTEND=noninteractive

if ! command -v caddy >/dev/null; then
  echo 'Installing Caddy'
  apt-get update
  if [[ $(apt-cache policy caddy | awk '/Candidate:/ {print $2}') =~ ^[0-9] ]]; then
    apt-get install -y caddy
  else
    apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
      | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
      > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update
    apt-get install -y caddy
  fi
fi

# Overwrite only the package's sample config or a file this script wrote earlier.
if [[ -s $CADDYFILE ]] && ! grep -qF "$MARKER" "$CADDYFILE" && ! grep -q '^[[:space:]]*root \* /usr/share/caddy' "$CADDYFILE"; then
  echo "$CADDYFILE already has a custom config; add this site to it manually:" >&2
  printf '%s {\n\treverse_proxy 127.0.0.1:%s\n}\n' "$DOMAIN" "$PORT" >&2
  exit 1
fi
install -d -m 0755 /etc/caddy
tmp=$(mktemp "$CADDYFILE.XXXXXXXX")
printf '%s\n%s {\n\treverse_proxy 127.0.0.1:%s\n}\n' "$MARKER" "$DOMAIN" "$PORT" > "$tmp"
chmod 0644 "$tmp"
caddy validate --adapter caddyfile --config "$tmp" >/dev/null || { rm -f "$tmp"; echo 'Caddy config check failed' >&2; exit 1; }
mv -f -- "$tmp" "$CADDYFILE"

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q '^Status: active'; then
  echo 'Opening ports 80 and 443 in ufw'
  ufw allow 80/tcp
  ufw allow 443/tcp
fi

systemctl enable caddy.service
systemctl restart caddy.service
echo 'Waiting for the HTTPS certificate'
for _ in $(seq 1 45); do
  if curl -fsS --max-time 5 "https://$DOMAIN/api/v1/health" >/dev/null 2>&1; then
    echo "HTTPS is ready: https://$DOMAIN"
    exit 0
  fi
  sleep 2
done
echo 'Caddy is running, but the HTTPS check did not pass yet.' >&2
echo 'Check that the domain points to this server and ports 80/443 are open in the provider firewall.' >&2
echo 'Logs: sudo journalctl -u caddy -n 50' >&2
exit 2
