"""Ciphertext blob store: bytes on disk (named sha256(key)), metadata in sqlite."""
from __future__ import annotations

import hashlib
import os
import shutil
import secrets
from pathlib import Path

from .db import Database, now_ms


def _obj(r) -> dict:
    return {"key": r["key"], "size": r["size"], "mtime_cli": r["mtime_cli"], "ctime_cli": r["ctime_cli"],
            "mtime_svr": r["mtime_svr"], "etag": r["etag"]}


class Storage:
    def __init__(self, db: Database, data_dir: Path):
        self.db = db
        self.root = Path(data_dir) / "vaults"

    def _dir(self, vid: str) -> Path:
        return self.root / vid / "blobs"

    def blob_path(self, vid: str, key: str) -> Path:
        return self._dir(vid) / hashlib.sha256(key.encode()).hexdigest()

    def list(self, vid: str) -> list[dict]:
        return [_obj(r) for r in self.db.query("SELECT * FROM blobs WHERE vault_id=? ORDER BY key", (vid,))]

    def stat(self, vid: str, key: str) -> dict | None:
        r = self.db.one("SELECT * FROM blobs WHERE vault_id=? AND key=?", (vid, key))
        return _obj(r) if r else None

    def put(self, vid: str, key: str, data: bytes, mtime_cli: int | None, ctime_cli: int | None) -> dict:
        d = self._dir(vid)
        d.mkdir(parents=True, exist_ok=True)
        target = self.blob_path(vid, key)
        tmp = d / f".tmp-{secrets.token_hex(6)}"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)  # atomic
        finally:
            if tmp.exists():
                tmp.unlink()
        ts = now_ms()
        etag = hashlib.sha256(data).hexdigest()
        mtime_cli = ts if mtime_cli is None else mtime_cli
        ctime_cli = mtime_cli if ctime_cli is None else ctime_cli
        self.db.execute(
            """INSERT INTO blobs(vault_id,key,size,mtime_cli,ctime_cli,mtime_svr,etag) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(vault_id,key) DO UPDATE SET size=excluded.size, mtime_cli=excluded.mtime_cli,
               ctime_cli=excluded.ctime_cli, mtime_svr=excluded.mtime_svr, etag=excluded.etag""",
            (vid, key, len(data), mtime_cli, ctime_cli, ts, etag),
        )
        return self.stat(vid, key)  # type: ignore[return-value]

    def delete(self, vid: str, key: str) -> None:
        self.db.execute("DELETE FROM blobs WHERE vault_id=? AND key=?", (vid, key))
        self.blob_path(vid, key).unlink(missing_ok=True)

    def purge(self, vid: str) -> None:
        self.db.execute("DELETE FROM blobs WHERE vault_id=?", (vid,))
        shutil.rmtree(self.root / vid, ignore_errors=True)
