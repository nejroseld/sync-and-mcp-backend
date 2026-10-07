"""RAG: line-based chunks, FTS5 text search, embeddings (OpenAI-compatible), hybrid RRF.

Only the current published note (``.md`` in AI Available, same version) is indexed.
Attachments, other AI files, and encrypted sync blobs never become chunks. Rows that no
longer match a published note are deleted before anything is sent to the embedding endpoint.
Chunks and FTS rows are written synchronously on publish; embeddings are filled in by a
background worker (`process_pending`) and may lag behind.
"""
from __future__ import annotations

import heapq
import ipaddress
import json
import logging
import re
import socket
import threading
from array import array
from typing import Callable, Sequence
from urllib.parse import urlsplit

import httpcore
import httpx

from .ai_store import AiStore
from .db import Database
from .errors import ApiError
from .vaults import MAX_CHUNK_CHARS, VaultService

try:  # optional acceleration
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover
    np = None

log = logging.getLogger("obsi.rag")

BATCH_SIZE = 32
MAX_TEXT_CHARS = MAX_CHUNK_CHARS
RRF_K = 60
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_CHUNKS_PER_VAULT = 200_000
SCAN_BATCH = 2000
_PRIVATE_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".home.arpa")

EmbedFn = Callable[[dict, list[str]], list[list[float]]]


class EmbeddingError(Exception):
    """Embedding failure whose message is safe to store and show (no URL, no response body)."""


def _public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def check_embedding_url(url: str, allow_private: bool = False) -> str:
    """Validate an embedding base URL; returns it normalized. Raises ValueError."""
    url = url.strip().rstrip("/")
    try:
        parts = urlsplit(url)
        parts.port
    except ValueError:
        raise ValueError("base_url is not a valid URL") from None
    schemes = ("https", "http") if allow_private else ("https",)
    if parts.scheme not in schemes:
        raise ValueError("base_url must use https")
    host = (parts.hostname or "").lower()
    if not host or parts.username is not None or parts.password is not None or parts.fragment:
        raise ValueError("base_url must be scheme://host[:port]/path without credentials")
    if allow_private:
        return url
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host == "localhost" or host.endswith(_PRIVATE_SUFFIXES) or "." not in host:
            raise ValueError("base_url must point to a public host") from None
        return url
    if not _public_ip(ip):
        raise ValueError("base_url must point to a public address")
    return url


class _PublicOnlyBackend(httpcore.SyncBackend):
    """Resolves and checks the address on every connect, so DNS rebinding cannot reach private hosts."""

    def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError:
            raise httpcore.ConnectError("name resolution failed") from None
        addrs = list(dict.fromkeys(ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos))
        if not addrs or not all(_public_ip(a) for a in addrs):
            raise httpcore.ConnectError("endpoint resolves to a non-public address")
        error: Exception = httpcore.ConnectError("connect failed")
        for addr in addrs:
            try:
                return super().connect_tcp(str(addr), port, timeout, local_address, socket_options)
            except httpcore.ConnectError as e:
                error = e
        raise error


def _transport(allow_private: bool) -> httpx.HTTPTransport:
    transport = httpx.HTTPTransport(retries=0)
    if not allow_private:
        # httpx has no public hook for the network backend; replace the pool it built.
        transport._pool = httpcore.ConnectionPool(ssl_context=httpx.create_ssl_context(),
                                                  network_backend=_PublicOnlyBackend())
    return transport


def http_embed(cfg: dict, texts: list[str], timeout: float = 60.0, allow_private: bool = False) -> list[list[float]]:
    """POST {base_url}/embeddings (OpenAI format). Raises on any failure."""
    try:
        url = check_embedding_url(cfg["base_url"], allow_private) + "/embeddings"
    except ValueError as e:
        raise EmbeddingError(str(e)) from None
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    body = {"model": cfg["model"], "input": [t[:MAX_TEXT_CHARS] for t in texts]}
    buf = bytearray()
    with httpx.Client(transport=_transport(allow_private), timeout=timeout,
                      follow_redirects=False, trust_env=False) as client:
        with client.stream("POST", url, headers=headers, json=body) as r:
            if r.status_code != 200:
                raise EmbeddingError(f"HTTP {r.status_code}")
            for chunk in r.iter_bytes():
                buf += chunk
                if len(buf) > MAX_RESPONSE_BYTES:
                    raise EmbeddingError("response too large")
    try:
        data = sorted(json.loads(buf)["data"], key=lambda d: d.get("index", 0))
        vecs = [d["embedding"] for d in data]
    except (ValueError, KeyError, TypeError, AttributeError):
        raise EmbeddingError("malformed response") from None
    if len(vecs) != len(texts):
        raise EmbeddingError("embedding count mismatch")
    return vecs


def describe_error(e: Exception) -> str:
    """Short error label for logs and the admin API: never the URL, key or response body."""
    if isinstance(e, EmbeddingError):
        return f"EmbeddingError: {e}"
    if isinstance(e, httpx.TimeoutException):
        return "Timeout"
    if isinstance(e, httpx.ConnectError) and "non-public" in str(e):
        return "ConnectError: endpoint resolves to a non-public address"
    return type(e).__name__


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
    def __init__(self, db: Database, vaults: VaultService, ai: AiStore, embed_fn: EmbedFn = http_embed,
                 allow_private: bool = False):
        self.db, self.vaults, self.ai = db, vaults, ai
        self.embed_fn = embed_fn
        self.allow_private = allow_private
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
        if base_url is not None:
            try:
                base_url = check_embedding_url(base_url, self.allow_private)
            except ValueError as e:
                raise ApiError(422, "invalid_embedding_url", str(e)) from None
        if model is not None and not 0 < len(model) <= 200:
            raise ApiError(422, "validation_error", "model must be 1 to 200 characters")
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
        # Notes only. A published .txt/.csv/image must not stay in the index from an earlier build.
        if not str(path).lower().endswith(".md"):
            self.remove_file(vid, path)
            return
        v = self.vaults.get(vid)
        if not v:
            return
        text = data.decode("utf-8", errors="replace")
        chunks = chunk_text(text, v["rag"]["chunk_chars"], v["rag"]["chunk_overlap"])
        with self.db.transaction():
            self._delete_chunks("vault_id=? AND path=?", (vid, path))
            room = MAX_CHUNKS_PER_VAULT - self.db.one("SELECT COUNT(*) n FROM chunks WHERE vault_id=?", (vid,))["n"]
            if len(chunks) > room:
                log.warning("vault %s reached %d chunks; indexed %d of %d chunks of one file",
                            vid, MAX_CHUNKS_PER_VAULT, max(room, 0), len(chunks))
                chunks = chunks[:max(room, 0)]
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

    def prune_unpublished(self) -> int:
        """Delete chunks that are not the current published note of the same version.

        Search already hides these rows. Pruning them stops the embedding worker from
        sending text that is no longer published.
        """
        stale = self.db.query(
            """SELECT c.id FROM chunks c
               LEFT JOIN ai_files f
                 ON f.vault_id=c.vault_id AND f.path=c.path AND f.version=c.version AND f.kind='note'
               WHERE f.path IS NULL"""
        )
        if not stale:
            return 0
        ids = [r["id"] for r in stale]
        removed = 0
        with self.db.transaction():
            for i in range(0, len(ids), 500):
                batch = ids[i:i + 500]
                ph = ",".join("?" * len(batch))
                self._delete_chunks(f"id IN ({ph})", tuple(batch))
                removed += len(batch)
        return removed

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
        return ("FROM chunks c JOIN vaults v ON v.id=c.vault_id "
                "JOIN ai_files f ON f.vault_id=c.vault_id AND f.path=c.path "
                "AND f.version=c.version AND f.kind='note' "
                "WHERE v.rag_enabled=1 AND (c.embedding IS NULL OR c.emb_key IS NOT ?)", (key,))

    def _pending_count(self) -> int:
        sql, p = self._pending_sql()
        return self.db.one("SELECT COUNT(*) n " + sql, p)["n"]

    def process_pending(self, max_batches: int = 1000) -> int:
        """Embed pending chunks. Returns chunks embedded, or -1 after a failure (retry later)."""
        self.prune_unpublished()
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
                self.last_error = describe_error(e)
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
                self.last_error = describe_error(e)
                log.warning("embedding worker failed: %s", self.last_error)
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
                JOIN ai_files f ON f.vault_id=c.vault_id AND f.path=c.path
                    AND f.version=c.version AND f.kind='note'
                WHERE chunks_fts MATCH ? AND c.vault_id IN ({ph}) ORDER BY s LIMIT ?""",
            (match, *vids, n))
        return [dict(r, score=-r["s"]) for r in rows]

    def _semantic_hits(self, qvec: array, vids: list[str], key: str, n: int) -> list[dict]:
        if not vids:
            return []
        ph = ",".join("?" * len(vids))
        width = 4 * len(qvec)
        q = np.frombuffer(qvec.tobytes(), dtype=np.float32) if np is not None else None
        top: list[tuple[float, int]] = []  # min-heap of the best n (score, chunk id)
        last = 0
        while True:
            rows = self.db.query(
                f"""SELECT c.id, c.embedding FROM chunks c
                    JOIN ai_files f ON f.vault_id=c.vault_id AND f.path=c.path
                        AND f.version=c.version AND f.kind='note'
                    WHERE c.vault_id IN ({ph}) AND c.emb_key=? AND c.embedding IS NOT NULL AND c.id > ?
                    ORDER BY c.id LIMIT ?""", (*vids, key, last, SCAN_BATCH))
            if not rows:
                break
            last = rows[-1]["id"]
            rows = [r for r in rows if len(r["embedding"]) == width]
            if not rows:
                continue
            if q is not None:
                mat = np.frombuffer(b"".join(r["embedding"] for r in rows), dtype=np.float32).reshape(len(rows), -1)
                scores = (mat @ q).tolist()
            else:
                scores = []
                for r in rows:
                    a = array("f")
                    a.frombytes(r["embedding"])
                    scores.append(sum(x * y for x, y in zip(a, qvec)))
            for r, s in zip(rows, scores):
                if len(top) < n:
                    heapq.heappush(top, (s, -r["id"]))
                elif s > top[0][0]:
                    heapq.heapreplace(top, (s, -r["id"]))
        if not top:
            return []
        best = sorted(top, reverse=True)
        ids = [-i for _, i in best]
        details = {r["id"]: r for r in self.db.query(
            f"""SELECT c.id, c.vault_id, c.path, c.version, c.start_line, c.end_line, c.text
                FROM chunks c
                JOIN ai_files f ON f.vault_id=c.vault_id AND f.path=c.path
                    AND f.version=c.version AND f.kind='note'
                WHERE c.id IN ({",".join("?" * len(ids))})""", ids)}
        return [dict(details[-i], score=s) for s, i in best if -i in details]

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
