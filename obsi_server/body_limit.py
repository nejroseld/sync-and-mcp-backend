"""ASGI request body limits, applied before framework body parsing."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

ASGIApp = Callable[[dict[str, Any], Callable[[], Awaitable[dict[str, Any]]], Callable[[dict[str, Any]], Awaitable[None]]], Awaitable[None]]

_AUTH_PATHS = {"/api/v1/auth/register", "/api/v1/auth/login"}
_MCP_PATH = "/mcp"
_BLOB_PATH = re.compile(r"^/api/v1/vaults/[^/]+/(?:files/|ai/files/)")


class BodyLimitMiddleware:
    """Reject oversized request bodies before FastAPI or Pydantic consumes them.

    ``max_body_bytes`` should be set from ``config.max_body_bytes`` when the
    middleware is installed. The limit applies to blob and AI file PUTs.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int = 200 * 1024 * 1024):
        self.app = app
        self.max_body_bytes = max_body_bytes

    def limit_for(self, method: str, path: str) -> int:
        if method == "POST" and path in _AUTH_PATHS:
            return 16 * 1024
        if path == _MCP_PATH:
            return 8 * 1024 * 1024
        if method == "PUT" and _BLOB_PATH.match(path):
            return self.max_body_bytes
        return 1 * 1024 * 1024

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "").upper()
        path = scope.get("path", "")
        limit = self.limit_for(method, path)
        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > limit:
                    await self._too_large(send, limit)
                    return
            except ValueError:
                pass

        # File PUTs and MCP already call read_body(), which counts every streamed
        # chunk. Pre-reading those bodies here would double memory use for large
        # sync uploads. Other routes need a cap before FastAPI parses JSON: an
        # exception raised from receive() is otherwise turned into HTTP 400.
        if path == _MCP_PATH or (method == "PUT" and _BLOB_PATH.match(path)):
            await self.app(scope, receive, send)
            return

        seen = 0
        parts = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body = message.get("body", b"")
            seen += len(body)
            if seen > limit:
                await self._too_large(send, limit)
                return
            parts.append(body)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": b"".join(parts), "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _too_large(send, limit: int) -> None:
        body = json.dumps({"error": {
            "code": "payload_too_large",
            "message": f"body exceeds {limit} bytes",
        }}).encode("utf-8")
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode("ascii"))]})
        await send({"type": "http.response.body", "body": body})
