"""Ciphertext blob store: bytes on disk (named sha256(key)), metadata in sqlite."""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import secrets
from pathlib import Path
from typing import Any

from .db import Database, now_ms
from .errors import ApiError

log = logging.getLogger(__name__)


def _obj(r) -> dict:
    return {"key": r["key"], "size": r["size"], "mtime_cli": r["mtime_cli"], "ctime_cli": r["ctime_cli"],
            "mtime_svr": r["mtime_svr"], "etag": r["etag"]}


class Storage:
    def __init__(self, db: Database, data_dir: Path, config: Any | None = None, *, s3_client: Any | None = None):
        self.db = db
        self.root = Path(data_dir) / "vaults"
        self.config = config
        self._s3 = s3_client

    def _s3_client(self):
        if self._s3 is None:
            try:
                import boto3
            except ImportError as exc:
                raise ApiError(503, "s3_unavailable", "S3 offload requires boto3; install obsi-server[s3]") from exc
            self._s3 = boto3.client("s3", region_name=getattr(self.config, "s3_region", None),
                                   endpoint_url=getattr(self.config, "s3_endpoint", None))
        return self._s3

    def _s3_key(self, vid: str, key: str) -> str:
        prefix = str(getattr(self.config, "s3_prefix", "obsi")).strip("/")
        digest = hashlib.sha256(key.encode()).hexdigest()
        parts = [p for p in (prefix, vid, digest) if p]
        return "/".join(parts)

    def s3_put_object(self, object_key: str, data: bytes) -> None:
        """Store bytes under an application-managed key in the configured bucket."""
        bucket = getattr(self.config, "s3_bucket", None)
        if not bucket:
            raise ApiError(503, "s3_unavailable", "S3 is not configured; set OBSI_S3_BUCKET")
        prefix = str(getattr(self.config, "s3_prefix", "obsi")).strip("/")
        key = "/".join(p for p in (prefix, object_key.lstrip("/")) if p)
        self._s3_client().put_object(Bucket=bucket, Key=key, Body=data)

    def s3_get_object(self, object_key: str) -> bytes:
        bucket = getattr(self.config, "s3_bucket", None)
        if not bucket:
            raise ApiError(503, "s3_unavailable", "S3 is not configured; set OBSI_S3_BUCKET")
        prefix = str(getattr(self.config, "s3_prefix", "obsi")).strip("/")
        key = "/".join(p for p in (prefix, object_key.lstrip("/")) if p)
        return self._s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()

    def s3_delete_object(self, object_key: str) -> None:
        bucket = getattr(self.config, "s3_bucket", None)
        if not bucket:
            raise ApiError(503, "s3_unavailable", "S3 is not configured; set OBSI_S3_BUCKET")
        prefix = str(getattr(self.config, "s3_prefix", "obsi")).strip("/")
        key = "/".join(p for p in (prefix, object_key.lstrip("/")) if p)
        self._s3_client().delete_object(Bucket=bucket, Key=key)

    def _s3_delete(self, vid: str, key: str) -> None:
        bucket = getattr(self.config, "s3_bucket", None)
        if bucket:
            self._s3_client().delete_object(Bucket=bucket, Key=self._s3_key(vid, key))

    def _dir(self, vid: str) -> Path:
        return self.root / vid / "blobs"

    def blob_path(self, vid: str, key: str) -> Path:
        return self._dir(vid) / hashlib.sha256(key.encode()).hexdigest()

    def list(self, vid: str) -> list[dict]:
        return [_obj(r) for r in self.db.query("SELECT * FROM blobs WHERE vault_id=? ORDER BY key", (vid,))]

    def stat(self, vid: str, key: str) -> dict | None:
        r = self.db.one("SELECT * FROM blobs WHERE vault_id=? AND key=?", (vid, key))
        return _obj(r) if r else None

    def put(self, vid: str, key: str, data: bytes, mtime_cli: int | None, ctime_cli: int | None,
            is_markdown: bool | None = None) -> dict:
        threshold = int(getattr(self.config, "s3_threshold_mb", 10)) * 1024 * 1024
        bucket = getattr(self.config, "s3_bucket", None)
        # Keys are encrypted by the plugin, so an omitted type hint is ambiguous.
        # Keep ambiguous objects local; clients that know the plaintext extension
        # send an explicit false for attachments.
        offload = bool(bucket) and is_markdown is False and len(data) > threshold
        d = self._dir(vid)
        d.mkdir(parents=True, exist_ok=True)
        target = self.blob_path(vid, key)
        old = self.db.one("SELECT backend FROM blobs WHERE vault_id=? AND key=?", (vid, key))
        old_backend = old["backend"] if old and "backend" in old.keys() else "local"
        backend = "s3" if offload else "local"
        if offload:
            self._s3_client().put_object(Bucket=bucket, Key=self._s3_key(vid, key), Body=data)
        else:
            tmp = d / f".tmp-{secrets.token_hex(6)}"
            try:
                with open(tmp, "wb") as f:
                    f.write(data)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, target)
            finally:
                if tmp.exists():
                    tmp.unlink()
        ts = now_ms()
        etag = hashlib.sha256(data).hexdigest()
        mtime_cli = ts if mtime_cli is None else mtime_cli
        ctime_cli = mtime_cli if ctime_cli is None else ctime_cli
        self.db.execute(
            """INSERT INTO blobs(vault_id,key,size,mtime_cli,ctime_cli,mtime_svr,etag,backend) VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(vault_id,key) DO UPDATE SET size=excluded.size, mtime_cli=excluded.mtime_cli,
               ctime_cli=excluded.ctime_cli, mtime_svr=excluded.mtime_svr, etag=excluded.etag, backend=excluded.backend""",
            (vid, key, len(data), mtime_cli, ctime_cli, ts, etag, backend),
        )
        if backend == "s3":
            target.unlink(missing_ok=True)
        elif old_backend == "s3":
            try:
                self._s3_delete(vid, key)
            except Exception:
                # The DB now points to the successfully written local copy;
                # report success and leave a harmless orphan for later cleanup.
                log.exception("failed to remove replaced S3 blob for vault %s", vid)
        return self.stat(vid, key)  # type: ignore[return-value]

    def read(self, vid: str, key: str) -> bytes:
        row = self.db.one("SELECT backend FROM blobs WHERE vault_id=? AND key=?", (vid, key))
        if not row:
            raise FileNotFoundError(key)
        backend = row["backend"] if "backend" in row.keys() else "local"
        if backend == "s3":
            bucket = getattr(self.config, "s3_bucket", None)
            if not bucket:
                raise ApiError(503, "s3_unavailable", "blob is stored in S3 but OBSI_S3_BUCKET is not configured")
            result = self._s3_client().get_object(Bucket=bucket, Key=self._s3_key(vid, key))
            return result["Body"].read()
        return self.blob_path(vid, key).read_bytes()

    def delete(self, vid: str, key: str) -> None:
        row = self.db.one("SELECT backend FROM blobs WHERE vault_id=? AND key=?", (vid, key))
        if row and row["backend"] == "s3":
            self._s3_delete(vid, key)
        self.db.execute("DELETE FROM blobs WHERE vault_id=? AND key=?", (vid, key))
        self.blob_path(vid, key).unlink(missing_ok=True)

    def purge(self, vid: str) -> None:
        rows = self.db.query("SELECT key,backend FROM blobs WHERE vault_id=?", (vid,))
        # Remove remote copies before metadata so an S3 failure leaves the vault
        # recoverable and the caller can retry the purge.
        for row in rows:
            if row["backend"] == "s3":
                self._s3_delete(vid, row["key"])
        self.db.execute("DELETE FROM blobs WHERE vault_id=?", (vid,))
        shutil.rmtree(self.root / vid, ignore_errors=True)
