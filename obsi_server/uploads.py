"""Request bodies spooled to disk, per-key locks and per-vault quotas."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .db import Database
from .errors import ApiError


@dataclass
class Upload:
    """A fully received request body in a temp file under the data directory."""
    path: Path
    size: int
    sha256: str

    def read_bytes(self) -> bytes:
        return self.path.read_bytes()

    def discard(self) -> None:
        self.path.unlink(missing_ok=True)


def tmp_dir(data_dir: Path) -> Path:
    # Inside the data directory (not /tmp, which may be RAM-backed) and on the same
    # filesystem as the blobs, so a finished upload is moved, not copied.
    d = Path(data_dir) / "tmp"
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def clear_tmp(data_dir: Path) -> None:
    shutil.rmtree(Path(data_dir) / "tmp", ignore_errors=True)


async def receive_upload(request, limit: int, directory: Path) -> Upload:
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > limit:
        raise ApiError(413, "payload_too_large", f"body exceeds {limit} bytes")
    fd, name = tempfile.mkstemp(dir=directory, prefix=".up-")
    path, size, digest = Path(name), 0, hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise ApiError(413, "payload_too_large", f"body exceeds {limit} bytes")
                digest.update(chunk)
                f.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return Upload(path, size, digest.hexdigest())


class KeyedLock:
    """One lock per key, dropped when nobody holds or waits for it."""

    def __init__(self):
        self._guard = threading.Lock()
        self._locks: dict[object, list] = {}

    @contextmanager
    def __call__(self, key: object) -> Iterator[None]:
        with self._guard:
            entry = self._locks.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._guard:
                entry[1] -= 1
                if not entry[1]:
                    self._locks.pop(key, None)


def vault_usage(db: Database, vid: str) -> int:
    """Bytes a vault holds on this server: ciphertext, AI Available and pending MCP writes."""
    return db.one(
        """SELECT (SELECT COALESCE(SUM(size),0) FROM blobs WHERE vault_id=?)
                + (SELECT COALESCE(SUM(size),0) FROM ai_files WHERE vault_id=?)
                + (SELECT COALESCE(SUM(length(CAST(content AS BLOB))),0) FROM changes
                   WHERE vault_id=? AND status='pending') AS n""", (vid, vid, vid))["n"]


def check_quota(db: Database, quota: int, vid: str, adding: int, freeing: int = 0) -> None:
    if quota <= 0 or adding <= freeing:
        return
    used = vault_usage(db, vid)
    if used - freeing + adding > quota:
        raise ApiError(507, "quota_exceeded", "vault storage quota exceeded",
                       {"quota_bytes": quota, "used_bytes": used})
