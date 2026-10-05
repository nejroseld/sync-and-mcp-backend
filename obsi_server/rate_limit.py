"""Small, shared GCRA rate limiter backed by the server's SQLite database."""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from .db import Database, now_ms
from .errors import ApiError


@dataclass(frozen=True)
class Limit:
    key: str
    burst: int
    period_ms: int
    cost: int = 1


class RateLimiter:
    def __init__(self, db: Database):
        self.db = db
        self._last_cleanup_ms = 0

    @staticmethod
    def key(kind: str, value: str) -> str:
        # Keep user names and client addresses out of the counters table.
        return kind + ":" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    def check(self, *limits: Limit) -> None:
        """Consume all buckets atomically, or reject without consuming any."""
        if not limits:
            return
        now = now_ms()
        updates: list[tuple[str, int]] = []
        retry_ms = 0
        with self.db.transaction():
            for limit in limits:
                if limit.burst < 1 or limit.period_ms < 1 or limit.cost < 1:
                    raise ValueError("rate limit values must be positive")
                if limit.cost > limit.burst:
                    retry_ms = max(retry_ms, limit.period_ms)
                    continue
                spacing = max(1, math.ceil(limit.period_ms / limit.burst))
                row = self.db.one("SELECT tat_ms FROM rate_limits WHERE key=?", (limit.key,))
                next_tat = max(now, row["tat_ms"] if row else now) + limit.cost * spacing
                # One whole bucket may be spent at once; it then refills gradually.
                excess = next_tat - now - limit.burst * spacing
                if excess > 0:
                    retry_ms = max(retry_ms, excess)
                else:
                    updates.append((limit.key, next_tat))
            if retry_ms:
                seconds = max(1, math.ceil(retry_ms / 1000))
                raise ApiError(429, "rate_limited", "too many requests; retry later",
                               {"retry_after_seconds": seconds})
            for key, tat in updates:
                self.db.execute(
                    "INSERT INTO rate_limits(key,tat_ms) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET tat_ms=excluded.tat_ms", (key, tat))
            # Avoid unbounded growth from one-off addresses and account names.
            if now - self._last_cleanup_ms > 3_600_000:
                self.db.execute("DELETE FROM rate_limits WHERE tat_ms < ?", (now - 86_400_000,))
                self._last_cleanup_ms = now
