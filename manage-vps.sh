#!/usr/bin/env bash
# Run on your computer: installs obsi-mcp on an Ubuntu VPS over SSH and manages it afterwards.
# Usage: ./manage-vps.sh [--lang ru|en] [user@]host[:port]
# The first run is a short wizard; later runs open a menu. Works with macOS bash 3.2.
# UI strings live in scripts/i18n/<lang>.sh; the language comes from --lang, OBSI_LANG or the locale.
set -uo pipefail
set +x

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
I18N_DIR="$SOURCE_DIR/scripts/i18n"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/obsi-mcp"
CONFIG_FILE="$CONFIG_DIR/vps.conf"
ERROR_LOG="$CONFIG_DIR/last-error.log"
DEFAULT_KEY="$HOME/.ssh/obsi_mcp_ed25519"
REMOTE_DATA=/var/lib/obsi-mcp

SSH_HOST=
SSH_USER=root
SSH_PORT=22
APP_PORT=8765
KEY_PATH=
DOMAIN=
UI_LANG=

SAVED_HOST=     # host from the config file: a host key mismatch there is more suspicious
AUTH=           # key | password, for the current session
SUDO_PW=        # kept in memory only, never written anywhere
SPIN_PID=
SSH_OPTS=()

WORK=$(mktemp -d "${TMPDIR:-/tmp}/obsi-mcp.XXXXXX") || { echo 'Cannot create a temp directory' >&2; exit 1; }
LOG=$WORK/step.log
CONTROL=$WORK/m

# ───────────────────────────── strings ─────────────────────────────

# m KEY ARGS... — print the localized printf format L_KEY with ARGS.
m() {
  local __k="L_$1"
  shift
  # shellcheck disable=SC2059  # the format comes from the locale files
  printf -- "${!__k:-$__k}" "$@"
}

detect_lang() {
  local l=${LANG_ARG:-${OBSI_LANG:-$UI_LANG}}
  [[ -n $l ]] || l=${LC_ALL:-${LC_MESSAGES:-${LANG:-}}}
  if [[ -z $l && $(uname 2>/dev/null) == Darwin ]]; then l=$(defaults read -g AppleLocale 2>/dev/null); fi
  l=$(printf '%s' "$l" | tr '[:upper:]' '[:lower:]')
  l=${l%%[_.@-]*}
  printf '%s' "${l:-en}"
}

load_strings() { # LANG
  # shellcheck disable=SC1091
  . "$I18N_DIR/en.sh" || return 1
  [[ $1 == en ]] && return 0
  if [[ $1 =~ ^[a-z]{2,3}$ && -f $I18N_DIR/$1.sh ]]; then
    # shellcheck disable=SC1090
    . "$I18N_DIR/$1.sh"
  else
    LANG_WARNING=$1
  fi
}

# ───────────────────────────── appearance ─────────────────────────────

FANCY=0
R='' B='' DIM='' RED='' GRN='' YEL='' ACC='' ACC2='' HIDE_CUR='' SHOW_CUR=''
GRADIENT=()
if [[ -t 1 && -z ${NO_COLOR:-} && ${TERM:-dumb} != dumb ]]; then
  FANCY=1
  R=$'\033[0m' B=$'\033[1m' DIM=$'\033[2m'
  RED=$'\033[31m' GRN=$'\033[32m' YEL=$'\033[33m'
  HIDE_CUR=$'\033[?25l' SHOW_CUR=$'\033[?25h'
  if (( $(tput colors 2>/dev/null || echo 8) >= 256 )); then
    ACC=$'\033[38;5;141m' ACC2=$'\033[38;5;117m'
    GRADIENT=($'\033[38;5;99m' $'\033[38;5;105m' $'\033[38;5;111m' $'\033[38;5;117m' $'\033[38;5;123m' $'\033[38;5;159m')
  else
    ACC=$'\033[35m' ACC2=$'\033[36m'
    GRADIENT=($'\033[35m' $'\033[35m' $'\033[35m' $'\033[36m' $'\033[36m' $'\033[36m')
  fi
fi
FRAMES=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)

cols() { local c; c=$(tput cols 2>/dev/null) || c=80; [[ $c =~ ^[0-9]+$ ]] || c=80; printf '%s' "$c"; }
nap() { (( FANCY )) && sleep "$1"; return 0; }

ok()   { printf '  %s✔%s %s\n' "$GRN" "$R" "$*"; }
bad()  { printf '  %s✘%s %s\n' "$RED" "$R" "$*" >&2; return 1; }
warn() { printf '  %s!%s %s\n' "$YEL" "$R" "$*"; }
note() { printf '  %s%s%s\n' "$DIM" "$*" "$R"; }
say()  { printf '  %s\n' "$*"; }

typewrite() {
  local text=$1 i
  if (( ! FANCY )); then printf '  %s\n' "$text"; return; fi
  printf '  %s' "$DIM"
  for (( i = 0; i < ${#text}; i++ )); do printf '%s' "${text:i:1}"; sleep 0.008; done
  printf '%s\n' "$R"
}

banner() {
  local lines=(
    '       _         _                            '
    '  ___ | |__  ___(_)      _ __ ___   ___ _ __  '
    ' / _ \| '"'"'_ \/ __| |_____| '"'"'_ ` _ \ / __| '"'"'_ \ '
    '| (_) | |_) \__ \ |_____| | | | | | (__| |_) |'
    ' \___/|_.__/|___/_|     |_| |_| |_|\___| .__/ '
    '                                       |_|    '
  ) i
  printf '\n'
  for i in 0 1 2 3 4 5; do
    printf '  %s%s%s%s\n' "$B" "${GRADIENT[i]:-}" "${lines[i]}" "$R"
    nap 0.04
  done
  typewrite "$(m tagline)"
  printf '\n'
}

section() { # N TOTAL TITLE
  local title=" $1/$2 · $3 " width line
  width=$(( $(cols) - 4 )); (( width > 60 )) && width=60
  line=$(printf '%*s' $(( width - ${#title} - 3 )) '' | sed 's/ /━/g')
  printf '\n  %s━━%s%s%s%s%s%s\n\n' "$ACC" "$R" "$B" "$title" "$R" "$ACC" "$line$R"
}

elapsed() { local s=$1; if (( s >= 60 )); then printf '%d:%02d' $((s / 60)) $((s % 60)); else printf '%ss' "$s"; fi; }

# spin TITLE CMD... — run CMD in the background with an animated status line; output goes to $LOG.
# SPIN_SILENT=1 suppresses the failure mark so the caller can explain the problem itself.
spin() {
  local title=$1 start=$SECONDS i=0 rc line max
  shift
  "$@" >"$LOG" 2>&1 </dev/null &
  SPIN_PID=$!
  if (( FANCY )); then
    printf '%s' "$HIDE_CUR"
    while kill -0 "$SPIN_PID" 2>/dev/null; do
      line=$(tail -n 1 "$LOG" 2>/dev/null | tr -d '\000-\037')
      max=$(( $(cols) - ${#title} - 16 ))
      (( max < 0 )) && max=0
      printf '\r\033[K  %s%s%s %s %s%s  %s%s' "$ACC" "${FRAMES[i % ${#FRAMES[@]}]}" "$R" "$title" \
        "$DIM" "$(elapsed $((SECONDS - start)))" "${line:0:max}" "$R"
      i=$((i + 1))
      sleep 0.1
    done
  else
    printf '  … %s\n' "$title"
  fi
  wait "$SPIN_PID"
  rc=$?
  SPIN_PID=
  (( FANCY )) && printf '\r\033[K%s' "$SHOW_CUR"
  if (( rc == 0 )); then
    printf '  %s✔%s %s %s%s%s\n' "$GRN" "$R" "$title" "$DIM" "$(elapsed $((SECONDS - start)))" "$R"
  elif [[ -z ${SPIN_SILENT:-} ]]; then
    printf '  %s✘%s %s\n' "$RED" "$R" "$title"
  fi
  return "$rc"
}

show_log() { # print the tail of the last step and keep a copy for troubleshooting
  mkdir -p "$CONFIG_DIR" && cp -f "$LOG" "$ERROR_LOG" 2>/dev/null
  printf '\n'
  tail -n "${1:-15}" "$LOG" | sed "s/^/    $DIM│$R /"
  printf '\n'
  note "$(m full_log "$ERROR_LOG")"
}

ask() { # VAR QUESTION [DEFAULT]
  local __q=$2 __def=${3:-} __ans
  printf '  %s?%s %s%s%s' "$ACC" "$R" "$B" "$__q" "$R"
  [[ -n $__def ]] && printf ' %s[%s]%s' "$DIM" "$__def" "$R"
  printf ' %s›%s ' "$ACC" "$R"
  if [[ -t 0 ]]; then read -r -e __ans || exit 0; else read -r __ans || exit 0; fi
  __ans=$(printf '%s' "$__ans" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
  printf -v "$1" '%s' "${__ans:-$__def}"
}

ask_secret() { # VAR QUESTION
  local __ans
  printf '  %s?%s %s%s%s %s›%s ' "$ACC" "$R" "$B" "$2" "$R" "$ACC" "$R"
  read -r -s __ans || exit 0
  printf '%s\n' "$DIM•••$R"
  printf -v "$1" '%s' "$__ans"
}

# Answers are accepted in any supported language, whatever the UI language is.
is_yes() { case $1 in y|Y|yes|Yes|YES|д|Д|да|Да|ДА) return 0 ;; *) return 1 ;; esac; }
is_no()  { case $1 in n|N|no|No|NO|н|Н|нет|Нет|НЕТ|-) return 0 ;; *) return 1 ;; esac; }

confirm() { # QUESTION DEFAULT(y|n)
  local ans hint
  if [[ $2 == y ]]; then hint=$(m yes_no_y); else hint=$(m yes_no_n); fi
  while true; do
    printf '  %s?%s %s%s%s %s(%s)%s %s›%s ' "$ACC" "$R" "$B" "$1" "$R" "$DIM" "$hint" "$R" "$ACC" "$R"
    read -r ans || exit 0
    ans=${ans:-$2}
    is_yes "$ans" && return 0
    is_no "$ans" && return 1
  done
}

pause() { printf '\n  %s%s%s ' "$DIM" "$(m continue)" "$R"; read -r _ || exit 0; }

# ───────────────────────────── config ─────────────────────────────

save_config() {
  mkdir -p "$CONFIG_DIR" && chmod 0700 "$CONFIG_DIR" || return 1
  (
    umask 077
    printf 'SSH_HOST=%q\nSSH_USER=%q\nSSH_PORT=%q\nAPP_PORT=%q\nKEY_PATH=%q\nDOMAIN=%q\nUI_LANG=%q\n' \
      "$SSH_HOST" "$SSH_USER" "$SSH_PORT" "$APP_PORT" "$KEY_PATH" "$DOMAIN" "$UI_LANG" > "$CONFIG_FILE"
  )
}

load_config() {
  # This file is written by save_config and owned by the current user.
  # shellcheck disable=SC1090
  [[ -f $CONFIG_FILE ]] && . "$CONFIG_FILE"
  valid_port "$APP_PORT" || APP_PORT=8765
  valid_port "$SSH_PORT" || SSH_PORT=22
  valid_host "$SSH_HOST" && valid_user "$SSH_USER" || SSH_HOST=
  valid_domain "$DOMAIN" || DOMAIN=
  [[ $UI_LANG =~ ^[a-z]{2,3}$ ]] || UI_LANG=
  SAVED_HOST=$SSH_HOST
  return 0
}

valid_port()   { [[ $1 =~ ^[0-9]+$ ]] && (( 10#$1 >= 1 && 10#$1 <= 65535 )); }
valid_host()   { [[ $1 =~ ^[a-zA-Z0-9._-]+$ ]]; }
valid_user()   { [[ $1 =~ ^[a-z_][a-z0-9_-]*$ ]]; }
valid_domain() { [[ $1 =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]]; }

# Accepts what hosting providers usually send: "1.2.3.4", "root@1.2.3.4", "user@host:2222",
# "ssh root@1.2.3.4 -p 2222", "ssh://user@host:port".
parse_address() {
  local a=$1 user='' port='' re='(^|[[:space:]])-p[[:space:]]*([0-9]+)'
  if [[ $a =~ $re ]]; then port=${BASH_REMATCH[2]}; a=${a/"${BASH_REMATCH[0]}"/ }; fi
  a=$(printf '%s' "$a" | sed 's/^[[:space:]]*//; s/^ssh[[:space:]]\{1,\}//; s/^ssh:\/\///; s/[[:space:]]*$//')
  if [[ $a == *@* ]]; then user=${a%%@*}; a=${a#*@}; fi
  re='^(.+):([0-9]+)$'
  if [[ $a =~ $re ]]; then a=${BASH_REMATCH[1]}; port=${port:-${BASH_REMATCH[2]}}; fi
  valid_host "$a" || { bad "$(m bad_address "$1")"; return 1; }
  [[ -z $user ]] || valid_user "$user" || { bad "$(m bad_user "$user")"; return 1; }
  [[ -z $port ]] || valid_port "$port" || { bad "$(m bad_port "$port")"; return 1; }
  if [[ $a != "$SSH_HOST" ]]; then KEY_PATH='' DOMAIN=''; fi
  SSH_HOST=$a
  SSH_USER=${user:-root}
  SSH_PORT=$((10#${port:-22}))
}

# ───────────────────────────── ssh ─────────────────────────────

build_ssh_opts() {
  SSH_OPTS=(-p "$SSH_PORT" -o "ControlPath=$CONTROL" -o ConnectTimeout=10
            -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o LogLevel=ERROR)
  if [[ -n $KEY_PATH && -f $KEY_PATH ]]; then SSH_OPTS+=(-i "$KEY_PATH" -o IdentitiesOnly=yes); fi
}

# Commands passed here are fixed by this script; variable parts are validated beforehand.
# shellcheck disable=SC2029
remote()    { ssh -n "${SSH_OPTS[@]}" -o ControlMaster=no "$SSH_USER@$SSH_HOST" "$@"; }
# shellcheck disable=SC2029
remote_in() { ssh "${SSH_OPTS[@]}" -o ControlMaster=no "$SSH_USER@$SSH_HOST" "$@"; }

# Run as root on the VPS; stdin is forwarded to the command (after the sudo password, if any).
# Always redirect stdin, e.g. `as_root 'true' </dev/null`.
as_root() {
  if [[ $SSH_USER == root ]]; then
    remote_in "$1"
  elif [[ -n $SUDO_PW ]]; then
    # -k makes sudo read the password every time, so it never leaks into the command's stdin.
    { printf '%s\n' "$SUDO_PW"; cat; } | remote_in "sudo -k -S -p '' $1"
  else
    remote_in "sudo -n $1"
  fi
}

close_master() {
  [[ -S $CONTROL ]] && ssh -o "ControlPath=$CONTROL" -O exit "$SSH_USER@$SSH_HOST" >/dev/null 2>&1
  rm -f "$CONTROL"
  AUTH='' SUDO_PW=''
}

open_master() { ssh "${SSH_OPTS[@]}" -o ControlMaster=yes -o ControlPersist=yes "$@" "$SSH_USER@$SSH_HOST" true; }

host_key_name() { if (( SSH_PORT == 22 )); then printf '%s' "$SSH_HOST"; else printf '[%s]:%s' "$SSH_HOST" "$SSH_PORT"; fi; }

# A saved key for this address that no longer matches the server.
handle_host_key_mismatch() {
  local name def=y
  name=$(host_key_name)
  ssh-keygen -F "$name" >/dev/null 2>&1 || return 1
  warn "$(m hostkey_changed "$name")"
  if [[ $SSH_HOST == "$SAVED_HOST" ]]; then
    note "$(m hostkey_changed_old)"
    def=n
  else
    note "$(m hostkey_changed_new)"
  fi
  confirm "$(m hostkey_forget)" "$def" || return 1
  ssh-keygen -R "$name" >/dev/null 2>&1
}

# Opens one shared SSH connection for the whole session, so a password is typed at most once.
connect() {
  local key retried=0 rc target="$SSH_USER@$SSH_HOST"
  close_master
  build_ssh_opts
  while true; do
    if SPIN_SILENT=1 spin "$(m connecting "$target")" open_master -o BatchMode=yes; then
      AUTH=key
      break
    fi
    rc=$?
    if [[ ! -s $LOG ]]; then # ssh failed silently (dropped connection); one retry is usually enough
      echo "ssh exited with code $rc and no message" > "$LOG"
      (( retried++ == 0 )) && { sleep 1; continue; }
    fi
    if grep -qi 'identification has changed\|host key verification failed' "$LOG"; then
      handle_host_key_mismatch && continue
      bad "$(m ssh_failed)"; show_log 5; return 1
    elif grep -qi 'could not resolve' "$LOG"; then
      bad "$(m not_resolved "$SSH_HOST")"; return 1
    elif grep -qi 'timed out' "$LOG"; then
      bad "$(m timed_out "$SSH_HOST")"; return 1
    elif grep -qi 'refused' "$LOG"; then
      bad "$(m refused "$SSH_PORT" "$SSH_HOST")"; return 1
    elif ! grep -qi 'permission denied' "$LOG"; then
      bad "$(m ssh_failed)"; show_log 5; return 1
    fi

    if grep -qi 'permission denied (publickey)' "$LOG"; then
      warn "$(m key_only)"
      ask key "$(m ask_key_path)"
      [[ -n $key ]] || return 1
      key=${key/#\~/$HOME}
      [[ -f $key ]] || { bad "$(m file_not_found "$key")"; continue; }
      KEY_PATH=$key
      build_ssh_opts
      continue
    fi

    say "$(m enter_password "$B$target$R")"
    note "$(m password_hidden)"
    if open_master -o NumberOfPasswordPrompts=3 >/dev/null 2>"$LOG"; then
      AUTH=password
      ok "$(m connected "$target")"
      break
    fi
    bad "$(m wrong_password)"
    [[ $SSH_USER == root ]] && note "$(m other_user_hint)"
    return 1
  done
  setup_root
}

master_alive() { [[ -n $AUTH ]] && ssh -o "ControlPath=$CONTROL" -O check "$SSH_USER@$SSH_HOST" >/dev/null 2>&1; }
ensure_connected() { master_alive || connect; }

setup_root() {
  local pw try
  [[ $SSH_USER == root ]] && return 0
  remote 'sudo -n true' >/dev/null 2>&1 && return 0
  remote 'command -v sudo' >/dev/null 2>&1 || { bad "$(m no_sudo "$SSH_USER" "$SSH_HOST")"; return 1; }
  say "$(m need_sudo)"
  for try in 1 2 3; do
    ask_secret pw "$(m ask_sudo "$SSH_USER")"
    if printf '%s\n' "$pw" | remote_in "sudo -k -S -p '' true" >/dev/null 2>&1; then
      SUDO_PW=$pw
      ok "$(m sudo_ok)"
      return 0
    fi
    (( try < 3 )) && warn "$(m try_again)"
  done
  bad "$(m sudo_failed "$SSH_USER")"
  return 1
}

key_login_works() { # KEY (may be empty: default keys / agent)
  local opts=(-p "$SSH_PORT" -o ControlPath=none -o BatchMode=yes -o ConnectTimeout=10
              -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no -o LogLevel=ERROR)
  [[ -n $1 ]] && opts+=(-i "$1" -o IdentitiesOnly=yes)
  ssh -n "${opts[@]}" "$SSH_USER@$SSH_HOST" true >/dev/null 2>&1
}

setup_key() {
  local key=${KEY_PATH:-$DEFAULT_KEY}
  if [[ ! -f $key ]]; then
    mkdir -p "$(dirname "$key")" && chmod 0700 "$(dirname "$key")" || return 1
    ssh-keygen -q -t ed25519 -N '' -C "obsi-mcp@$(hostname 2>/dev/null || echo pc)" -f "$key" </dev/null || return 1
  fi
  [[ -f $key.pub ]] || { bad "$(m no_pubkey "$key.pub")"; return 1; }
  # Expansion happens on the VPS.
  # shellcheck disable=SC2016
  remote_in 'umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && k=$(cat) &&
    { grep -qxF "$k" ~/.ssh/authorized_keys || printf "%s\n" "$k" >> ~/.ssh/authorized_keys; }' < "$key.pub" \
    || { bad "$(m key_add_failed)"; return 1; }
  key_login_works "$key" || { bad "$(m key_login_failed)"; return 1; }
  KEY_PATH=$key
  AUTH=key
  build_ssh_opts
  save_config
  ok "$(m key_ready "$DIM$(m key_ready_detail "$key")$R")"
}

push_file() { # LOCAL NAME → prints the new remote temp dir
  # shellcheck disable=SC2016
  remote_in 'd=$(mktemp -d /tmp/obsi-mcp.XXXXXXXX) && cat > "$d/'"$2"'" && printf "%s" "$d"' < "$1"
}

valid_remote_dir() { [[ $1 =~ ^/tmp/obsi-mcp\.[a-zA-Z0-9]+$ ]]; }

# ───────────────────────────── actions ─────────────────────────────

check_os() {
  local os
  # shellcheck disable=SC2016
  os=$(remote '. /etc/os-release 2>/dev/null; printf "%s %s" "${ID:-?}" "${VERSION_ID:-}"') || return 1
  if [[ $os != ubuntu* ]]; then
    bad "$(m os_wrong "${os% *}")"
    return 1
  fi
  ok "$(m os_ok "${os#ubuntu }")"
}

upload_code() {
  local dir
  # shellcheck disable=SC2016
  dir=$(COPYFILE_DISABLE=1 tar -czf - -C "$SOURCE_DIR" --exclude=__pycache__ --exclude='*.pyc' --exclude=i18n \
          pyproject.toml obsi_server install-vps.sh scripts \
        | remote_in 'd=$(mktemp -d /tmp/obsi-mcp.XXXXXXXX) && tar -xzf - -C "$d" && printf "%s" "$d"') || return 1
  valid_remote_dir "$dir" || { echo "Unexpected remote dir: $dir"; return 1; }
  printf '%s' "$dir" > "$WORK/remote_dir"
}

wait_health() {
  remote "if command -v curl >/dev/null; then
      for i in \$(seq 1 20); do curl -fsS --max-time 3 http://127.0.0.1:$APP_PORT/api/v1/health && exit 0; sleep 1; done; exit 1
    else sleep 3; systemctl is-active obsi-mcp.service; fi"
}

deploy() {
  local dir rc=0
  ensure_connected || return 1
  check_os || return 1
  spin "$(m uploading)" upload_code || { show_log 5; return 1; }
  dir=$(cat "$WORK/remote_dir")
  spin "$(m installing)" \
    as_root "env OBSI_PORT=$APP_PORT OBSI_S3_MODE=skip bash '$dir/install-vps.sh'" || { show_log; rc=1; }
  as_root "rm -rf -- '$dir'" </dev/null >/dev/null 2>&1
  (( rc == 0 )) || return 1
  spin "$(m health)" wait_health || { show_log 10; return 1; }
}

server_ip() {
  local ip
  if [[ $SSH_HOST =~ ^[0-9.]+$ ]]; then printf '%s' "$SSH_HOST"; return; fi
  # shellcheck disable=SC2016
  ip=$(remote 'printf "%s" "$SSH_CONNECTION"' 2>/dev/null) && printf '%s' "$(printf '%s' "$ip" | awk '{print $3}')"
}

check_dns() {
  local resolved ip
  resolved=$(remote "getent ahostsv4 '$DOMAIN' | awk '{print \$1; exit}'" 2>/dev/null)
  ip=$(server_ip)
  if [[ -z $resolved ]]; then
    warn "$(m dns_missing "$DOMAIN")"
    note "$(m dns_missing_hint "$DOMAIN" "${ip:-$(m server_ip)}")"
    confirm "$(m continue_anyway)" n
  elif [[ -n $ip && $resolved != "$ip" ]]; then
    warn "$(m dns_other "$DOMAIN" "$resolved" "$ip")"
    confirm "$(m continue_anyway)" n
  else
    ok "$(m dns_ok "$DOMAIN")"
  fi
}

public_ipv4() { # the server's IPv4 if it looks reachable from the internet
  local ip
  ip=$(server_ip)
  [[ $ip =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  [[ $ip =~ ^(10|127)\. || $ip =~ ^192\.168\. || $ip =~ ^172\.(1[6-9]|2[0-9]|3[01])\. || $ip =~ ^100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\. ]] && return 1
  printf '%s' "$ip"
}

ask_domain() {
  local d ip free=''
  ip=$(public_ipv4) && free="$(printf '%s' "$ip" | tr . -).sslip.io"
  say "$(m domain_intro)"
  if [[ -n $free ]]; then
    say "$(m domain_free "$B$free$R")"
    note "$(m domain_own "$ip")"
  else
    say "$(m domain_need)"
  fi
  note "$(m domain_skip)"
  printf '\n'
  while true; do
    ask d "$(m ask_domain)" "${DOMAIN:-$free}"
    d=$(printf '%s' "$d" | tr '[:upper:]' '[:lower:]' | sed 's#^https\{0,1\}://##; s#/.*$##')
    if [[ -z $d ]] || is_no "$d"; then
      DOMAIN=''
      save_config
      warn "$(m no_https "$APP_PORT")"
      return 0
    fi
    if valid_domain "$d"; then DOMAIN=$d; save_config; return 0; fi
    bad "$(m bad_domain "$d")"
  done
}

setup_https() {
  local dir rc
  [[ -n $DOMAIN ]] || return 0
  ensure_connected || return 1
  check_dns || return 1
  if ! dir=$(push_file "$SOURCE_DIR/scripts/setup-https.sh" setup-https.sh) || ! valid_remote_dir "$dir"; then
    bad "$(m https_upload_failed)"; return 1
  fi
  spin "$(m https_enabling "$DOMAIN")" as_root "bash '$dir/setup-https.sh' '$DOMAIN' '$APP_PORT'"
  rc=$?
  as_root "rm -rf -- '$dir'" </dev/null >/dev/null 2>&1
  case $rc in
    0) return 0 ;;
    2) warn "$(m https_pending)"
       note "$(m https_pending_hint)"
       note "$(m https_retry "$(m menu_domain)")" ;;
    *) show_log 10 ;;
  esac
  return 1
}

public_url() { if [[ -n $DOMAIN ]]; then printf 'https://%s' "$DOMAIN"; else printf 'http://127.0.0.1:%s' "$APP_PORT"; fi; }

field() { printf '%s\n' "$TOKENS" | awk -v h="$1" 'index($0, h) == 1 { getline; print; exit }'; }

block() { printf '\n  %s●%s %s%s%s\n' "$ACC" "$R" "$B" "$1" "$R"; }
row()   { # bash printf pads by bytes, so pad non-ASCII labels by hand
  local pad=$(( 21 - ${#1} )); (( pad < 1 )) && pad=1
  printf '  %s│%s %s%s%*s%s%s\n' "$ACC" "$R" "$DIM" "$1" "$pad" '' "$R" "$2"
}
line()  { printf '  %s│%s %s\n' "$ACC" "$R" "$1"; }
dim()   { line "$DIM$1$R"; }

show_credentials() {
  local admin device mcp vault url
  ensure_connected || return 1
  TOKENS=$(as_root "cat $REMOTE_DATA/TOKENS.txt" </dev/null 2>/dev/null) || {
    bad "$(m tokens_missing)"
    note "$(m tokens_missing_hint)"
    return 1
  }
  vault=$(printf '%s\n' "$TOKENS" | awk '/^Vault:/ {print $2; exit}')
  admin=$(field 'Admin token:')
  device=$(field 'Device token')
  mcp=$(field 'MCP token:')
  TOKENS=
  url=$(public_url)

  if [[ -z $DOMAIN ]]; then
    printf '\n'
    warn "$(m no_https_warn)"
    note "$(m no_https_hint "$(m menu_domain)")"
  fi
  block "$(m cred_plugin)"
  row "$(m cred_server_url)" "$url"
  row "$(m cred_user_token)" "$device"
  row "$(m cred_vault)" "Personal $DIM($vault)$R"
  dim "$(m cred_password_note)"

  block "$(m cred_mcp)"
  row "$(m cred_url)" "$url/mcp"
  row "$(m cred_header)" "Authorization: Bearer $mcp"
  dim "$(m cred_example)"
  line "claude mcp add --transport http obsi $url/mcp --header \"Authorization: Bearer $mcp\""

  block "$(m cred_admin)"
  line "$admin"
  dim "$(m cred_admin_note)"

  block "$(m cred_more)"
  line "$(m cred_more_1)"
  line "$(m cred_more_2)"
  line "$(m cred_more_3)"
  line "$(m cred_more_4)"
  dim "$(m cred_more_5)"
  printf '\n'
}

celebrate() {
  local msg i c
  msg=$(m finished)
  printf '\n'
  if (( FANCY )); then
    for (( i = 0; i < 3; i++ )); do
      for c in ✦ ✧ ✶ ✷; do printf '\r  %s%s%s %s%s%s' "$ACC2" "$c" "$R" "$B" "$msg" "$R"; sleep 0.07; done
    done
    printf '\r\033[K'
  fi
  printf '  %s%s✔ %s%s\n' "$B" "$GRN" "$msg" "$R"
}

wizard() {
  local addr
  section 1 4 "$(m step_server)"
  say "$(m wizard_intro)"
  note "$(m wizard_paste)"
  printf '\n'
  while true; do
    if [[ -n ${1:-} ]]; then addr=$1; set --; else ask addr "$(m ask_address)" "${SSH_HOST:+$SSH_USER@$SSH_HOST}"; fi
    [[ -n $addr ]] || continue
    parse_address "$addr" || continue

    section 2 4 "$(m step_connect)"
    if connect; then break; fi
    printf '\n'
    note "$(m try_again_exit)"
  done
  save_config
  if [[ $AUTH == password ]]; then
    setup_key || warn "$(m password_stays)"
  fi

  section 3 4 "$(m step_https)"
  ask_domain

  section 4 4 "$(m step_install)"
  deploy || { printf '\n'; bad "$(m install_failed)"; return 1; }
  setup_https || true
  celebrate
  show_credentials
  note "$(m reopen "$(m menu_credentials)")"
  note "$(m next_run)"
}

status_logs() {
  ensure_connected || return 1
  printf '\n'
  as_root 'systemctl status --no-pager --lines=0 obsi-mcp.service; echo; journalctl -u obsi-mcp.service -n 40 --no-pager' \
    </dev/null 2>&1 | sed "s/^/    /"
  if [[ -n $DOMAIN ]]; then
    printf '\n    %sHTTPS (Caddy): %s%s\n' "$DIM" "$(remote 'systemctl is-active caddy.service' 2>/dev/null)" "$R"
  fi
}

tunnel() {
  ensure_connected || return 1
  if ! ssh -o "ControlPath=$CONTROL" -O forward -L "$APP_PORT:127.0.0.1:$APP_PORT" "$SSH_USER@$SSH_HOST" 2>"$LOG"; then
    bad "$(m tunnel_failed "$APP_PORT")"
    return 1
  fi
  printf '\n'
  ok "$(m tunnel_open "${B}http://127.0.0.1:$APP_PORT$R")"
  note "$(m tunnel_hint)"
  printf '\n  %s%s%s ' "$DIM" "$(m tunnel_close)" "$R"
  read -r _
  ssh -o "ControlPath=$CONTROL" -O cancel -L "$APP_PORT:127.0.0.1:$APP_PORT" "$SSH_USER@$SSH_HOST" >/dev/null 2>&1
  ok "$(m tunnel_closed)"
}

env_line() {
  local value=$2
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  printf '%s="%s"\n' "$1" "$value"
}

configure_s3() {
  local current bucket region endpoint access secret v
  ensure_connected || return 1
  printf '\n'
  say "$(m s3_intro)"
  note "$(m s3_note)"
  current=$(as_root "sed -n 's/^OBSI_S3_BUCKET=\"\\(.*\\)\"$/\\1/p' /etc/obsi-mcp/s3.env" </dev/null 2>/dev/null)
  [[ -n $current ]] && ok "$(m s3_current "$current")"
  printf '\n'
  if [[ -n $current ]]; then confirm "$(m s3_reconfigure)" n || return 0; else confirm "$(m s3_enable)" n || return 0; fi
  while true; do
    ask bucket "$(m s3_bucket)" "$current"
    [[ $bucket =~ ^[a-zA-Z0-9._-]{3,63}$ ]] && break
    bad "$(m s3_bad_bucket)"
  done
  ask endpoint "$(m s3_endpoint)"
  ask region "$(m s3_region)"
  ask access "$(m s3_access)"
  secret=
  if [[ -n $access ]]; then
    while [[ -z $secret ]]; do ask_secret secret 'Secret access key'; done
  fi
  for v in "$endpoint" "$region" "$access" "$secret"; do
    [[ $v != *$'\n'* && $v != *$'\r'* ]] || { bad "$(m s3_newlines)"; return 1; }
  done
  {
    env_line OBSI_S3_BUCKET "$bucket"
    [[ -n $region ]] && env_line OBSI_S3_REGION "$region"
    [[ -n $endpoint ]] && env_line OBSI_S3_ENDPOINT "$endpoint"
    env_line OBSI_S3_PREFIX obsi
    env_line OBSI_S3_THRESHOLD_MB 10
    if [[ -n $access ]]; then
      env_line AWS_ACCESS_KEY_ID "$access"
      env_line AWS_SECRET_ACCESS_KEY "$secret"
    fi
  } | as_root 'sh -c "umask 077 && mkdir -p /etc/obsi-mcp && chmod 700 /etc/obsi-mcp && cat > /etc/obsi-mcp/s3.env.new && mv -f /etc/obsi-mcp/s3.env.new /etc/obsi-mcp/s3.env"' \
    || { bad "$(m s3_save_failed)"; return 1; }
  secret=
  ok "$(m s3_saved)"
  deploy && ok "$(m s3_restarted)"
}

harden_ssh() {
  local dir client_ip rc=0
  ensure_connected || return 1
  printf '\n'
  say "$(m harden_intro)"
  warn "$(m harden_warn)"
  note "$(m harden_lost "${KEY_PATH:-$(m harden_default_key)}")"
  printf '\n'
  confirm "$(m harden_confirm)" n || return 0
  if ! key_login_works "$KEY_PATH"; then
    setup_key || return 1
  fi
  # Expansion happens on the VPS, where SSH_CONNECTION describes this login.
  # shellcheck disable=SC2016
  client_ip=$(remote 'printf "%s" "$SSH_CONNECTION"') || return 1
  client_ip=${client_ip%% *}
  [[ $client_ip =~ ^[0-9a-fA-F:.]+$ ]] || { bad "$(m client_ip_failed)"; return 1; }
  if ! dir=$(push_file "$SOURCE_DIR/scripts/enable-ssh-key-only.sh" key-only.sh) || ! valid_remote_dir "$dir"; then
    bad "$(m upload_failed)"; return 1
  fi
  spin "$(m harden_applying)" as_root "bash '$dir/key-only.sh' '$SSH_USER' '$client_ip'" || { show_log 5; rc=1; }
  if (( rc == 0 )); then
    spin "$(m harden_checking)" key_login_works "$KEY_PATH" || { bad "$(m harden_failed)"; rc=1; }
  fi
  if (( rc == 0 )); then
    spin "$(m harden_confirming)" as_root "bash '$dir/key-only.sh' --confirm" || { show_log 5; rc=1; }
  fi
  as_root "rm -rf -- '$dir'" </dev/null >/dev/null 2>&1
  (( rc == 0 )) && ok "$(m harden_done)"
  return "$rc"
}

service_state() {
  remote 'if [ -f /etc/systemd/system/obsi-mcp.service ]; then systemctl is-active obsi-mcp.service; else echo missing; fi' 2>/dev/null
}

item() { # N LABEL [HINT]
  printf '  %s%s%s  %s' "$ACC" "$1" "$R" "$2"
  [[ -n ${3:-} ]] && printf ' %s%s%s' "$DIM" "$3" "$R"
  printf '\n'
}

menu() {
  local state choice def label dot
  while true; do
    state=offline
    master_alive && state=$(service_state)
    case $state in
      active)  dot="$GRN$(m state_active)$R"; def=1; label=$(m menu_update) ;;
      missing) dot="$YEL$(m state_missing)$R"; def=1; label=$(m menu_install) ;;
      offline) dot="$RED$(m state_offline)$R"; def=1; label=$(m menu_install_update) ;;
      *)       dot="$RED● $state$R"; def=3; label=$(m menu_reinstall) ;;
    esac
    printf '\n  %s%s@%s%s  %s  %s%s%s\n\n' "$B" "$SSH_USER" "$SSH_HOST" "$R" "$dot" "$DIM" "${DOMAIN:+https://$DOMAIN}" "$R"
    item 1 "$label"
    item 2 "$(m menu_credentials)" "$(m menu_credentials_hint)"
    item 3 "$(m menu_status)"
    item 4 "$(m menu_domain)" "${DOMAIN:-$(m menu_domain_none)}"
    item 5 "$(m menu_tunnel)" "$(m menu_tunnel_hint)"
    item 6 "$(m menu_s3)" "$(m menu_s3_hint)"
    item 7 "$(m menu_harden)"
    item 8 "$(m menu_other)"
    item 0 "$(m menu_exit)"
    printf '\n'
    ask choice "$(m menu_choose)" "$def"
    case $choice in
      1) deploy && { [[ $state == missing ]] && setup_https; celebrate; [[ $state == missing ]] && show_credentials; } ;;
      2) show_credentials ;;
      3) status_logs; pause ;;
      4) ensure_connected && { printf '\n'; ask_domain; [[ -n $DOMAIN ]] && setup_https && ok "$(m reachable_at "https://$DOMAIN")"; } ;;
      5) tunnel ;;
      6) configure_s3 ;;
      7) harden_ssh ;;
      8) close_master; wizard ;;
      0|q|й|exit) return 0 ;;
      *) bad "$(m menu_unknown)" ;;
    esac
  done
}

# ───────────────────────────── main ─────────────────────────────

cleanup() {
  [[ -n $SPIN_PID ]] && kill "$SPIN_PID" 2>/dev/null
  close_master
  rm -rf "$WORK"
  printf '%s' "$SHOW_CUR"
}
trap cleanup EXIT
trap 'printf "\n"; exit 130' INT TERM

ADDRESS=
LANG_ARG=
while (( $# )); do
  case $1 in
    --lang) LANG_ARG=${2:-}; shift ;;
    --lang=*) LANG_ARG=${1#--lang=} ;;
    *) ADDRESS=$1 ;;
  esac
  shift
done

load_config
if [[ -n $LANG_ARG ]]; then UI_LANG=$(printf '%s' "$LANG_ARG" | tr '[:upper:]' '[:lower:]'); fi
LANG_WARNING=
if [[ ! -f $I18N_DIR/en.sh ]]; then
  echo 'Run the script from the server repository folder' >&2
  exit 1
fi
load_strings "$(detect_lang)"
[[ -n $LANG_WARNING ]] && UI_LANG='' && warn "$(m unknown_lang "$LANG_WARNING")"

missing=
for tool in ssh ssh-keygen tar; do command -v "$tool" >/dev/null || missing="$missing $tool"; done
if [[ -n $missing ]]; then
  bad "$(m missing_tools "$missing")"
  note "$(m missing_tools_hint)"
  exit 1
fi
[[ -f $SOURCE_DIR/install-vps.sh && -f $SOURCE_DIR/pyproject.toml ]] || { bad "$(m run_from_repo)"; exit 1; }

banner
if [[ -n $ADDRESS || -z $SSH_HOST ]]; then
  wizard "$ADDRESS" || true
  [[ -n $AUTH ]] || exit 1
  pause
else
  [[ -n $LANG_ARG ]] && save_config
  connect || note "$(m offline_hint)"
fi
menu
