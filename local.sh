#!/usr/bin/env bash
# Local one-click run: venv + deps, first-run bootstrap (vault + tokens -> data/TOKENS.txt), serve.
# Overrides: OBSI_HOST (127.0.0.1), OBSI_PORT (8766), OBSI_DATA_DIR (./data), PYTHON (python3).
# Optional S3: set OBSI_S3_BUCKET (or s3_bucket in OBSI_CONFIG); boto3 is installed only then.
set -euo pipefail
cd "$(dirname "$0")"

HOST="${OBSI_HOST:-127.0.0.1}"
PORT="${OBSI_PORT:-8766}"
DATA="${OBSI_DATA_DIR:-$PWD/data}"
PY="${PYTHON:-python3}"
unset OBSI_DATA_DIR OBSI_HOST OBSI_PORT  # the CLI flags below are the single source of truth

NEW_VENV=0
if [ ! -x .venv/bin/obsi-server ]; then
  echo "==> Creating venv and installing dependencies"
  "$PY" -c 'import sys; sys.exit(sys.version_info < (3, 11))' || { echo "Python 3.11+ required"; exit 1; }
  "$PY" -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  NEW_VENV=1
fi
S3_BUCKET=$(.venv/bin/python -c 'from obsi_server.config import Config; print(Config.load().s3_bucket or "")')
if [ -n "$S3_BUCKET" ]; then
  if [ "$NEW_VENV" -eq 1 ] || ! .venv/bin/python -c 'import boto3' 2>/dev/null; then
    .venv/bin/pip install -q -e '.[s3]'
  fi
elif [ "$NEW_VENV" -eq 1 ]; then
  .venv/bin/pip install -q -e .
fi
obsi() { .venv/bin/obsi-server --data-dir "$DATA" "$@"; }
token_of() { sed -n '/{/,$p' | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

mkdir -p "$DATA"
if [ "$(obsi admin list-tokens)" = "[]" ]; then
  echo "==> First run: creating vault 'Personal' and tokens"
  ADMIN=$(obsi admin create-token --kind admin --name admin | token_of token)
  VAULT=$(obsi admin create-vault --name Personal | token_of id)
  DEVICE=$(obsi admin create-token --kind device --name device --vault "$VAULT" --ops read,write | token_of token)
  MCP=$(obsi admin create-token --kind mcp --name claude --vault "$VAULT" --ops list,search,read,write | token_of token)
  URL="http://127.0.0.1:$PORT"
  umask 077
  cat > "$DATA/TOKENS.txt" <<EOF
obsi-mcp local server
URL:     $URL

VAULT (Personal):  $VAULT

ADMIN token:
$ADMIN

DEVICE token (read+write):
$DEVICE

MCP token (list/search/read/write):
$MCP

Obsidian plugin: Server URL $URL, device token above, mount with empty path -> vault Personal,
your own encryption password.

Claude Code:
claude mcp add --transport http obsi $URL/mcp --header "Authorization: Bearer $MCP"

Debug:
curl -v $URL/api/v1/health
curl -v -H "Authorization: Bearer $DEVICE" $URL/api/v1/me
EOF
  echo "==> Tokens saved to $DATA/TOKENS.txt"
fi

echo "==> obsi-mcp on http://$HOST:$PORT  (data: $DATA, tokens: $DATA/TOKENS.txt)  Ctrl+C to stop"
exec .venv/bin/obsi-server --data-dir "$DATA" serve --host "$HOST" --port "$PORT"
