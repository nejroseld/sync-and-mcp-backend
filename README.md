# obsi-mcp server

Сервер для плагина Obsidian: хранит зашифрованные blob'ы vault'ов (sync), принимает опубликованный
plaintext «AI Available», отдаёт его по MCP (streamable HTTP) и строит поиск (FTS5 + эмбеддинги).
Контракт описан в `../docs/API.md`, архитектура в `../docs/ARCHITECTURE.md`.

Зависимости: Python 3.11+, `fastapi`, `uvicorn`, `httpx`. Всё остальное стандартная библиотека
(sqlite3 с FTS5). `numpy` необязателен: если установлен, косинус считается быстрее.

## Запуск

```bash
cd server
python3 -m obsi_server serve --host 0.0.0.0 --port 8765
# или: pip install -e . && obsi-server serve
```

Конфигурация через переменные окружения (или TOML-файл, путь в `OBSI_CONFIG`, секция `[server]`):

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `OBSI_DATA_DIR` | `./obsi-data` | sqlite-база и блобы (`vaults/<vid>/blobs/<sha256(key)>`) |
| `OBSI_HOST` / `OBSI_PORT` | `127.0.0.1` / `8765` | адрес |
| `OBSI_FEATURES` | `ai,mcp,rag` | включённые модули (`all`, `none`); `mcp` и `rag` требуют `ai` |
| `OBSI_MAX_BODY_MB` | `200` | лимит тела запроса |
| `OBSI_RAG_WORKER` | `1` | фоновый поток эмбеддингов |
| `OBSI_CORS_ORIGINS` | `*` | список через запятую |

Пример TOML: `[server]\ndata_dir="/srv/obsi"\nfeatures=["ai","mcp"]\nmax_body_mb=500`.

Сервер говорит по HTTP; для доступа из интернета ставьте за reverse proxy с TLS.

## Быстрый старт

```bash
cd server
python3 -m obsi_server admin create-token --kind admin --name root          # админ-токен (показывается один раз)
python3 -m obsi_server admin create-vault --name Personal                   # -> id вида v_xxxxxxxxxxxxxxxx
python3 -m obsi_server admin create-token --kind device --name laptop --vault v_XXX --ops read,write
python3 -m obsi_server admin create-token --kind mcp --name claude --vault v_XXX --ops list,search,read,write
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
cd server && python3 -m unittest discover -s tests -v
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
