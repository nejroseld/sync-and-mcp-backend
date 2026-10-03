import base64
import unittest

from helpers import ServerCase, sha


class ProtocolTests(ServerCase):
    def test_initialize_versions(self):
        for v in ("2025-06-18", "2025-03-26", "2024-11-05"):
            res = self.rpc("initialize", {"protocolVersion": v, "capabilities": {}, "clientInfo": {"name": "t"}}).json()["result"]
            self.assertEqual(res["protocolVersion"], v)
            self.assertIn("tools", res["capabilities"])
        res = self.rpc("initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
        self.assertEqual(res["protocolVersion"], "2025-06-18")
        self.assertEqual(res["serverInfo"]["name"], "obsi-mcp")

    def test_notification_202_and_get_405(self):
        r = self.client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=self.h(self.mcp_tok))
        self.assertEqual((r.status_code, r.content), (202, b""))
        self.assertEqual(self.client.get("/mcp", headers=self.h(self.mcp_tok)).status_code, 405)

    def test_auth(self):
        self.assertEqual(self.client.post("/mcp", json={}).status_code, 401)
        self.assertEqual(self.client.post("/mcp", json={}, headers=self.h(self.dev)).status_code, 403)

    def test_errors(self):
        self.assertEqual(self.rpc("nope").json()["error"]["code"], -32601)
        self.assertEqual(self.rpc("tools/call", {"name": "zzz"}).json()["error"]["code"], -32602)
        r = self.client.post("/mcp", content=b"{bad", headers=self.h(self.mcp_tok))
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, -32700))
        self.assertEqual(self.rpc("ping").json()["result"], {})

    def test_tools_list(self):
        tools = self.rpc("tools/list").json()["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["list_vaults", "list_notes", "search", "read_note", "write_note", "get_change_status"])
        self.assertTrue(all(t["inputSchema"]["type"] == "object" for t in tools))
        self.ctx.config.rag = False
        names = [t["name"] for t in self.rpc("tools/list").json()["result"]["tools"]]
        self.assertNotIn("search", names)


class ToolTests(ServerCase):
    def test_list_vaults(self):
        self.make_vault("Hidden")
        self.publish("a.md", "x")
        d = self.data(self.call("list_vaults", {}))
        self.assertEqual(d, [{"id": self.vid, "name": "Personal", "operations": ["list", "read", "search", "write"],
                              "rag_enabled": True, "notes_count": 1}])
        res = self.call("list_vaults", {})
        self.assertEqual(res["structuredContent"], {"result": d})

    def test_list_notes(self):
        for p in ("A/1.md", "A/2.md", "B/3.md"):
            self.publish(p, p)
        d = self.data(self.call("list_notes", {"vault": self.vid, "prefix": "A/"}))
        self.assertEqual([x["path"] for x in d], ["A/1.md", "A/2.md"])
        self.assertEqual(set(d[0]), {"path", "version", "kind", "size", "mtime"})
        d = self.data(self.call("list_notes", {"vault": self.vid, "limit": 1, "offset": 1}))
        self.assertEqual([x["path"] for x in d], ["A/2.md"])

    def test_list_needs_grant(self):
        tok = self.make_token("s", "mcp", {self.vid: ["search"]})
        self.assertTrue(self.call("list_notes", {"vault": self.vid}, tok)["isError"])
        self.assertTrue(self.call("read_note", {"vault": self.vid, "path": "a.md"}, tok)["isError"])

    def test_read_note_ranges(self):
        text = "l1\nl2\nl3\nl4\n"
        self.publish("a.md", text)
        d = self.data(self.call("read_note", {"vault": self.vid, "path": "a.md"}))
        self.assertEqual((d["content"], d["start_line"], d["end_line"], d["total_lines"], d["version"]),
                         (text, 1, 4, 4, sha(text.encode())))
        d = self.data(self.call("read_note", {"vault": self.vid, "path": "a.md", "start_line": 2, "end_line": 3}))
        self.assertEqual((d["content"], d["start_line"], d["end_line"]), ("l2\nl3\n", 2, 3))
        d = self.data(self.call("read_note", {"vault": self.vid, "path": "a.md", "start_line": 3, "end_line": 99}))
        self.assertEqual((d["content"], d["end_line"]), ("l3\nl4\n", 4))
        self.assertTrue(self.call("read_note", {"vault": self.vid, "path": "a.md", "start_line": 9})["isError"])
        self.assertTrue(self.call("read_note", {"vault": self.vid, "path": "a.md", "start_line": 3, "end_line": 2})["isError"])

    def test_read_image_and_binary(self):
        png = b"\x89PNG\r\n\x1a\nxxxx"
        self.publish("img/a.png", png)
        self.publish("doc/a.pdf", b"%PDF-1.4")
        self.publish("t.txt", "plain text")
        res = self.call("read_note", {"vault": self.vid, "path": "img/a.png"})
        img = [c for c in res["content"] if c["type"] == "image"][0]
        self.assertEqual((img["mimeType"], base64.b64decode(img["data"])), ("image/png", png))
        res = self.call("read_note", {"vault": self.vid, "path": "doc/a.pdf"})
        self.assertEqual([c["type"] for c in res["content"]], ["text"])
        self.assertIn("binary", self.data(res)["note"])
        self.assertEqual(self.data(self.call("read_note", {"vault": self.vid, "path": "t.txt"}))["content"], "plain text")

    def test_read_requires_ai_available(self):
        self.assertTrue(self.call("read_note", {"vault": self.vid, "path": "not-published.md"})["isError"])
        other = self.make_vault("Other")
        self.publish("secret.md", "secret", vid=other)
        res = self.call("read_note", {"vault": other, "path": "secret.md"})
        self.assertTrue(res["isError"])
        self.assertNotIn("secret", res["content"][0]["text"].replace("secret.md", ""))

    def test_vault_by_name(self):
        self.publish("a.md", "x")
        self.assertEqual(len(self.data(self.call("list_notes", {"vault": "Personal"}))), 1)


class SearchTests(ServerCase):
    def test_text_search_fields(self):
        self.publish("a.md", "intro\nthe quick brown fox\njumps\n")
        self.publish("b.md", "nothing relevant")
        res = self.call("search", {"query": "fox", "mode": "text"})
        d = self.data(res)
        self.assertEqual(len(d), 1)
        self.assertEqual({k: d[0][k] for k in ("vault", "path", "version", "start_line", "end_line")},
                         {"vault": self.vid, "path": "a.md", "version": sha(b"intro\nthe quick brown fox\njumps\n"),
                          "start_line": 1, "end_line": 3})
        self.assertIn("fox", d[0]["snippet"])
        self.assertIsInstance(d[0]["score"], float)

    def test_hostile_query_is_safe(self):
        self.publish("a.md", "hello world")
        for q in ('"', "AND OR NOT", "hello*", "a:b", "(", "***"):
            self.call("search", {"query": q, "mode": "text"})
        self.assertTrue(self.call("search", {"query": "   "})["isError"])

    def test_fallback_note_without_embeddings(self):
        self.publish("a.md", "kitten")
        res = self.call("search", {"query": "kitten"})
        self.assertEqual(len(self.data(res)), 1)
        self.assertIn("not configured", res["structuredContent"]["note"])

    def test_no_leak_from_ungranted_vault(self):
        other = self.make_vault("Other")
        self.publish("x.md", "uniqueword here", vid=other)
        self.publish("y.md", "uniqueword there")
        d = self.data(self.call("search", {"query": "uniqueword", "mode": "text"}))
        self.assertEqual([(r["vault"], r["path"]) for r in d], [(self.vid, "y.md")])
        res = self.call("search", {"query": "uniqueword", "vaults": [other]})
        self.assertTrue(res["isError"])
        # grant without search op also yields nothing
        tok = self.make_token("nosearch", "mcp", {other: ["read"]})
        self.assertEqual(self.data(self.call("search", {"query": "uniqueword"}, tok)), [])

    def test_no_leak_after_delete_or_revoke_grant(self):
        self.publish("x.md", "ghostword")
        self.client.delete(f"/api/v1/vaults/{self.vid}/ai/files/x.md", headers=self.h(self.dev))
        self.assertEqual(self.data(self.call("search", {"query": "ghostword"})), [])
        self.publish("z.md", "ghostword2")
        self.ctx.auth.grant(self.mcp_tok["id"], self.vid, ["list"])
        self.assertEqual(self.data(self.call("search", {"query": "ghostword2"})), [])

    def test_stale_chunks_never_returned(self):
        # even if a chunk row outlived its file, the join on ai_files hides it
        self.publish("x.md", "orphanword")
        self.ctx.db.execute("DELETE FROM ai_files WHERE path='x.md'")
        self.assertEqual(self.data(self.call("search", {"query": "orphanword", "mode": "text"})), [])


class RagTests(ServerCase):
    def configure(self, model="m1", base="http://emb/v1"):
        r = self.client.put("/api/v1/admin/settings", headers=self.h(self.admin),
                            json={"embedding": {"base_url": base, "api_key": "sk-secret-1234", "model": model}})
        self.assertEqual(r.status_code, 200)
        return r.json()

    def seed(self):
        self.publish("cats.md", "my kitten sleeps all day")
        self.publish("cars.md", "the auto needs new tires")
        self.publish("bank.md", "money in the bank")

    def test_settings_masked(self):
        s = self.configure()
        self.assertEqual(s["embedding"]["model"], "m1")
        self.assertNotIn("secret", s["embedding"]["api_key"])
        g = self.client.get("/api/v1/admin/settings", headers=self.h(self.admin)).json()
        self.assertTrue(g["embedding"]["api_key"].endswith("1234"))
        # PUT back the masked key keeps the stored one
        self.client.put("/api/v1/admin/settings", headers=self.h(self.admin), json={"embedding": g["embedding"]})
        self.assertEqual(self.ctx.db.get_setting("embedding")["api_key"], "sk-secret-1234")

    def test_semantic_and_hybrid(self):
        self.seed()
        self.configure()
        self.assertEqual(self.ctx.rag.process_pending(), 3)
        d = self.data(self.call("search", {"query": "feline", "mode": "semantic"}))
        self.assertEqual(d[0]["path"], "cats.md")  # no word overlap: only embeddings can find it
        self.assertEqual(self.data(self.call("search", {"query": "feline", "mode": "text"})), [])
        res = self.call("search", {"query": "feline vehicle", "mode": "hybrid", "limit": 2})
        self.assertEqual({r["path"] for r in self.data(res)}, {"cats.md", "cars.md"})
        self.assertNotIn("note", res["structuredContent"])
        h = self.data(self.call("search", {"query": "bank", "mode": "hybrid"}))
        self.assertEqual(h[0]["path"], "bank.md")  # text + semantic agree

    def test_only_ai_available_sent_to_embedder(self):
        other = self.make_vault("Other")
        self.publish("a.md", "kitten")
        self.publish("gone.md", "auto secret")
        self.client.delete(f"/api/v1/vaults/{self.vid}/ai/files/gone.md", headers=self.h(self.dev))
        self.configure()
        self.ctx.rag.process_pending()
        sent = [t for _, texts in self.embedder.calls for t in texts]
        self.assertEqual(sent, ["kitten"])
        self.ctx.vaults.update(other, rag={"enabled": False})
        self.publish("b.md", "money", vid=other)
        self.ctx.rag.process_pending()
        self.assertNotIn("money", [t for _, texts in self.embedder.calls for t in texts])

    def test_embedding_not_called_when_unconfigured(self):
        self.seed()
        self.assertEqual(self.ctx.rag.process_pending(), 0)
        self.assertEqual(self.embedder.calls, [])

    def test_vault_rag_disabled_falls_back(self):
        self.seed()
        self.configure()
        self.ctx.rag.process_pending()
        self.client.patch(f"/api/v1/admin/vaults/{self.vid}", headers=self.h(self.admin), json={"rag": {"enabled": False}})
        res = self.call("search", {"query": "kitten", "mode": "hybrid"})
        self.assertIn("RAG is disabled", res["structuredContent"]["note"])
        self.assertEqual(self.data(res)[0]["path"], "cats.md")

    def test_embedding_failure_does_not_crash(self):
        self.seed()
        self.configure()
        self.embedder.fail = True
        self.assertEqual(self.ctx.rag.process_pending(), -1)
        self.assertIn("RuntimeError", self.ctx.rag.status()["last_error"])
        res = self.call("search", {"query": "kitten", "mode": "hybrid"})  # query embedding fails too
        self.assertIn("failed", res["structuredContent"]["note"])
        self.assertEqual(self.data(res)[0]["path"], "cats.md")
        self.embedder.fail = False
        self.assertEqual(self.ctx.rag.process_pending(), 3)
        self.assertIsNone(self.ctx.rag.status()["last_error"])

    def test_batching(self):
        for i in range(70):
            self.publish(f"n{i}.md", f"note {i} kitten")
        self.configure()
        self.ctx.rag.process_pending()
        self.assertEqual([len(t) for _, t in self.embedder.calls], [32, 32, 6])

    def test_model_change_triggers_reindex(self):
        self.seed()
        self.configure(model="m1")
        self.ctx.rag.process_pending()
        self.assertEqual(self.ctx.rag.status()["embedded"], 3)
        n = len(self.embedder.calls)
        self.configure(model="m2")
        st = self.ctx.rag.status()
        self.assertEqual((st["embedded"], st["pending"]), (0, 3))
        self.ctx.rag.process_pending()
        self.assertEqual(self.embedder.calls[-1][0]["model"], "m2")
        self.assertGreater(len(self.embedder.calls), n)
        self.assertEqual(self.ctx.rag.status()["embedded"], 3)
        keys = {r["emb_key"] for r in self.ctx.db.query("SELECT emb_key FROM chunks")}
        self.assertEqual(keys, {"m2@http://emb/v1"})
        # base_url change also rebuilds; identical settings do not
        self.configure(model="m2")
        self.assertEqual(self.ctx.rag.status()["pending"], 0)
        self.configure(model="m2", base="http://other/v1")
        self.assertEqual(self.ctx.rag.status()["pending"], 3)

    def test_manual_reindex_endpoint(self):
        self.seed()
        self.configure()
        self.ctx.rag.process_pending()
        r = self.client.post(f"/api/v1/admin/vaults/{self.vid}/reindex", headers=self.h(self.admin))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["index"]["pending"], 3)
        self.assertEqual(self.ctx.rag.process_pending(), 3)

    def test_chunk_settings_change_rechunks(self):
        self.publish("long.md", "\n".join(f"line number {i}" for i in range(100)))
        before = self.ctx.db.one("SELECT COUNT(*) c FROM chunks")["c"]
        self.client.patch(f"/api/v1/admin/vaults/{self.vid}", headers=self.h(self.admin), json={"rag": {"chunk_chars": 100, "chunk_overlap": 0}})
        self.assertGreater(self.ctx.db.one("SELECT COUNT(*) c FROM chunks")["c"], before)

    def test_delete_removes_embeddings(self):
        self.seed()
        self.configure()
        self.ctx.rag.process_pending()
        self.client.delete(f"/api/v1/vaults/{self.vid}/ai/files/cats.md", headers=self.h(self.dev))
        d = self.data(self.call("search", {"query": "feline", "mode": "semantic"}))
        self.assertNotIn("cats.md", [r["path"] for r in d])
        self.assertEqual(self.ctx.db.one("SELECT COUNT(*) c FROM chunks WHERE path='cats.md'")["c"], 0)

    def test_semantic_no_leak_between_vaults(self):
        other = self.make_vault("Other")
        self.publish("c.md", "kitten", vid=other)
        self.configure()
        self.ctx.rag.process_pending()
        self.assertEqual(self.data(self.call("search", {"query": "feline", "mode": "semantic"})), [])


class ChunkTests(unittest.TestCase):
    def test_chunking(self):
        from obsi_server.rag import chunk_text
        text = "\n".join(f"line{i:03d}" for i in range(50))
        chunks = chunk_text(text, 100, 20)
        self.assertGreater(len(chunks), 3)
        self.assertEqual(chunks[0][0], 1)
        self.assertEqual(chunks[-1][1], 50)
        self.assertTrue(all(len(c[2]) <= 100 for c in chunks))
        # overlap: consecutive chunks share lines, and always progress
        self.assertTrue(all(b[0] > a[0] for a, b in zip(chunks, chunks[1:])))
        self.assertLessEqual(chunks[1][0], chunks[0][1])
        long = chunk_text("x" * 500, 100, 0)
        self.assertEqual(len(long), 5)
        self.assertEqual(chunk_text("", 100, 0), [])


class ConfigTests(unittest.TestCase):
    def test_env(self):
        from obsi_server.config import Config
        c = Config.load({"OBSI_DATA_DIR": "/x", "OBSI_PORT": "9", "OBSI_FEATURES": "ai,mcp"})
        self.assertEqual((str(c.data_dir), c.port, c.features), ("/x", 9, {"ai": True, "mcp": True, "rag": False}))
        c = Config.load({"OBSI_FEATURES": "none"})
        self.assertEqual(c.features, {"ai": False, "mcp": False, "rag": False})


if __name__ == "__main__":
    unittest.main()
