# obsi-mcp — HTTP API v1 (контракт клиент ↔ сервер)

Базовый префикс: `/api/v1`. Авторизация: `Authorization: Bearer <token>`.
Ошибки: JSON `{"error": {"code": "<snake_case>", "message": "..."}}` и HTTP-статус
(401 нет/плохой/отозванный токен, 403 нет гранта, 404 нет объекта, 409 конфликт версий, 422 валидация).
Пути файлов в URL — percent-encoded, разделитель `/` допустим (`{path:path}`). CORS открыт
(`*`, методы GET/PUT/POST/DELETE/OPTIONS, заголовки Authorization, Content-Type, X-*; expose `ETag`, `X-*`) —
плагин на мобильных ходит через `requestUrl`, но CORS не мешает.

## Общее

### `GET /api/v1/health` (без auth)
`{"ok": true, "version": "0.1.0", "features": {"ai": true, "mcp": true, "rag": true}}`

### `GET /api/v1/me`
`{"token_id": "...", "name": "laptop", "kind": "device", "grants": {"v_xxx": ["read","write"]}}`

### `GET /api/v1/vaults`
Vault'ы, на которые у токена есть хоть один грант (admin — все):
`{"vaults": [{"id": "v_xxx", "name": "Personal", "created_at": 1730000000000, "rag": {"enabled": true}}]}`

## Admin (kind=admin)

* `POST /api/v1/admin/vaults` `{"name": "Work"}` → `{"id": "v_...", "name": "Work", ...}`
* `PATCH /api/v1/admin/vaults/{vid}` `{"name"?: str, "rag"?: {"enabled": bool, "chunk_chars"?: int, "chunk_overlap"?: int}}`
* `DELETE /api/v1/admin/vaults/{vid}` — удаляет блобы, AI Available, индекс, pending changes.
* `GET /api/v1/admin/tokens` → `{"tokens": [{id, name, kind, grants, created_at, revoked_at, last_used_at}]}`
* `POST /api/v1/admin/tokens` `{"name": "phone", "kind": "device"|"mcp"|"admin", "grants": {"v_xxx": ["read","write"]}}`
  → `{"id": "...", "token": "<plaintext, показывается один раз>", ...}`.
  Допустимые операции: device — `read`,`write`; mcp — `list`,`search`,`read`,`write`; admin — grants игнорируются.
* `PATCH /api/v1/admin/tokens/{id}` `{"name"?, "grants"?}`
* `POST /api/v1/admin/tokens/{id}/revoke` → токен перестаёт работать немедленно.
* `GET /api/v1/admin/settings` / `PUT /api/v1/admin/settings`
  `{"embedding": {"base_url": "http://localhost:11434/v1", "api_key": "...", "model": "nomic-embed-text"}}`
  (в GET `api_key` маскируется). Смена `model` или `base_url` → перестройка всех индексов.
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
  и пустое тело). Заголовки `X-Mtime-Cli`, `X-Ctime-Cli` (ms). Ответ — объект файла как в листинге.
* `DELETE /api/v1/vaults/{vid}/files/{key:path}` (`write`) → `204`. Удаление отсутствующего — тоже 204.
* Лимит размера тела настраивается (по умолчанию 200 MB).

## AI Available (plaintext, kind=device, грант `write` для изменения, `read` для манифеста)

`path` — путь относительно корня vault'а в plaintext (например `Projects/idea.md`).

* `GET /api/v1/vaults/{vid}/ai/manifest` →
  `{"files": [{"path": "Projects/idea.md", "version": "<sha256 hex>", "mtime": 1730000000000, "size": 512, "kind": "note"|"attachment"}]}`
* `PUT /api/v1/vaults/{vid}/ai/files/{path:path}` — тело = содержимое; заголовки `X-Mtime` (ms),
  `Content-Type` (опц.). `kind` = `note` для `.md`, иначе `attachment`. `version` = sha256 тела.
  Если файл есть с другим `version` и бóльшим `mtime` → `409 {"error":{"code":"stale_publish"}}`.
  Ответ — объект манифеста. Ставит файл в очередь индексации.
* `DELETE /api/v1/vaults/{vid}/ai/files/{path:path}` → `204`; синхронно удаляет из индекса.
* `POST /api/v1/vaults/{vid}/ai/clear` → удалить весь AI Available vault'а (выключение AI на клиенте).

## Pending changes (запись через MCP → клиент)

* `GET /api/v1/vaults/{vid}/changes?status=pending` (device, `write`) →
  `{"changes": [{"id": "c_...", "path": "Notes/x.md", "op": "create"|"update", "content": "<utf-8 text>", "base_version": "<sha256>"|null, "status": "pending", "created_at": 1730000000000, "token_name": "claude"}]}`
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
| `read_note` | `vault`, `path`, `start_line?`, `end_line?` (1-based, включительно) | `read` | `{vault, path, version, content, start_line, end_line, total_lines, pending_changes}`; для картинки — image content |
| `write_note` | `vault`, `path`, `content`, `base_version` (null = создать) | `write` | `{change_id, status:"pending"}`; конфликт → isError с текущей `version` |
| `get_change_status` | `change_id` | `write` на vault изменения | `{id, status, message, new_version}` |

Поиск идёт только по vault'ам с грантом `search` и только по данным AI Available. Семантический режим
недоступен (→ текстовый, с пометкой), если embedding не настроен или RAG vault'а выключен.

## Заметки реализации сервера (уточнения к контракту)

* `ETag` в ответах storage — hex sha256 без кавычек (как `etag` в листинге).
* Admin-токен не имеет доступа к данным: sync/AI/changes endpoints требуют `kind=device` (403 иначе);
  `/mcp` требует `kind=mcp` (403 иначе, 401 без токена).
* Дополнительные коды: `413 payload_too_large` (лимит тела), `404 feature_disabled`
  (модуль выключен), `405` на `GET /mcp`.
* `stale_publish` (409) возвращает в `error` ещё `current_version` и `current_mtime`. PUT с тем же
  `version` не меняет данные (mtime обновляется, только если больше).
* `PATCH /admin/vaults/{vid}` меняет `chunk_chars`/`chunk_overlap` → чанки vault'а пересоздаются.
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
* `read_note` для не-картинок бинарных вложений возвращает только метаданные; для картинок — текстовый
  блок с метаданными и блок `image`. Для текста `start_line`/`end_line` в ответе — фактические границы
  (при пустом файле 0/0), `content` сохраняет исходные переводы строк.
* `list_notes` — `limit` максимум 1000. `search.limit` максимум 100.
* Пакетные JSON-RPC запросы (массив) поддерживаются.
* Токен: `obsi_` + urlsafe-строка; id токена `tok_...`; id изменения `c_...`.
