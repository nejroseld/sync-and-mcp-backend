"""Pending changes queue: MCP writes waiting to be applied by a device plugin."""
from __future__ import annotations

import secrets

from .db import Database, now_ms
from .errors import ApiError

ACK_STATUSES = {"applied", "conflict", "rejected"}


def _obj(r) -> dict:
    return {
        "id": r["id"], "path": r["path"], "op": r["op"], "content": r["content"],
        "base_version": r["base_version"], "status": r["status"], "message": r["message"],
        "new_version": r["new_version"], "created_at": r["created_at"], "token_name": r["token_name"],
    }


class ChangeQueue:
    def __init__(self, db: Database):
        self.db = db

    def create(self, vid: str, path: str, op: str, content: str, base_version: str | None,
               token_id: str, token_name: str) -> dict:
        cid = "c_" + secrets.token_hex(8)
        self.db.execute(
            "INSERT INTO changes(id,vault_id,path,op,content,base_version,created_at,token_name,token_id) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (cid, vid, path, op, content, base_version, now_ms(), token_name, token_id),
        )
        return self.get(cid)  # type: ignore[return-value]

    def get(self, cid: str, vid: str | None = None) -> dict | None:
        r = self.db.one("SELECT * FROM changes WHERE id=?", (cid,))
        if not r or (vid is not None and r["vault_id"] != vid):
            return None
        return _obj(r)

    def vault_of(self, cid: str) -> str | None:
        r = self.db.one("SELECT vault_id FROM changes WHERE id=?", (cid,))
        return r["vault_id"] if r else None

    def list(self, vid: str, status: str | None = "pending") -> list[dict]:
        if status:
            rows = self.db.query("SELECT * FROM changes WHERE vault_id=? AND status=? ORDER BY created_at, id", (vid, status))
        else:
            rows = self.db.query("SELECT * FROM changes WHERE vault_id=? ORDER BY created_at, id", (vid,))
        return [_obj(r) for r in rows]

    def pending_count(self, vid: str, path: str) -> int:
        return self.db.one("SELECT COUNT(*) c FROM changes WHERE vault_id=? AND path=? AND status='pending'",
                           (vid, path))["c"]

    def ack(self, vid: str, cid: str, status: str, message: str | None, new_version: str | None) -> dict:
        if status not in ACK_STATUSES:
            raise ApiError(422, "validation_error", f"status must be one of {sorted(ACK_STATUSES)}")
        with self.db.transaction():
            ch = self.get(cid, vid)
            if not ch:
                raise ApiError(404, "not_found", "change not found")
            if ch["status"] != "pending":
                raise ApiError(409, "already_acked", f"change already {ch['status']}")
            self.db.execute("UPDATE changes SET status=?, message=?, new_version=? WHERE id=?",
                            (status, message, new_version, cid))
        return self.get(cid)  # type: ignore[return-value]

    def purge(self, vid: str) -> None:
        self.db.execute("DELETE FROM changes WHERE vault_id=?", (vid,))
