"""Tiny in-memory TTL cache with single-flight dedupe.

Extraction results are cached so repeated page loads don't re-hit the
provider API (and don't burn CDN rate limits). Concurrent identical
requests share one upstream call.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable


class TTLCache:
    def __init__(self, default_ttl: float = 300.0, max_entries: int = 512):
        self._ttl = default_ttl
        self._max = max_entries
        self._data: dict[str, tuple[float, Any]] = {}
        self._flights: dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def get_or_fetch(
        self, key: str, fetch: Callable[[], Awaitable[Any]], ttl: float | None = None
    ) -> Any:
        now = time.monotonic()
        hit = self._data.get(key)
        if hit and hit[0] > now:
            return hit[1]

        async with self._lock:
            # Re-check after acquiring the lock.
            hit = self._data.get(key)
            if hit and hit[0] > now:
                return hit[1]
            flight = self._flights.get(key)
            if flight is None:
                flight = asyncio.get_running_loop().create_future()
                self._flights[key] = flight
                owner = True
            else:
                owner = False

        if not owner:
            return await flight

        try:
            value = await fetch()
            self._store(key, value, ttl or self._ttl)
            flight.set_result(value)
            return value
        except Exception as exc:
            flight.set_exception(exc)
            raise
        finally:
            async with self._lock:
                self._flights.pop(key, None)

    def _store(self, key: str, value: Any, ttl: float) -> None:
        if len(self._data) >= self._max:
            # Evict oldest expiry.
            oldest = min(self._data, key=lambda k: self._data[k][0])
            self._data.pop(oldest, None)
        self._data[key] = (time.monotonic() + ttl, value)

    def invalidate(self, key: str) -> None:
        self._data.pop(key, None)

    def clear(self) -> None:
        self._data.clear()

    def stats(self) -> dict:
        now = time.monotonic()
        return {"entries": len(self._data), "live": sum(1 for e, _ in self._data.values() if e > now)}
