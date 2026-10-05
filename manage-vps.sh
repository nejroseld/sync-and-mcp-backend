#!/usr/bin/env bash
# Run on your computer to deploy and manage an Ubuntu VPS over SSH.
set -uo pipefail
set +x

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/obsi-mcp"
CONFIG_FILE="$CONFIG_DIR/vps.conf"
SSH_HOST=
SSH_USER=
SSH_PORT=22
APP_PORT=8765
KEY_PATH=

fail() { printf 'Ошибка: %s\n' "$*" >&2; return 1; }

save_config() {
  mkdir -p "$CONFIG_DIR" || return 1
  chmod 0700 "$CONFIG_DIR" || return 1
  umask 077
  {
    printf 'SSH_HOST=%q\n' "$SSH_HOST"
    printf 'SSH_USER=%q\n' "$SSH_USER"
    printf 'SSH_PORT=%q\n' "$SSH_PORT"
    printf 'APP_PORT=%q\n' "$APP_PORT"
    printf 'KEY_PATH=%q\n' "$KEY_PATH"
  } > "$CONFIG_FILE"
  chmod 0600 "$CONFIG_FILE"
}

load_config() {
  if [[ -f $CONFIG_FILE ]]; then
    # This file is created by save_config and owned by the current user.
    # shellcheck disable=SC1090
    . "$CONFIG_FILE"
  fi
}

valid_port() { [[ $1 =~ ^[0-9]+$ ]] && (( 10#$1 >= 1 && 10#$1 <= 65535 )); }

configure() {
  local answer
  read -r -p "VPS IP или домен [$SSH_HOST]: " answer || return 1
  SSH_HOST=${answer:-$SSH_HOST}
  [[ $SSH_HOST =~ ^[a-zA-Z0-9._-]+$ ]] || fail 'Неверный адрес VPS' || return 1
  read -r -p "SSH-пользователь [$SSH_USER]: " answer || return 1
  SSH_USER=${answer:-$SSH_USER}
  [[ $SSH_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || fail 'Неверное имя пользователя' || return 1
  read -r -p "SSH-порт [$SSH_PORT]: " answer || return 1
  SSH_PORT=${answer:-$SSH_PORT}
  valid_port "$SSH_PORT" || fail 'Неверный SSH-порт' || return 1
  SSH_PORT=$((10#$SSH_PORT))
  read -r -p "Локальный порт MCP на VPS [$APP_PORT]: " answer || return 1
  APP_PORT=${answer:-$APP_PORT}
  valid_port "$APP_PORT" || fail 'Неверный порт MCP' || return 1
  APP_PORT=$((10#$APP_PORT))
  save_config
}

check_config() {
  [[ -n $SSH_HOST && -n $SSH_USER ]] || fail 'Сначала задайте параметры VPS (пункт 1)' || return 1
  [[ $SSH_HOST =~ ^[a-zA-Z0-9._-]+$ && $SSH_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || fail 'Неверные параметры VPS' || return 1
  valid_port "$SSH_PORT" && valid_port "$APP_PORT" || fail 'Неверный порт' || return 1
}

ssh_args() {
  SSH_ARGS=(-p "$SSH_PORT")
  SCP_ARGS=(-P "$SSH_PORT")
  if [[ -n $KEY_PATH ]]; then
    SSH_ARGS+=(-i "$KEY_PATH")
    SCP_ARGS+=(-i "$KEY_PATH")
  fi
}

# Commands passed here are fixed by this script; variable parts are validated above.
# shellcheck disable=SC2029
remote() { ssh "${SSH_ARGS[@]}" "$SSH_USER@$SSH_HOST" "$@"; }
remote_tty() { ssh -tt "${SSH_ARGS[@]}" "$SSH_USER@$SSH_HOST" "$@"; }

deploy() {
  check_config || return 1
  command -v tar >/dev/null && command -v scp >/dev/null || fail 'Нужны tar и scp' || return 1
  [[ -f $SOURCE_DIR/install-vps.sh && -f $SOURCE_DIR/pyproject.toml ]] || fail 'Запустите скрипт из репозитория сервера' || return 1
  ssh_args
  local tmp archive remote_dir result=0
  tmp=$(mktemp -d) || return 1
  archive=$tmp/source.tar.gz
  tar -czf "$archive" -C "$SOURCE_DIR" pyproject.toml obsi_server install-vps.sh || { rm -rf "$tmp"; return 1; }
  remote_dir=$(remote 'mktemp -d /tmp/obsi-mcp.XXXXXXXX') || { rm -rf "$tmp"; return 1; }
  if [[ ! $remote_dir =~ ^/tmp/obsi-mcp\.[a-zA-Z0-9]+$ ]]; then
    rm -rf "$tmp"
    fail 'Неверный временный путь на VPS'
    return 1
  fi
  scp "${SCP_ARGS[@]}" "$archive" "$SSH_USER@$SSH_HOST:$remote_dir/source.tar.gz" || result=1
  if (( result == 0 )); then remote "tar -xzf '$remote_dir/source.tar.gz' -C '$remote_dir'" || result=1; fi
  if (( result == 0 )); then remote_tty "sudo env OBSI_PORT=$APP_PORT bash '$remote_dir/install-vps.sh'" || result=1; fi
  remote "rm -rf -- '$remote_dir'" || true
  rm -rf "$tmp"
  return "$result"
}

enable_key_auth() {
  check_config || return 1
  command -v ssh-keygen >/dev/null && command -v ssh-copy-id >/dev/null && command -v scp >/dev/null || fail 'Нужны ssh-keygen, ssh-copy-id и scp' || return 1
  local answer remote_dir client_ip result=0
  if [[ -z $KEY_PATH ]]; then
    KEY_PATH="$HOME/.ssh/id_ed25519_obsi_mcp"
  fi
  read -r -p "Путь к SSH-ключу [$KEY_PATH]: " answer || return 1
  KEY_PATH=${answer:-$KEY_PATH}
  [[ $KEY_PATH == /* ]] || fail 'Укажите абсолютный путь к ключу' || return 1
  if [[ ! -f $KEY_PATH ]]; then
    mkdir -p "$(dirname "$KEY_PATH")" || return 1
    chmod 0700 "$(dirname "$KEY_PATH")" || return 1
    ssh-keygen -t ed25519 -f "$KEY_PATH" -C 'obsi-mcp-vps' || return 1
  fi
  [[ -f $KEY_PATH.pub ]] || fail 'Нет публичного ключа (.pub)' || return 1
  echo 'Копирую публичный ключ на VPS. SSH может запросить текущий пароль.'
  ssh-copy-id -i "$KEY_PATH.pub" -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" || return 1
  ssh -o BatchMode=yes -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
    -o IdentitiesOnly=yes -i "$KEY_PATH" -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" true \
    || fail 'Вход по ключу не подтвердился; настройки SSH на VPS не менялись' || return 1
  save_config || return 1
  ssh_args
  echo 'Вход по ключу проверен.'
  read -r -p 'Отключить парольный SSH-вход на VPS для всех пользователей? [y/N]: ' answer || return 1
  [[ $answer == y || $answer == Y ]] || return 0
  [[ -f $SOURCE_DIR/scripts/enable-ssh-key-only.sh ]] || fail 'Не найден серверный SSH-скрипт' || return 1
  # Expansion happens on the VPS, where SSH_CONNECTION describes this login.
  # shellcheck disable=SC2016
  client_ip=$(remote 'printf "%s" "$SSH_CONNECTION"') || return 1
  client_ip=${client_ip%% *}
  [[ $client_ip =~ ^[0-9a-fA-F:.]+$ ]] || fail 'Не удалось определить IP текущего SSH-клиента' || return 1
  remote_dir=$(remote 'mktemp -d /tmp/obsi-mcp.XXXXXXXX') || return 1
  [[ $remote_dir =~ ^/tmp/obsi-mcp\.[a-zA-Z0-9]+$ ]] || fail 'Неверный временный путь на VPS' || return 1
  scp "${SCP_ARGS[@]}" "$SOURCE_DIR/scripts/enable-ssh-key-only.sh" "$SSH_USER@$SSH_HOST:$remote_dir/key-only.sh" || result=1
  if (( result == 0 )); then remote_tty "sudo bash '$remote_dir/key-only.sh' '$SSH_USER' '$client_ip'" || result=1; fi
  if (( result == 0 )); then
    ssh -o BatchMode=yes -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
      -o IdentitiesOnly=yes -i "$KEY_PATH" -p "$SSH_PORT" "$SSH_USER@$SSH_HOST" true \
      || { fail 'Повторный вход по ключу не прошёл. Через 2 минуты настройки SSH откатятся автоматически.'; result=1; }
  fi
  if (( result == 0 )); then
    remote_tty "sudo bash '$remote_dir/key-only.sh' --confirm" || result=1
  fi
  remote "rm -rf -- '$remote_dir'" || true
  if (( result == 0 )); then
    echo 'SSH настроен на вход только по ключу.'
  else
    echo 'Если изменение применилось, но не подтвердилось, прежние настройки SSH восстановятся через 2 минуты.' >&2
  fi
  return "$result"
}

load_config
while true; do
  printf '\nobsi-mcp VPS: %s@%s (SSH %s, MCP %s)\n' "$SSH_USER" "$SSH_HOST" "$SSH_PORT" "$APP_PORT"
  printf '%s\n' '1) Настроить подключение' '2) Установить / обновить сервер' '3) Статус сервиса' '4) Последние логи' '5) Показать токены' '6) Создать SSH-ключ / отключить вход по паролю' '0) Выход'
  read -r -p '> ' choice || break
  case $choice in
    1) configure || true ;;
    2)
      if deploy && [[ -z $KEY_PATH ]]; then
        read -r -p 'Настроить SSH-ключ для этого VPS? [y/N]: ' answer || continue
        if [[ $answer == y || $answer == Y ]]; then enable_key_auth || true; fi
      fi
      ;;
    3) if check_config; then ssh_args; remote 'systemctl status --no-pager obsi-mcp.service' || true; fi ;;
    4) if check_config; then ssh_args; remote_tty 'sudo journalctl -u obsi-mcp.service -n 80 --no-pager' || true; fi ;;
    5) if check_config; then ssh_args; remote_tty 'sudo cat /var/lib/obsi-mcp/TOKENS.txt' || true; fi ;;
    6) enable_key_auth || true ;;
    0) break ;;
    *) echo 'Неизвестный пункт' ;;
  esac
done
