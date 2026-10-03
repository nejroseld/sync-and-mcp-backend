from __future__ import annotations


class ApiError(Exception):
    """Error rendered as {"error": {"code", "message"}} with an HTTP status."""

    def __init__(self, status: int, code: str, message: str = "", extra: dict | None = None):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code
        self.extra = extra or {}
