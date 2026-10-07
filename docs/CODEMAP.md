# Карта кода сервера

- `obsi_server/app.py` — FastAPI, REST API, MCP endpoint и обработка ошибок.
- `obsi_server/cli.py`, `config.py` — команды запуска и конфигурация.
- `obsi_server/db.py`, `vaults.py`, `auth.py` — SQLite, vault'ы, токены и гранты.
- `obsi_server/storage.py` — хранилище зашифрованных файлов.
- `obsi_server/uploads.py` — приём тел во временный файл, блокировки по ключу, квота vault'а.
- `obsi_server/ai_store.py`, `changes.py` — AI Available и очередь правок.
- `obsi_server/rag.py`, `mcp.py` — поиск и MCP-инструменты.
- `tests/` — тесты API и MCP; `local.sh` — запуск на локальной машине.

Формат запросов и ответов описан в [API.md](API.md).
