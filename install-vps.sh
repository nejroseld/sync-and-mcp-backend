#!/usr/bin/env bash
# Install obsi-mcp from this checkout as a systemd service on Ubuntu.
set -euo pipefail
set +x

APP_USER=obsi-mcp
APP_DIR=/opt/obsi-mcp
DATA_DIR=/var/lib/obsi-mcp
SERVICE_FILE=/etc/systemd/system/obsi-mcp.service
S3_ENV_FILE=/etc/obsi-mcp/s3.env
PORT="${OBSI_PORT:-8765}"
S3_MODE="${OBSI_S3_MODE:-auto}"
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
env_line() {
  local value=$2
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  printf '%s="%s"\n' "$1" "$value"
}

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
[[ $S3_MODE == auto || $S3_MODE == configure || $S3_MODE == skip ]] || die 'OBSI_S3_MODE must be auto, configure or skip'

prompt_s3() {
  local bucket region endpoint prefix threshold access_key secret_key tmp
  [[ -r /dev/tty ]] || die 'S3 setup needs an interactive terminal'
  read -r -p 'S3 bucket: ' bucket </dev/tty || die 'S3 setup cancelled'
  [[ $bucket =~ ^[a-zA-Z0-9._-]{3,63}$ ]] || die 'Invalid S3 bucket name'
  read -r -p 'S3 region (empty for provider default): ' region </dev/tty || die 'S3 setup cancelled'
  read -r -p 'S3 endpoint URL (empty for AWS): ' endpoint </dev/tty || die 'S3 setup cancelled'
  read -r -p 'S3 key prefix [obsi]: ' prefix </dev/tty || die 'S3 setup cancelled'
  prefix=${prefix:-obsi}
  read -r -p 'Offload files larger than MB [10]: ' threshold </dev/tty || die 'S3 setup cancelled'
  threshold=${threshold:-10}
  [[ $threshold =~ ^[0-9]+$ ]] || die 'S3 threshold must be a non-negative integer'
  read -r -p 'AWS access key ID (empty for IAM role/other credential provider): ' access_key </dev/tty || die 'S3 setup cancelled'
  secret_key=
  if [[ -n $access_key ]]; then
    read -r -s -p 'AWS secret access key: ' secret_key </dev/tty || die 'S3 setup cancelled'
    printf '\n' >/dev/tty
    [[ -n $secret_key ]] || die 'AWS secret access key is required with an access key ID'
  fi
  for value in "$region" "$endpoint" "$prefix" "$access_key" "$secret_key"; do
    [[ $value != *$'\n'* && $value != *$'\r'* ]] || die 'S3 values cannot contain newlines'
  done
  install -d -m 0700 -o root -g root "$(dirname "$S3_ENV_FILE")"
  umask 077
  tmp=$(mktemp "${S3_ENV_FILE}.XXXXXXXX") || die 'Cannot create S3 environment file'
  {
    env_line OBSI_S3_BUCKET "$bucket"
    if [[ -n $region ]]; then env_line OBSI_S3_REGION "$region"; fi
    if [[ -n $endpoint ]]; then env_line OBSI_S3_ENDPOINT "$endpoint"; fi
    env_line OBSI_S3_PREFIX "$prefix"
    env_line OBSI_S3_THRESHOLD_MB "$threshold"
    if [[ -n $access_key ]]; then
      env_line AWS_ACCESS_KEY_ID "$access_key"
      env_line AWS_SECRET_ACCESS_KEY "$secret_key"
    fi
  } > "$tmp"
  chmod 0600 "$tmp"
  mv -f -- "$tmp" "$S3_ENV_FILE"
  echo 'S3 configuration saved for the service.'
}

if [[ $S3_MODE == configure ]]; then
  prompt_s3
elif [[ $S3_MODE == auto && ! -f $S3_ENV_FILE && -r /dev/tty ]]; then
  answer=
  read -r -p 'Configure optional S3 attachment storage? [y/N]: ' answer </dev/tty || true
  if [[ $answer == y || $answer == Y ]]; then prompt_s3; fi
fi

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
if [[ -f $S3_ENV_FILE ]]; then
  "$APP_DIR/venv/bin/python" -m pip install --upgrade "${SOURCE_DIR}[s3]"
else
  "$APP_DIR/venv/bin/python" -m pip install --upgrade "$SOURCE_DIR"
fi

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
EnvironmentFile=-$S3_ENV_FILE
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
