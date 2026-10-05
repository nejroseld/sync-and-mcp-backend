"""Tokens (stored as sha256), kinds and per-vault grants."""
from __future__ import annotations

import hashlib
import json
import secrets
import unicodedata
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
    user_id: str | None = None
    is_session: bool = False

    def can(self, vault_id: str, op: str) -> bool:
        return op in self.grants.get(vault_id, ())

    def has_any(self, vault_id: str) -> bool:
        return bool(self.grants.get(vault_id))

    def public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "kind": self.kind, "grants": self.grants,
            "created_at": self.created_at, "revoked_at": self.revoked_at,
            "last_used_at": self.last_used_at,
            "user_id": self.user_id,
            "is_session": self.is_session,
        }


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _row(r) -> Token:
    return Token(r["id"], r["name"], r["kind"], json.loads(r["grants"]), r["created_at"],
                 r["revoked_at"], r["last_used_at"], r["user_id"], bool(r["is_session"]))


class AuthService:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def normalize_username(username: str) -> str:
        return unicodedata.normalize("NFKC", username).strip().casefold()

    @staticmethod
    def _password_hash(password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
        return "scrypt$16384$8$1$" + salt.hex() + "$" + digest.hex()

    @staticmethod
    def _verify_password(password: str, encoded: str) -> bool:
        try:
            scheme, n, r, p, salt, expected = encoded.split("$")
            if scheme != "scrypt":
                return False
            actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                                    n=int(n), r=int(r), p=int(p))
            return secrets.compare_digest(actual.hex(), expected)
        except (ValueError, TypeError):
            return False

    @staticmethod
    def public_user(row) -> dict:
        return {"id": row["id"], "username": row["username"], "created_at": row["created_at"]}

    def create_invite(self, name: str) -> dict:
        code = "inv_" + secrets.token_urlsafe(32)
        invite_id, ts = "i_" + secrets.token_hex(8), now_ms()
        self.db.execute("INSERT INTO invites(id,name,code_hash,created_at) VALUES(?,?,?,?)",
                        (invite_id, name.strip(), hash_token(code), ts))
        return {"id": invite_id, "name": name.strip(), "code": code, "created_at": ts, "used_at": None}

    def list_invites(self) -> list[dict]:
        return [dict(r) for r in self.db.query("SELECT id,name,created_at,used_at FROM invites ORDER BY created_at,id")]

    def list_users(self) -> list[dict]:
        """List accounts with their owned vault and user-token counts."""
        rows = self.db.query(
            """SELECT u.id,u.username,u.created_at,
                      (SELECT COUNT(*) FROM vaults v WHERE v.owner_user_id=u.id) AS vault_count,
                      (SELECT COUNT(*) FROM tokens t WHERE t.user_id=u.id) AS token_count
               FROM users u ORDER BY u.created_at,u.id"""
        )
        return [dict(row) for row in rows]

    def register(self, invite_code: str, username: str, password: str) -> tuple[dict, str]:
        norm = self.normalize_username(username)
        if len(norm) < 3 or len(norm) > 64 or any(ord(c) < 32 for c in norm):
            raise ApiError(422, "invalid_username", "username must be 3 to 64 characters")
        if len(password) < 12 or len(password) > 1024:
            raise ApiError(422, "weak_password", "password must be at least 12 characters")
        uid, ts = "u_" + secrets.token_hex(12), now_ms()
        try:
            with self.db.transaction():
                invite = self.db.one("SELECT * FROM invites WHERE code_hash=? AND used_at IS NULL",
                                     (hash_token(invite_code),))
                if not invite:
                    raise ApiError(401, "invalid_invite", "invite code is invalid or already used")
                self.db.execute("INSERT INTO users(id,username,password_hash,created_at) VALUES(?,?,?,?)",
                                (uid, norm, self._password_hash(password), ts))
                self.db.execute("UPDATE invites SET used_at=? WHERE id=? AND used_at IS NULL", (ts, invite["id"]))
        except Exception as e:
            if "UNIQUE constraint failed: users.username" in str(e):
                raise ApiError(409, "username_taken", "username is already registered") from e
            raise
        user = self.public_user(self.db.one("SELECT * FROM users WHERE id=?", (uid,)))
        plain = "obsi_" + secrets.token_urlsafe(32)
        tid = "tok_" + secrets.token_hex(6)
        self.db.execute("INSERT INTO tokens(id,name,kind,token_hash,grants,created_at,user_id,is_session) VALUES(?,?,?,?,?,?,?,1)",
                        (tid, "user:" + norm, "device", hash_token(plain), "{}", ts, uid))
        return user, plain

    def login(self, username: str, password: str) -> tuple[dict, str]:
        norm = self.normalize_username(username)
        row = self.db.one("SELECT * FROM users WHERE username=?", (norm,))
        if not row or not self._verify_password(password, row["password_hash"]):
            raise ApiError(401, "invalid_credentials", "username or password is incorrect")
        user = self.public_user(row)
        plain = "obsi_" + secrets.token_urlsafe(32)
        tid, ts = "tok_" + secrets.token_hex(6), now_ms()
        self.db.execute("INSERT INTO tokens(id,name,kind,token_hash,grants,created_at,user_id,is_session) VALUES(?,?,?,?,?,?,?,1)",
                        (tid, "user:" + norm, "device", hash_token(plain), "{}", ts, user["id"]))
        return user, plain

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

    def create_user_token(self, user_id: str, name: str, kind: str,
                          grants: dict[str, list[str]] | None = None) -> tuple[Token, str]:
        if kind not in ("device", "mcp"):
            raise ApiError(422, "invalid_kind", "user tokens must be kind=device or kind=mcp")
        clean = self.clean_grants(kind, grants)
        plain = "obsi_" + secrets.token_urlsafe(32)
        tid, ts = "tok_" + secrets.token_hex(6), now_ms()
        self.db.execute("INSERT INTO tokens(id,name,kind,token_hash,grants,created_at,user_id) VALUES(?,?,?,?,?,?,?)",
                        (tid, name, kind, hash_token(plain), json.dumps(clean), ts, user_id))
        return Token(tid, name, kind, clean, ts, user_id=user_id), plain

    def list_user(self, user_id: str) -> list[Token]:
        return [_row(r) for r in self.db.query("SELECT * FROM tokens WHERE user_id=? ORDER BY created_at,id", (user_id,))]

    def update_user(self, user_id: str, token_id: str, name=None, grants=None) -> Token:
        tok = self.get(token_id)
        if not tok or tok.user_id != user_id or tok.is_session:
            raise ApiError(404, "not_found", "token not found")
        return self.update(token_id, name, grants)

    def revoke_user(self, user_id: str, token_id: str) -> Token:
        tok = self.get(token_id)
        if not tok or tok.user_id != user_id:
            raise ApiError(404, "not_found", "token not found")
        return self.revoke(token_id)

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
