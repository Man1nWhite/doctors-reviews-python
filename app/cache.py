from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class _Entry(Generic[T]):
    expires_at: float
    stale_until: float
    value: T


@dataclass(frozen=True)
class CacheLookup(Generic[T]):
    value: T
    stale: bool


class AsyncTTLCache(Generic[T]):
    def __init__(self, ttl: float, max_entries: int, clock=time.monotonic, *, stale_ttl: float = 0):
        self.ttl = max(0.0, ttl)
        self.stale_ttl = max(0.0, stale_ttl)
        self.max_entries = max(1, max_entries)
        self.clock = clock
        self._entries: OrderedDict[str, _Entry[T]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> T | None:
        cached = await self.lookup(key)
        return cached.value if cached is not None and not cached.stale else None

    async def lookup(self, key: str) -> CacheLookup[T] | None:
        """Return fresh or retained stale data without extending its lifetime."""
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            now = self.clock()
            if entry.stale_until <= now:
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return CacheLookup(entry.value, stale=entry.expires_at <= now)

    async def set(self, key: str, value: T, ttl: float | None = None) -> None:
        async with self._lock:
            duration = self.ttl if ttl is None else max(0.0, ttl)
            expires_at = self.clock() + duration
            stale_until = expires_at + (self.stale_ttl if duration > 0 else 0)
            self._entries[key] = _Entry(expires_at, stale_until, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._entries.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._entries.clear()
