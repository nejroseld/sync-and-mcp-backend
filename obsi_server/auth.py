"""Tokens (stored as sha256), kinds and per-vault grants."""
from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field

from .db import Database, now_ms
from .errors import ApiError

ALLOWED_OPS = {
    "device": {"read", "write"},
    "mcp": {"list", "search", "read", "write"},
    "admin": set(),
}
LAST_USED_THROTTLE_MS = 5_000


@dataclass
class Token:
    id: str
    name: str
    kind: str
    grants: dict[str, list[str]]
    created_at: int
    revoked_at: int | None = None
    last_used_at: int | None = None

    def can(self, vault_id: str, op: str) -> bool:
        return op in self.grants.get(vault_id, ())

    def has_any(self, vault_id: str) -> bool:
        return bool(self.grants.get(vault_id))

    def public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "kind": self.kind, "grants": self.grants,
            "created_at": self.created_at, "revoked_at": self.revoked_at,
            "last_used_at": self.last_used_at,
        }


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _row(r) -> Token:
    return Token(r["id"], r["name"], r["kind"], json.loads(r["grants"]), r["created_at"],
                 r["revoked_at"], r["last_used_at"])


class AuthService:
    def __init__(self, db: Database):
        self.db = db

    def clean_grants(self, kind: str, grants: dict[str, list[str]] | None) -> dict[str, list[str]]:
        if kind not in ALLOWED_OPS:
            raise ApiError(422, "invalid_kind", f"kind must be one of {sorted(ALLOWED_OPS)}")
        if kind == "admin":
            return {}
        out: dict[str, list[str]] = {}
        for vid, ops in (grants or {}).items():
            bad = set(ops) - ALLOWED_OPS[kind]
            if bad:
                raise ApiError(422, "invalid_grant", f"operations {sorted(bad)} not allowed for kind={kind}")
            if ops:
                out[vid] = sorted(set(ops))
        return out

    def create(self, name: str, kind: str, grants: dict[str, list[str]] | None = None) -> tuple[Token, str]:
        grants = self.clean_grants(kind, grants)
        plain = "obsi_" + secrets.token_urlsafe(32)
        tid = "tok_" + secrets.token_hex(6)
        ts = now_ms()
        self.db.execute(
            "INSERT INTO tokens(id,name,kind,token_hash,grants,created_at) VALUES(?,?,?,?,?,?)",
            (tid, name, kind, hash_token(plain), json.dumps(grants), ts),
        )
        return Token(tid, name, kind, grants, ts), plain

    def authenticate(self, plain: str | None) -> Token | None:
        """Returns the token if it exists and is not revoked; touches last_used_at."""
        if not plain:
            return None
        row = self.db.one("SELECT * FROM tokens WHERE token_hash=? AND revoked_at IS NULL", (hash_token(plain),))
        if not row:
            return None
        tok = _row(row)
        ts = now_ms()
        if tok.last_used_at is None or ts - tok.last_used_at > LAST_USED_THROTTLE_MS:
            self.db.execute("UPDATE tokens SET last_used_at=? WHERE id=?", (ts, tok.id))
            tok.last_used_at = ts
        return tok

    def get(self, token_id: str) -> Token | None:
        row = self.db.one("SELECT * FROM tokens WHERE id=?", (token_id,))
        return _row(row) if row else None

    def list(self) -> list[Token]:
        return [_row(r) for r in self.db.query("SELECT * FROM tokens ORDER BY created_at, id")]

    def update(self, token_id: str, name: str | None = None, grants: dict[str, list[str]] | None = None) -> Token:
        tok = self.get(token_id)
        if not tok:
            raise ApiError(404, "not_found", "token not found")
        if name is not None:
            tok.name = name
        if grants is not None:
            tok.grants = self.clean_grants(tok.kind, grants)
        self.db.execute("UPDATE tokens SET name=?, grants=? WHERE id=?", (tok.name, json.dumps(tok.grants), tok.id))
        return tok

    def grant(self, token_id: str, vault_id: str, ops: list[str]) -> Token:
        tok = self.get(token_id)
        if not tok:
            raise ApiError(404, "not_found", "token not found")
        grants = dict(tok.grants)
        grants[vault_id] = ops
        return self.update(token_id, grants=grants)

    def revoke(self, token_id: str) -> Token:
        tok = self.get(token_id)
        if not tok:
            raise ApiError(404, "not_found", "token not found")
        if tok.revoked_at is None:
            tok.revoked_at = now_ms()
            self.db.execute("UPDATE tokens SET revoked_at=? WHERE id=?", (tok.revoked_at, tok.id))
        return tok

    def drop_vault(self, vault_id: str) -> None:
        for tok in self.list():
            if vault_id in tok.grants:
                grants = {k: v for k, v in tok.grants.items() if k != vault_id}
                self.db.execute("UPDATE tokens SET grants=? WHERE id=?", (json.dumps(grants), tok.id))
