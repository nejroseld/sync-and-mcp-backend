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
PASSWORD_SCRYPT_N = 2**14
PASSWORD_SCRYPT_R = 8
PASSWORD_SCRYPT_P = 5
PASSWORD_MIN, PASSWORD_MAX = 12, 1024
SESSION_PREFIX = "user:"
MAX_SESSIONS_PER_USER = 10
DAY_MS = 86_400_000

_dummy_hash: str | None = None


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
    expires_at: int | None = None
    account: bool = False  # account rights: owned vaults, vault creation, token management
    parent_id: str | None = None  # the token that issued this one

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
            "expires_at": self.expires_at,
            "account": self.account,
            "parent_id": self.parent_id,
        }


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _row(r) -> Token:
    return Token(r["id"], r["name"], r["kind"], json.loads(r["grants"]), r["created_at"],
                 r["revoked_at"], r["last_used_at"], r["user_id"], bool(r["is_session"]), r["expires_at"],
                 bool(r["account"]), r["parent_id"])


class AuthService:
    def __init__(self, db: Database, session_ttl_days: int = 180, session_idle_days: int = 30,
                 invite_ttl_days: int = 7):
        self.db = db
        self.session_ttl_ms = session_ttl_days * DAY_MS
        self.session_idle_ms = session_idle_days * DAY_MS
        self.invite_ttl_ms = invite_ttl_days * DAY_MS

    @staticmethod
    def normalize_username(username: str) -> str:
        return unicodedata.normalize("NFKC", username).strip().casefold()

    @staticmethod
    def _password_hash(password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=PASSWORD_SCRYPT_N,
                                r=PASSWORD_SCRYPT_R, p=PASSWORD_SCRYPT_P)
        return f"scrypt${PASSWORD_SCRYPT_N}${PASSWORD_SCRYPT_R}${PASSWORD_SCRYPT_P}$" + salt.hex() + "$" + digest.hex()

    @staticmethod
    def _verify_password(password: str, encoded: str) -> bool:
        try:
            scheme, n, r, p, salt, expected = encoded.split("$")
            n_, r_, p_ = int(n), int(r), int(p)
            # A tampered hash row must not turn a login into an arbitrary CPU or memory cost.
            if (scheme != "scrypt" or not 2 <= n_ <= PASSWORD_SCRYPT_N or n_ & (n_ - 1)
                    or not 1 <= r_ <= PASSWORD_SCRYPT_R or not 1 <= p_ <= PASSWORD_SCRYPT_P
                    or len(salt) > 128 or len(expected) != 128):
                return False
            actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=n_, r=r_, p=p_)
            return secrets.compare_digest(actual.hex(), expected)
        except (ValueError, TypeError):
            return False

    @classmethod
    def _dummy_password_hash(cls) -> str:
        global _dummy_hash
        if _dummy_hash is None:
            _dummy_hash = cls._password_hash(secrets.token_urlsafe(16))
        return _dummy_hash

    @staticmethod
    def public_user(row) -> dict:
        return {"id": row["id"], "username": row["username"], "created_at": row["created_at"]}

    # ---- invites ---------------------------------------------------------------------
    def create_invite(self, name: str) -> dict:
        code = "inv_" + secrets.token_urlsafe(32)
        invite_id, ts = "i_" + secrets.token_hex(8), now_ms()
        expires = ts + self.invite_ttl_ms
        self.db.execute("INSERT INTO invites(id,name,code_hash,created_at,expires_at) VALUES(?,?,?,?,?)",
                        (invite_id, name.strip(), hash_token(code), ts, expires))
        return {"id": invite_id, "name": name.strip(), "code": code, "created_at": ts, "used_at": None,
                "expires_at": expires, "revoked_at": None, "status": "active"}

    @staticmethod
    def _invite(r, ts: int) -> dict:
        d = {k: r[k] for k in ("id", "name", "created_at", "used_at", "expires_at", "revoked_at")}
        if d["used_at"]:
            d["status"] = "used"
        elif d["revoked_at"]:
            d["status"] = "revoked"
        elif d["expires_at"] is not None and d["expires_at"] <= ts:
            d["status"] = "expired"
        else:
            d["status"] = "active"
        return d

    def list_invites(self) -> list[dict]:
        ts = now_ms()
        return [self._invite(r, ts) for r in self.db.query(
            "SELECT id,name,created_at,used_at,expires_at,revoked_at FROM invites ORDER BY created_at,id")]

    def revoke_invite(self, invite_id: str) -> dict:
        ts = now_ms()
        self.db.execute("UPDATE invites SET revoked_at=? WHERE id=? AND used_at IS NULL AND revoked_at IS NULL",
                        (ts, invite_id))
        r = self.db.one("SELECT id,name,created_at,used_at,expires_at,revoked_at FROM invites WHERE id=?", (invite_id,))
        if not r:
            raise ApiError(404, "not_found", "invite not found")
        return self._invite(r, ts)

    def list_users(self) -> list[dict]:
        """List accounts with their owned vault and user-token counts."""
        rows = self.db.query(
            """SELECT u.id,u.username,u.created_at,
                      (SELECT COUNT(*) FROM vaults v WHERE v.owner_user_id=u.id) AS vault_count,
                      (SELECT COUNT(*) FROM tokens t WHERE t.user_id=u.id) AS token_count
               FROM users u ORDER BY u.created_at,u.id"""
        )
        return [dict(row) for row in rows]

    # ---- accounts --------------------------------------------------------------------
    def register(self, invite_code: str, username: str, password: str) -> tuple[dict, str]:
        norm = self.normalize_username(username)
        if len(norm) < 3 or len(norm) > 64 or any(ord(c) < 32 for c in norm):
            raise ApiError(422, "invalid_username", "username must be 3 to 64 characters")
        if len(password) < PASSWORD_MIN or len(password) > PASSWORD_MAX:
            raise ApiError(422, "weak_password", "password must be 12 to 1024 characters")
        uid, ts = "u_" + secrets.token_hex(12), now_ms()
        valid_invite = ("SELECT * FROM invites WHERE code_hash=? AND used_at IS NULL AND revoked_at IS NULL "
                        "AND (expires_at IS NULL OR expires_at > ?)")
        if not self.db.one(valid_invite, (hash_token(invite_code), ts)):
            raise ApiError(401, "invalid_invite", "invite code is invalid, expired or already used")
        password_hash = self._password_hash(password)  # outside the DB lock: scrypt is slow
        try:
            with self.db.transaction():
                invite = self.db.one(valid_invite, (hash_token(invite_code), ts))
                if not invite:
                    raise ApiError(401, "invalid_invite", "invite code is invalid, expired or already used")
                self.db.execute("INSERT INTO users(id,username,password_hash,created_at) VALUES(?,?,?,?)",
                                (uid, norm, password_hash, ts))
                self.db.execute("UPDATE invites SET used_at=? WHERE id=? AND used_at IS NULL", (ts, invite["id"]))
        except Exception as e:
            if "UNIQUE constraint failed: users.username" in str(e):
                raise ApiError(409, "username_taken", "username is already registered") from e
            raise
        user = self.public_user(self.db.one("SELECT * FROM users WHERE id=?", (uid,)))
        return user, self._new_session(uid, norm)

    def login(self, username: str, password: str) -> tuple[dict, str]:
        norm = self.normalize_username(username)
        if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
            raise ApiError(401, "invalid_credentials", "username or password is incorrect")
        row = self.db.one("SELECT * FROM users WHERE username=?", (norm,))
        if not row:
            # Same scrypt cost as a real account, so timing does not reveal registered names.
            self._verify_password(password, self._dummy_password_hash())
            raise ApiError(401, "invalid_credentials", "username or password is incorrect")
        if not self._verify_password(password, row["password_hash"]):
            raise ApiError(401, "invalid_credentials", "username or password is incorrect")
        encoded = row["password_hash"]
        if encoded.split("$")[:4] != ["scrypt", str(PASSWORD_SCRYPT_N),
                                       str(PASSWORD_SCRYPT_R), str(PASSWORD_SCRYPT_P)]:
            self.db.execute("UPDATE users SET password_hash=? WHERE id=? AND password_hash=?",
                            (self._password_hash(password), row["id"], encoded))
        user = self.public_user(row)
        return user, self._new_session(user["id"], norm)

    def _new_session(self, user_id: str, username: str) -> str:
        plain = "obsi_" + secrets.token_urlsafe(32)
        tid, ts = "tok_" + secrets.token_hex(6), now_ms()
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO tokens(id,name,kind,token_hash,grants,created_at,user_id,is_session,expires_at,account) "
                "VALUES(?,?,?,?,?,?,?,1,?,1)",
                (tid, SESSION_PREFIX + username, "device", hash_token(plain), "{}", ts, user_id,
                 ts + self.session_ttl_ms))
            live = self.db.query(
                "SELECT id FROM tokens WHERE user_id=? AND is_session=1 AND revoked_at IS NULL "
                "ORDER BY COALESCE(last_used_at, created_at) DESC, created_at DESC, rowid DESC", (user_id,))
            for r in live[MAX_SESSIONS_PER_USER:]:
                self.db.execute("UPDATE tokens SET revoked_at=? WHERE id=?", (ts, r["id"]))
        return plain

    # ---- tokens ----------------------------------------------------------------------
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

    @staticmethod
    def _check_user_token_name(name: str) -> None:
        if name.strip().lower().startswith(SESSION_PREFIX):
            raise ApiError(422, "invalid_name", f"token names starting with '{SESSION_PREFIX}' are reserved")

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
                          grants: dict[str, list[str]] | None = None, account: bool | None = None,
                          parent_id: str | None = None) -> tuple[Token, str]:
        """`account` defaults to True for a device token without grants (the "add device" flow)."""
        if kind not in ("device", "mcp"):
            raise ApiError(422, "invalid_kind", "user tokens must be kind=device or kind=mcp")
        self._check_user_token_name(name)
        clean = self.clean_grants(kind, grants)
        if account is None:
            account = kind == "device" and not clean
        if account and kind != "device":
            raise ApiError(422, "invalid_account", "only device tokens can have account rights")
        if account and clean:
            raise ApiError(422, "invalid_account", "an account token has access to all owned vaults; omit grants")
        plain = "obsi_" + secrets.token_urlsafe(32)
        tid, ts = "tok_" + secrets.token_hex(6), now_ms()
        self.db.execute("INSERT INTO tokens(id,name,kind,token_hash,grants,created_at,user_id,account,parent_id) "
                        "VALUES(?,?,?,?,?,?,?,?,?)",
                        (tid, name, kind, hash_token(plain), json.dumps(clean), ts, user_id, int(account), parent_id))
        return Token(tid, name, kind, clean, ts, user_id=user_id, account=account, parent_id=parent_id), plain

    def list_user(self, user_id: str) -> list[Token]:
        return [_row(r) for r in self.db.query("SELECT * FROM tokens WHERE user_id=? ORDER BY created_at,id", (user_id,))]

    def update_user(self, user_id: str, token_id: str, name=None, grants=None) -> Token:
        tok = self.get(token_id)
        if not tok or tok.user_id != user_id or tok.is_session:
            raise ApiError(404, "not_found", "token not found")
        if name is not None:
            self._check_user_token_name(name)
        if tok.account and grants:
            raise ApiError(422, "invalid_account", "an account token has access to all owned vaults; omit grants")
        return self.update(token_id, name, grants)

    def revoke_user(self, user_id: str, token_id: str) -> tuple[Token, list[str]]:
        tok = self.get(token_id)
        if not tok or tok.user_id != user_id:
            raise ApiError(404, "not_found", "token not found")
        return self.revoke_tree(token_id)

    def authenticate(self, plain: str | None) -> Token | None:
        """Returns the token if it exists, is not revoked and has not expired; touches last_used_at."""
        if not plain:
            return None
        ts = now_ms()
        row = self.db.one("SELECT * FROM tokens WHERE token_hash=? AND revoked_at IS NULL "
                          "AND (expires_at IS NULL OR expires_at > ?)", (hash_token(plain), ts))
        if not row:
            return None
        tok = _row(row)
        if tok.is_session and (tok.last_used_at or tok.created_at) + self.session_idle_ms <= ts:
            return None
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
        return self.revoke_tree(token_id)[0]

    def revoke_tree(self, token_id: str) -> tuple[Token, list[str]]:
        """Revoke a token and every token issued through it, directly or not.

        Returns the token and the ids of descendants revoked by this call. A leaked token
        could have issued new ones; revoking it must not leave those alive. Session
        eviction (login cap) and expiry do not cascade: they are not a compromise signal.
        """
        ts = now_ms()
        with self.db.transaction():
            tok = self.get(token_id)
            if not tok:
                raise ApiError(404, "not_found", "token not found")
            if tok.revoked_at is None:
                tok.revoked_at = ts
                self.db.execute("UPDATE tokens SET revoked_at=? WHERE id=?", (ts, tok.id))
            rows = self.db.query(
                "WITH RECURSIVE tree(id) AS (SELECT id FROM tokens WHERE parent_id=? "
                "UNION SELECT t.id FROM tokens t JOIN tree ON t.parent_id=tree.id) "
                "SELECT tokens.id FROM tokens JOIN tree ON tokens.id=tree.id WHERE tokens.revoked_at IS NULL "
                "ORDER BY tokens.created_at, tokens.id", (tok.id,))
            ids = [r["id"] for r in rows]
            for tid in ids:
                self.db.execute("UPDATE tokens SET revoked_at=? WHERE id=?", (ts, tid))
        return tok, ids

    def drop_vault(self, vault_id: str) -> None:
        for tok in self.list():
            if vault_id in tok.grants:
                grants = {k: v for k, v in tok.grants.items() if k != vault_id}
                self.db.execute("UPDATE tokens SET grants=? WHERE id=?", (json.dumps(grants), tok.id))
