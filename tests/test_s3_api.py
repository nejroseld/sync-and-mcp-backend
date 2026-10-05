"""HTTP routing for S3 backed sync and AI Available files."""
from helpers import ServerCase
from test_ai_s3 import FakeS3


class S3ApiTests(ServerCase):
    def test_encrypted_attachment_hint_routes_to_s3_and_downloads(self):
        self.ctx.config.s3_bucket = "bucket"
        self.ctx.config.s3_threshold_mb = 0
        fake = FakeS3()
        self.ctx.storage._s3 = fake
        base = f"/api/v1/vaults/{self.vid}/files/opaque-key"
        headers = self.h(self.dev, **{"X-Obsi-Markdown": "false"})
        self.assertEqual(self.client.put(base, content=b"encrypted attachment", headers=headers).status_code, 200)
        self.assertEqual(self.ctx.db.one("SELECT backend FROM blobs WHERE vault_id=?", (self.vid,))["backend"], "s3")
        self.assertEqual(self.client.get(base, headers=self.h(self.dev)).content, b"encrypted attachment")
        self.assertEqual(self.client.head(base, headers=self.h(self.dev)).headers["content-length"], "20")

        note = f"/api/v1/vaults/{self.vid}/files/another-opaque-key"
        self.assertEqual(self.client.put(note, content=b"encrypted note", headers=self.h(self.dev, **{"X-Obsi-Markdown": "true"})).status_code, 200)
        self.assertEqual(self.ctx.db.one("SELECT backend FROM blobs WHERE key='another-opaque-key'")["backend"], "local")

        ai = f"/api/v1/vaults/{self.vid}/ai/files/picture.png"
        self.assertEqual(self.client.put(ai, content=b"image", headers=self.h(self.dev)).status_code, 200)
        self.assertEqual(self.ctx.ai.get(self.vid, "picture.png")[1], b"image")
        self.assertEqual(self.ctx.db.one("SELECT backend FROM ai_files WHERE path='picture.png'")["backend"], "s3")

        self.assertEqual(self.client.delete(base, headers=self.h(self.dev)).status_code, 204)
        self.assertEqual(self.client.delete(ai, headers=self.h(self.dev)).status_code, 204)
        self.assertEqual(fake.objects, {})
