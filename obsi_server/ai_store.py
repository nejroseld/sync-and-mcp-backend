"""AI Available: plaintext files published by clients, one set per vault."""
from __future__ import annotations

import hashlib
import posixpath
from typing import Callable

from .db import Database, now_ms
from .errors import ApiError
from .storage import Storage

TEXT_EXT = {".md", ".txt", ".csv"}
IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp"}


def ext(path: str) -> str:
    return posixpath.splitext(path)[1].lower()


def kind_of(path: str) -> str:
    return "note" if ext(path) == ".md" else "attachment"


def is_text(path: str) -> bool:
    return ext(path) in TEXT_EXT


def norm_path(path: str) -> str:
    p = path.strip()
    parts = p.split("/")
    if not p or p.startswith("/") or "\\" in p or "\x00" in p or any(s in ("", ".", "..") for s in parts):
        raise ApiError(422, "invalid_path", "path must be relative, without '..' or empty segments")
    return p


def _meta(r) -> dict:
    return {"path": r["path"], "version": r["version"], "mtime": r["mtime"], "size": r["size"], "kind": r["kind"]}


class AiStore:
    def __init__(self, db: Database, storage: Storage | None = None):
        self.db = db
        self.storage = storage
        self.on_put: list[Callable[[str, dict, bytes], None]] = []
        self.on_delete: list[Callable[[str, str], None]] = []
        self.on_clear: list[Callable[[str], None]] = []

    def manifest(self, vid: str, prefix: str = "", limit: int | None = None, offset: int = 0) -> list[dict]:
        sql = ("SELECT path,version,mtime,size,kind FROM ai_files WHERE vault_id=? "
               "AND substr(path,1,length(?))=? ORDER BY path")
        params: list = [vid, prefix, prefix]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        return [_meta(r) for r in self.db.query(sql, params)]

    def meta(self, vid: str, path: str) -> dict | None:
        r = self.db.one("SELECT path,version,mtime,size,kind FROM ai_files WHERE vault_id=? AND path=?", (vid, path))
        return _meta(r) if r else None

    def get(self, vid: str, path: str) -> tuple[dict, bytes] | None:
        r = self.db.one("SELECT * FROM ai_files WHERE vault_id=? AND path=?", (vid, path))
        if not r:
            return None
        if r["backend"] == "s3":
            if self.storage is None:
                raise RuntimeError("AI attachment is stored in S3 but storage is unavailable")
            return _meta(r), self.storage.s3_get_object(self._s3_key(vid, path, r["version"]))
        return _meta(r), bytes(r["content"])

    @staticmethod
    def _s3_key(vid: str, path: str, version: str) -> str:
        return f"ai/{vid}/{hashlib.sha256(path.encode()).hexdigest()}/{version}"

    def _delete_s3(self, vid: str, path: str, version: str, backend: str) -> None:
        if backend == "s3":
            if self.storage is None:
                raise RuntimeError("AI attachment is stored in S3 but storage is unavailable")
            self.storage.s3_delete_object(self._s3_key(vid, path, version))

    def count(self, vid: str) -> int:
        return self.db.one("SELECT COUNT(*) c FROM ai_files WHERE vault_id=?", (vid,))["c"]

    def put(self, vid: str, path: str, data: bytes, mtime: int | None) -> dict:
        path = norm_path(path)
        mtime = now_ms() if mtime is None else mtime
        version = hashlib.sha256(data).hexdigest()
        threshold = int(getattr(getattr(self.storage, "config", None), "s3_threshold_mb", 10)) * 1024 * 1024
        offload = ext(path) != ".md" and len(data) > threshold
        with self.db.transaction():
            old = self.db.one("SELECT version,mtime,backend FROM ai_files WHERE vault_id=? AND path=?", (vid, path))
            if old and old["version"] != version and mtime < old["mtime"]:
                raise ApiError(409, "stale_publish", "stored version is newer than the published one",
                               {"current_version": old["version"], "current_mtime": old["mtime"]})
            if old and old["version"] == version:
                if mtime > old["mtime"]:
                    self.db.execute("UPDATE ai_files SET mtime=? WHERE vault_id=? AND path=?", (mtime, vid, path))
                return self.meta(vid, path)  # type: ignore[return-value]
            if offload:
                if self.storage is None:
                    raise RuntimeError("S3 offload is required but storage is unavailable")
                self.storage.s3_put_object(self._s3_key(vid, path, version), data)
            self.db.execute(
                """INSERT INTO ai_files(vault_id,path,version,mtime,size,kind,content,backend) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(vault_id,path) DO UPDATE SET version=excluded.version, mtime=excluded.mtime,
                   size=excluded.size, kind=excluded.kind, content=excluded.content, backend=excluded.backend""",
                (vid, path, version, mtime, len(data), kind_of(path), b"" if offload else data,
                 "s3" if offload else "local"),
            )
        if old:
            self._delete_s3(vid, path, old["version"], old["backend"])
        meta = self.meta(vid, path)
        for cb in self.on_put:
            cb(vid, meta, data)  # type: ignore[arg-type]
        return meta  # type: ignore[return-value]

    def delete(self, vid: str, path: str) -> None:
        old = self.db.one("SELECT version,backend FROM ai_files WHERE vault_id=? AND path=?", (vid, path))
        if old:
            self._delete_s3(vid, path, old["version"], old["backend"])
        with self.db.transaction():
            self.db.execute("DELETE FROM ai_files WHERE vault_id=? AND path=?", (vid, path))
        for cb in self.on_delete:
            cb(vid, path)  # synchronous removal from the index

    def clear(self, vid: str) -> None:
        rows = self.db.query("SELECT path,version,backend FROM ai_files WHERE vault_id=?", (vid,))
        for row in rows:
            self._delete_s3(vid, row["path"], row["version"], row["backend"])
        with self.db.transaction():
            self.db.execute("DELETE FROM ai_files WHERE vault_id=?", (vid,))
        for cb in self.on_clear:
            cb(vid)
