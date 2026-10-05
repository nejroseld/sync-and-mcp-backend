import asyncio
import json
import unittest

from obsi_server.body_limit import BodyLimitMiddleware


class BodyLimitTests(unittest.TestCase):
    def invoke(self, middleware, *, path="/api/v1/example", method="POST", headers=(), chunks=()):
        async def run():
            events = iter(chunks)
            sent = []

            async def receive():
                try:
                    return next(events)
                except StopIteration:
                    return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                sent.append(message)

            await middleware({"type": "http", "method": method, "path": path,
                              "headers": list(headers)}, receive, send)
            return sent

        return asyncio.run(run())

    @staticmethod
    async def echo_body(scope, receive, send):
        body = bytearray()
        while True:
            message = await receive()
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break
        payload = str(len(body)).encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": payload})

    def test_chunked_oversize_is_413_before_app_finishes(self):
        middleware = BodyLimitMiddleware(self.echo_body)
        sent = self.invoke(middleware, method="POST", path="/api/v1/auth/login", chunks=(
            {"type": "http.request", "body": b"x" * 8192, "more_body": True},
            {"type": "http.request", "body": b"x" * 8193, "more_body": False},
        ))
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(json.loads(sent[1]["body"]), {"error": {
            "code": "payload_too_large", "message": "body exceeds 16384 bytes"}})

    def test_normal_request_passes(self):
        middleware = BodyLimitMiddleware(self.echo_body)
        sent = self.invoke(middleware, chunks=(
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"56", "more_body": False},
        ))
        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(sent[1]["body"], b"6")

    def test_content_length_oversize_rejected_without_reading(self):
        middleware = BodyLimitMiddleware(self.echo_body)
        sent = self.invoke(middleware, headers=((b"content-length", b"16385"),),
                           path="/api/v1/auth/login")
        self.assertEqual(sent[0]["status"], 413)
        self.assertIn(b"16384", sent[1]["body"])


if __name__ == "__main__":
    unittest.main()
