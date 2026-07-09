"""Fixed-window per-key rate limiter, in-process.

Good enough for the self-host OAuth surface: single-replica installs are
the OSS default (the same assumption the visitor-salt fallback makes).
Guards DCR spam and authorize-POST hammering; NOT a security control
against token guessing (tokens are 256-bit — guessing is hopeless).
"""

from __future__ import annotations

import time


class RateLimiter:
    def __init__(self, *, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window = window_seconds
        self._buckets: dict[str, tuple[float, int]] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        ts = time.time() if now is None else now
        start, count = self._buckets.get(key, (ts, 0))
        if ts - start >= self._window:
            start, count = ts, 0
        if count >= self._limit:
            self._buckets[key] = (start, count)
            return False
        self._buckets[key] = (start, count + 1)
        # Opportunistic purge so long-running processes don't accumulate keys.
        if len(self._buckets) > 10_000:
            cutoff = ts - self._window
            self._buckets = {k: v for k, v in self._buckets.items() if v[0] > cutoff}
        return True
