# Market Data Interface Design

*Design doc — authoritative on the `MarketDataProvider` abstraction and the shared price cache built on top of it. Upstream context: [`PLAN.md`](./PLAN.md) §6 (Market Data) and §7 (Database) define the product-level requirements this implements. Downstream detail: [`MASSIVE_API.md`](./MASSIVE_API.md) documents the real API calls `MassiveProvider` makes; [`MARKET_SIMULATOR.md`](./MARKET_SIMULATOR.md) documents `SimulatorProvider`'s internals. This doc is the seam between them.*

## 1. Goal

One piece of code (SSE streaming, price cache, trade execution pricing) should never know or care whether prices come from Massive's REST API or from the in-process simulator. Both are swapped in behind a single `MarketDataProvider` interface, selected once at startup by whether `MASSIVE_API_KEY` is set (PLAN.md §5). Everything above the provider — the shared cache, the SSE stream, the tracked-ticker-set logic — is written once, against the interface, and works identically for both.

```
┌──────────────┐     ┌──────────────┐     ┌───────────────────┐     ┌─────────────┐
│ MassiveProvider │  │SimulatorProvider│    │                    │     │             │
│ (httpx → Massive)│  │(GBM, in-process) │──▶│ MarketDataProvider │◀───│  PriceCache  │◀── SSE stream, trade pricing
└──────────────┘     └──────────────┘     │     (ABC)          │     │ (poller loop)│
                                            └───────────────────┘     └─────────────┘
```

- **`MarketDataProvider`** (§2): the abstract contract. Stateless per call — "give me the latest price for these tickers."
- **`PriceCache`** (§3): the stateful layer. Owns the tracked-ticker set, the in-memory cache, opening-price capture, and the background polling loop that drives the provider.

This split matters: the provider doesn't know about watchlists, positions, or "opening price" — that's all cache-layer concern, identical regardless of provider. The provider's only job is turning a list of tickers into prices.

## 2. The `MarketDataProvider` Interface

```python
# backend/app/market/interface.py
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class TickerQuote:
    """A single provider-sourced quote. Deliberately minimal — everything
    derived (previous price, change %, tracked-since) lives in PriceCache,
    not here."""
    ticker: str
    price: float
    timestamp: str  # ISO 8601 UTC, e.g. "2026-08-06T14:30:00Z"


class MarketDataProvider(ABC):
    """Implemented by MassiveProvider and SimulatorProvider. The cache layer
    (PriceCache) is the only caller of this interface."""

    @property
    @abstractmethod
    def poll_interval_seconds(self) -> float:
        """How often PriceCache's background loop should call fetch_quotes.
        Provider-specific: Massive's free tier needs ~15s (rate limit);
        the simulator can run much faster (~0.5s, see MARKET_SIMULATOR.md)."""

    @abstractmethod
    async def fetch_quotes(self, tickers: list[str]) -> dict[str, TickerQuote]:
        """Fetch the latest price for every ticker in `tickers`, in a single
        batched operation — never one call per ticker (see MASSIVE_API.md §3
        on why: rate limits are per-call, not per-ticker).

        Contract:
        - `tickers` is deduplicated, non-empty, already normalized
          (uppercase, per PLAN.md §8 conventions) by the caller.
        - Returns a dict keyed by ticker. A ticker the provider has no data
          for (e.g. simulator has no seed for an exotic symbol, or Massive
          returned nothing for it) is simply ABSENT from the returned dict
          — never a null/placeholder entry. Callers must handle partial
          results.
        - Never raises for a partial/degraded result. Only raises for a
          total failure of the call itself (network error, auth error,
          rate-limited with no data at all) — PriceCache is responsible for
          catching that and falling back to stale cached values (§3).
        - Does not throttle or retry internally beyond what's needed to
          complete one call; polling cadence and retry-on-failure are the
          cache layer's job, driven by `poll_interval_seconds`.
        """

    async def fetch_quote(self, ticker: str) -> TickerQuote | None:
        """Convenience wrapper for the single-ticker immediate-fetch case
        (PLAN.md §6: a newly tracked ticker gets its first price immediately
        rather than waiting for the next poll). Default implementation just
        calls fetch_quotes([ticker]) — providers don't need to override this."""
        result = await self.fetch_quotes([ticker])
        return result.get(ticker)
```

Two things are deliberately **not** part of this interface:

- **No lifecycle methods** (`start`/`stop`/`connect`). Both implementations are call-and-return; neither holds a persistent connection (Massive is REST-polled, not WebSocket — see MASSIVE_API.md §1). Any setup (e.g. an `httpx.AsyncClient` instance) is constructor-time, not a separate lifecycle step.
- **No "seed price" concept.** That's simulator-specific (MARKET_SIMULATOR.md) and Massive doesn't need it (a real quote is a real quote). Where a seed matters, it's internal to `SimulatorProvider`, not exposed through the interface.

## 3. `PriceCache` — the Stateful Layer

This is what PLAN.md §6 calls the "Shared Price Cache." It owns everything the provider interface deliberately excludes.

```python
# backend/app/market/cache.py
import asyncio
import time
from dataclasses import dataclass, field

from .interface import MarketDataProvider


@dataclass
class CachedPrice:
    ticker: str
    price: float
    previous_price: float
    opening_price: float       # captured once, held for container lifetime — PLAN.md §6
    timestamp: str


class PriceCache:
    def __init__(self, provider: MarketDataProvider):
        self._provider = provider
        self._prices: dict[str, CachedPrice] = {}
        self._tracked: set[str] = set()   # watchlist ∪ open-position tickers
        self._lock = asyncio.Lock()
        self._poll_task: asyncio.Task | None = None

    # --- tracked-ticker-set management (PLAN.md §6) ---

    async def track(self, ticker: str) -> None:
        """Add a ticker to the tracked set. If it's new, fetch its first
        price immediately rather than waiting for the next poll tick."""
        async with self._lock:
            is_new = ticker not in self._tracked
            self._tracked.add(ticker)
        if is_new:
            await self._seed_price(ticker)

    async def untrack_if_unused(self, ticker: str, still_has_position: bool) -> None:
        """Called from DELETE /api/watchlist/{ticker}. Per PLAN.md §6/§8,
        an open position keeps a ticker tracked even after watchlist removal."""
        if still_has_position:
            return
        async with self._lock:
            self._tracked.discard(ticker)
            # Deliberately NOT removing from self._prices: a re-added ticker
            # should show its last known price immediately rather than
            # going blank, and it'll be refreshed on the next poll anyway.

    # --- reads (SSE stream, trade execution, /api/portfolio) ---

    def get(self, ticker: str) -> CachedPrice | None:
        return self._prices.get(ticker)

    def snapshot(self) -> list[CachedPrice]:
        """All currently tracked prices — used for the SSE initial snapshot
        (PLAN.md §6) and REST reads."""
        return [self._prices[t] for t in self._tracked if t in self._prices]

    # --- background polling loop ---

    def start(self) -> None:
        """Called once from FastAPI's lifespan handler (PLAN.md §11) —
        never per-request."""
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()

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
            # PLAN.md §6: on a failed/errored poll, keep serving the last
            # known price for every ticker; retry next interval. No
            # exponential backoff needed at this call volume — the fixed
            # poll_interval_seconds cadence IS the retry interval.
            return
        now = _iso_now()
        for ticker, quote in quotes.items():
            self._apply(ticker, quote.price, quote.timestamp)
        # Tickers absent from `quotes` (see fetch_quotes contract, §2) simply
        # keep their last cached value — same stale-on-partial-failure
        # behavior as a total poll failure, just scoped to one ticker.

    async def _seed_price(self, ticker: str) -> None:
        quote = await self._provider.fetch_quote(ticker)
        if quote is not None:
            # First price for a ticker: previous_price and opening_price
            # both start equal to the first observed price (zero initial
            # change), per PLAN.md §6's "opening price captured the first
            # time a ticker is tracked" rule.
            self._prices[ticker] = CachedPrice(
                ticker=ticker,
                price=quote.price,
                previous_price=quote.price,
                opening_price=quote.price,
                timestamp=quote.timestamp,
            )

    def _apply(self, ticker: str, price: float, timestamp: str) -> None:
        existing = self._prices.get(ticker)
        opening = existing.opening_price if existing else price
        previous = existing.price if existing else price
        self._prices[ticker] = CachedPrice(
            ticker=ticker,
            price=price,
            previous_price=previous,
            opening_price=opening,
            timestamp=timestamp,
        )


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

`change_direction` (up/down, tick-to-tick) and `daily_change_pct` (vs. opening price) — both referenced in PLAN.md §6's SSE event shape — are cheap derived values (`price > previous_price`, `(price - opening_price) / opening_price`) computed where the SSE event is built, not stored redundantly on `CachedPrice`.

### Concurrency Notes

- `PriceCache` is a per-process singleton, created once in FastAPI's lifespan handler and held in app state — consistent with PLAN.md §11's single-worker constraint (the cache is process-local; multi-worker would need Redis or similar, explicitly out of scope).
- The `asyncio.Lock` only guards `_tracked` mutation (watchlist add/remove racing the poll loop reading the ticker list); reads of `_prices` from the SSE handler are lock-free — `dict` reads are safe under asyncio's single-threaded event loop, and slightly-stale reads mid-poll are harmless (the next tick corrects them).

## 4. Provider Selection

```python
# backend/app/market/factory.py
import os

from .interface import MarketDataProvider
from .massive_provider import MassiveProvider
from .simulator_provider import SimulatorProvider


def create_market_data_provider() -> MarketDataProvider:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveProvider(api_key=api_key)
    seed = os.environ.get("SIMULATOR_SEED")
    return SimulatorProvider(seed=int(seed) if seed else None)
```

Called once, at startup, from the FastAPI lifespan handler:

```python
# backend/app/main.py (excerpt)
from contextlib import asynccontextmanager
from fastapi import FastAPI

from .market.factory import create_market_data_provider
from .market.cache import PriceCache

@asynccontextmanager
async def lifespan(app: FastAPI):
    provider = create_market_data_provider()
    cache = PriceCache(provider)
    app.state.price_cache = cache
    # seed tracked set from DB (watchlist ∪ open positions) before starting
    for ticker in await load_initial_tracked_tickers():
        await cache.track(ticker)
    cache.start()
    yield
    await cache.stop()

app = FastAPI(lifespan=lifespan)
```

This is the *only* place `MASSIVE_API_KEY` is read (PLAN.md §5's env-var contract). No other module checks the environment variable — they depend on `MarketDataProvider`/`PriceCache`, never on which branch was taken.

## 5. `MassiveProvider`

```python
# backend/app/market/massive_provider.py
import httpx
from .interface import MarketDataProvider, TickerQuote

MASSIVE_BASE_URL = "https://api.massive.com"

class MassiveProvider(MarketDataProvider):
    def __init__(self, api_key: str, poll_interval_seconds: float = 15.0):
        self._api_key = api_key
        self._interval = poll_interval_seconds
        self._client = httpx.AsyncClient(base_url=MASSIVE_BASE_URL, timeout=10.0)

    @property
    def poll_interval_seconds(self) -> float:
        return self._interval

    async def fetch_quotes(self, tickers: list[str]) -> dict[str, TickerQuote]:
        resp = await self._client.get(
            "/v2/snapshot/locale/us/markets/stocks/tickers",
            params={"tickers": ",".join(tickers)},
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
        out: dict[str, TickerQuote] = {}
        for row in data.get("tickers", []):
            price = _extract_price(row)
            if price is None:
                continue  # no trade data for this ticker yet — leave absent
            out[row["ticker"]] = TickerQuote(
                ticker=row["ticker"],
                price=price,
                timestamp=_ns_to_iso(row.get("updated")),
            )
        return out


def _extract_price(row: dict) -> float | None:
    # Prefer the literal last trade; fall back to today's/yesterday's close.
    # See MASSIVE_API.md §3 "What InvestAlly Actually Needs From This Response".
    if row.get("lastTrade", {}).get("p") is not None:
        return row["lastTrade"]["p"]
    if row.get("day", {}).get("c"):
        return row["day"]["c"]
    return row.get("prevDay", {}).get("c")


def _ns_to_iso(ns: int | None) -> str:
    from datetime import datetime, timezone
    if not ns:
        from .cache import _iso_now
        return _iso_now()
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

`poll_interval_seconds` defaults to 15s (free-tier-safe per MASSIVE_API.md §3) but is a constructor parameter — a paid-tier deployment can pass a shorter interval without a code change, e.g. via an additional env var if that becomes necessary later. Not adding that env var now — YAGNI until there's a paid-tier deployment to configure.

Per MASSIVE_API.md §5, this deliberately does **not** depend on the `massive-api-client` PyPI package — raw `httpx` against the one documented endpoint keeps the dependency surface minimal and fits FastAPI's async model natively (the official client is sync-only).

## 6. `SimulatorProvider`

```python
# backend/app/market/simulator_provider.py
from .interface import MarketDataProvider, TickerQuote

class SimulatorProvider(MarketDataProvider):
    def __init__(self, seed: int | None = None, poll_interval_seconds: float = 0.5):
        self._interval = poll_interval_seconds
        self._sim = ...  # GBM simulator internals — see MARKET_SIMULATOR.md

    @property
    def poll_interval_seconds(self) -> float:
        return self._interval

    async def fetch_quotes(self, tickers: list[str]) -> dict[str, TickerQuote]:
        return self._sim.step(tickers)  # see MARKET_SIMULATOR.md for `.step()`
```

Full internals (GBM math, correlated moves, seed price table, random events, `SIMULATOR_SEED` determinism) are in `MARKET_SIMULATOR.md` — this file only fixes the two things the interface requires: a fast `poll_interval_seconds` (~500ms, matching PLAN.md §6) and a `fetch_quotes` that returns the same `dict[str, TickerQuote]` shape `MassiveProvider` does.

## 7. Testing Strategy

PLAN.md §12 requires "both implementations conform to the abstract interface" as an explicit backend unit-test target. Concretely, one parametrized conformance suite runs against both providers:

```python
# backend/tests/market/test_provider_conformance.py
import pytest
from app.market.simulator_provider import SimulatorProvider
# MassiveProvider is exercised via a mocked httpx transport, not a live API call

@pytest.mark.parametrize("provider", [
    SimulatorProvider(seed=42),
    make_mocked_massive_provider(),
])
async def test_fetch_quotes_returns_requested_tickers(provider):
    quotes = await provider.fetch_quotes(["AAPL", "GOOGL"])
    assert set(quotes) <= {"AAPL", "GOOGL"}   # subset — partial results allowed
    for q in quotes.values():
        assert q.price > 0
        assert q.timestamp  # ISO 8601 string

@pytest.mark.parametrize("provider", [...])
async def test_fetch_quotes_never_raises_on_unknown_ticker(provider):
    quotes = await provider.fetch_quotes(["ZZZZZ_NOT_REAL"])
    assert quotes == {} or "ZZZZZ_NOT_REAL" not in quotes
```

- `MassiveProvider` is tested against a mocked `httpx` transport (`httpx.MockTransport` or `respx`) using fixture JSON shaped like MASSIVE_API.md §3's response — never a live network call in unit tests.
- `SimulatorProvider` is tested directly, using `SIMULATOR_SEED` for deterministic assertions (PLAN.md §12).
- `PriceCache` itself is tested against a trivial fake `MarketDataProvider` (fixed/scripted quotes) to verify tracked-set semantics, stale-on-failure behavior, and opening-price-capture-once — independent of either real provider.

## 8. Module Layout

```
backend/app/market/
├── interface.py           # MarketDataProvider ABC, TickerQuote
├── cache.py                # PriceCache, CachedPrice
├── factory.py               # create_market_data_provider()
├── massive_provider.py      # MassiveProvider
└── simulator_provider.py    # SimulatorProvider (imports simulator internals)
```

This mirrors PLAN.md §4's boundary rule — everything market-data-related lives under `backend/app/market/`, and no other backend module reaches past `PriceCache`/`MarketDataProvider` into provider internals.
