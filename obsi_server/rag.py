"""RAG: line-based chunks, FTS5 text search, embeddings (OpenAI-compatible), hybrid RRF.

Only AI Available content ever reaches the chunk tables and the embedding endpoint.
Chunks and FTS rows are written synchronously on publish; embeddings are filled in by a
background worker (`process_pending`) and may lag behind.
"""
from __future__ import annotations

import logging
import re
import threading
from array import array
from typing import Callable, Sequence

import httpx

from .ai_store import AiStore, is_text
from .db import Database
from .vaults import VaultService

try:  # optional acceleration
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover
    np = None

log = logging.getLogger("obsi.rag")

BATCH_SIZE = 32
MAX_TEXT_CHARS = 8000
RRF_K = 60

EmbedFn = Callable[[dict, list[str]], list[list[float]]]


def http_embed(cfg: dict, texts: list[str], timeout: float = 60.0) -> list[list[float]]:
    """POST {base_url}/embeddings (OpenAI format). Raises on any failure."""
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    r = httpx.post(cfg["base_url"].rstrip("/") + "/embeddings", headers=headers, timeout=timeout,
                   json={"model": cfg["model"], "input": [t[:MAX_TEXT_CHARS] for t in texts]})
    r.raise_for_status()
    data = sorted(r.json()["data"], key=lambda d: d.get("index", 0))
    if len(data) != len(texts):
        raise ValueError("embedding count mismatch")
    return [d["embedding"] for d in data]


def chunk_text(text: str, chunk_chars: int, overlap: int) -> list[tuple[int, int, str]]:
    """Split into (start_line, end_line, text) chunks of ~chunk_chars with line-based overlap."""
    cc = max(50, chunk_chars)
    pieces: list[tuple[int, str]] = []
    for no, line in enumerate(text.splitlines(), 1):
        if len(line) <= cc:
            pieces.append((no, line))
        else:
            pieces += [(no, line[k:k + cc]) for k in range(0, len(line), cc)]
    out: list[tuple[int, int, str]] = []
    i = 0
    while i < len(pieces):
        j, size = i, 0
        while j < len(pieces) and (j == i or size + len(pieces[j][1]) + 1 <= cc):
            size += len(pieces[j][1]) + 1
            j += 1
        body = "\n".join(p[1] for p in pieces[i:j])
        if body.strip():
            out.append((pieces[i][0], pieces[j - 1][0], body))
        if j >= len(pieces):
            break
        k, acc = j, 0
        while k > i + 1 and acc + len(pieces[k - 1][1]) + 1 <= overlap:
            acc += len(pieces[k - 1][1]) + 1
            k -= 1
        i = k
    return out


def _normalize(v: Sequence[float]) -> array:
    a = array("f", v)
    norm = sum(x * x for x in a) ** 0.5 or 1.0
    return array("f", (x / norm for x in a))


def _snippet(text: str, n: int = 300) -> str:
    t = re.sub(r"\s+", " ", text).strip()
    return t if len(t) <= n else t[:n].rstrip() + "..."


def fts_query(q: str) -> str | None:
    toks = re.findall(r"\w+", q)
    return " OR ".join('"%s"' % t for t in toks) if toks else None


class RagService:
    def __init__(self, db: Database, vaults: VaultService, ai: AiStore, embed_fn: EmbedFn = http_embed):
        self.db, self.vaults, self.ai = db, vaults, ai
        self.embed_fn = embed_fn
        self.last_error: str | None = None
        self._event = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- wiring -------------------------------------------------------------
    def attach(self) -> None:
        """Subscribe to AI store / vault events (called only when the rag feature is on)."""
        self.ai.on_put.append(lambda vid, meta, data: self.index_file(vid, meta, data))
        self.ai.on_delete.append(lambda vid, path: self.remove_file(vid, path))
        self.ai.on_clear.append(lambda vid: self.remove_vault(vid))
        self.vaults.on_delete.append(self.remove_vault)
        self.vaults.on_rag_settings_changed.append(self.reindex_vault)

    # ---- embedding settings ----------------------------------------------------
    def embedding_cfg(self) -> dict | None:
        cfg = self.db.get_setting("embedding")
        return cfg if cfg and cfg.get("base_url") and cfg.get("model") else None

    @staticmethod
    def _key(cfg: dict) -> str:
        return f"{cfg['model']}@{cfg['base_url'].rstrip('/')}"

    def set_embedding(self, base_url: str | None, api_key: str | None, model: str | None) -> bool:
        """Store settings; returns True (and drops all vectors) if model or base_url changed."""
        old = self.db.get_setting("embedding", {}) or {}
        new = dict(old)
        for k, v in (("base_url", base_url), ("api_key", api_key), ("model", model)):
            if v is not None:
                new[k] = v
        self.db.set_setting("embedding", new)
        changed = (old.get("model"), (old.get("base_url") or "").rstrip("/")) != (
            new.get("model"), (new.get("base_url") or "").rstrip("/"))
        if changed:
            self.db.execute("UPDATE chunks SET embedding=NULL, emb_key=NULL")
            self.last_error = None
        self.kick()
        return changed

    def status(self) -> dict:
        r = self.db.one("SELECT COUNT(*) total, SUM(embedding IS NOT NULL) emb FROM chunks")
        total, emb = r["total"], r["emb"] or 0
        return {"chunks": total, "embedded": emb, "pending": self._pending_count(), "last_error": self.last_error}

    # ---- indexing ----------------------------------------------------------------
    def index_file(self, vid: str, meta: dict, data: bytes) -> None:
        path = meta["path"]
        if not is_text(path):
            self.remove_file(vid, path)
            return
        v = self.vaults.get(vid)
        if not v:
            return
        text = data.decode("utf-8", errors="replace")
        chunks = chunk_text(text, v["rag"]["chunk_chars"], v["rag"]["chunk_overlap"])
        with self.db.transaction():
            self._delete_chunks("vault_id=? AND path=?", (vid, path))
            for s, e, body in chunks:
                cur = self.db.execute(
                    "INSERT INTO chunks(vault_id,path,version,start_line,end_line,text) VALUES(?,?,?,?,?,?)",
                    (vid, path, meta["version"], s, e, body))
                self.db.execute("INSERT INTO chunks_fts(rowid,text) VALUES(?,?)", (cur.lastrowid, body))
        self.kick()

    def _delete_chunks(self, where: str, params: tuple) -> None:
        self.db.execute(f"DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE {where})", params)
        self.db.execute(f"DELETE FROM chunks WHERE {where}", params)

    def remove_file(self, vid: str, path: str) -> None:
        with self.db.transaction():
            self._delete_chunks("vault_id=? AND path=?", (vid, path))

    def remove_vault(self, vid: str) -> None:
        with self.db.transaction():
            self._delete_chunks("vault_id=?", (vid,))

    def reindex_vault(self, vid: str) -> None:
        """Rebuild chunks (current chunk settings) and drop vectors for one vault."""
        self.remove_vault(vid)
        for m in self.ai.manifest(vid):
            got = self.ai.get(vid, m["path"])
            if got:
                self.index_file(vid, got[0], got[1])

    def reindex_all(self) -> None:
        for v in self.vaults.list():
            self.reindex_vault(v["id"])

    # ---- embedding worker --------------------------------------------------------
    def _pending_sql(self) -> tuple[str, tuple]:
        cfg = self.embedding_cfg()
        key = self._key(cfg) if cfg else ""
        return ("FROM chunks c JOIN vaults v ON v.id=c.vault_id WHERE v.rag_enabled=1 "
                "AND (c.embedding IS NULL OR c.emb_key IS NOT ?)", (key,))

    def _pending_count(self) -> int:
        sql, p = self._pending_sql()
        return self.db.one("SELECT COUNT(*) n " + sql, p)["n"]

    def process_pending(self, max_batches: int = 1000) -> int:
        """Embed pending chunks. Returns chunks embedded, or -1 after a failure (retry later)."""
        cfg = self.embedding_cfg()
        if not cfg:
            return 0
        key, done = self._key(cfg), 0
        for _ in range(max_batches):
            sql, p = self._pending_sql()
            rows = self.db.query("SELECT c.id, c.text " + sql + " ORDER BY c.id LIMIT ?", p + (BATCH_SIZE,))
            if not rows:
                break
            try:
                vecs = self.embed_fn(cfg, [r["text"] for r in rows])
            except Exception as e:  # network, HTTP, bad payload: keep chunks pending
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("embedding failed: %s", self.last_error)
                return -1
            if self.embedding_cfg() != cfg:  # settings changed meanwhile: discard this batch
                self.kick()
                return done
            with self.db.transaction():
                for r, v in zip(rows, vecs):
                    self.db.execute("UPDATE chunks SET embedding=?, emb_key=? WHERE id=?",
                                    (_normalize(v).tobytes(), key, r["id"]))
            self.last_error = None
            done += len(rows)
        return done

    def kick(self) -> None:
        self._event.set()

    def start_worker(self) -> None:
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="obsi-rag", daemon=True)
        self._thread.start()

    def stop_worker(self) -> None:
        self._stop.set()
        self._event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                n = self.process_pending()
            except Exception as e:  # never let the worker die
                self.last_error = f"{type(e).__name__}: {e}"
                n = -1
            if n < 0:
                self._event.wait(backoff)
                backoff = min(backoff * 2, 120.0)
            else:
                backoff = 5.0
                self._event.wait(10.0)  # idle poll also picks up changes made by the CLI
            self._event.clear()

    # ---- search ---------------------------------------------------------------------
    def _text_hits(self, q: str, vids: list[str], n: int) -> list[dict]:
        match = fts_query(q)
        if not match or not vids:
            return []
        ph = ",".join("?" * len(vids))
        rows = self.db.query(
            f"""SELECT c.id, c.vault_id, c.path, c.version, c.start_line, c.end_line, c.text, bm25(chunks_fts) s
                FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid
                JOIN ai_files f ON f.vault_id=c.vault_id AND f.path=c.path AND f.version=c.version
                WHERE chunks_fts MATCH ? AND c.vault_id IN ({ph}) ORDER BY s LIMIT ?""",
            (match, *vids, n))
        return [dict(r, score=-r["s"]) for r in rows]

    def _semantic_hits(self, qvec: array, vids: list[str], key: str, n: int) -> list[dict]:
        if not vids:
            return []
        ph = ",".join("?" * len(vids))
        rows = self.db.query(
            f"""SELECT c.id, c.vault_id, c.path, c.version, c.start_line, c.end_line, c.text, c.embedding
                FROM chunks c JOIN ai_files f ON f.vault_id=c.vault_id AND f.path=c.path AND f.version=c.version
                WHERE c.vault_id IN ({ph}) AND c.emb_key=? AND c.embedding IS NOT NULL""", (*vids, key))
        rows = [r for r in rows if len(r["embedding"]) == 4 * len(qvec)]
        if not rows:
            return []
        if np is not None:
            mat = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
            scores = (mat @ np.frombuffer(qvec.tobytes(), dtype=np.float32)).tolist()
        else:
            scores = []
            for r in rows:
                a = array("f")
                a.frombytes(r["embedding"])
                scores.append(sum(x * y for x, y in zip(a, qvec)))
        order = sorted(range(len(rows)), key=lambda i: -scores[i])[:n]
        return [dict(rows[i], score=scores[i]) for i in order]

    def search(self, query: str, vault_ids: list[str], mode: str = "hybrid", limit: int = 10) -> tuple[list[dict], str | None]:
        """Search AI Available chunks of the given vaults. Returns (results, note)."""
        note: str | None = None
        cfg = self.embedding_cfg()
        sem_vids = [v for v in vault_ids if (self.vaults.get(v) or {}).get("rag", {}).get("enabled")]
        want_sem = mode in ("semantic", "hybrid")
        qvec = None
        if want_sem:
            if not cfg:
                note = "embeddings are not configured; used text search"
            elif not sem_vids:
                note = "RAG is disabled for the searched vaults; used text search"
            else:
                try:
                    qvec = _normalize(self.embed_fn(cfg, [query])[0])
                except Exception as e:
                    note = f"embedding request failed ({type(e).__name__}); used text search"
        n = max(limit * 5, 20)
        sem = self._semantic_hits(qvec, sem_vids, self._key(cfg), n) if qvec is not None and cfg else []
        if mode == "semantic" and qvec is not None and not sem:
            note = "no embedded chunks yet (index is building); used text search"
        text = [] if (mode == "semantic" and sem) else self._text_hits(query, vault_ids, n)
        if mode == "text" or qvec is None or (mode == "semantic" and not sem):
            hits = text
        elif mode == "semantic":
            hits = sem
        else:  # hybrid: reciprocal rank fusion
            fused: dict[int, dict] = {}
            for lst in (text, sem):
                for rank, h in enumerate(lst):
                    e = fused.setdefault(h["id"], dict(h, score=0.0))
                    e["score"] += 1.0 / (RRF_K + rank + 1)
            hits = sorted(fused.values(), key=lambda h: -h["score"])
        return [{
            "vault": h["vault_id"], "path": h["path"], "version": h["version"], "snippet": _snippet(h["text"]),
            "start_line": h["start_line"], "end_line": h["end_line"], "score": round(float(h["score"]), 5),
        } for h in hits[:limit]], note
