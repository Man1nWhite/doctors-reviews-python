from __future__ import annotations

import asyncio
import time


class FixedWindowRateLimiter:
    def __init__(self, limit: int, window: float, clock=time.monotonic, max_buckets: int = 4096):
        self.limit = max(1, limit)
        self.window = max(0.001, window)
        self.clock = clock
        self.max_buckets = max(1, max_buckets)
        self._buckets: dict[str, tuple[float, int]] = {}
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        expired = [k for k, (start, _) in self._buckets.items() if now - start >= self.window]
        for k in expired:
            self._buckets.pop(k, None)
        while len(self._buckets) > self.max_buckets:
            self._buckets.pop(next(iter(self._buckets)))

    async def check(self, key: str) -> tuple[bool, float]:
        now = self.clock()
        async with self._lock:
            start, count = self._buckets.get(key, (now, 0))
            if now - start >= self.window:
                start, count = now, 0
            if count >= self.limit:
                return False, max(0.0, self.window - (now - start))
            self._buckets[key] = (start, count + 1)
            if len(self._buckets) > self.max_buckets:
                self._prune(now)
            return True, max(0.0, self.window - (now - start))
