"""PriceCache — the stateful layer on top of MarketDataProvider.

This is PLAN.md Sec 6's "Shared Price Cache." It owns everything the
provider interface deliberately excludes: the tracked-ticker set,
opening-price capture, ordering/staleness protection, and the background
polling loop. See planning/MARKET_DATA_DESIGN.md Sec 6.
"""

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone

from .interface import MarketDataProvider


@dataclass
class CachedPrice:
    ticker: str
    price: float
    previous_price: float
    opening_price: float  # captured once, held for container lifetime
    timestamp: str
    version: int  # bumped on every REAL update from the provider —
    # never on a poll that returned stale/no data. This is what lets SSE
    # tell "the price actually changed" apart from "the cache was just
    # re-read."


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PriceCache:
    def __init__(self, provider: MarketDataProvider):
        self._provider = provider
        self._prices: dict[str, CachedPrice] = {}
        self._tracked: set[str] = set()  # watchlist ∪ open-position tickers
        self._lock = asyncio.Lock()
        self._poll_task: asyncio.Task | None = None

    # --- tracked-ticker-set management ---

    async def track(self, ticker: str) -> None:
        """Add a single ticker to the tracked set (watchlist add, or a
        trade opening a position in an untracked ticker). If it's newly
        tracked, attempt an immediate best-effort price fetch — but the
        ticker is added to `_tracked` FIRST, unconditionally, so even if
        the immediate fetch fails or is skipped for budget reasons, the
        next scheduled poll (which iterates `_tracked`) will pick it up."""
        async with self._lock:
            is_new = ticker not in self._tracked
            self._tracked.add(ticker)
        if not is_new:
            return
        if ticker in self._prices:
            # Re-added ticker: opening_price is captured the FIRST time a
            # ticker is tracked in the container's lifetime — a later
            # untrack/re-track must not reset it. Nothing to do here; the
            # existing entry is reused as-is and will refresh in place on
            # the next poll.
            return
        quote = await self._provider.fetch_quote_immediate(ticker)
        if quote is not None:
            self._apply_if_newer(ticker, quote.price, quote.timestamp)
        # else: stays tracked with no cache entry yet ("pending"); picked
        # up automatically by the next _poll_once() call.

    async def track_many(self, tickers: list[str]) -> None:
        """Batch version of track(), used ONCE at startup to seed the
        initial tracked set (watchlist ∪ open positions) with a SINGLE
        provider call instead of one call per ticker. Seeding N tickers
        via track() in a loop costs N provider calls before the first
        scheduled poll even runs, which can blow a rate-limited free-tier
        budget immediately."""
        new: list[str] = []
        async with self._lock:
            for ticker in tickers:
                if ticker not in self._tracked:
                    self._tracked.add(ticker)
                    if ticker not in self._prices:
                        new.append(ticker)
        if not new:
            return
        try:
            quotes = await self._provider.fetch_quotes(new)
        except Exception:
            return  # picked up by the first scheduled poll instead
        for ticker, quote in quotes.items():
            self._apply_if_newer(ticker, quote.price, quote.timestamp)

    async def untrack_if_unused(self, ticker: str, still_has_position: bool) -> None:
        """Called from DELETE /api/watchlist/{ticker}. An open position
        keeps a ticker tracked even after watchlist removal."""
        if still_has_position:
            return
        async with self._lock:
            self._tracked.discard(ticker)
            # Deliberately NOT removing from self._prices: a re-added
            # ticker shows its last known price immediately instead of
            # going blank, and both its price AND its opening_price carry
            # forward unchanged (see track() above).

    # --- reads (SSE stream, trade execution, /api/portfolio, /api/watchlist) ---

    def get(self, ticker: str) -> CachedPrice | None:
        """None means "tracked but no price has arrived yet" (a brand new
        ticker still waiting on its first poll) OR "not tracked." Callers
        (REST routes) render this as `price: null, price_pending: true`
        rather than treating it as an error."""
        return self._prices.get(ticker)

    def snapshot(self) -> list[CachedPrice]:
        """All currently tracked-AND-priced entries — used for the SSE
        initial snapshot and REST reads. Tracked-but-pending tickers are
        simply absent until their first price lands."""
        return [self._prices[t] for t in self._tracked if t in self._prices]

    @property
    def tracked_tickers(self) -> frozenset[str]:
        return frozenset(self._tracked)

    # --- background polling loop ---

    def start(self) -> None:
        """Called once from FastAPI's lifespan handler — never per-request."""
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Cancels the poll loop AND WAITS for it to actually finish, then
        releases provider resources (e.g. MassiveProvider's httpx
        client)."""
        if self._poll_task:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        await self._provider.aclose()

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._provider.poll_interval_seconds)
            await self._poll_once()

    async def _poll_once(self) -> None:
        async with self._lock:
            tickers = list(self._tracked)
        if not tickers:
            return
        try:
            quotes = await self._provider.fetch_quotes(tickers)
        except Exception:
            # On a failed/errored poll, keep serving the last known price
            # for every ticker; retry next interval. No exponential
            # backoff — the fixed poll_interval_seconds cadence IS the
            # retry interval. Crucially: `version` is NOT bumped here, so
            # SSE correctly treats this as "nothing changed," not a new
            # price to flash.
            return
        for ticker, quote in quotes.items():
            self._apply_if_newer(ticker, quote.price, quote.timestamp)
        # Tickers absent from `quotes` (partial result) simply keep their
        # last cached value and version, same stale-on-failure behavior as
        # a total poll failure, just scoped to one ticker.

    def _apply_if_newer(self, ticker: str, price: float, timestamp: str) -> None:
        """Writes a new quote into the cache, guarding against out-of-order
        application. An immediate seed fetch and a scheduled poll for the
        same ticker can both be in flight at once; if the seed fetch is
        slow and lands AFTER a newer poll result already wrote a fresher
        price, applying it would silently roll the price backwards.
        `timestamp` strings are fixed-width ISO 8601 UTC
        (%Y-%m-%dT%H:%M:%SZ), so lexicographic comparison is chronological
        comparison — no parsing needed."""
        existing = self._prices.get(ticker)
        if existing is not None and timestamp < existing.timestamp:
            return
        opening = existing.opening_price if existing else price
        previous = existing.price if existing else price
        version = (existing.version + 1) if existing else 1
        self._prices[ticker] = CachedPrice(
            ticker=ticker,
            price=price,
            previous_price=previous,
            opening_price=opening,
            timestamp=timestamp,
            version=version,
        )
