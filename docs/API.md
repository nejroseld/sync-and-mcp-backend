# Sync and MCP — HTTP API v1 (контракт клиент ↔ сервер)

Базовый префикс: `/api/v1`. Авторизация: `Authorization: Bearer <token>`.
Ошибки: JSON `{"error": {"code": "<snake_case>", "message": "..."}}` и HTTP-статус
(401 нет/плохой/отозванный токен, 403 нет гранта, 404 нет объекта, 409 конфликт версий, 422 валидация).
Пути файлов в URL — percent-encoded, разделитель `/` допустим (`{path:path}`). CORS разрешён только
для явного списка origin (`OBSI_CORS_ORIGINS`, по умолчанию `app://obsidian.md`, `capacitor://localhost`,
`http://localhost`; `*` сервер не принимает), методы GET/PUT/POST/PATCH/DELETE/HEAD/OPTIONS, заголовки
Authorization, Content-Type, X-*; expose `ETag`, `X-*`. Плагин на мобильных ходит через `requestUrl`,
ему CORS не нужен. Все ответы идут с `Cache-Control: no-store`. `/docs`, `/redoc` и `/openapi.json`
выключены (включаются `OBSI_API_DOCS=1` для разработки).

## Общее

### `GET /api/v1/health` (без auth)
Без токена с внешнего адреса — `{"ok": true}`. С localhost или с любым действующим токеном:
`{"ok": true, "version": "0.1.0", "features": {"ai": true, "mcp": true, "rag": true}}`

### `GET /api/v1/me`
`{"token_id": "...", "name": "laptop", "kind": "device", "grants": {"v_xxx": ["read","write"]}, "account_token": false}`
Пользовательские токены также возвращают `user: {id, username, created_at}`.

### Пользовательские аккаунты
* `POST /api/v1/auth/register` `{"invite_code":"...","username":"alice","password":"..."}` → `{"token":"...","user":{"id":"u_...","username":"alice","created_at":...}}`. Код приглашения одноразовый и действует 7 дней (`OBSI_INVITE_TTL_DAYS`); отозванный или просроченный код → `401 invalid_invite`. Имя нормализуется (Unicode NFKC, пробелы по краям удаляются, регистр приводится к нижнему), пароль — от 12 до 1024 символов.
* `POST /api/v1/auth/login` `{"username":"alice","password":"..."}` → account session token и объект пользователя. `/api/v1/me` сообщает `account_token: true`.
  Session token живёт 180 дней (`OBSI_SESSION_TTL_DAYS`, поле `expires_at`) и гаснет после 30 дней без запросов (`OBSI_SESSION_IDLE_DAYS`); после этого нужен повторный вход (`401`). У пользователя живёт не больше 10 сессий: новый вход отзывает самые давно использованные.
* `POST /api/v1/vaults` с account session token `{"name":"Private"}` → создаёт принадлежащий пользователю vault. Session token имеет implicit owner access.
* `GET /api/v1/vaults` для account session показывает его vault'ы и vault'ы, явно выданные через grants.
* `GET /api/v1/tokens` → список собственных токенов, включая account sessions.
* `POST /api/v1/tokens` принимает `{"name":"Laptop","kind":"device"|"mcp","grants":{"v_xxx":["read","write"]}}` → токен, секрет которого показывается один раз. Grants разрешены только для принадлежащих пользователю vault'ов. Имена с префиксом `user:` зарезервированы за сессиями (`422 invalid_name`). Для MCP-ассистента рекомендуется `["list","search","read"]`, `write` — только если он должен предлагать правки.
* `PATCH /api/v1/tokens/{id}` принимает `{"name"?,"grants"?}` для собственного delegated token; `POST /api/v1/tokens/{id}/revoke` отзывает собственный токен.

Delegated device/MCP tokens имеют только явные grants, не могут создавать vault'ы или управлять токенами. Существующие админские `/api/v1/admin/tokens` и их поведение сохранены.

Admin управляет приглашениями (`kind=admin`): `POST /api/v1/admin/invites` `{"name":"signup"}` возвращает code единожды; `GET /api/v1/admin/invites` возвращает список без кодов с полями `expires_at`, `revoked_at` и `status` (`active`, `used`, `revoked`, `expired`); `POST /api/v1/admin/invites/{id}/revoke` отзывает неиспользованное приглашение.
`GET /api/v1/admin/users` возвращает зарегистрированных пользователей и количества принадлежащих им vault'ов и пользовательских токенов: `{"users":[{"id":"u_...","username":"alice","created_at":1730000000000,"vault_count":1,"token_count":2}]}`. Доступен только admin-токен.

### `GET /api/v1/vaults`
Vault'ы, на которые у токена есть хоть один грант (admin — все):
`{"vaults": [{"id": "v_xxx", "name": "Personal", "created_at": 1730000000000, "owner_user_id": "u_xxx", "rag": {"enabled": true}}]}`

## Admin (kind=admin)

* `POST /api/v1/admin/vaults` `{"name": "Work"}` → `{"id": "v_...", "name": "Work", ...}`
* `PATCH /api/v1/admin/vaults/{vid}` `{"name"?: str, "rag"?: {"enabled": bool, "chunk_chars"?: int, "chunk_overlap"?: int}}`
* `DELETE /api/v1/admin/vaults/{vid}` — удаляет блобы, AI Available, индекс, pending changes.
* `GET /api/v1/admin/tokens` → `{"tokens": [{id, name, kind, grants, created_at, revoked_at, last_used_at}]}`
* `POST /api/v1/admin/tokens` `{"name": "phone", "kind": "device"|"mcp"|"admin", "grants": {"v_xxx": ["read","write"]}}`
  → `{"id": "...", "token": "<plaintext, показывается один раз>", ...}`.
  Допустимые операции: device — `read`,`write`; mcp — `list`,`search`,`read`,`write`; admin — grants игнорируются.
  Пустой список операций прав не даёт; для ассистента обычно хватает `list`,`search`,`read`.
* `PATCH /api/v1/admin/tokens/{id}` `{"name"?, "grants"?}`
* `POST /api/v1/admin/tokens/{id}/revoke` → токен перестаёт работать немедленно.
* `GET /api/v1/admin/settings` / `PUT /api/v1/admin/settings`
  `{"embedding": {"base_url": "https://api.example.com/v1", "api_key": "...", "model": "text-embedding-3-small"}}`
  (в GET `api_key` маскируется). Смена `model` или `base_url` → перестройка всех индексов.
  `base_url` — только `https` на публичный адрес (без учётных данных в URL, без loopback, link-local,
  RFC 1918, ULA и metadata-адресов), иначе `422 invalid_embedding_url`. Адрес проверяется ещё раз при
  каждом подключении, редиректы и `HTTP_PROXY` не используются, ответ ограничен 16 MiB. `OBSI_EMBEDDING_ALLOW_PRIVATE=1`
  на сервере разрешает `http` и частные адреса (локальный Ollama).
* `POST /api/v1/admin/vaults/{vid}/reindex` → перестроить индекс vault'а.

## Sync storage (шифротекст, kind=device)

`key` — непрозрачная строка от `FakeFsEncrypt` (зашифрованный путь). Папки — ключи, оканчивающиеся на `/`.
Сервер ничего не знает о содержимом.

* `GET /api/v1/vaults/{vid}/files` (грант `read`) →
  `{"files": [{"key": "abc/def", "size": 123, "mtime_cli": 1730000000000, "ctime_cli": 1730000000000, "mtime_svr": 1730000000500, "etag": "<sha256 hex>"}]}`
* `GET /api/v1/vaults/{vid}/files/{key:path}` (`read`) → тело `application/octet-stream`, заголовки
  `ETag`, `X-Mtime-Cli`, `X-Ctime-Cli`, `X-Mtime-Svr`. 404 если нет.
* `HEAD` — то же без тела (stat).
* `PUT /api/v1/vaults/{vid}/files/{key:path}` (`write`) — тело = содержимое (для папки `key` с `/` на конце
  и пустое тело). Заголовки `X-Mtime-Cli`, `X-Ctime-Cli` (ms), а также необязательный
  `X-Obsi-Markdown: true|false`: плагин передаёт тип исходного файла, поскольку имя зашифровано.
  При настроенном S3 файлы с `false` размером больше порога хранятся в объектном хранилище;
  без настройки S3 или заголовка остаются локальными. Ответ — объект файла как в листинге.
* `DELETE /api/v1/vaults/{vid}/files/{key:path}` (`write`) → `204`. Удаление отсутствующего — тоже 204.
* Лимит размера тела настраивается (по умолчанию 200 MB). Тело пишется во временный файл, а не в память;
  одновременно принимается не больше `OBSI_MAX_CONCURRENT_UPLOADS` (4) тел больше 1 MiB, остальные ждут.
* Квота vault'а (`OBSI_VAULT_QUOTA_MB`, по умолчанию 10 GiB) считает шифротекст, AI Available и ожидающие
  MCP-правки. Запись сверх квоты отклоняется до записи: `507 {"error":{"code":"quota_exceeded","quota_bytes":...,"used_bytes":...}}`.

## AI Available (plaintext, kind=device, грант `write` для изменения, `read` для манифеста)

`path` — путь относительно корня vault'а в plaintext (например `Projects/idea.md`).

* `GET /api/v1/vaults/{vid}/ai/manifest` →
  `{"files": [{"path": "Projects/idea.md", "version": "<sha256 hex>", "mtime": 1730000000000, "size": 512, "kind": "note"|"attachment"}]}`
* `PUT /api/v1/vaults/{vid}/ai/files/{path:path}` — тело = содержимое; заголовки `X-Mtime` (ms),
  `Content-Type` (опц.). `kind` = `note` для `.md`, иначе `attachment`. `version` = sha256 тела.
  Если файл есть с другим `version` и бóльшим `mtime` → `409 {"error":{"code":"stale_publish"}}`.
  Ответ — объект манифеста. Ставит файл в очередь индексации. Лимит одного файла — `OBSI_MAX_AI_FILE_MB`
  (32 MiB). На vault индексируется не больше 200 000 чанков, остальное в поиск не попадает.
* `DELETE /api/v1/vaults/{vid}/ai/files/{path:path}` → `204`; синхронно удаляет из индекса.
* `POST /api/v1/vaults/{vid}/ai/clear` → удалить весь AI Available vault'а (выключение AI на клиенте).
* При настроенном S3 опубликованные вложения (любые файлы, кроме `.md`) размером больше
  порога тоже хранятся в объектном хранилище; манифест и MCP API не меняются.

## Pending changes (запись через MCP → клиент)

* `GET /api/v1/vaults/{vid}/changes?status=pending` (device, `write`) →
  `{"changes": [{"id": "c_...", "path": "Notes/x.md", "op": "create"|"update", "content": "<utf-8 text>", "base_version": "<sha256>"|null, "status": "pending", "created_at": 1730000000000, "token_name": "claude", "token_id": "tok_..."}]}`
  `token_id` — токен, который поставил правку; плагин показывает его пользователю перед применением.
* `POST /api/v1/vaults/{vid}/changes/{id}/ack` (device, `write`)
  `{"status": "applied"|"conflict"|"rejected", "message"?: str, "new_version"?: "<sha256>"}` → объект change.
  Повторный ack уже не-pending изменения → 409.

## MCP — `POST /mcp`

Streamable HTTP transport (MCP spec 2025-06-18; принимаем и `2025-03-26`, `2024-11-05` — отвечаем той
версией, что запросил клиент, если поддерживаем). JSON-RPC 2.0, ответ `application/json` (без SSE).
`GET /mcp` → 405. Авторизация: Bearer токен kind=mcp (401 с `WWW-Authenticate: Bearer` без токена).
Методы: `initialize`, `notifications/initialized` (→ 202 без тела), `ping`, `tools/list`, `tools/call`.
Ошибки инструментов возвращаются как `result: {content:[{type:"text",text:...}], isError: true}`.

Инструменты (все результаты: `content: [{type:"text", text: <JSON>}]` + `structuredContent`):

| tool | args | грант | результат |
|---|---|---|---|
| `list_vaults` | — | любой | `[{id, name, operations, rag_enabled, notes_count}]` |
| `list_notes` | `vault`, `prefix?`, `limit?=200`, `offset?` | `list` | `[{path, version, kind, size, mtime}]` |
| `search` | `query`, `mode?="hybrid"` (`text`/`semantic`/`hybrid`), `vaults?`, `limit?=10` | `search` | `[{vault, path, version, snippet, start_line, end_line, score}]` |
| `read_note` | `vault`, `path`, `start_line?`, `end_line?` (1-based, включительно) | `read` | `{vault, path, version, content, start_line, end_line, total_lines, pending_changes, truncated, next_start_line?}`; для картинки до 4 MiB — image content |
| `write_note` | `vault`, `path`, `content` (до 2 MiB), `base_version` (null = создать) | `write` | `{change_id, status:"pending"}`; конфликт → isError с текущей `version`; больше 500 ожидающих правок на vault → isError |
| `get_change_status` | `change_id` | `write` на vault изменения | `{id, status, message, new_version}` |

Поиск идёт только по vault'ам с грантом `search` и только по данным AI Available. Семантический режим
недоступен (→ текстовый, с пометкой), если embedding не настроен или RAG vault'а выключен.

## Заметки реализации сервера (уточнения к контракту)

* `ETag` в ответах storage — hex sha256 без кавычек (как `etag` в листинге).
* Admin-токен не читает данные напрямую: sync/AI/changes endpoints требуют `kind=device` (403 иначе),
  `/mcp` требует `kind=mcp` (403 иначе, 401 без токена). Admin-токен может выдать новый токен
  с доступом к любому vault, поэтому его нужно хранить как полный доступ к серверу.
* Дополнительные коды: `413 payload_too_large` (лимит тела), `507 quota_exceeded` (квота vault'а), `413 batch_too_large`
  (более 100 сообщений в одном MCP batch), `429 rate_limited` с заголовком `Retry-After`,
  `503 s3_unavailable` (для уже
  выгруженного объекта отсутствует настройка S3 или драйвер), `404 feature_disabled`
  (модуль выключен), `405` на `GET /mcp`.
* `stale_publish` (409) возвращает в `error` ещё `current_version` и `current_mtime`. PUT с тем же
  `version` не меняет данные (mtime обновляется, только если больше).
* `PATCH /admin/vaults/{vid}` меняет `chunk_chars`/`chunk_overlap` → чанки vault'а пересоздаются.
  `chunk_chars` — от 50 до 8000, `chunk_overlap` — от 0 и меньше `chunk_chars`, иначе `422`.
  Ответы vault'ов в admin включают `rag.chunk_chars` и `rag.chunk_overlap`.
* `GET /admin/settings` дополнительно возвращает `index: {chunks, embedded, pending, last_error}`;
  `api_key` маскируется как `****abcd`, при PUT маскированное значение означает «не менять».
  `PUT` принимает частичный объект `embedding`.
* Пути AI Available: относительные, без `..`, пустых сегментов и `\` (иначе 422 `invalid_path`).
* MCP: если результат инструмента — массив, `structuredContent` = `{"result": [...]}` (требование спеки:
  объект), а текстовый блок содержит сам массив. `search` при фолбэке добавляет второй текстовый блок
  `Note: ...` и поле `note` в `structuredContent`. Инструмент `search` не показывается, если
  `features.rag=false`. Аргумент `vault` принимает id или (однозначное) имя vault'а среди доступных.
* Ошибки конфликта `write_note` — `isError` с JSON `{error, code:"conflict", version}`.
* `write_note` принимает только заметки `.md` (плагин применяет только их).
* `read_note` для не-картинок бинарных вложений возвращает только метаданные; для картинок до 4 MiB —
  текстовый блок с метаданными и блок `image`, для больших картинок только метаданные. Для текста
  `start_line`/`end_line` в ответе — фактические границы (при пустом файле 0/0), `content` сохраняет
  исходные переводы строк. Без `end_line` отдаётся не больше 2000 строк; любой ответ — не больше 256 KiB
  целыми строками. Если отдано не всё, `truncated: true` и `next_start_line` указывает, откуда читать дальше.
* Текстовый блок ответов `read_note`, `search` и `list_notes` обёрнут: сначала строка-предупреждение, что
  дальше данные vault'а, не инструкции, затем JSON между `<untrusted-vault-data>` и
  `</untrusted-vault-data>`. Символ `<` внутри JSON экранирован как `\u003c`, поэтому закрывающий тег
  нельзя подделать содержимым заметки. `structuredContent` остаётся без обёртки.
* `list_notes` — `limit` максимум 1000. `search.limit` максимум 100.
* Пакетные JSON-RPC запросы (массив) поддерживаются.
* Токен: `obsi_` + urlsafe-строка; id токена `tok_...`; id изменения `c_...`.
