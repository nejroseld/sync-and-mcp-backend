# obsi-mcp server

Сервер для плагина Obsidian: хранит зашифрованные blob'ы vault'ов (sync), принимает опубликованный
plaintext «AI Available», отдаёт его по MCP (streamable HTTP) и строит поиск (FTS5 + эмбеддинги).
Контракт описан в `docs/API.md`, архитектура в `docs/ARCHITECTURE.md`.

Зависимости: Python 3.11+, `fastapi`, `uvicorn`, `httpx`. Для S3 установите `pip install -e '.[s3]'`.
Всё остальное стандартная библиотека
(sqlite3 с FTS5). `numpy` необязателен: если установлен, косинус считается быстрее.

## Установка на Ubuntu VPS

### С компьютера пользователя (рекомендуется)

Нужны VPS с Ubuntu 22.04 или новее и данные для входа из письма хостинга. На компьютере
должны быть `ssh` и `tar` (в macOS и Linux они уже есть; в Windows запускайте из WSL или Git Bash).
Из каталога репозитория:

```bash
./manage-vps.sh                 # или сразу с адресом: ./manage-vps.sh root@203.0.113.10
```

Первый запуск проводит через четыре шага:

1. **Сервер.** Адрес можно вставить в любом виде: `203.0.113.10`, `ubuntu@host:2222`,
   `ssh root@host -p 2222`. Пользователь по умолчанию `root`, порт `22`.
2. **Подключение.** Пароль SSH вводится один раз: скрипт держит одно общее соединение
   на всю сессию. Если пользователь не `root`, один раз запрашивается пароль `sudo` (он
   хранится только в памяти скрипта). Затем скрипт создаёт ключ `~/.ssh/obsi_mcp_ed25519`
   и добавляет его на сервер, поэтому следующие запуски обходятся без пароля. Если сервер
   пускает только по ключу, скрипт попросит путь к ключу от хостинга.
3. **HTTPS-адрес.** По Enter сервер получает бесплатный адрес вида `203-0-113-10.sslip.io`
   (DNS-сервис, который возвращает IP из имени), свой домен с A-записью на IP сервера тоже
   подойдёт. Скрипт проверяет DNS, ставит Caddy и получает сертификат Let's Encrypt
   (`scripts/setup-https.sh`; при активном `ufw` открывает порты 80 и 443). Ответ «нет»
   оставляет сервер без HTTPS: тогда он доступен только через SSH-туннель из меню.
4. **Установка.** Код загружается на VPS и ставится через `install-vps.sh` без вопросов
   (S3 не настраивается). В конце скрипт проверяет `/api/v1/health` и показывает три токена:
   токен пользователя с адресом сервера для плагина Obsi Sync, MCP-токен с URL и заголовком
   `Authorization` для любого MCP-клиента (с примером для Claude Code) и токен
   администратора. Там же есть инструкция, как выпускать новые токены и приглашения
   пользователей в плагине: Obsi Sync → «Сервер» → «Доступ администратора».

Последующие запуски открывают меню. По Enter выполняется пункт по умолчанию (обновление
сервера). В меню также есть данные для подключения, состояние и логи, домен и HTTPS,
SSH-туннель, S3, отключение входа по паролю и переход на другой сервер. При обновлении
настроенный S3 сохраняется. Параметры подключения хранятся в `~/.config/obsi-mcp/vps.conf`
с правами `0600`, пароли туда не пишутся. Журнал последней ошибки сохраняется в
`~/.config/obsi-mcp/last-error.log`. Переменная `NO_COLOR=1` отключает цвета и анимацию.

Язык интерфейса берётся из `--lang ru|en` (запоминается в `vps.conf`), переменной `OBSI_LANG`
или системной локали; по умолчанию английский. Строки лежат в `scripts/i18n/<язык>.sh`.
Чтобы добавить язык, скопируйте `en.sh` в `<код>.sh` и переведите значения; ключи, которых
нет в переводе, показываются по-английски.

Пункт «Вход на сервер только по ключу» отключает парольную и keyboard-interactive
SSH-аутентификацию для всех пользователей VPS; включайте его, только если у остальных
тоже настроен вход по ключу. После изменения скрипт проверяет новый SSH-сеанс. Если
подтвердить вход не удаётся, прежние настройки SSH восстановятся через две минуты.

### Непосредственно на VPS

Из каталога этого репозитория:

```bash
sudo ./install-vps.sh
# другой локальный порт: sudo env OBSI_PORT=9000 ./install-vps.sh
```

Скрипт устанавливает Python 3.11+ и зависимости в `/opt/obsi-mcp/venv`, создаёт отдельного
системного пользователя и сервис `obsi-mcp.service`. Данные хранятся в `/var/lib/obsi-mcp`.
При первой установке создаются vault `Personal` и токены; прочитать их можно командой
`sudo cat /var/lib/obsi-mcp/TOKENS.txt`. Повторный запуск обновляет приложение и сервис, сохраняя
данные и ранее выданные токены. Настройки S3 и необязательные ключи доступа сохраняются
в `/etc/obsi-mcp/s3.env` с правами `0600`; при отсутствии этого файла используется только
локальное хранилище.

Проверка: `curl http://127.0.0.1:8765/api/v1/health`; состояние:
`sudo systemctl status obsi-mcp`; логи: `sudo journalctl -u obsi-mcp -f`.
Сервис слушает только `127.0.0.1`. Для доступа с другого устройства настройте reverse proxy
с HTTPS на VPS и укажите его публичный URL в плагине и MCP-клиенте (`/mcp`).

## Самый быстрый запуск: `./local.sh`

```bash
./local.sh
```

При первом запуске создаёт `.venv`, ставит зависимости, заводит vault `Personal`, admin-, device- и
MCP-токены и пишет их в `data/TOKENS.txt` (вместе с командой для Claude Code и командами отладки).
Затем запускает сервер на `http://127.0.0.1:8766`. Повторные запуски просто стартуют сервер.
Переопределить: `OBSI_PORT=9000 OBSI_HOST=0.0.0.0 OBSI_DATA_DIR=~/obsi-data ./local.sh`.
Для локального S3 задайте `OBSI_S3_BUCKET` (или `s3_bucket` в файле `OBSI_CONFIG`) и при необходимости `OBSI_S3_REGION`,
`OBSI_S3_ENDPOINT`, `OBSI_S3_PREFIX`, `OBSI_S3_THRESHOLD_MB` и стандартные переменные
учётных данных AWS перед запуском `./local.sh`.

## Запуск на своей машине (пошагово)

Сценарий: сервер и Obsidian на одном компьютере. Нужен Python 3.11+ (`python3 --version`).

**1. Забрать код** (из Git-репозитория сервера или скопировать его каталог, например с ВМ):

```bash
scp -r ai@192.168.122.187:/path/to/obsi-server ~/obsi-server
cd ~/obsi-server
```

Если скопировать вместе с `data/`, сохранятся уже созданные vault'ы и токены
(см. `data/TOKENS.txt`); тогда шаг 4 можно пропустить.

**2. Установить зависимости в venv:**

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .            # fastapi, uvicorn, httpx; numpy по желанию: pip install -e '.[fast]'
```

**3. Запустить сервер** (терминал оставить открытым, остановка — Ctrl+C):

```bash
obsi-server --data-dir ./data serve --port 8766
# проверка из другого терминала:
curl http://127.0.0.1:8766/api/v1/health
```

По умолчанию сервер слушает только `127.0.0.1` — для Obsidian на той же машине этого достаточно.
Для телефона в локальной сети: `--host 0.0.0.0` и открыть порт в firewall
(`sudo firewall-cmd --add-port=8766/tcp`).

**4. Создать vault и токены** (в другом терминале, с тем же `--data-dir`; сервер можно не
останавливать):

```bash
. .venv/bin/activate
obsi-server --data-dir ./data admin create-token --kind admin --name root
obsi-server --data-dir ./data admin create-vault --name Personal          # -> v_XXX
obsi-server --data-dir ./data admin create-token --kind device --name laptop --vault v_XXX --ops read,write
obsi-server --data-dir ./data admin create-token --kind mcp --name claude --vault v_XXX --ops list,search,read,write
```

Токены показываются один раз — сохраните их.

**5. Плагин в Obsidian:** в отдельном репозитории плагина выполните `npm install && npm run build`,
затем скопируйте `main.js`, `manifest.json`, `styles.css` в `<vault>/.obsidian/plugins/obsi-sync/`,
включить в Settings → Community plugins. В настройках плагина: Server URL `http://127.0.0.1:8766`,
device-токен, mount с пустым путём → vault `Personal`, свой пароль шифрования.

**6. MCP в Claude Code:**

```bash
claude mcp add --transport http obsi http://127.0.0.1:8766/mcp --header "Authorization: Bearer <mcp-токен>"
```

**Отладка:** `curl -v http://127.0.0.1:8766/api/v1/health` — сервер жив;
`curl -H "Authorization: Bearer <device-токен>" http://127.0.0.1:8766/api/v1/me` — токен валиден.
`connection refused` — сервер не запущен или другой порт; `401` — неверный/отозванный токен.

## Запуск (кратко)

```bash
python3 -m obsi_server --data-dir ./data serve --host 0.0.0.0 --port 8765
# или: pip install -e . && obsi-server --data-dir ./data serve
```

Все команды (`serve` и `admin ...`) должны использовать один и тот же каталог данных
(`--data-dir` или `OBSI_DATA_DIR`; по умолчанию `./obsi-data` относительно текущей папки).

Конфигурация через переменные окружения (или TOML-файл, путь в `OBSI_CONFIG`, секция `[server]`):

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `OBSI_DATA_DIR` | `./obsi-data` | sqlite-база и блобы (`vaults/<vid>/blobs/<sha256(key)>`) |
| `OBSI_HOST` / `OBSI_PORT` | `127.0.0.1` / `8765` | адрес |
| `OBSI_FEATURES` | `ai,mcp,rag` | включённые модули (`all`, `none`); `mcp` и `rag` требуют `ai` |
| `OBSI_MAX_BODY_MB` | `200` | лимит тела запроса |
| `OBSI_RAG_WORKER` | `1` | фоновый поток эмбеддингов |
| `OBSI_CORS_ORIGINS` | `*` | список через запятую |
| `OBSI_S3_BUCKET` | не задан | бакет для вложений больше порога; нужны учётные данные boto3 |
| `OBSI_S3_THRESHOLD_MB` | `10` | порог выгрузки файлов, кроме `.md` |
| `OBSI_S3_REGION` / `OBSI_S3_ENDPOINT` / `OBSI_S3_PREFIX` | не задан / не задан / `obsi` | параметры S3 |

Пример TOML: `[server]\ndata_dir="/srv/obsi"\nfeatures=["ai","mcp"]\nmax_body_mb=500`.

Сервер говорит по HTTP; для доступа из интернета ставьте за reverse proxy с TLS.
Настройка S3 и поведение файлов описаны в [docs/S3.md](docs/S3.md).

Для выдачи пользователю доступа администратор создаёт одноразовое приглашение в плагине
или через `POST /api/v1/admin/invites`. Пользователь вводит код, логин и пароль в мастере
подключения плагина и затем создаёт собственный vault. Старые device-токены и гранты
продолжают действовать; подробности в [docs/API.md](docs/API.md).

## Быстрый старт

```bash
python3 -m obsi_server --data-dir ./data admin create-token --kind admin --name root          # админ-токен (показывается один раз)
python3 -m obsi_server --data-dir ./data admin create-vault --name Personal                   # -> id вида v_xxxxxxxxxxxxxxxx
python3 -m obsi_server --data-dir ./data admin create-token --kind device --name laptop --vault v_XXX --ops read,write
python3 -m obsi_server --data-dir ./data admin create-token --kind mcp --name claude --vault v_XXX --ops list,search,read,write
```

Device-токен и id vault'а вводятся в плагине. Дальше всё то же доступно через admin REST API
(`/api/v1/admin/...`) с admin-токеном.

Другие команды: `list-vaults`, `list-tokens`, `revoke TOKEN_ID`, `grant TOKEN_ID --vault V --ops a,b`,
`rag-enable V`, `rag-disable V`, `reindex V`,
`set-embedding --base-url http://localhost:11434/v1 --api-key KEY --model nomic-embed-text`.
Смена `model` или `base-url` сбрасывает векторы, индекс перестраивается в фоне (работающий сервер
подхватывает изменения из CLI в течение ~10 секунд).

В эмбеддинг-эндпоинт отправляются только тексты из AI Available vault'ов с включённым RAG.
Без настроенных эмбеддингов (или при выключенном RAG vault'а) `search` работает как текстовый и
сообщает об этом в поле `note`.

## Подключение MCP-клиентов

Endpoint: `http://HOST:8765/mcp`, авторизация `Authorization: Bearer <mcp-токен>`.

```bash
claude mcp add --transport http obsi http://HOST:8765/mcp --header "Authorization: Bearer obsi_..."
```

Claude Desktop (через `claude_desktop_config.json`, мост `mcp-remote`):

```json
{ "mcpServers": { "obsi": { "command": "npx",
  "args": ["mcp-remote", "http://HOST:8765/mcp", "--header", "Authorization: Bearer obsi_..."] } } }
```

Либо добавьте сервер как custom connector в настройках клиента, если он поддерживает заголовок
Authorization.

## Тесты

```bash
python3 -m unittest discover -s tests -v
```

## Smoke-тест (выполнялся вручную)

```bash
export OBSI_DATA_DIR=/tmp/obsi-smoke
python3 -m obsi_server serve --port 18765 &
curl -s http://127.0.0.1:18765/api/v1/health
# {"ok":true,"version":"0.1.0","features":{"ai":true,"mcp":true,"rag":true}}
curl -s -X POST http://127.0.0.1:18765/mcp -H "Authorization: Bearer $MCP" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
# {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18","capabilities":{"tools":{"listChanged":false}},"serverInfo":{"name":"obsi-mcp",...}}}
curl -s -X POST http://127.0.0.1:18765/mcp -H "Authorization: Bearer $MCP" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
# list_vaults, list_notes, search, read_note, write_note, get_change_status
```

Проверено также: PUT в `ai/files/...` и затем `search` через `tools/call` находит заметку.

## Устройство

`config`, `db` (sqlite WAL, миграции по `user_version`), `auth`, `vaults`, `storage`, `ai_store`,
`changes`, `rag`, `mcp`, `app` (REST + сборка), `cli`. Модули связаны хуками: `ai_store` сообщает
`rag` о PUT/DELETE/clear (удаление из индекса синхронное), `vaults` вызывает purge при удалении vault'а.
Чанки и FTS пишутся синхронно при публикации, эмбеддинги догоняет фоновый воркер
(батчи по 32, таймаут 60 с, при ошибке повтор с backoff, сервер не падает).
