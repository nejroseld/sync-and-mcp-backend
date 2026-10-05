import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from obsi_server.app import create_app
from obsi_server.auth import AuthService
from obsi_server.config import Config
from obsi_server.db import Database
from obsi_server.errors import ApiError
from obsi_server.rate_limit import Limit, RateLimiter


class SharedLimiterTests(unittest.TestCase):
    def test_password_hash_is_upgraded_after_legacy_login(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "db.sqlite")
            try:
                auth = AuthService(db)
                invite = auth.create_invite("test")["code"]
                user, _ = auth.register(invite, "alice", "a sufficiently long password")
                current = db.one("SELECT password_hash FROM users WHERE id=?", (user["id"],))["password_hash"]
                self.assertTrue(current.startswith("scrypt$16384$8$5$"))
                salt = bytes(range(16))
                legacy = hashlib.scrypt(b"a sufficiently long password", salt=salt,
                                        n=2**14, r=8, p=1)
                old = f"scrypt$16384$8$1${salt.hex()}${legacy.hex()}"
                db.execute("UPDATE users SET password_hash=? WHERE id=?", (old, user["id"]))
                auth.login("alice", "a sufficiently long password")
                upgraded = db.one("SELECT password_hash FROM users WHERE id=?", (user["id"],))["password_hash"]
                self.assertTrue(upgraded.startswith("scrypt$16384$8$5$"))
                self.assertNotEqual(upgraded, old)
            finally:
                db.close()

    def test_database_rejects_shared_directory_without_changing_it(self):
        with tempfile.TemporaryDirectory() as d:
            shared = Path(d) / "shared"
            shared.mkdir(mode=0o755)
            shared.chmod(0o755)
            with self.assertRaises(PermissionError):
                Database(shared / "db.sqlite")
            self.assertEqual(shared.stat().st_mode & 0o777, 0o755)

    def test_burst_refills_and_is_shared_between_connections(self):
        with tempfile.TemporaryDirectory() as d:
            first = Database(Path(d) / "db.sqlite")
            second = Database(Path(d) / "db.sqlite")
            try:
                a, b = RateLimiter(first), RateLimiter(second)
                bucket = Limit("login:test", 2, 2_000)
                with patch("obsi_server.rate_limit.now_ms", return_value=10_000):
                    a.check(bucket)
                    b.check(bucket)
                    with self.assertRaises(ApiError) as blocked:
                        a.check(bucket)
                    self.assertEqual(blocked.exception.status, 429)
                with patch("obsi_server.rate_limit.now_ms", return_value=11_000):
                    b.check(bucket)
            finally:
                first.close()
                second.close()


async def _inline_thread(fn, *args, **kwargs):
    # Keep these route checks independent of a worker thread; production uses
    # AnyIO's normal threadpool.
    return fn(*args)


class SecurityRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(Config(data_dir=Path(self.tmp.name), rag_worker=False,
                                     sync_requests_per_minute=2))
        self.inline = patch("anyio.to_thread.run_sync", new=_inline_thread)
        self.inline.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        self.inline.stop()
        self.app.state.ctx.db.close()
        self.tmp.cleanup()

    async def test_login_throttle_and_json_body_cap(self):
        body = {"username": "nobody", "password": "wrong password"}
        for _ in range(10):
            self.assertEqual((await self.client.post("/api/v1/auth/login", json=body)).status_code, 401)
        blocked = await self.client.post("/api/v1/auth/login", json=body)
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["error"]["code"], "rate_limited")
        self.assertGreaterEqual(int(blocked.headers["retry-after"]), 1)
        oversized = await self.client.post("/api/v1/auth/login", content=b"x" * 16_385)
        self.assertEqual(oversized.status_code, 413)

        async def chunks():
            yield b"x" * 8192
            yield b"x" * 8193

        streamed = await self.client.post("/api/v1/auth/login", content=chunks())
        self.assertEqual(streamed.status_code, 413)
        self.assertEqual(streamed.json()["error"]["code"], "payload_too_large")
        admin_json = await self.client.post("/api/v1/admin/tokens", content=b"x" * (1024 * 1024 + 1))
        self.assertEqual(admin_json.status_code, 413)

    async def test_token_limit_keeps_small_sync_burst_usable(self):
        _, secret = self.app.state.ctx.auth.create("device", "device")
        headers = {"Authorization": "Bearer " + secret}
        for _ in range(2):
            self.assertEqual((await self.client.get("/api/v1/me", headers=headers)).status_code, 200)
        self.assertEqual((await self.client.get("/api/v1/me", headers=headers)).status_code, 429)

    async def test_sync_upload_above_json_cap_is_allowed(self):
        ctx = self.app.state.ctx
        vid = ctx.vaults.create("sync")["id"]
        _, secret = ctx.auth.create("sync-device", "device", {vid: ["read", "write"]})
        headers = {"Authorization": "Bearer " + secret}
        data = b"x" * (1024 * 1024 + 1)
        put = await self.client.put(f"/api/v1/vaults/{vid}/files/opaque", content=data,
                                    headers=headers)
        self.assertEqual(put.status_code, 200)
        get = await self.client.get(f"/api/v1/vaults/{vid}/files/opaque", headers=headers)
        self.assertEqual(get.content, data)

    async def test_mcp_batch_is_bounded_without_restricting_normal_calls(self):
        ctx = self.app.state.ctx
        _, secret = ctx.auth.create("mcp", "mcp")
        headers = {"Authorization": "Bearer " + secret}
        ping = lambda number: {"jsonrpc": "2.0", "id": number, "method": "ping"}
        allowed = await self.client.post("/mcp", json=[ping(i) for i in range(100)], headers=headers)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(len(allowed.json()), 100)
        blocked = await self.client.post("/mcp", json=[ping(i) for i in range(101)], headers=headers)
        self.assertEqual(blocked.status_code, 413)

    async def test_user_cannot_read_another_users_vault(self):
        ctx = self.app.state.ctx
        ctx.config.sync_requests_per_minute = 10
        alice_invite = ctx.auth.create_invite("alice")["code"]
        bob_invite = ctx.auth.create_invite("bob")["code"]
        alice = (await self.client.post("/api/v1/auth/register", json={
            "invite_code": alice_invite, "username": "alice", "password": "alice-password-long",
        })).json()
        bob = (await self.client.post("/api/v1/auth/register", json={
            "invite_code": bob_invite, "username": "bob", "password": "bob-password-long",
        })).json()
        ah = {"Authorization": "Bearer " + alice["token"]}
        bh = {"Authorization": "Bearer " + bob["token"]}
        vid = (await self.client.post("/api/v1/vaults", json={"name": "private"}, headers=ah)).json()["id"]
        self.assertEqual((await self.client.put(f"/api/v1/vaults/{vid}/ai/files/secret.md",
                                                content=b"private", headers=ah)).status_code, 200)
        self.assertEqual((await self.client.get(f"/api/v1/vaults/{vid}/ai/manifest",
                                                 headers=bh)).status_code, 403)
        self.assertEqual((await self.client.get(f"/api/v1/vaults/{vid}/files/anything",
                                                 headers=bh)).status_code, 403)
        self.assertNotIn(vid, [v["id"] for v in
                               (await self.client.get("/api/v1/vaults", headers=bh)).json()["vaults"]])


if __name__ == "__main__":
    unittest.main()
