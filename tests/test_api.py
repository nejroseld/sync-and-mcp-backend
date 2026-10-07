import unittest

from helpers import ServerCase, sha


class AuthTests(ServerCase):
    def test_health_no_auth(self):
        r = self.client.get("/api/v1/health")
        self.assertEqual(r.json(), {"ok": True})
        r = self.client.get("/api/v1/health", headers=self.h(self.dev))
        self.assertEqual(r.json()["features"], {"ai": True, "mcp": True, "rag": True})

    def test_api_docs_are_off_and_responses_are_not_cached(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 404)
        r = self.client.post("/api/v1/admin/tokens", headers=self.h(self.admin), json={"name": "x", "kind": "device"})
        self.assertEqual(r.headers["cache-control"], "no-store")

    def test_cors_allows_only_listed_origins(self):
        def preflight(origin):
            return self.client.options("/api/v1/me", headers={
                "Origin": origin, "Access-Control-Request-Method": "GET"})
        self.assertEqual(preflight("app://obsidian.md").headers.get("access-control-allow-origin"), "app://obsidian.md")
        self.assertIsNone(preflight("https://evil.example").headers.get("access-control-allow-origin"))
        from obsi_server.config import Config
        with self.assertRaises(ValueError):
            Config.load({"OBSI_CORS_ORIGINS": "*"})

    def test_overflowing_number_header_is_422(self):
        for value in ("inf", "nan", "1e400", "1e30"):
            r = self.client.put(f"/api/v1/vaults/{self.vid}/files/k", content=b"x",
                                headers=self.h(self.dev, **{"X-Mtime-Cli": value}))
            self.assertEqual(r.status_code, 422, value)

    def test_missing_and_bad_token(self):
        for hdr in ({}, {"Authorization": "Bearer nope"}):
            r = self.client.get("/api/v1/me", headers=hdr)
            self.assertEqual(r.status_code, 401)
            self.assertEqual(r.json()["error"]["code"], "unauthorized")

    def test_me_and_last_used(self):
        r = self.client.get("/api/v1/me", headers=self.h(self.dev))
        self.assertEqual(r.json()["grants"], {self.vid: ["read", "write"]})
        tok = self.ctx.auth.get(self.dev["id"])
        self.assertIsNotNone(tok.last_used_at)

    def test_tokens_stored_hashed(self):
        row = self.ctx.db.one("SELECT token_hash FROM tokens WHERE id=?", (self.dev["id"],))
        self.assertEqual(row["token_hash"], sha(self.dev["token"].encode()))

    def test_admin_create_list_revoke(self):
        r = self.client.post("/api/v1/admin/tokens", headers=self.h(self.admin),
                             json={"name": "phone", "kind": "device", "grants": {self.vid: ["read"]}})
        self.assertEqual(r.status_code, 200)
        tok = r.json()
        self.assertIn("token", tok)
        self.assertEqual(self.client.get("/api/v1/me", headers={"Authorization": "Bearer " + tok["token"]}).status_code, 200)
        r = self.client.post(f"/api/v1/admin/tokens/{tok['id']}/revoke", headers=self.h(self.admin))
        self.assertIsNotNone(r.json()["revoked_at"])
        self.assertEqual(self.client.get("/api/v1/me", headers={"Authorization": "Bearer " + tok["token"]}).status_code, 401)
        listed = self.client.get("/api/v1/admin/tokens", headers=self.h(self.admin)).json()["tokens"]
        self.assertNotIn("token", listed[0])
        self.assertTrue(any(t["id"] == tok["id"] and t["revoked_at"] for t in listed))

    def test_admin_requires_admin_kind(self):
        for tok in (self.dev, self.mcp_tok):
            r = self.client.get("/api/v1/admin/tokens", headers=self.h(tok))
            self.assertEqual(r.status_code, 403)

    def test_invalid_grants_422(self):
        r = self.client.post("/api/v1/admin/tokens", headers=self.h(self.admin),
                             json={"name": "x", "kind": "device", "grants": {self.vid: ["search"]}})
        self.assertEqual(r.status_code, 422)
        r = self.client.post("/api/v1/admin/tokens", headers=self.h(self.admin),
                             json={"name": "x", "kind": "device", "grants": {"v_missing": ["read"]}})
        self.assertEqual(r.status_code, 422)

    def test_revoked_mcp_token_stops_working(self):
        self.assertEqual(self.rpc("ping").status_code, 200)
        self.client.post(f"/api/v1/admin/tokens/{self.mcp_tok['id']}/revoke", headers=self.h(self.admin))
        r = self.rpc("ping")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.headers["www-authenticate"], "Bearer")

    def test_vault_listing_by_grant(self):
        other = self.make_vault("Work")
        mine = self.client.get("/api/v1/vaults", headers=self.h(self.dev)).json()["vaults"]
        self.assertEqual([v["id"] for v in mine], [self.vid])
        allv = self.client.get("/api/v1/vaults", headers=self.h(self.admin)).json()["vaults"]
        self.assertEqual({v["id"] for v in allv}, {self.vid, other})

    def test_vault_admin_patch_delete(self):
        r = self.client.post("/api/v1/admin/vaults", headers=self.h(self.admin), json={"name": "Work"})
        vid = r.json()["id"]
        self.assertTrue(vid.startswith("v_") and len(vid) == 18)
        r = self.client.patch(f"/api/v1/admin/vaults/{vid}", headers=self.h(self.admin),
                              json={"name": "W2", "rag": {"enabled": False}})
        self.assertEqual(r.json()["rag"]["enabled"], False)
        self.assertEqual(self.client.delete(f"/api/v1/admin/vaults/{vid}", headers=self.h(self.admin)).status_code, 204)
        self.assertEqual(self.client.delete(f"/api/v1/admin/vaults/{vid}", headers=self.h(self.admin)).status_code, 404)


class GrantTests(ServerCase):
    def test_grant_enforcement_storage(self):
        other = self.make_vault("Other")
        ro = self.make_token("ro", "device", {self.vid: ["read"]})
        base = f"/api/v1/vaults/{self.vid}/files"
        self.assertEqual(self.client.put(f"{base}/k", content=b"x", headers=self.h(ro)).status_code, 403)
        self.assertEqual(self.client.get(base, headers=self.h(ro)).status_code, 200)
        self.assertEqual(self.client.get(f"/api/v1/vaults/{other}/files", headers=self.h(ro)).status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/vaults/{other}/ai/manifest", headers=self.h(ro)).status_code, 403)
        self.assertEqual(self.client.delete(f"{base}/k", headers=self.h(ro)).status_code, 403)
        # mcp / admin tokens may not use device endpoints
        self.assertEqual(self.client.get(base, headers=self.h(self.mcp_tok)).status_code, 403)
        self.assertEqual(self.client.get(base, headers=self.h(self.admin)).status_code, 403)

    def test_unknown_vault_404(self):
        tok = self.make_token("d", "device", {"v_ghost": ["read"]})
        self.assertEqual(self.client.get("/api/v1/vaults/v_ghost/files", headers=self.h(tok)).status_code, 404)

    def test_ai_write_needs_write(self):
        ro = self.make_token("ro", "device", {self.vid: ["read"]})
        r = self.client.put(f"/api/v1/vaults/{self.vid}/ai/files/a.md", content=b"x", headers=self.h(ro))
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.client.post(f"/api/v1/vaults/{self.vid}/ai/clear", headers=self.h(ro)).status_code, 403)
        self.assertEqual(self.client.get(f"/api/v1/vaults/{self.vid}/changes", headers=self.h(ro)).status_code, 403)


class StorageTests(ServerCase):
    def url(self, key=""):
        return f"/api/v1/vaults/{self.vid}/files" + (f"/{key}" if key else "")

    def test_crud_head_folder(self):
        h = self.h(self.dev, **{"X-Mtime-Cli": "111", "X-Ctime-Cli": "100"})
        r = self.client.put(self.url("abc/def"), content=b"cipher", headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"key": "abc/def", "size": 6, "mtime_cli": 111, "ctime_cli": 100,
                                    "mtime_svr": r.json()["mtime_svr"], "etag": sha(b"cipher")})
        r = self.client.put(self.url("abc/"), content=b"", headers=h)
        self.assertEqual(r.json()["size"], 0)
        files = self.client.get(self.url(), headers=self.h(self.dev)).json()["files"]
        self.assertEqual([f["key"] for f in files], ["abc/", "abc/def"])
        g = self.client.get(self.url("abc/def"), headers=self.h(self.dev))
        self.assertEqual(g.content, b"cipher")
        self.assertEqual(g.headers["etag"], sha(b"cipher"))
        self.assertEqual(g.headers["x-mtime-cli"], "111")
        self.assertEqual(g.headers["x-ctime-cli"], "100")
        self.assertIn("x-mtime-svr", g.headers)
        hd = self.client.head(self.url("abc/def"), headers=self.h(self.dev))
        self.assertEqual((hd.status_code, hd.content, hd.headers["content-length"]), (200, b"", "6"))
        self.assertEqual(self.client.head(self.url("nope"), headers=self.h(self.dev)).status_code, 404)
        self.assertEqual(self.client.get(self.url("nope"), headers=self.h(self.dev)).json()["error"]["code"], "not_found")
        # overwrite
        self.client.put(self.url("abc/def"), content=b"v2", headers=h)
        self.assertEqual(self.client.get(self.url("abc/def"), headers=self.h(self.dev)).content, b"v2")
        # folder get/delete
        self.assertEqual(self.client.get(self.url("abc/"), headers=self.h(self.dev)).content, b"")
        self.assertEqual(self.client.delete(self.url("abc/def"), headers=self.h(self.dev)).status_code, 204)
        self.assertEqual(self.client.delete(self.url("abc/def"), headers=self.h(self.dev)).status_code, 204)
        self.assertEqual(self.client.get(self.url("abc/def"), headers=self.h(self.dev)).status_code, 404)

    def test_blob_layout_and_isolation(self):
        self.client.put(self.url("k1"), content=b"1", headers=self.h(self.dev))
        p = self.ctx.storage.blob_path(self.vid, "k1")
        self.assertTrue(p.exists())
        self.assertEqual(p.name, sha(b"k1"))
        self.assertEqual(p.parent.parts[-2:], (self.vid, "blobs"))
        self.assertEqual([x.name for x in p.parent.iterdir()], [sha(b"k1")])  # no temp leftovers

    def test_body_limit(self):
        self.ctx.config.max_body_bytes = 10
        r = self.client.put(self.url("big"), content=b"x" * 11, headers=self.h(self.dev))
        self.assertEqual(r.status_code, 413)
        self.assertEqual(list((self.ctx.config.data_dir / "tmp").iterdir()), [])

    def test_vault_quota_covers_sync_ai_and_mcp_writes(self):
        self.ctx.config.vault_quota_bytes = 100
        self.assertEqual(self.client.put(self.url("a"), content=b"x" * 60, headers=self.h(self.dev)).status_code, 200)
        r = self.client.put(self.url("b"), content=b"x" * 50, headers=self.h(self.dev))
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (507, "quota_exceeded"))
        r = self.client.put(f"/api/v1/vaults/{self.vid}/ai/files/n.md", content=b"x" * 50, headers=self.h(self.dev))
        self.assertEqual(r.status_code, 507)
        res = self.call("write_note", {"vault": self.vid, "path": "n.md", "content": "x" * 50, "base_version": None})
        self.assertTrue(res["isError"])
        self.assertEqual(self.client.put(self.url("a"), content=b"x" * 90, headers=self.h(self.dev)).status_code, 200)

    def test_ai_file_limit_is_separate_from_sync_limit(self):
        self.ctx.config.max_ai_file_bytes = 10
        r = self.client.put(f"/api/v1/vaults/{self.vid}/ai/files/a.md", content=b"x" * 11, headers=self.h(self.dev))
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self.client.put(self.url("k"), content=b"x" * 11, headers=self.h(self.dev)).status_code, 200)

    def test_delete_vault_removes_blobs(self):
        self.client.put(self.url("k"), content=b"1", headers=self.h(self.dev))
        p = self.ctx.storage.blob_path(self.vid, "k")
        self.client.delete(f"/api/v1/admin/vaults/{self.vid}", headers=self.h(self.admin))
        self.assertFalse(p.exists())
        self.assertEqual(self.ctx.auth.get(self.dev["id"]).grants, {})


class AiStoreTests(ServerCase):
    def base(self):
        return f"/api/v1/vaults/{self.vid}"

    def test_publish_manifest(self):
        m = self.publish("Projects/idea.md", "# idea\nhello", mtime=5)
        self.assertEqual(m, {"path": "Projects/idea.md", "version": sha(b"# idea\nhello"), "mtime": 5,
                             "size": 12, "kind": "note"})
        self.publish("img/a.png", b"\x89PNG", mtime=5)
        man = self.client.get(self.base() + "/ai/manifest", headers=self.h(self.dev)).json()["files"]
        self.assertEqual([(f["path"], f["kind"]) for f in man], [("Projects/idea.md", "note"), ("img/a.png", "attachment")])

    def test_stale_publish_409(self):
        self.publish("a.md", "new", mtime=2000)
        r = self.client.put(self.base() + "/ai/files/a.md", content=b"old", headers=self.h(self.dev, **{"X-Mtime": "1000"}))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["code"], "stale_publish")
        # same hash with older mtime is fine; newer mtime with different hash is fine
        self.assertEqual(self.client.put(self.base() + "/ai/files/a.md", content=b"new",
                                         headers=self.h(self.dev, **{"X-Mtime": "1000"})).status_code, 200)
        self.assertEqual(self.publish("a.md", "newer", mtime=3000)["version"], sha(b"newer"))

    def test_invalid_path(self):
        r = self.client.put(self.base() + "/ai/files/%2e%2e/b.md", content=b"x", headers=self.h(self.dev))
        self.assertIn(r.status_code, (404, 422))
        from obsi_server.ai_store import norm_path
        from obsi_server.errors import ApiError
        for bad in ("", "/abs.md", "a/../b", "a//b", "a\\b"):
            with self.assertRaises(ApiError):
                norm_path(bad)

    def test_delete_removes_from_index_and_search(self):
        self.publish("a.md", "zebra stripes")
        self.assertEqual(len(self.data(self.call("search", {"query": "zebra", "mode": "text"}))), 1)
        self.assertEqual(self.ctx.db.one("SELECT COUNT(*) c FROM chunks")["c"], 1)
        r = self.client.delete(self.base() + "/ai/files/a.md", headers=self.h(self.dev))
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.ctx.db.one("SELECT COUNT(*) c FROM chunks")["c"], 0)
        self.assertEqual(self.ctx.db.one("SELECT COUNT(*) c FROM chunks_fts")["c"], 0)
        self.assertEqual(self.data(self.call("search", {"query": "zebra", "mode": "text"})), [])
        self.assertTrue(self.call("read_note", {"vault": self.vid, "path": "a.md"})["isError"])

    def test_clear(self):
        other = self.make_vault("Other")
        self.publish("a.md", "one")
        self.publish("b.md", "two")
        self.publish("c.md", "three", vid=other)
        r = self.client.post(self.base() + "/ai/clear", headers=self.h(self.dev))
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.client.get(self.base() + "/ai/manifest", headers=self.h(self.dev)).json()["files"], [])
        self.assertEqual(self.ctx.ai.count(other), 1)
        self.assertEqual(self.ctx.db.one("SELECT COUNT(*) c FROM chunks")["c"], 1)

    def test_update_reindexes_chunks(self):
        self.publish("a.md", "alpha content", mtime=1)
        self.publish("a.md", "beta content", mtime=2)
        self.assertEqual(self.data(self.call("search", {"query": "alpha", "mode": "text"})), [])
        self.assertEqual(len(self.data(self.call("search", {"query": "beta", "mode": "text"}))), 1)

    def test_feature_disabled(self):
        self.ctx.config.ai = False
        self.assertEqual(self.client.get(self.base() + "/ai/manifest", headers=self.h(self.dev)).status_code, 404)
        self.assertEqual(self.rpc("ping").status_code, 404)  # mcp needs ai


class ChangesTests(ServerCase):
    def test_flow_and_ack(self):
        m = self.publish("n.md", "old")
        res = self.call("write_note", {"vault": self.vid, "path": "n.md", "content": "new", "base_version": m["version"]})
        d = self.data(res)
        self.assertEqual(d["status"], "pending")
        self.assertEqual(self.data(self.call("read_note", {"vault": self.vid, "path": "n.md"}))["pending_changes"], 1)
        r = self.client.get(f"/api/v1/vaults/{self.vid}/changes?status=pending", headers=self.h(self.dev))
        ch = r.json()["changes"]
        self.assertEqual(len(ch), 1)
        self.assertEqual((ch[0]["id"], ch[0]["op"], ch[0]["content"], ch[0]["base_version"], ch[0]["token_name"],
                          ch[0]["token_id"]), (d["change_id"], "update", "new", m["version"], "claude", self.mcp_tok["id"]))
        r = self.client.post(f"/api/v1/vaults/{self.vid}/changes/{d['change_id']}/ack", headers=self.h(self.dev),
                             json={"status": "applied", "new_version": sha(b"new")})
        self.assertEqual(r.json()["status"], "applied")
        again = self.client.post(f"/api/v1/vaults/{self.vid}/changes/{d['change_id']}/ack", headers=self.h(self.dev),
                                 json={"status": "applied"})
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.client.get(f"/api/v1/vaults/{self.vid}/changes", headers=self.h(self.dev)).json()["changes"], [])
        st = self.data(self.call("get_change_status", {"change_id": d["change_id"]}))
        self.assertEqual((st["status"], st["new_version"]), ("applied", sha(b"new")))
        self.assertEqual(self.data(self.call("read_note", {"vault": self.vid, "path": "n.md"}))["pending_changes"], 0)

    def test_ack_validation_and_unknown(self):
        r = self.client.post(f"/api/v1/vaults/{self.vid}/changes/c_x/ack", headers=self.h(self.dev), json={"status": "applied"})
        self.assertEqual(r.status_code, 404)
        r = self.client.post(f"/api/v1/vaults/{self.vid}/changes/c_x/ack", headers=self.h(self.dev), json={"status": "bogus"})
        self.assertEqual(r.status_code, 422)

    def test_ack_other_vault_not_found(self):
        other = self.make_vault("O")
        tok = self.make_token("d2", "device", {other: ["read", "write"]})
        d = self.data(self.call("write_note", {"vault": self.vid, "path": "new.md", "content": "x", "base_version": None}))
        r = self.client.post(f"/api/v1/vaults/{other}/changes/{d['change_id']}/ack", headers=self.h(tok), json={"status": "applied"})
        self.assertEqual(r.status_code, 404)

    def test_conflicts(self):
        m = self.publish("n.md", "old")
        res = self.call("write_note", {"vault": self.vid, "path": "n.md", "content": "x", "base_version": "bad"})
        self.assertTrue(res["isError"])
        self.assertEqual(self.data(res)["version"], m["version"])
        res = self.call("write_note", {"vault": self.vid, "path": "n.md", "content": "x", "base_version": None})
        self.assertTrue(res["isError"])
        res = self.call("write_note", {"vault": self.vid, "path": "ghost.md", "content": "x", "base_version": m["version"]})
        self.assertTrue(res["isError"])
        ok = self.call("write_note", {"vault": self.vid, "path": "brand/new.md", "content": "x", "base_version": None})
        self.assertNotIn("isError", ok)
        self.assertEqual(self.client.get(f"/api/v1/vaults/{self.vid}/changes", headers=self.h(self.dev)).json()["changes"][0]["op"], "create")

    def test_write_note_size_and_queue_limits(self):
        import obsi_server.mcp as mcp
        res = self.call("write_note", {"vault": self.vid, "path": "big.md",
                                       "content": "x" * (mcp.MAX_CHANGE_BYTES + 1), "base_version": None})
        self.assertTrue(res["isError"])
        self.addCleanup(setattr, mcp, "MAX_PENDING_CHANGES", mcp.MAX_PENDING_CHANGES)
        mcp.MAX_PENDING_CHANGES = 2
        for i in range(2):
            self.assertNotIn("isError", self.call("write_note", {"vault": self.vid, "path": f"n{i}.md",
                                                                 "content": "x", "base_version": None}))
        self.assertTrue(self.call("write_note", {"vault": self.vid, "path": "n3.md", "content": "x",
                                                 "base_version": None})["isError"])

    def test_write_requires_grant(self):
        ro = self.make_token("ro", "mcp", {self.vid: ["list", "read", "search"]})
        res = self.call("write_note", {"vault": self.vid, "path": "x.md", "content": "x", "base_version": None}, ro)
        self.assertTrue(res["isError"])
        d = self.data(self.call("write_note", {"vault": self.vid, "path": "x.md", "content": "x", "base_version": None}))
        res = self.call("get_change_status", {"change_id": d["change_id"]}, ro)
        self.assertTrue(res["isError"])


if __name__ == "__main__":
    unittest.main()
