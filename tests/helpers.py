import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from obsi_server.app import create_app
from obsi_server.config import Config
from obsi_server.mcp import UNTRUSTED_CLOSE, UNTRUSTED_NOTICE, UNTRUSTED_OPEN

GROUPS = [{"cat", "kitten", "feline"}, {"car", "vehicle", "auto"}, {"money", "bank", "finance"}]


class FakeEmbedder:
    """Deterministic concept-bucket embeddings; records every call."""

    def __init__(self):
        self.calls: list[tuple[dict, list[str]]] = []
        self.fail = False

    def __call__(self, cfg, texts):
        if self.fail:
            raise RuntimeError("embedding server down")
        self.calls.append((dict(cfg), list(texts)))
        out = []
        for t in texts:
            words = set(t.lower().replace(".", " ").split())
            out.append([float(len(words & g)) for g in GROUPS] + [0.01])
        return out


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class ServerCase(unittest.TestCase):
    rag_worker = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.embedder = FakeEmbedder()
        cfg = Config(data_dir=Path(self._tmp.name), rag_worker=self.rag_worker)
        self.app = create_app(cfg, embed_fn=self.embedder)
        self.ctx = self.app.state.ctx
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.admin = self.make_token("root", "admin")
        self.vid = self.make_vault("Personal")
        self.dev = self.make_token("laptop", "device", {self.vid: ["read", "write"]})
        self.mcp_tok = self.make_token("claude", "mcp", {self.vid: ["list", "search", "read", "write"]})

    # helpers
    @staticmethod
    def h(tok, **extra):
        return {"Authorization": f"Bearer {tok['token']}", **extra}

    def make_token(self, name, kind, grants=None):
        tok, plain = self.ctx.auth.create(name, kind, grants)
        return {**tok.public(), "token": plain}

    def make_vault(self, name):
        return self.ctx.vaults.create(name)["id"]

    def publish(self, path, text, vid=None, mtime=1000):
        if vid and vid != self.vid:  # vaults the device token has no grant on: write directly
            data = text.encode() if isinstance(text, str) else text
            return self.ctx.ai.put(vid, path, data, mtime)
        r = self.client.put(f"/api/v1/vaults/{vid or self.vid}/ai/files/{path}", content=text.encode()
                            if isinstance(text, str) else text, headers=self.h(self.dev, **{"X-Mtime": str(mtime)}))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def rpc(self, method, params=None, tok=None, mid=1):
        body = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            body["params"] = params
        return self.client.post("/mcp", json=body, headers=self.h(tok or self.mcp_tok))

    def call(self, name, args, tok=None):
        r = self.rpc("tools/call", {"name": name, "arguments": args}, tok)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["result"]

    def data(self, res):
        text = res["content"][0]["text"]
        if text.startswith(UNTRUSTED_NOTICE):
            notice, opening, text, closing = text.split("\n")
            self.assertEqual((opening, closing), (UNTRUSTED_OPEN, UNTRUSTED_CLOSE))
        return json.loads(text)
