#!/usr/bin/env bash
# Local one-click run: venv + deps, first-run admin token and invitation, serve.
# Overrides: OBSI_HOST (127.0.0.1), OBSI_PORT (8766), OBSI_DATA_DIR (./data), PYTHON (python3).
# Optional S3: set OBSI_S3_BUCKET (or s3_bucket in OBSI_CONFIG); boto3 is installed only then.
set -euo pipefail
cd "$(dirname "$0")"
umask 077

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
install_locked() { # LOCKFILE — pinned, hash-checked dependencies, then this checkout in editable mode
  .venv/bin/pip install -q --require-hashes -r "$1" && .venv/bin/pip install -q --no-deps -e .
}
if [ "$NEW_VENV" -eq 1 ]; then
  install_locked requirements.lock
fi
S3_BUCKET=$(.venv/bin/python -c 'from obsi_server.config import Config; print(Config.load().s3_bucket or "")')
if [ -n "$S3_BUCKET" ] && ! .venv/bin/python -c 'import boto3' 2>/dev/null; then
  install_locked requirements-s3.lock
fi
obsi() { .venv/bin/obsi-server --data-dir "$DATA" "$@"; }
token_of() { sed -n '/{/,$p' | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

mkdir -p "$DATA"
chmod 700 "$DATA"
# The data directory may predate this launcher and contain credentials or vault data.
find "$DATA" -type d -exec chmod 700 {} +
find "$DATA" -type f -exec chmod 600 {} +
if [ "$(obsi admin list-tokens)" = "[]" ]; then
  echo "==> First run: creating admin access and a first user invitation"
  ADMIN=$(obsi admin create-token --kind admin --name admin | token_of token)
  INVITE=$(obsi admin create-invite --name first-user | token_of code)
  URL="http://127.0.0.1:$PORT"
  MESSAGE="Sync and MCP invitation
Server: $URL
Code: $INVITE
In Obsidian: Sync and MCP → \"I have an invitation\", paste this whole message."
  cat > "$DATA/TOKENS.txt" <<EOF
obsi-mcp local server
URL:     $URL

ADMIN token:
$ADMIN

First user invitation (one use):
$INVITE

Invitation message (forward it to the first user, or paste it into Sync and MCP yourself):
$MESSAGE

The user then picks a username and an account password, creates their own server vault
and chooses a separate encryption password.
For another user, run: obsi-server --data-dir "$DATA" admin create-invite --name NAME
For an AI assistant, create an MCP token with a grant on a chosen vault; list,search,read is
enough to read notes, add write only if the assistant should propose edits.

Debug:
curl -v $URL/api/v1/health
curl -v -H "Authorization: Bearer $ADMIN" $URL/api/v1/me
EOF
  echo "==> Tokens saved to $DATA/TOKENS.txt"
  if [ -t 1 ]; then
    printf '==> First user invitation (one use), forward this whole message:\n\n%s\n\n' "$MESSAGE"
  fi
  ADMIN='' INVITE='' MESSAGE=''
fi

echo "==> obsi-mcp on http://$HOST:$PORT  (data: $DATA, tokens: $DATA/TOKENS.txt)  Ctrl+C to stop"
echo "==> Add a user: .venv/bin/obsi-server --data-dir '$DATA' admin create-invite --name NAME"
exec .venv/bin/obsi-server --data-dir "$DATA" serve --host "$HOST" --port "$PORT"
