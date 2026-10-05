import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from obsi_server.cli import main
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


class UserAdminCliTests(unittest.TestCase):
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
