import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from obsi_server.config import Config
from obsi_server.db import Database
from obsi_server.storage import Storage


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.put_args = []

    def put_object(self, Bucket, Key, Body, **extra):
        self.objects[(Bucket, Key)] = Body.read() if hasattr(Body, "read") else bytes(Body)
        self.put_args.append(extra)

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


class S3StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "db.sqlite")
        self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO vaults(id,name,created_at) VALUES('v','V',0)")
        self.config = Config(data_dir=Path(self.tmp.name), s3_bucket="bucket", s3_prefix="test",
                             s3_threshold_mb=0)
        self.s3 = FakeS3()
        self.storage = Storage(self.db, Path(self.tmp.name), self.config, s3_client=self.s3)

    def test_large_attachment_offloads_and_reads(self):
        obj = self.storage.put("v", "opaque-attachment", b"attachment", None, None, is_markdown=False)
        self.assertNotIn("backend", obj)
        self.assertEqual(self.db.one("SELECT backend FROM blobs WHERE vault_id='v' AND key='opaque-attachment'")["backend"], "s3")
        self.assertEqual(self.storage.read("v", "opaque-attachment"), b"attachment")
        self.assertFalse(self.storage.blob_path("v", "opaque-attachment").exists())

    def test_markdown_and_unknown_type_stay_local(self):
        md = self.storage.put("v", "opaque-md", b"note", None, None, is_markdown=True)
        unknown = self.storage.put("v", "opaque-unknown", b"unknown", None, None)
        self.assertNotIn("backend", md)
        self.assertNotIn("backend", unknown)
        self.assertEqual(self.db.one("SELECT backend FROM blobs WHERE vault_id='v' AND key='opaque-md'")["backend"], "local")
        self.assertEqual(self.db.one("SELECT backend FROM blobs WHERE vault_id='v' AND key='opaque-unknown'")["backend"], "local")
        self.assertEqual(self.storage.read("v", "opaque-md"), b"note")

    def test_overwrite_and_delete_remove_remote_object(self):
        first = self.storage.put("v", "opaque", b"large", None, None, is_markdown=False)
        remote_key = self.storage._s3_key("v", "opaque")
        self.assertIn(("bucket", remote_key), self.s3.objects)
        self.storage.put("v", "opaque", b"small", None, None, is_markdown=True)
        self.assertNotIn(("bucket", remote_key), self.s3.objects)
        self.assertEqual(self.storage.read("v", "opaque"), b"small")
        self.storage.put("v", "opaque", b"large again", None, None, is_markdown=False)
        self.storage.delete("v", "opaque")
        self.assertNotIn(("bucket", remote_key), self.s3.objects)

    def test_server_side_encryption_follows_config(self):
        self.storage.put("v", "aws", b"x", None, None, is_markdown=False)
        self.assertEqual(self.s3.put_args[-1], {"ServerSideEncryption": "AES256"})
        self.config.s3_endpoint = "https://s3.example.net"
        self.storage.put("v", "compatible", b"x", None, None, is_markdown=False)
        self.assertEqual(self.s3.put_args[-1], {})
        self.config.s3_sse, self.config.s3_kms_key_id = "aws:kms", "key-1"
        self.storage.put("v", "kms", b"x", None, None, is_markdown=False)
        self.assertEqual(self.s3.put_args[-1], {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": "key-1"})

    def test_spooled_upload_is_moved_or_streamed_and_removed(self):
        from obsi_server.uploads import Upload, tmp_dir
        import hashlib

        for key, is_md in (("local", True), ("remote", False)):
            path = tmp_dir(Path(self.tmp.name)) / f".up-{key}"
            path.write_bytes(b"spooled")
            upload = Upload(path, 7, hashlib.sha256(b"spooled").hexdigest())
            obj = self.storage.put("v", key, upload, None, None, is_markdown=is_md)
            self.assertEqual((obj["size"], obj["etag"]), (7, upload.sha256))
            self.assertEqual(self.storage.read("v", key), b"spooled")
            self.assertFalse(path.exists())

    def test_quota_rejects_before_writing(self):
        self.config.vault_quota_bytes = 10
        self.storage.put("v", "a", b"12345678", None, None, is_markdown=True)
        self.storage.put("v", "a", b"1234567890", None, None, is_markdown=True)  # replacing frees the old size
        from obsi_server.errors import ApiError
        with self.assertRaises(ApiError) as e:
            self.storage.put("v", "b", b"1", None, None, is_markdown=True)
        self.assertEqual((e.exception.status, e.exception.code), (507, "quota_exceeded"))
        self.assertIsNone(self.storage.stat("v", "b"))

    def test_concurrent_puts_of_one_key_keep_row_and_backend_consistent(self):
        import threading

        self.config.s3_threshold_mb = 0

        def writer(i):
            for n in range(20):
                self.storage.put("v", "same", f"{i}-{n}".encode(), None, None, is_markdown=(n + i) % 2 == 0)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        row = self.db.one("SELECT backend FROM blobs WHERE key='same'")
        remote = ("bucket", self.storage._s3_key("v", "same")) in self.s3.objects
        local = self.storage.blob_path("v", "same").exists()
        self.assertEqual((remote, local), (row["backend"] == "s3", row["backend"] == "local"))
        self.storage.read("v", "same")

    def test_without_bucket_large_attachment_stays_local(self):
        cfg = SimpleNamespace(s3_bucket=None, s3_threshold_mb=0)
        storage = Storage(self.db, Path(self.tmp.name), cfg, s3_client=self.s3)
        storage.put("v", "opaque", b"large", None, None, is_markdown=False)
        self.assertEqual(storage.stat("v", "opaque")["size"], 5)
        self.assertEqual(self.db.one("SELECT backend FROM blobs WHERE key='opaque'")["backend"], "local")


if __name__ == "__main__":
    unittest.main()
