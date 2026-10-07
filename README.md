# Sync and MCP — server

**English** | [Русский](README.ru.md)

The server for the [Sync and MCP](https://github.com/nejroseld/sync-and-mcp) Obsidian plugin. It stores
end-to-end encrypted vault blobs (sync), receives the plain-text notes published as "AI Available",
serves them over MCP (streamable HTTP) and builds search over them (FTS5 + embeddings).
The HTTP contract is in [docs/API.md](docs/API.md), the architecture in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
(both in Russian).

Get the code:

```bash
git clone https://github.com/nejroseld/sync-and-mcp-backend.git
cd sync-and-mcp-backend
```

Technical names come from the project's working name, obsi-mcp: the Python package `obsi_server`,
the `obsi-server` command, the `obsi-mcp.service` unit, the `/opt/obsi-mcp` and `/var/lib/obsi-mcp`
directories and the `OBSI_*` variables. They are kept so that existing servers update without a migration.

## Privacy model

- Synced files arrive **encrypted** on the device with rclone crypt. The server never sees the
  encryption password, file names or contents.
- Notes a user allows for AI are published **in plain text**: the server indexes only the current
  version of a published note (`.md`) and hands it to MCP clients. Attachments, other AI Available
  files, and the encrypted sync store are not indexed. Unpublishing deletes the chunks before that
  text is sent for embeddings. AI access is off by default in the plugin.
- MCP clients never write to a vault directly. A suggested edit is queued, and the user's Obsidian
  client applies it only if the AI rules allow it and the note has not changed.

## Multiple users

Every user registers a separate account and creates their own vaults. The administrator issues a
one-time invitation; for the second and further users run the same command with another name:

```bash
obsi-server --data-dir ./data admin create-invite --name alice
obsi-server --data-dir ./data admin create-invite --name bob
obsi-server --data-dir ./data admin list-users
```

The code is shown once. A person needs two values, the server address and the code, so forward them
in one message:

```
Sync and MCP invitation for alice
Server: https://203-0-113-10.sslip.io
Code: inv_...
In Obsidian: Sync and MCP → "I have an invitation", paste this whole message.
```

`manage-vps.sh` ("Users and invitations" menu item), `local.sh` and `install-vps.sh` (for the first
invitation) print this message ready to forward. The plugin takes the first http(s) address and the
`inv_...` code from the pasted text; line labels do not matter. The user then picks a username and
password, creates a server vault and sets a separate encryption password. Next time they choose
"Sign in to my account". A user sees only their own vaults and the ones the administrator explicitly
shared. In the plugin, the administrator can create invitations and list accounts on the "Server" tab.

After signing in, an account session can call `/api/v1/tokens` to issue device or MCP tokens with
explicit grants on the user's own vaults. Delegated tokens get no implicit owner access, cannot
create vaults and cannot manage tokens. See [docs/API.md](docs/API.md) for the full contract.

On a fresh install `local.sh` and `install-vps.sh` create an admin token and the first invitation.
On an existing server the data and old tokens are kept; create invitations for new users with the
command above (on a VPS use `--data-dir /var/lib/obsi-mcp` and the service user). Shared vaults and
tokens with grants keep working.

Dependencies: Python 3.11+, `fastapi`, `uvicorn`, `httpx`. For S3 install `pip install -e '.[s3]'`.
Everything else is the standard library (sqlite3 with FTS5). `numpy` is optional: when installed,
cosine similarity is computed faster.

## Installing on an Ubuntu VPS

### From your computer (recommended)

You need a VPS with Ubuntu 22.04 or later and the login details from your hosting provider. Your
computer needs `ssh` and `tar` (macOS and Linux have them; on Windows run from WSL or Git Bash).
From the repository directory:

```bash
./manage-vps.sh                 # or with the address: ./manage-vps.sh root@203.0.113.10
```

The first run has four steps:

1. **Server.** Paste the address in any form: `203.0.113.10`, `ubuntu@host:2222`,
   `ssh root@host -p 2222`. The default user is `root`, the default port `22`.
2. **Connection.** The SSH password is entered once: the script keeps one shared connection for the
   whole session. On the first connection to a new server the script shows its SSH key fingerprints
   and waits for you to confirm that they match the hosting panel or console
   (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`); no password is sent without confirmation.
   If the user is not `root`, the `sudo` password is asked once (kept only in the script's memory).
   The script then creates the key `~/.ssh/obsi_mcp_ed25519` (mode `0600`; with a running ssh-agent
   you can set a passphrase) and adds it to the server, so later runs need no password. If the
   server accepts keys only, the script asks for the path to the key from your provider.
3. **HTTPS address.** Press Enter to get a free address like `203-0-113-10.sslip.io` (a DNS service
   that returns the IP from the name); your own domain with an A record pointing to the server works
   too. The script checks DNS, installs Caddy and obtains a Let's Encrypt certificate
   (`scripts/setup-https.sh`; with an active `ufw` it opens ports 80 and 443). Answering "no" leaves
   the server without HTTPS: it is then reachable only through the SSH tunnel from the menu.
4. **Installation.** The code is uploaded to the VPS and installed with `install-vps.sh` without
   questions (S3 is not configured). At the end the script checks `/api/v1/health` and shows the
   first invitation as a ready message with the server address and code; the admin token is shown
   only after a separate confirmation. Forward the invitation or paste it into Sync and MCP yourself:
   "I have an invitation".

Later runs open a menu. Enter runs the default item (server update). The menu also has connection
details, status and logs, domain and HTTPS, SSH tunnel, S3, users and invitations, disabling password
login and switching to another server. "Users and invitations" lists registered users (date, number
of vaults and tokens) and unused invitations, then asks for a name and prints a message to forward;
you can create several invitations in a row, an empty name returns to the menu. Configured S3 is
kept on update. Connection settings are stored in `~/.config/obsi-mcp/vps.conf` with mode `0600`;
passwords are never written there. The log of the last error is saved to
`~/.config/obsi-mcp/last-error.log`. `NO_COLOR=1` disables colours and animation.

The interface language comes from `--lang ru|en` (remembered in `vps.conf`), the `OBSI_LANG`
variable or the system locale; the default is English. Strings live in `scripts/i18n/<lang>.sh`.
To add a language, copy `en.sh` to `<code>.sh` and translate the values; missing keys are shown in
English.

"Key-only server login" disables password and keyboard-interactive SSH authentication for all VPS
users; enable it only if everyone else also has key login set up. After the change the script checks
a new SSH session. If login cannot be confirmed, the previous SSH settings are restored after two
minutes.

### Directly on the VPS

From this repository's directory:

```bash
sudo ./install-vps.sh
# another local port: sudo env OBSI_PORT=9000 ./install-vps.sh
```

The script installs Python 3.11+ and creates `/opt/obsi-mcp/venv` from scratch. Dependencies are
installed only from `requirements.lock` (or `requirements-s3.lock` when S3 is configured) with hash
checking, so a repeated run produces the same set of packages. The application code is copied to
`/opt/obsi-mcp/src`. The script creates a dedicated system user and the `obsi-mcp.service` unit
(`PrivateTmp`, `MemoryHigh=768M`, `MemoryMax=1G`, `TasksMax=128`; change the memory limits with
`OBSI_MEMORY_HIGH` and `OBSI_MEMORY_MAX`). Data is stored in `/var/lib/obsi-mcp`.
The first installation creates an admin token and the first invitation; read them with
`sudo cat /var/lib/obsi-mcp/TOKENS.txt`. The invitation is printed on screen only when the output
goes to a terminal. It is valid for 7 days. The invitation message there contains the placeholder
`<your server address>`: replace it with the public HTTPS address before forwarding. A repeated run
updates the application and the service and keeps the data and previously issued tokens. S3
settings and optional access keys are kept in `/etc/obsi-mcp/s3.env` with mode `0600`; without this
file only local storage is used.

Check: `curl http://127.0.0.1:8765/api/v1/health`; status: `sudo systemctl status obsi-mcp`;
logs: `sudo journalctl -u obsi-mcp -f`. Without a token, `/api/v1/health` from an external address
answers only `{"ok": true}`; the version and modules are visible from localhost or with any valid
token. The service listens on `127.0.0.1` only. For access from other devices put a reverse proxy
with HTTPS in front of it on the VPS and use its public URL in the plugin and the MCP client (`/mcp`).

## Quickest start: `./local.sh`

```bash
./local.sh
```

The first run creates `.venv`, installs dependencies, creates an admin token and the first
invitation and writes them to `data/TOKENS.txt`; the invitation is also printed in the terminal as a
ready message. Dependencies are installed from `requirements.lock` with hash checking. The data
directory and its contents are accessible only to the owner (`0700` for directories, `0600` for
files), including on a rerun with old data. If an old `TOKENS.txt` used to be readable by other local
users, revoke every token stored in it and issue new ones: changing permissions does not revoke
secrets. The script then starts the server on `http://127.0.0.1:8766`. Later runs just start the
server. Override: `OBSI_PORT=9000 OBSI_HOST=0.0.0.0 OBSI_DATA_DIR=~/obsi-data ./local.sh`.
For local S3 set `OBSI_S3_BUCKET` (or `s3_bucket` in the `OBSI_CONFIG` file) and, if needed,
`OBSI_S3_REGION`, `OBSI_S3_ENDPOINT`, `OBSI_S3_PREFIX`, `OBSI_S3_THRESHOLD_MB` and the standard AWS
credential variables before running `./local.sh`.

## Running on your machine (step by step)

Scenario: the server and Obsidian on the same computer. You need Python 3.11+ (`python3 --version`).

**1. Get the code:**

```bash
git clone https://github.com/nejroseld/sync-and-mcp-backend.git ~/sync-and-mcp-backend
cd ~/sync-and-mcp-backend
```

When moving a working server from another machine, copy its directory together with `data/`: this
keeps the existing vaults and tokens (see `data/TOKENS.txt`), and step 4 can be skipped.

**2. Install dependencies into a venv:**

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .            # fastapi, uvicorn, httpx; optional numpy: pip install -e '.[fast]'
```

**3. Start the server** (keep the terminal open, stop with Ctrl+C):

```bash
obsi-server --data-dir ./data serve --port 8766
# check from another terminal:
curl http://127.0.0.1:8766/api/v1/health
```

By default the server listens on `127.0.0.1` only, which is enough for Obsidian on the same machine.
For a phone on the local network use `--host 0.0.0.0` and open the port in the firewall
(`sudo firewall-cmd --add-port=8766/tcp`).

**4. Create an admin token and an invitation** (in another terminal with the same `--data-dir`; the
server can keep running):

```bash
. .venv/bin/activate
obsi-server --data-dir ./data admin create-token --kind admin --name root
obsi-server --data-dir ./data admin create-invite --name alice
```

Tokens are shown once, so save them.

**5. The plugin in Obsidian:** install [Sync and MCP](https://github.com/nejroseld/sync-and-mcp)
with BRAT (`nejroseld/sync-and-mcp`) or copy `main.js`, `manifest.json`, `styles.css` from a plugin
release into `<vault>/.obsidian/plugins/sync-and-mcp/` and enable it in Settings → Community plugins.
In the plugin's wizard choose "I have an invitation" and paste the address `http://127.0.0.1:8766`
with the invitation code (or the whole message), then pick a username and password, create a vault
and enter a separate encryption password.

**6. MCP in Claude Code:**

```bash
claude mcp add --transport http obsi http://127.0.0.1:8766/mcp --header "Authorization: Bearer <mcp-token>"
```

**Debugging:** `curl -v http://127.0.0.1:8766/api/v1/health` shows that the server is alive;
`curl -H "Authorization: Bearer <device-token>" http://127.0.0.1:8766/api/v1/me` shows that the token
is valid. `connection refused` means the server is not running or uses another port; `401` means a
wrong or revoked token.

## Running (short version)

```bash
python3 -m obsi_server --data-dir ./data serve --host 0.0.0.0 --port 8765
# or: pip install -e . && obsi-server --data-dir ./data serve
```

All commands (`serve` and `admin ...`) must use the same data directory (`--data-dir` or
`OBSI_DATA_DIR`; the default is `./obsi-data` relative to the current directory). When started
directly, the server requires a private data directory (`chmod 700 DIR`); `local.sh` fixes the
permissions of an old directory automatically.

Configuration comes from environment variables (or a TOML file at the path in `OBSI_CONFIG`, section
`[server]`):

| Variable | Default | Meaning |
|---|---|---|
| `OBSI_DATA_DIR` | `./obsi-data` | SQLite database and blobs (`vaults/<vid>/blobs/<sha256(key)>`) |
| `OBSI_HOST` / `OBSI_PORT` | `127.0.0.1` / `8765` | listen address |
| `OBSI_FEATURES` | `ai,mcp,rag` | enabled modules (`all`, `none`); `mcp` and `rag` require `ai` |
| `OBSI_MAX_BODY_MB` | `200` | limit for encrypted file uploads (the body is written to a temporary file in `DATA_DIR/tmp`, not to memory); the JSON API is limited to 1 MiB, login and registration to 16 KiB, MCP to 8 MiB |
| `OBSI_MAX_AI_FILE_MB` | `32` | limit for one AI Available file (kept in memory and in SQLite) |
| `OBSI_VAULT_QUOTA_MB` | `10240` | vault quota: ciphertext + AI Available + pending MCP edits; `0` means no quota; exceeding it returns `507 quota_exceeded` |
| `OBSI_MAX_CONCURRENT_UPLOADS` | `4` | concurrent uploads larger than 1 MiB; the rest wait in a queue |
| `OBSI_SYNC_REQUESTS_PER_MINUTE` | `6000` | request limit per authorized token per minute; raise it for especially heavy sync |
| `OBSI_RAG_WORKER` | `1` | background embedding thread |
| `OBSI_CORS_ORIGINS` | `app://obsidian.md,capacitor://localhost,http://localhost` | explicit comma-separated origin list; `*` is not accepted |
| `OBSI_API_DOCS` | `0` | `1` enables `/docs`, `/redoc`, `/openapi.json` (development only) |
| `OBSI_SESSION_TTL_DAYS` / `OBSI_SESSION_IDLE_DAYS` | `180` / `30` | session lifetime after login and idle lifetime; at most 10 sessions live per user |
| `OBSI_INVITE_TTL_DAYS` | `7` | invitation lifetime |
| `OBSI_EMBEDDING_ALLOW_PRIVATE` | `0` | `1` allows `http://` and private addresses for embeddings (local Ollama); otherwise only public `https` |
| `OBSI_S3_BUCKET` | unset | bucket for attachments above the threshold; requires boto3 credentials |
| `OBSI_S3_THRESHOLD_MB` | `10` | offload threshold for files other than `.md` |
| `OBSI_S3_REGION` / `OBSI_S3_ENDPOINT` / `OBSI_S3_PREFIX` | unset / unset / `obsi` | S3 parameters |
| `OBSI_S3_SSE` / `OBSI_S3_KMS_KEY_ID` | `auto` / unset | server-side encryption of S3 objects: `AES256`, `aws:kms`, `none`; `auto` means `AES256` for AWS and `none` for a custom endpoint |

TOML example: `[server]\ndata_dir="/srv/obsi"\nfeatures=["ai","mcp"]\nmax_body_mb=500`.

Login requests are rate-limited by IP and account name; registration by IP. MCP has separate limits
for calls and search, reindexing and AI cleanup have their own limits. When a limit is exceeded the
server answers `429` with `Retry-After`. Counters are stored in SQLite and apply across several
processes sharing one database.

The server speaks plain HTTP; for internet access put it behind a reverse proxy with TLS.
S3 setup and file behaviour are described in [docs/S3.md](docs/S3.md).

To give someone access, the administrator creates a one-time invitation in the plugin or with
`POST /api/v1/admin/invites`. The user enters the code, username and password in the plugin's
connection wizard and then creates their own vault. Old device tokens and grants keep working;
details are in [docs/API.md](docs/API.md).

## Quick start

```bash
python3 -m obsi_server --data-dir ./data admin create-token --kind admin --name root
python3 -m obsi_server --data-dir ./data admin create-invite --name alice
python3 -m obsi_server --data-dir ./data admin list-users
```

For a shared folder or an MCP client you can create a vault and a token with grants separately
through the CLI or the admin REST API (`/api/v1/admin/...`).

Other commands: `list-invites`, `revoke-invite INVITE_ID`, `list-vaults`, `list-tokens`,
`revoke TOKEN_ID`, `grant TOKEN_ID --vault V --ops a,b`, `rag-enable V`, `rag-disable V`, `reindex V`,
`set-embedding --base-url https://api.example.com/v1 --model text-embedding-3-small --api-key-file ~/emb.key`
(key file with mode `0600`; `--api-key-prompt` asks for the key without echo, `--clear-api-key`
removes it). Parameters that are not passed stay unchanged. The key is not accepted as a
command-line argument: it would be visible in the process list and shell history. The embedding
address must be public `https`: the server checks it when saving and on every connection, does not
follow redirects and ignores `HTTP_PROXY`. For a local Ollama (`http://localhost:11434/v1`) start the
server with `OBSI_EMBEDDING_ALLOW_PRIVATE=1`. Changing `model` or `base-url` resets the vectors and
the index is rebuilt in the background (a running server picks up CLI changes within ~10 seconds).

Only text of the current published notes (`.md`) from vaults with RAG enabled is sent to the embedding endpoint. A chunk that is no longer that note is deleted and not sent. Without configured embeddings (or with RAG disabled for a vault) `search` works as full-text search and says
so in the `note` field.

## Connecting MCP clients

Endpoint: `http://HOST:8765/mcp`, authorization `Authorization: Bearer <mcp-token>`.
An assistant needs only the `list,search,read` operations; grant `write` only if it should suggest
edits (they are still applied by the plugin, not by the server). Note text in MCP responses is
marked as untrusted data: the model must not follow instructions found in notes.

```bash
claude mcp add --transport http obsi http://HOST:8765/mcp --header "Authorization: Bearer obsi_..."
```

Claude Desktop (via `claude_desktop_config.json` and the `mcp-remote` bridge):

```json
{ "mcpServers": { "obsi": { "command": "npx",
  "args": ["mcp-remote", "http://HOST:8765/mcp", "--header", "Authorization: Bearer obsi_..."] } } }
```

Or add the server as a custom connector in the client settings if it supports the Authorization
header.

## Tests

```bash
python3 -m unittest discover -s tests -t tests -v
```

## Smoke test (run manually)

```bash
export OBSI_DATA_DIR=/tmp/obsi-smoke
python3 -m obsi_server serve --port 18765 &
curl -s http://127.0.0.1:18765/api/v1/health
# {"ok":true,"version":"0.1.0","features":{"ai":true,"mcp":true,"rag":true}}  (from localhost)
curl -s -X POST http://127.0.0.1:18765/mcp -H "Authorization: Bearer $MCP" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
# {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"sync-and-mcp",...}}}
curl -s -X POST http://127.0.0.1:18765/mcp -H "Authorization: Bearer $MCP" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
# list_vaults, list_notes, search, read_note, write_note, get_change_status
```

Also verified: a PUT to `ai/files/...` followed by `search` through `tools/call` finds the note.

## Structure

`config`, `db` (SQLite WAL, migrations by `user_version`), `auth`, `vaults`, `storage`, `ai_store`,
`changes`, `rag`, `mcp`, `app` (REST + assembly), `cli`. Modules are connected by hooks: `ai_store`
notifies `rag` about PUT/DELETE/clear (removal from the index is synchronous), `vaults` calls purge
when a vault is deleted. Chunks and FTS are written synchronously on publish; embeddings are filled
in by a background worker (batches of 32, 60 s timeout, retry with backoff on error, the server does
not crash).

## License

[GNU GPL v3](LICENSE).
