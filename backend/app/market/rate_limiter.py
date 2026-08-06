"""A token bucket shared by every call path on a rate-limited provider.

See planning/MARKET_DATA_DESIGN.md Sec 5: MassiveProvider's poll loop and
its immediate per-ticker seeding path must share ONE budget, or the two
paths can combine to exceed the free-tier rate limit.
"""

import asyncio
import time


class TokenBucketLimiter:
    """`capacity` tokens, refilled continuously at `capacity / period_seconds`
    tokens/sec. One instance is shared by every call path on a provider so
    scheduled polls and immediate seed fetches draw from the same budget."""

    def __init__(self, capacity: int, period_seconds: float):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        if period_seconds <= 0:
            raise ValueError("period_seconds must be positive")
        self._capacity = capacity
        self._tokens = float(capacity)
        self._refill_rate = capacity / period_seconds
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    def try_acquire(self) -> bool:
        """Non-blocking: consumes a token and returns True if one's
        available, otherwise returns False without waiting. Used by
        fetch_quote_immediate, which must never make a user-facing
        request wait on rate-limit recovery."""
        self._refill()
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        return False

    async def acquire(self) -> None:
        """Blocking: waits until a token is available, then consumes it.
        Used by the poll loop, which has no request waiting on it and
        should simply run a little late rather than skip a cycle."""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self._refill_rate
            await asyncio.sleep(wait)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
        self._updated = now
