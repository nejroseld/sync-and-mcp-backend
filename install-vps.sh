#!/usr/bin/env bash
# Install obsi-mcp from this checkout as a systemd service on Ubuntu.
set -euo pipefail
set +x

APP_USER=obsi-mcp
APP_DIR=/opt/obsi-mcp
DATA_DIR=/var/lib/obsi-mcp
SERVICE_FILE=/etc/systemd/system/obsi-mcp.service
PORT="${OBSI_PORT:-8765}"
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die 'Run with sudo: sudo ./install-vps.sh'
[[ -f $SOURCE_DIR/pyproject.toml && -d $SOURCE_DIR/obsi_server ]] || die 'Run from the server repository checkout'
[[ -f /etc/os-release ]] || die 'Ubuntu is required'
# shellcheck disable=SC1091
. /etc/os-release
[[ ${ID:-} == ubuntu ]] || die 'This installer supports Ubuntu only'
if [[ ! $PORT =~ ^[0-9]+$ ]] || (( 10#$PORT < 1 || 10#$PORT > 65535 )); then
  die 'OBSI_PORT must be between 1 and 65535'
fi
PORT=$((10#$PORT))
command -v systemctl >/dev/null || die 'systemd is required'

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3 python3-venv

python_ok() {
  "$1" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1
}

PYTHON=
for candidate in python3 python3.13 python3.12 python3.11; do
  if command -v "$candidate" >/dev/null 2>&1 && python_ok "$candidate"; then
    PYTHON=$(command -v "$candidate")
    break
  fi
done
if [[ -z $PYTHON ]]; then
  apt-get install -y python3.11 python3.11-venv || die 'Install Python 3.11+ and its venv package, then retry'
  PYTHON=$(command -v python3.11)
fi

if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin --user-group "$APP_USER"
fi
install -d -m 0755 "$APP_DIR"
install -d -m 0700 -o "$APP_USER" -g "$APP_USER" "$DATA_DIR"

if [[ ! -x $APP_DIR/venv/bin/python ]]; then
  "$PYTHON" -m venv "$APP_DIR/venv" || die "Cannot create venv with $PYTHON; install its matching python3.x-venv package"
fi
"$APP_DIR/venv/bin/python" -m pip install --upgrade "$SOURCE_DIR"

obsi() { runuser -u "$APP_USER" -- "$APP_DIR/venv/bin/obsi-server" --data-dir "$DATA_DIR" "$@"; }
json_field() { "$APP_DIR/venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }
json_count() { "$APP_DIR/venv/bin/python" -c 'import json,sys; print(len(json.load(sys.stdin)))'; }

TOKENS_FILE=$DATA_DIR/TOKENS.txt
if [[ -f $TOKENS_FILE ]]; then
  echo "Existing credentials preserved in $TOKENS_FILE"
elif [[ $(obsi admin list-tokens | json_count) == 0 ]]; then
  [[ $(obsi admin list-vaults | json_count) == 0 ]] || die 'Vaults already exist; create tokens manually for the existing vault'
  echo 'Creating initial vault and tokens'
  VAULT=$(obsi admin create-vault --name Personal | json_field id)
  ADMIN=$(obsi admin create-token --kind admin --name admin | json_field token)
  DEVICE=$(obsi admin create-token --kind device --name device --vault "$VAULT" --ops read,write | json_field token)
  MCP=$(obsi admin create-token --kind mcp --name mcp --vault "$VAULT" --ops list,search,read,write | json_field token)
  umask 077
  cat > "$TOKENS_FILE" <<EOF
Vault: $VAULT

Admin token:
$ADMIN

Device token (Obsidian plugin):
$DEVICE

MCP token:
$MCP

Local MCP endpoint: http://127.0.0.1:$PORT/mcp
Publish the service through a TLS reverse proxy before connecting from another machine.
EOF
  chmod 0600 "$TOKENS_FILE"
  echo "Credentials saved to $TOKENS_FILE (root access only)"
else
  echo "Tokens already exist; $TOKENS_FILE is absent. Existing tokens cannot be recovered." >&2
fi

cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=obsi-mcp server
After=network.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$DATA_DIR
ExecStart=$APP_DIR/venv/bin/obsi-server --data-dir $DATA_DIR serve --host 127.0.0.1 --port $PORT
Restart=on-failure
RestartSec=3
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable obsi-mcp.service
systemctl restart obsi-mcp.service
echo "Service: $(systemctl is-active obsi-mcp.service)"
echo "Health: http://127.0.0.1:$PORT/api/v1/health"
echo "Credentials: sudo cat $TOKENS_FILE"
echo 'Logs: sudo journalctl -u obsi-mcp -f'
