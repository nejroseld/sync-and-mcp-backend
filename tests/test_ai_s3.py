"""AI Available attachment offload and cleanup without an external S3 service."""
import io
import tempfile
import unittest
from pathlib import Path

from obsi_server.ai_store import AiStore
from obsi_server.config import Config
from obsi_server.db import Database
from obsi_server.storage import Storage


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body):
        self.objects[(Bucket, Key)] = bytes(Body)

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


class AiS3Tests(unittest.TestCase):
    def test_attachment_offload_read_overwrite_and_clear(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "obsi.db")
            try:
                fake = FakeS3()
                cfg = Config(data_dir=Path(d), s3_bucket="bucket", s3_threshold_mb=0)
                ai = AiStore(db, Storage(db, cfg.data_dir, cfg, s3_client=fake))
                ai.put("v_one", "image.png", b"first", 1)
                row = db.one("SELECT backend,content FROM ai_files WHERE vault_id=?", ("v_one",))
                self.assertEqual((row["backend"], bytes(row["content"])), ("s3", b""))
                self.assertEqual(ai.get("v_one", "image.png")[1], b"first")
                ai.put("v_one", "image.png", b"second", 2)
                self.assertEqual(ai.get("v_one", "image.png")[1], b"second")
                self.assertEqual(len(fake.objects), 1)
                ai.put("v_one", "note.md", b"# local", 1)
                self.assertEqual(db.one("SELECT backend FROM ai_files WHERE path='note.md'")["backend"], "local")
                ai.clear("v_one")
                self.assertEqual(fake.objects, {})
                self.assertEqual(ai.manifest("v_one"), [])
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
