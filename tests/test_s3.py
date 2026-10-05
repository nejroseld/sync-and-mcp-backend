import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from obsi_server.config import Config
from obsi_server.db import Database
from obsi_server.errors import ApiError
from obsi_server.storage import Storage


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = bytes(Body)

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

    def test_offload_without_bucket_fails_before_local_write(self):
        cfg = SimpleNamespace(s3_bucket=None, s3_threshold_mb=0)
        storage = Storage(self.db, Path(self.tmp.name), cfg, s3_client=self.s3)
        with self.assertRaises(ApiError) as error:
            storage.put("v", "opaque", b"large", None, None, is_markdown=False)
        self.assertEqual((error.exception.status, error.exception.code), (503, "s3_unavailable"))
        self.assertIsNone(storage.stat("v", "opaque"))


if __name__ == "__main__":
    unittest.main()
