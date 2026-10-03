"""Vault registry. Other modules register `on_delete` hooks to purge their data."""
from __future__ import annotations

import base64
import secrets
from typing import Callable

from .db import Database, now_ms
from .errors import ApiError


def _public(r) -> dict:
    return {
        "id": r["id"], "name": r["name"], "created_at": r["created_at"],
        "rag": {"enabled": bool(r["rag_enabled"]), "chunk_chars": r["chunk_chars"],
                "chunk_overlap": r["chunk_overlap"]},
    }


class VaultService:
    def __init__(self, db: Database):
        self.db = db
        self.on_delete: list[Callable[[str], None]] = []
        self.on_rag_settings_changed: list[Callable[[str], None]] = []

    def create(self, name: str) -> dict:
        vid = "v_" + base64.b32encode(secrets.token_bytes(10)).decode().lower()
        self.db.execute("INSERT INTO vaults(id,name,created_at) VALUES(?,?,?)", (vid, name, now_ms()))
        return self.get(vid)  # type: ignore[return-value]

    def get(self, vid: str) -> dict | None:
        r = self.db.one("SELECT * FROM vaults WHERE id=?", (vid,))
        return _public(r) if r else None

    def require(self, vid: str) -> dict:
        v = self.get(vid)
        if not v:
            raise ApiError(404, "not_found", "vault not found")
        return v

    def list(self, only: set[str] | None = None) -> list[dict]:
        rows = self.db.query("SELECT * FROM vaults ORDER BY created_at, id")
        return [_public(r) for r in rows if only is None or r["id"] in only]

    def update(self, vid: str, name: str | None = None, rag: dict | None = None) -> dict:
        v = self.require(vid)
        name = v["name"] if name is None else name
        r = dict(v["rag"])
        rechunk = False
        if rag:
            for k in ("enabled", "chunk_chars", "chunk_overlap"):
                if rag.get(k) is not None:
                    if k != "enabled":
                        if int(rag[k]) < 0 or (k == "chunk_chars" and int(rag[k]) < 50):
                            raise ApiError(422, "validation_error", f"bad {k}")
                        rechunk = rechunk or int(rag[k]) != r[k]
                    r[k] = rag[k]
        self.db.execute(
            "UPDATE vaults SET name=?, rag_enabled=?, chunk_chars=?, chunk_overlap=? WHERE id=?",
            (name, int(bool(r["enabled"])), int(r["chunk_chars"]), int(r["chunk_overlap"]), vid),
        )
        if rechunk:
            for cb in self.on_rag_settings_changed:
                cb(vid)
        return self.require(vid)

    def delete(self, vid: str) -> bool:
        if not self.get(vid):
            return False
        for cb in self.on_delete:
            cb(vid)
        self.db.execute("DELETE FROM vaults WHERE id=?", (vid,))
        return True
