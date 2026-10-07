import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from obsi_server.cli import main
from obsi_server.db import Database, MIGRATIONS
from helpers import ServerCase


class UserAccountTests(ServerCase):
    def register(self, invite, username="Alice", password="correct horse battery staple"):
        return self.client.post("/api/v1/auth/register", json={
            "invite_code": invite, "username": username, "password": password,
        })

    def test_invite_register_login_and_vault_ownership(self):
        invite = self.client.post("/api/v1/admin/invites", headers=self.h(self.admin),
                                  json={"name": "Alice signup"}).json()
        listed = self.client.get("/api/v1/admin/invites", headers=self.h(self.admin)).json()["invites"]
        self.assertNotIn("code", listed[0])
        reg = self.register(invite["code"])
        self.assertEqual(reg.status_code, 200, reg.text)
        data = reg.json()
        self.assertEqual(data["user"]["username"], "alice")
        self.assertNotIn("password", data["user"])
        user_headers = {"Authorization": "Bearer " + data["token"]}
        self.assertEqual(self.client.get("/api/v1/me", headers=user_headers).json()["user"], data["user"])

        created = self.client.post("/api/v1/vaults", headers=user_headers, json={"name": "Private"})
        self.assertEqual(created.status_code, 200, created.text)
        vid = created.json()["id"]
        self.assertEqual(created.json()["owner_user_id"], data["user"]["id"])
        self.assertIn(vid, [v["id"] for v in self.client.get("/api/v1/vaults", headers=user_headers).json()["vaults"]])
        base = f"/api/v1/vaults/{vid}/files"
        self.assertEqual(self.client.put(base + "/note", content=b"cipher", headers=user_headers).status_code, 200)
        ai_path = f"/api/v1/vaults/{vid}/ai/files/secret.md"
        self.assertEqual(self.client.put(ai_path, content=b"private note", headers=user_headers).status_code, 200)

        second_invite = self.client.post("/api/v1/admin/invites", headers=self.h(self.admin),
                                         json={"name": "Bob signup"}).json()
        bob = self.register(second_invite["code"], "Bob").json()
        bob_headers = {"Authorization": "Bearer " + bob["token"]}
        self.assertEqual(self.client.get(base + "/note", headers=bob_headers).status_code, 403)
        self.assertEqual(self.client.put(base + "/note", content=b"tamper", headers=bob_headers).status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/vaults/{vid}/ai/manifest", headers=bob_headers).status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/vaults/{vid}/files/note", headers=bob_headers).status_code, 403)
        self.assertNotIn(vid, [v["id"] for v in self.client.get("/api/v1/vaults", headers=bob_headers).json()["vaults"]])

        login = self.client.post("/api/v1/auth/login", json={"username": "ALICE", "password": "correct horse battery staple"})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["user"]["id"], data["user"]["id"])

    def test_invite_one_use_password_policy_and_normalized_unique_usernames(self):
        invite = self.client.post("/api/v1/admin/invites", headers=self.h(self.admin), json={"name": "signup"}).json()
        self.assertEqual(self.register(invite["code"], password="short").status_code, 422)
        first = self.register(invite["code"])
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.register(invite["code"], "other").status_code, 401)

        invite2 = self.client.post("/api/v1/admin/invites", headers=self.h(self.admin), json={"name": "signup"}).json()
        dupe = self.register(invite2["code"], "ALICE")
        self.assertEqual(dupe.status_code, 409)
        self.assertEqual(self.client.post("/api/v1/auth/login", json={"username": "alice", "password": "wrong password"}).status_code, 401)

    def test_user_credentials_are_not_stored_in_plaintext_and_revoke_works(self):
        inv = self.client.post("/api/v1/admin/invites", headers=self.h(self.admin), json={"name": "x"}).json()
        result = self.register(inv["code"]).json()
        row = self.ctx.db.one("SELECT password_hash FROM users WHERE id=?", (result["user"]["id"],))
        self.assertTrue(row["password_hash"].startswith("scrypt$"))
        self.assertNotIn("correct horse battery staple", row["password_hash"])
        token = self.ctx.db.one("SELECT id FROM tokens WHERE token_hash != '' AND user_id=?", (result["user"]["id"],))
        revoke = self.client.post(f"/api/v1/admin/tokens/{token['id']}/revoke", headers=self.h(self.admin))
        self.assertIsNotNone(revoke.json()["revoked_at"])
        self.assertEqual(self.client.get("/api/v1/me", headers={"Authorization": "Bearer " + result["token"]}).status_code, 401)

    def test_legacy_grant_access_remains_available(self):
        r = self.client.get(f"/api/v1/vaults/{self.vid}/files", headers=self.h(self.dev))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/vaults", headers=self.h(self.admin)).status_code, 200)

    def test_account_tokens_are_scoped_and_self_managed(self):
        invitation = self.client.post("/api/v1/admin/invites", headers=self.h(self.admin),
                                      json={"name": "Alice"}).json()
        alice = self.register(invitation["code"]).json()
        ah = {"Authorization": "Bearer " + alice["token"]}
        own = self.client.post("/api/v1/vaults", headers=ah, json={"name": "Owned"}).json()["id"]

        invite_b = self.client.post("/api/v1/admin/invites", headers=self.h(self.admin),
                                    json={"name": "Bob"}).json()
        bob = self.register(invite_b["code"], "Bob").json()
        bob_headers = {"Authorization": "Bearer " + bob["token"]}
        bob_vault = self.client.post("/api/v1/vaults", headers=bob_headers,
                                     json={"name": "Bob vault"}).json()["id"]

        self.assertTrue(self.client.get("/api/v1/me", headers=ah).json()["account_token"])
        self.assertEqual(self.client.post("/api/v1/tokens", headers=ah, json={
            "name": "bad", "kind": "device", "grants": {bob_vault: ["read"]},
        }).status_code, 422)
        self.assertEqual(self.client.post("/api/v1/tokens", headers=ah, json={
            "name": "admin escalation", "kind": "admin", "grants": {},
        }).status_code, 422)
        issued = self.client.post("/api/v1/tokens", headers=ah, json={
            "name": "limited", "kind": "device", "grants": {own: ["read"]},
        })
        self.assertEqual(issued.status_code, 200, issued.text)
        delegated = issued.json()
        dh = {"Authorization": "Bearer " + delegated["token"]}
        self.assertFalse(self.client.get("/api/v1/me", headers=dh).json()["account_token"])
        self.assertEqual(self.client.post("/api/v1/vaults", headers=dh, json={"name": "Nope"}).status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/vaults/{own}/files", headers=dh).status_code, 200)
        self.assertEqual(self.client.put(f"/api/v1/vaults/{own}/files/note", headers=dh,
                                        content=b"blocked").status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/vaults/{bob_vault}/files", headers=dh).status_code, 403)
        self.assertEqual([v["id"] for v in self.client.get("/api/v1/vaults", headers=dh).json()["vaults"]], [own])
        self.assertEqual(self.client.get("/api/v1/tokens", headers=dh).status_code, 403)
        self.assertEqual(self.client.patch(f"/api/v1/tokens/{delegated['id']}", headers=bob_headers,
                                           json={"name": "stolen"}).status_code, 404)
        self.assertEqual(self.client.post(f"/api/v1/tokens/{delegated['id']}/revoke", headers=bob_headers).status_code, 404)
        self.assertEqual(self.client.patch(f"/api/v1/tokens/{delegated['id']}", headers=ah,
                                           json={"grants": {bob_vault: ["read"]}}).status_code, 422)
        mcp_created = self.client.post("/api/v1/tokens", headers=ah, json={
            "name": "MCP", "kind": "mcp", "grants": {own: ["list", "read"]},
        }).json()
        mcp_response = self.rpc("tools/call", {"name": "list_vaults", "arguments": {}},
                                {"token": mcp_created["token"]})
        self.assertEqual(mcp_response.status_code, 200, mcp_response.text)
        self.assertIn(own, mcp_response.text)
        listed = self.client.get("/api/v1/tokens", headers=ah).json()["tokens"]
        self.assertIn(delegated["id"], [t["id"] for t in listed])
        self.assertTrue(any(t["is_session"] for t in listed))
        revoked = self.client.post(f"/api/v1/tokens/{delegated['id']}/revoke", headers=ah)
        self.assertEqual(revoked.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/vaults", headers=dh).status_code, 401)

    def test_admin_user_list_includes_owned_vaults_and_user_tokens(self):
        invite = self.client.post("/api/v1/admin/invites", headers=self.h(self.admin), json={"name": "signup"}).json()
        alice = self.register(invite["code"]).json()
        user_headers = {"Authorization": "Bearer " + alice["token"]}
        self.client.post("/api/v1/vaults", headers=user_headers, json={"name": "Private"})
        response = self.client.get("/api/v1/admin/users", headers=self.h(self.admin))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["users"], [{
            "id": alice["user"]["id"], "username": "alice", "created_at": alice["user"]["created_at"],
            "vault_count": 1, "token_count": 1,
        }])
        self.assertEqual(self.client.get("/api/v1/admin/users", headers=self.h(self.dev)).status_code, 403)


class SessionLifetimeTests(ServerCase):
    def account(self, name="alice", password="correct horse battery staple"):
        code = self.ctx.auth.create_invite(name)["code"]
        return self.ctx.auth.register(code, name, password)

    def test_sessions_expire_absolutely_and_when_idle(self):
        from obsi_server import auth as auth_mod
        _, token = self.account()
        self.assertIsNotNone(self.ctx.auth.authenticate(token))
        row = self.ctx.db.one("SELECT created_at, expires_at FROM tokens WHERE is_session=1")
        self.assertEqual(row["expires_at"] - row["created_at"], 180 * auth_mod.DAY_MS)
        with patch("obsi_server.auth.now_ms", return_value=row["created_at"] + 31 * auth_mod.DAY_MS):
            self.assertIsNone(self.ctx.auth.authenticate(token))
        self.ctx.db.execute("UPDATE tokens SET expires_at=? WHERE is_session=1", (auth_mod.now_ms() - 1,))
        self.assertIsNone(self.ctx.auth.authenticate(token))
        delegated, plain = self.ctx.auth.create("laptop", "device")
        with patch("obsi_server.auth.now_ms", return_value=row["created_at"] + 400 * auth_mod.DAY_MS):
            self.assertIsNotNone(self.ctx.auth.authenticate(plain))

    def test_old_sessions_are_revoked_beyond_the_cap(self):
        from obsi_server.auth import MAX_SESSIONS_PER_USER
        _, first = self.account()
        tokens = [self.ctx.auth.login("alice", "correct horse battery staple")[1]
                  for _ in range(MAX_SESSIONS_PER_USER)]
        self.assertIsNone(self.ctx.auth.authenticate(first))
        self.assertTrue(all(self.ctx.auth.authenticate(t) for t in tokens))
        live = self.ctx.db.one("SELECT COUNT(*) n FROM tokens WHERE is_session=1 AND revoked_at IS NULL")["n"]
        self.assertEqual(live, MAX_SESSIONS_PER_USER)

    def test_invites_expire_and_can_be_revoked(self):
        from obsi_server import auth as auth_mod
        invite = self.client.post("/api/v1/admin/invites", headers=self.h(self.admin), json={"name": "x"}).json()
        self.assertEqual(invite["expires_at"] - invite["created_at"], 7 * auth_mod.DAY_MS)
        with patch("obsi_server.auth.now_ms", return_value=invite["expires_at"]):
            with self.assertRaises(Exception):
                self.ctx.auth.register(invite["code"], "late", "correct horse battery staple")
        r = self.client.post(f"/api/v1/admin/invites/{invite['id']}/revoke", headers=self.h(self.admin))
        self.assertEqual(r.json()["status"], "revoked")
        reg = self.client.post("/api/v1/auth/register", json={
            "invite_code": invite["code"], "username": "bob", "password": "correct horse battery staple"})
        self.assertEqual(reg.status_code, 401)
        self.assertEqual(self.client.post("/api/v1/admin/invites/i_nope/revoke",
                                          headers=self.h(self.admin)).status_code, 404)

    def test_unknown_user_costs_a_scrypt_and_bad_lengths_do_not(self):
        from obsi_server.auth import AuthService
        self.account()
        with patch.object(AuthService, "_verify_password", wraps=AuthService._verify_password) as verify:
            for name, password in (("nobody", "long enough password"), ("alice", "short"), ("alice", "x" * 1025)):
                r = self.client.post("/api/v1/auth/login", json={"username": name, "password": password})
                self.assertEqual(r.status_code, 401)
        self.assertEqual(verify.call_count, 1)

    def test_tampered_scrypt_parameters_are_rejected(self):
        from obsi_server.auth import AuthService
        good = AuthService._password_hash("pw")
        _, n, r, p, salt, digest = good.split("$")
        for bad in (f"scrypt$1048576${r}${p}${salt}${digest}", f"scrypt${n}$64${p}${salt}${digest}",
                    f"scrypt${n}${r}$64${salt}${digest}", f"scrypt$3000${r}${p}${salt}${digest}"):
            with patch("hashlib.scrypt", side_effect=AssertionError("must not run")):
                self.assertFalse(AuthService._verify_password("pw", bad))
        self.assertTrue(AuthService._verify_password("pw", good))

    def test_session_prefix_is_reserved_for_account_tokens(self):
        user, token = self.account()
        h = {"Authorization": "Bearer " + token}
        r = self.client.post("/api/v1/tokens", headers=h, json={"name": "User:alice", "kind": "device"})
        self.assertEqual(r.status_code, 422)
        tid = self.client.post("/api/v1/tokens", headers=h, json={"name": "ok", "kind": "device"}).json()["id"]
        self.assertEqual(self.client.patch(f"/api/v1/tokens/{tid}", headers=h,
                                           json={"name": "user:alice"}).status_code, 422)

    def test_auth_responses_are_not_cached(self):
        code = self.ctx.auth.create_invite("x")["code"]
        r = self.client.post("/api/v1/auth/register", json={
            "invite_code": code, "username": "carol", "password": "correct horse battery staple"})
        self.assertEqual(r.headers["cache-control"], "no-store")


class UserAdminCliTests(unittest.TestCase):
    def test_set_embedding_keeps_key_unless_given_and_refuses_argv_key(self):
        import os
        with tempfile.TemporaryDirectory() as data_dir:
            def run(*args, ok=True):
                out, err = io.StringIO(), io.StringIO()
                with patch.dict("os.environ", {"OBSI_FEATURES": "none"}), redirect_stdout(out), redirect_stderr(err):
                    code = main(["--data-dir", data_dir, "admin", *args])
                self.assertEqual(code == 0, ok, err.getvalue())
                return err.getvalue()

            key_file = os.path.join(data_dir, "key")
            with open(key_file, "w") as f:
                f.write("sk-from-file\n")
            os.chmod(key_file, 0o644)
            self.assertIn("chmod 600", run("set-embedding", "--base-url", "https://emb.example/v1",
                                           "--model", "m", "--api-key-file", key_file, ok=False))
            os.chmod(key_file, 0o600)
            run("set-embedding", "--base-url", "https://emb.example/v1", "--model", "m", "--api-key-file", key_file)
            run("set-embedding", "--base-url", "https://other.example/v1")
            self.assertIn("process list", run("set-embedding", "--api-key", "sk-argv", ok=False))
            self.assertIn("https", run("set-embedding", "--base-url", "http://169.254.169.254", ok=False))
            db = Database(data_dir + "/obsi.db")
            self.assertEqual(db.get_setting("embedding"),
                             {"base_url": "https://other.example/v1", "api_key": "sk-from-file", "model": "m"})
            db.close()

    def test_admin_cli_revoke_invite(self):
        with tempfile.TemporaryDirectory() as data_dir:
            def run(*args):
                out = io.StringIO()
                with patch.dict("os.environ", {"OBSI_FEATURES": "none"}), redirect_stdout(out), redirect_stderr(io.StringIO()):
                    self.assertEqual(main(["--data-dir", data_dir, "admin", *args]), 0)
                return json.loads(out.getvalue())

            invite = run("create-invite", "--name", "x")
            self.assertEqual(run("revoke-invite", invite["id"])["status"], "revoked")
            self.assertEqual(run("list-invites")[0]["status"], "revoked")

    @staticmethod
    def _old_db(path, applied):
        conn = sqlite3.connect(path)
        for migration in applied:
            conn.executescript(migration)
        conn.execute(f"PRAGMA user_version={len(applied)}")
        return conn

    def test_existing_account_tokens_become_sessions_on_upgrade(self):
        with tempfile.TemporaryDirectory() as data_dir:
            path = data_dir + "/obsi.db"
            conn = self._old_db(path, MIGRATIONS[:4])
            conn.execute("INSERT INTO users(id,username,password_hash,created_at) VALUES('u_old','alice','hash',1)")
            for tid, name, grants in (("tok_old", "user:alice", "{}"),         # a login session
                                      ("tok_named", "user:bob", "{}"),          # delegated, name chosen by alice
                                      ("tok_scoped", "user:alice", '{"v":["read"]}')):  # delegated with grants
                conn.execute("INSERT INTO tokens(id,name,kind,token_hash,grants,created_at,user_id) "
                             "VALUES(?,?,'device',?,?,1,'u_old')", (tid, name, tid, grants))
            conn.commit()
            conn.close()
            db = Database(path)
            sessions = {r["id"]: r["is_session"] for r in db.query("SELECT id,is_session FROM tokens")}
            self.assertEqual(sessions, {"tok_old": 1, "tok_named": 0, "tok_scoped": 0})
            self.assertIsNotNone(db.one("SELECT expires_at FROM tokens WHERE id='tok_old'")["expires_at"])
            db.close()

    def test_upgrade_demotes_tokens_promoted_by_the_old_prefix_migration(self):
        old_prefix_migration = """
        ALTER TABLE tokens ADD COLUMN is_session INTEGER NOT NULL DEFAULT 0;
        UPDATE tokens SET is_session=1 WHERE user_id IS NOT NULL AND name LIKE 'user:%';
        """
        with tempfile.TemporaryDirectory() as data_dir:
            path = data_dir + "/obsi.db"
            conn = self._old_db(path, MIGRATIONS[:4])
            conn.execute("INSERT INTO users(id,username,password_hash,created_at) VALUES('u_old','alice','hash',1)")
            conn.execute("INSERT INTO tokens(id,name,kind,token_hash,grants,created_at,user_id) "
                         "VALUES('tok_old','user:alice','device','h1','{}',1,'u_old')")
            conn.execute("INSERT INTO tokens(id,name,kind,token_hash,grants,created_at,user_id) "
                         "VALUES('tok_mcp','user:assistant','mcp','h2','{}',1,'u_old')")
            conn.executescript(old_prefix_migration + MIGRATIONS[5])
            conn.execute("PRAGMA user_version=6")
            conn.commit()
            conn.close()
            db = Database(path)
            sessions = {r["id"]: r["is_session"] for r in db.query("SELECT id,is_session FROM tokens")}
            self.assertEqual(sessions, {"tok_old": 1, "tok_mcp": 0})
            db.close()

    def test_admin_cli_create_invite_and_list_commands(self):
        with tempfile.TemporaryDirectory() as data_dir:
            def run(*args):
                out, err = io.StringIO(), io.StringIO()
                with patch.dict("os.environ", {"OBSI_FEATURES": "none"}), redirect_stdout(out), redirect_stderr(err):
                    code = main(["--data-dir", data_dir, "admin", *args])
                self.assertEqual(code, 0, err.getvalue())
                return json.loads(out.getvalue())

            invite = run("create-invite", "--name", "CLI signup")
            self.assertTrue(invite["code"].startswith("inv_"))
            self.assertEqual(run("list-invites")[0]["name"], "CLI signup")
            self.assertEqual(run("list-users"), [])
