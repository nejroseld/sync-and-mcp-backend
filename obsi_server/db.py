"""SQLite wrapper: one shared connection (WAL), a lock, and versioned migrations."""
from __future__ import annotations

import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence


def now_ms() -> int:
    return int(time.time() * 1000)


MIGRATIONS: list[str] = [
    """
    CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE vaults(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at INTEGER NOT NULL,
        rag_enabled INTEGER NOT NULL DEFAULT 1,
        chunk_chars INTEGER NOT NULL DEFAULT 1200,
        chunk_overlap INTEGER NOT NULL DEFAULT 200);
    CREATE TABLE tokens(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE, grants TEXT NOT NULL DEFAULT '{}',
        created_at INTEGER NOT NULL, revoked_at INTEGER, last_used_at INTEGER);
    CREATE TABLE blobs(
        vault_id TEXT NOT NULL, key TEXT NOT NULL, size INTEGER NOT NULL,
        mtime_cli INTEGER, ctime_cli INTEGER, mtime_svr INTEGER NOT NULL, etag TEXT NOT NULL,
        PRIMARY KEY(vault_id, key));
    CREATE TABLE ai_files(
        vault_id TEXT NOT NULL, path TEXT NOT NULL, version TEXT NOT NULL,
        mtime INTEGER NOT NULL, size INTEGER NOT NULL, kind TEXT NOT NULL, content BLOB NOT NULL,
        PRIMARY KEY(vault_id, path));
    CREATE TABLE changes(
        id TEXT PRIMARY KEY, vault_id TEXT NOT NULL, path TEXT NOT NULL, op TEXT NOT NULL,
        content TEXT NOT NULL, base_version TEXT, status TEXT NOT NULL DEFAULT 'pending',
        message TEXT, new_version TEXT, created_at INTEGER NOT NULL,
        token_name TEXT, token_id TEXT);
    CREATE INDEX changes_vault ON changes(vault_id, status);
    CREATE TABLE chunks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, vault_id TEXT NOT NULL, path TEXT NOT NULL,
        version TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
        text TEXT NOT NULL, embedding BLOB, emb_key TEXT);
    CREATE INDEX chunks_vp ON chunks(vault_id, path);
    CREATE VIRTUAL TABLE chunks_fts USING fts5(text);
    """,
    """
    CREATE TABLE users(
        id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
        created_at INTEGER NOT NULL);
    CREATE TABLE invites(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, code_hash TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL, used_at INTEGER);
    ALTER TABLE tokens ADD COLUMN user_id TEXT REFERENCES users(id);
    ALTER TABLE vaults ADD COLUMN owner_user_id TEXT REFERENCES users(id);
    CREATE INDEX vault_owner ON vaults(owner_user_id);
    """,
    """
    ALTER TABLE blobs ADD COLUMN backend TEXT NOT NULL DEFAULT 'local';
    """,
    """
    ALTER TABLE ai_files ADD COLUMN backend TEXT NOT NULL DEFAULT 'local';
    """,
    # Login/register sessions are exactly 'user:' + the owner's username, kind=device, no grants.
    """
    ALTER TABLE tokens ADD COLUMN is_session INTEGER NOT NULL DEFAULT 0;
    UPDATE tokens SET is_session=1 WHERE user_id IS NOT NULL AND kind='device' AND grants='{}'
        AND name = 'user:' || (SELECT username FROM users WHERE users.id=tokens.user_id);
    """,
    """
    CREATE TABLE rate_limits(key TEXT PRIMARY KEY, tat_ms INTEGER NOT NULL);
    CREATE INDEX rate_limits_expiry ON rate_limits(tat_ms);
    """,
    # Databases upgraded with the old prefix-based is_session migration may hold promoted
    # delegated tokens; demote every session that does not match the login/register shape.
    """
    UPDATE tokens SET is_session=0 WHERE is_session=1 AND NOT (
        user_id IS NOT NULL AND kind='device' AND grants='{}'
        AND name = 'user:' || COALESCE((SELECT username FROM users WHERE users.id=tokens.user_id), ''));
    ALTER TABLE tokens ADD COLUMN expires_at INTEGER;
    UPDATE tokens SET expires_at = CAST(strftime('%s','now') AS INTEGER) * 1000 + 180 * 86400000
        WHERE is_session=1 AND revoked_at IS NULL;
    CREATE INDEX tokens_user_session ON tokens(user_id, is_session);
    ALTER TABLE invites ADD COLUMN expires_at INTEGER;
    ALTER TABLE invites ADD COLUMN revoked_at INTEGER;
    UPDATE invites SET expires_at = CAST(strftime('%s','now') AS INTEGER) * 1000 + 7 * 86400000
        WHERE used_at IS NULL;
    """,
]


class Database:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The database contains token hashes, password hashes, and configuration secrets.
        if stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
            raise PermissionError(f"data directory {path.parent} must be owner-only (chmod 700)")
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        path.chmod(0o600)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self._migrate()

    def _migrate(self) -> None:
        with self.lock:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            for i in range(version, len(MIGRATIONS)):
                try:
                    self.conn.executescript(f"BEGIN IMMEDIATE;\n{MIGRATIONS[i]}\nPRAGMA user_version={i + 1};\nCOMMIT;")
                except BaseException:
                    if self.conn.in_transaction:
                        self.conn.execute("ROLLBACK")
                    raise

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def get_setting(self, key: str, default: Any = None) -> Any:
        import json

        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key: str, value: Any) -> None:
        import json

        self.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def close(self) -> None:
        with self.lock:
            self.conn.close()
