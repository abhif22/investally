# Market Data Backend — Implementation Design

*Consolidated, implementation-ready design for InvestAlly's market data backend. Supersedes the split across [`MARKET_INTERFACE.md`](./MARKET_INTERFACE.md), [`MARKET_SIMULATOR.md`](./MARKET_SIMULATOR.md), and [`MASSIVE_API.md`](./MASSIVE_API.md) by pulling them into one buildable document and closing every market-data finding raised in [`CHANGE_REVIEW.md`](./CHANGE_REVIEW.md) and [`REVIEW.md`](./REVIEW.md). Product requirements remain authoritative in [`PLAN.md`](./PLAN.md) §5–§8, §11–§12; this document is how those requirements get built. §14 maps every fix here back to the review finding it resolves.*

## 1. Scope and Goals

The market data backend has one job: turn "a set of tickers" into "current prices for those tickers," continuously, from either a real data source (Massive) or a synthetic one (the built-in simulator), and make the two indistinguishable to everything above them (SSE streaming, trade execution pricing, `/api/portfolio`, `/api/watchlist`).

Three properties drive every decision below:

1. **One interface, two implementations.** `SimulatorProvider` and `MassiveProvider` both implement `MarketDataProvider`. No other module ever branches on which one is active (PLAN.md §5's `MASSIVE_API_KEY` check happens in exactly one place — the factory, §9).
2. **The cache is the source of truth for "now."** `PriceCache` is a per-process singleton that owns the tracked-ticker set, the latest known price per ticker, and the background loop that keeps them fresh. SSE, trade pricing, and REST reads all go through it — never through a provider directly.
3. **Degrade to stale, never to broken.** A provider hiccup (network error, rate limit, partial response) must never take down the app, block a request, or corrupt cache state with out-of-order data. Every failure mode below has an explicit, tested behavior.

## 2. Architecture

```
                    ┌─────────────────────┐
  MASSIVE_API_KEY   │  create_market_data_ │
  (env, read once) ─▶  provider() (factory) │
                    └──────────┬──────────┘
                               │ returns one of:
              ┌────────────────┴────────────────┐
              ▼                                  ▼
    ┌───────────────────┐             ┌────────────────────┐
    │  MassiveProvider   │             │  SimulatorProvider  │
    │  (httpx, rate-     │             │  (GBM, in-process,  │
    │   limited REST)    │             │   no I/O)            │
    └──────────┬─────────┘             └──────────┬──────────┘
               │  both implement MarketDataProvider │
               └────────────────┬────────────────────┘
                                 ▼
                        ┌─────────────────┐
                        │   PriceCache     │   per-process singleton
                        │  - tracked set   │   (FastAPI app.state)
                        │  - latest prices │
                        │  - poll loop     │
                        └────────┬─────────┘
                                 │
             ┌───────────────────┼───────────────────┐
             ▼                   ▼                    ▼
     GET /api/stream/prices  GET /api/portfolio   POST /api/portfolio/trade
     (SSE, §11)              GET /api/watchlist    (fill price, §12)
```

## 3. Module Layout

```
backend/app/market/
├── interface.py           # MarketDataProvider ABC, TickerQuote
├── rate_limiter.py        # TokenBucketLimiter (shared by MassiveProvider)
├── cache.py                # PriceCache, CachedPrice
├── factory.py               # create_market_data_provider()
├── massive_provider.py      # MassiveProvider
└── simulator/
    ├── __init__.py
    ├── simulator.py            # Simulator: GBM math, correlation, events
    ├── simulator_provider.py    # SimulatorProvider (MarketDataProvider adapter)
    └── seed_data.py             # KNOWN_SEED_PRICES, KNOWN_PARAMS, DEFAULT_SEED_PRICE

backend/app/api/
└── stream.py                # GET /api/stream/prices (SSE)

backend/tests/market/
├── test_provider_conformance.py
├── test_price_cache.py
├── test_simulator.py
├── test_massive_provider.py
└── test_stream_sse.py
```

Nothing outside `backend/app/market/` imports from `massive_provider.py` or `simulator/` directly — only `factory.py` does, and only `PriceCache`/`MarketDataProvider` are visible to the rest of the app.

## 4. The `MarketDataProvider` Interface

```python
# backend/app/market/interface.py
from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class TickerQuote:
    """A single provider-sourced quote. Deliberately minimal — everything
    derived (previous price, change %, tracked-since, version) lives in
    PriceCache, not here."""
    ticker: str
    price: float
    timestamp: str  # ISO 8601 UTC, e.g. "2026-08-06T14:30:00Z"


class MarketDataProvider(ABC):
    """Implemented by MassiveProvider and SimulatorProvider. PriceCache is
    the only caller of this interface."""

    @property
    @abstractmethod
    def poll_interval_seconds(self) -> float:
        """How often PriceCache's background loop should call fetch_quotes.
        Provider-specific: Massive's free tier needs ~15s (rate limit);
        the simulator can run much faster (~0.5s, see §7)."""

    @abstractmethod
    async def fetch_quotes(self, tickers: list[str]) -> dict[str, TickerQuote]:
        """Fetch the latest price for every ticker in `tickers`, in a single
        batched operation — never one call per ticker (see §8 on why: rate
        limits are per-call, not per-ticker).

        Contract:
        - `tickers` is deduplicated, non-empty, already normalized
          (uppercase, per PLAN.md §8 conventions) by the caller.
        - Returns a dict keyed by ticker. A ticker the provider has no data
          for is simply ABSENT from the returned dict — never a
          null/placeholder entry. Callers must handle partial results.
        - Never raises for a partial/degraded result. Only raises for a
          total failure of the call itself (network error, auth error,
          rate-limited with no data at all) — PriceCache catches that and
          falls back to stale cached values (§6).
        - May block waiting for rate-limit budget (MassiveProvider does —
          see §8). This method is only ever called from PriceCache's poll
          loop or its batch-seed path (§6), never from a path a user
          request is waiting on synchronously.
        """

    async def fetch_quote(self, ticker: str) -> TickerQuote | None:
        """Convenience wrapper. Default implementation just calls
        fetch_quotes([ticker]) — providers don't need to override this."""
        result = await self.fetch_quotes([ticker])
        return result.get(ticker)

    async def fetch_quote_immediate(self, ticker: str) -> TickerQuote | None:
        """Best-effort, NON-BLOCKING immediate fetch for a single newly
        tracked ticker (PLAN.md §6: a new ticker should get its first price
        right away rather than waiting for the next poll).

        Default implementation delegates to fetch_quote — fine for a
        provider with no rate limit (the simulator: this always succeeds
        instantly). A rate-limited provider (MassiveProvider) overrides
        this to return None immediately, without waiting or blocking, if
        satisfying it would spend budget the scheduled poll loop needs.
        PriceCache treats None as "no price yet — will arrive on the next
        scheduled poll," not as an error (§6)."""
        return await self.fetch_quote(ticker)

    async def aclose(self) -> None:
        """Release any held resources (e.g. an HTTP client). Default
        no-op — only MassiveProvider overrides this. Called exactly once,
        from PriceCache.stop() during FastAPI shutdown (§6, §9)."""
        return None
```

Two things are deliberately **not** part of this interface:

- **No `start`/`connect` lifecycle.** Both implementations are call-and-return; neither holds a persistent connection (Massive is REST-polled, not WebSocket). Setup is constructor-time. `aclose()` is the one exception — it exists purely for teardown symmetry with a constructor that opens a resource (§8), not for connection management.
- **No "seed price" concept.** That's simulator-specific (§7) and Massive doesn't need it. Where a seed matters it's internal to `SimulatorProvider`, never exposed through the interface.

## 5. Rate Limiting

Both of `MassiveProvider`'s call sites — the scheduled poll loop and immediate per-ticker seeding — must share **one** budget. Treating them as separate budgets is exactly how the free tier gets exceeded: ten tickers tracked at startup, each doing its own "immediate" fetch, is ten calls before the first scheduled poll even runs (`CHANGE_REVIEW.md` finding #1). A single token bucket, shared across both paths, makes that structurally impossible.

```python
# backend/app/market/rate_limiter.py
import asyncio
import time


class TokenBucketLimiter:
    """`capacity` tokens, refilled continuously at `capacity / period_seconds`
    tokens/sec. One instance is shared by every call path on a provider so
    scheduled polls and immediate seed fetches draw from the same budget."""

    def __init__(self, capacity: int, period_seconds: float):
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
```

## 6. `PriceCache` — the Stateful Layer

This is PLAN.md §6's "Shared Price Cache." It owns everything the provider interface deliberately excludes: the tracked-ticker set, opening-price capture, ordering/staleness protection, and the background polling loop.

```python
# backend/app/market/cache.py
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
    opening_price: float   # captured once, held for container lifetime — PLAN.md §6
    timestamp: str
    version: int            # bumped on every REAL update from the provider —
                             # never on a poll that returned stale/no data.
                             # This is what lets SSE (§11) tell "the price
                             # actually changed" apart from "the cache was
                             # just re-read." See CHANGE_REVIEW.md finding #4.


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class PriceCache:
    def __init__(self, provider: MarketDataProvider):
        self._provider = provider
        self._prices: dict[str, CachedPrice] = {}
        self._tracked: set[str] = set()   # watchlist ∪ open-position tickers
        self._lock = asyncio.Lock()
        self._poll_task: asyncio.Task | None = None

    # --- tracked-ticker-set management (PLAN.md §6) ---

    async def track(self, ticker: str) -> None:
        """Add a single ticker to the tracked set (watchlist add, or a
        trade opening a position in an untracked ticker). If it's newly
        tracked, attempt an immediate best-effort price fetch — but the
        ticker is added to `_tracked` FIRST, unconditionally, so even if
        the immediate fetch fails or is skipped for budget reasons, the
        next scheduled poll (which iterates `_tracked`) will pick it up
        (CHANGE_REVIEW.md finding #7 — no state where a ticker is
        "supposed to be tracked" but never gets a chance to be priced)."""
        async with self._lock:
            is_new = ticker not in self._tracked
            self._tracked.add(ticker)
        if not is_new:
            return
        if ticker in self._prices:
            # Re-added ticker: PLAN.md §6 defines opening_price as captured
            # the FIRST time a ticker is tracked in the container's
            # lifetime — a later untrack/re-track must not reset it.
            # Nothing to do here; the existing entry is reused as-is and
            # will refresh in place on the next poll (CHANGE_REVIEW.md #3).
            return
        quote = await self._provider.fetch_quote_immediate(ticker)
        if quote is not None:
            self._apply_if_newer(ticker, quote.price, quote.timestamp)
        # else: stays tracked with no cache entry yet ("pending"); picked
        # up automatically by the next _poll_once() call.

    async def track_many(self, tickers: list[str]) -> None:
        """Batch version of track(), used ONCE at startup to seed the
        initial tracked set (watchlist ∪ open positions) with a SINGLE
        provider call instead of one call per ticker. This is the fix for
        CHANGE_REVIEW.md finding #1: seeding ten default tickers via
        track() in a loop costs ten Massive calls before the first
        scheduled poll even runs, blowing the 5-calls/min free-tier
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
        """Called from DELETE /api/watchlist/{ticker}. Per PLAN.md §6/§8,
        an open position keeps a ticker tracked even after watchlist
        removal."""
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
        rather than treating it as an error — see PLAN.md §8."""
        return self._prices.get(ticker)

    def snapshot(self) -> list[CachedPrice]:
        """All currently tracked-AND-priced entries — used for the SSE
        initial snapshot (PLAN.md §6) and REST reads. Tracked-but-pending
        tickers are simply absent until their first price lands."""
        return [self._prices[t] for t in self._tracked if t in self._prices]

    # --- background polling loop ---

    def start(self) -> None:
        """Called once from FastAPI's lifespan handler (PLAN.md §11) —
        never per-request."""
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        """Cancels the poll loop AND WAITS for it to actually finish,
        then releases provider resources (e.g. MassiveProvider's httpx
        client). The previous design only called .cancel() without
        awaiting, so FastAPI's lifespan could exit — and the process
        could be torn down — before the task had actually stopped, and
        MassiveProvider's AsyncClient was never closed at all
        (CHANGE_REVIEW.md finding #5)."""
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
            # PLAN.md §6: on a failed/errored poll, keep serving the last
            # known price for every ticker; retry next interval. No
            # exponential backoff — the fixed poll_interval_seconds
            # cadence IS the retry interval. Crucially: `version` is NOT
            # bumped here, so SSE (§11) correctly treats this as "nothing
            # changed," not a new price to flash.
            return
        for ticker, quote in quotes.items():
            self._apply_if_newer(ticker, quote.price, quote.timestamp)
        # Tickers absent from `quotes` (partial result — see fetch_quotes
        # contract, §4) simply keep their last cached value and version,
        # same stale-on-failure behavior as a total poll failure, just
        # scoped to one ticker.

    def _apply_if_newer(self, ticker: str, price: float, timestamp: str) -> None:
        """Writes a new quote into the cache, guarding against out-of-order
        application. An immediate seed fetch (§ track()) and a scheduled
        poll for the same ticker can both be in flight at once; if the
        seed fetch is slow and lands AFTER a newer poll result already
        wrote a fresher price, applying it would silently roll the price
        backwards. `timestamp` strings are fixed-width ISO 8601 UTC
        (`%Y-%m-%dT%H:%M:%SZ`), so lexicographic comparison is
        chronological comparison — no parsing needed. This addresses
        CHANGE_REVIEW.md finding #7's "an out-of-order seed response may
        overwrite a newer poll result.""" 
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
```

### Concurrency Notes

- `PriceCache` is a per-process singleton, created once in FastAPI's lifespan handler and held in `app.state` — consistent with PLAN.md §11's single-worker constraint (the cache is process-local; multi-worker would need Redis or similar, explicitly out of scope).
- The `asyncio.Lock` only guards `_tracked` mutation. Reads of `_prices` from the SSE handler are lock-free — `dict` reads are safe under asyncio's single-threaded event loop, and a slightly-stale read mid-write is harmless (the next tick corrects it). `_apply_if_newer` itself is synchronous (no `await` inside it), so it can't be interleaved with another write to the same key — no lock needed there either.
- `change_direction` (up/down, tick-to-tick) and `daily_change_pct` (vs. opening price) are cheap derived values computed where the SSE event is built (§11), not stored redundantly on `CachedPrice`.

## 7. `SimulatorProvider` — GBM Simulator

Default, zero-network-dependency market data source (used whenever `MASSIVE_API_KEY` is unset). It needs to feel alive: prices tick frequently, related stocks drift together, and every so often something moves sharply enough to be interesting to watch.

> **Runtime dependency note**: this implementation uses NumPy for vectorized random draws. "Zero-dependency" (PLAN.md §6) refers to no *external network/service* dependency, not zero Python packages — NumPy is a declared `backend/pyproject.toml` dependency (`CHANGE_REVIEW.md` finding #9).

### 7.1 Where It Runs

There is no separate background task for the simulator. `PriceCache`'s existing poll loop (§6) calls `SimulatorProvider.fetch_quotes()` every `poll_interval_seconds` (~500ms), exactly like it calls `MassiveProvider` — just with a much shorter interval. This keeps "one poll loop drives whichever provider is active" true for both implementations rather than special-casing the simulator with its own scheduler.

### 7.2 Price Model: Geometric Brownian Motion

```
S(t+dt) = S(t) * exp((μ - ½σ²)·dt + σ·√dt·Z)
```

`dt` is a **fixed nominal step**, not measured wall-clock time between polls — `dt = poll_interval_seconds / SECONDS_PER_TRADING_YEAR`. Using measured elapsed time would make the price path depend on real scheduling jitter, breaking the determinism `SIMULATOR_SEED` is supposed to guarantee. A fixed nominal `dt` means the path depends only on the sequence of RNG draws, which the seed fully controls.

```python
# backend/app/market/simulator/simulator.py
SECONDS_PER_TRADING_YEAR = 252 * 6.5 * 3600  # 252 trading days, 6.5h/day
```

### 7.3 Per-Ticker Parameters and Seed Data

```python
# backend/app/market/simulator/seed_data.py
from dataclasses import dataclass


@dataclass(frozen=True)
class TickerParams:
    drift: float        # μ, annualized, e.g. 0.08 for a mild uptrend
    volatility: float    # σ, annualized, e.g. 0.30 for a typically volatile tech name
    sector: str          # correlation grouping — see §7.4


KNOWN_SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00,
    "TSLA": 250.00, "NVDA": 120.00, "META": 500.00,
    "JPM": 210.00, "V": 280.00, "NFLX": 650.00,
}

KNOWN_PARAMS: dict[str, TickerParams] = {
    "AAPL": TickerParams(drift=0.10, volatility=0.25, sector="tech"),
    "GOOGL": TickerParams(drift=0.10, volatility=0.28, sector="tech"),
    "MSFT": TickerParams(drift=0.10, volatility=0.24, sector="tech"),
    "AMZN": TickerParams(drift=0.12, volatility=0.32, sector="tech"),
    "TSLA": TickerParams(drift=0.05, volatility=0.55, sector="tech"),
    "NVDA": TickerParams(drift=0.20, volatility=0.50, sector="tech"),
    "META": TickerParams(drift=0.12, volatility=0.35, sector="tech"),
    "JPM": TickerParams(drift=0.08, volatility=0.22, sector="finance"),
    "V": TickerParams(drift=0.09, volatility=0.20, sector="finance"),
    "NFLX": TickerParams(drift=0.10, volatility=0.38, sector="media"),
}

DEFAULT_SEED_PRICE = 100.00
DEFAULT_PARAMS = TickerParams(drift=0.08, volatility=0.30, sector="general")


def seed_for(ticker: str) -> tuple[float, TickerParams]:
    """A ticker not in the table above (user adds an arbitrary symbol, or
    the LLM trades something exotic) gets a fixed, predictable synthetic
    seed — good enough for a simulated instrument that was never going to
    have a real reference price anyway."""
    if ticker in KNOWN_SEED_PRICES:
        return KNOWN_SEED_PRICES[ticker], KNOWN_PARAMS[ticker]
    return DEFAULT_SEED_PRICE, DEFAULT_PARAMS
```

Seven of the ten default tickers share the `tech` sector — this directly produces PLAN.md §6's "correlated moves across tickers (e.g., tech stocks move together)."

### 7.4 Correlated Moves

Each ticker's random draw `Z` is a variance-preserving mix of three factors, computed fresh every tick:

```
Z_i = β_market · Z_market + β_sector · Z_sector[sector(i)] + β_idio · Z_idio_i
```

with `β_market² + β_sector² + β_idio² = 1`.

```python
BETA_MARKET = 0.5
BETA_SECTOR = 0.3
BETA_IDIO = (1 - BETA_MARKET**2 - BETA_SECTOR**2) ** 0.5  # ≈ 0.812


def _tick_factors(rng, sorted_sectors: list[str]) -> tuple[float, dict[str, float]]:
    z_market = rng.standard_normal()
    # `sorted_sectors` MUST be sorted, not iterated from a set. A Python
    # `set`'s iteration order depends on hash randomization, which varies
    # PER PROCESS — the same SIMULATOR_SEED would draw z_sector[s] in a
    # different order across two runs (or two test workers), assigning
    # different random values to different sectors even though the RNG
    # itself is deterministic. This was CHANGE_REVIEW.md finding #2.
    z_sector = {s: rng.standard_normal() for s in sorted_sectors}
    return z_market, z_sector


def _combined_z(rng, sector: str, z_market: float, z_sector: dict[str, float]) -> float:
    z_idio = rng.standard_normal()
    return BETA_MARKET * z_market + BETA_SECTOR * z_sector[sector] + BETA_IDIO * z_idio
```

`z_market` is drawn once per tick, shared by every ticker. `z_sector[s]` is drawn once per tick per distinct sector present among *tracked* tickers, in sorted order, shared by every ticker in that sector. `z_idio` is drawn per-ticker (also in sorted ticker order — see §7.6) giving each stock its own independent wiggle.

### 7.5 Random Events

A discrete jump applied on top of the GBM step — representing a headline (earnings surprise, exec departure) rather than routine volatility.

```python
EVENT_PROBABILITY_PER_TICK = 0.0003   # per ticker, per tick
EVENT_MAGNITUDE_RANGE = (0.02, 0.05)  # 2-5%, direction chosen independently


def _maybe_apply_event(rng, price: float) -> float:
    if rng.random() >= EVENT_PROBABILITY_PER_TICK:
        return price
    magnitude = rng.uniform(*EVENT_MAGNITUDE_RANGE)
    direction = 1 if rng.random() < 0.5 else -1
    return price * (1 + direction * magnitude)
```

At `POLL_INTERVAL_SECONDS = 0.5` (2 ticks/sec), `p = 0.0003` works out to roughly one event per ticker every ~28 minutes — across a 10-ticker watchlist, one visible spike somewhere on the board every ~3 minutes. Tune `EVENT_PROBABILITY_PER_TICK` directly if that cadence feels off. Events apply **after** the GBM step, multiplicatively, so they compound with the ongoing random walk rather than replacing it.

### 7.6 Determinism (`SIMULATOR_SEED`)

All randomness — market/sector/idiosyncratic draws and event triggers/magnitudes — flows through a single `numpy.random.Generator` constructed once at `Simulator` startup, advanced in a **fixed, sorted order** every tick: market factor → sorted-sector factors → sorted-ticker idiosyncratic + event draws.

```python
import numpy as np
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .seed_data import TickerParams, seed_for


@dataclass
class _TickerState:
    price: float
    params: TickerParams


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Simulator:
    def __init__(
        self,
        seed: int | None = None,
        interval_seconds: float = 0.5,
        clock: Callable[[], str] | None = None,
    ):
        self._rng = np.random.default_rng(seed)   # seed=None → nondeterministic
        self._dt = interval_seconds / SECONDS_PER_TRADING_YEAR
        self._states: dict[str, _TickerState] = {}
        # `clock` is injectable so tests can get byte-identical
        # TickerQuote objects (including timestamp), not just identical
        # prices. Production leaves this as the real UTC clock.
        # CHANGE_REVIEW.md finding #2: the original design used
        # datetime.now() unconditionally, so even a perfectly
        # reproducible price path couldn't produce reproducible quotes.
        self._clock = clock or _default_clock

    def step(self, tickers: list[str]) -> dict[str, "TickerQuote"]:
        from ..interface import TickerQuote

        sorted_tickers = sorted(tickers)
        for ticker in sorted_tickers:
            if ticker not in self._states:
                price, params = seed_for(ticker)
                self._states[ticker] = _TickerState(price=price, params=params)

        sorted_sectors = sorted({self._states[t].params.sector for t in sorted_tickers})
        z_market, z_sector = _tick_factors(self._rng, sorted_sectors)

        now = self._clock()
        out: dict[str, TickerQuote] = {}
        for ticker in sorted_tickers:
            state = self._states[ticker]
            z = _combined_z(self._rng, state.params.sector, z_market, z_sector)
            mu, sigma = state.params.drift, state.params.volatility
            new_price = state.price * np.exp(
                (mu - 0.5 * sigma**2) * self._dt + sigma * (self._dt ** 0.5) * z
            )
            new_price = _maybe_apply_event(self._rng, new_price)
            new_price = max(new_price, 0.01)  # floor — guards float edge cases
            state.price = new_price
            out[ticker] = TickerQuote(ticker=ticker, price=round(new_price, 2), timestamp=now)
        return out
```

Because `_rng` is a single stateful `Generator` advanced in this fixed, fully-sorted order, the same `seed` reproduces the same sequence of prices for the same sequence of `step(tickers)` calls — independent of Python's hash randomization, dict insertion order, or which order tickers were first tracked in.

### 7.7 `SimulatorProvider` Adapter

```python
# backend/app/market/simulator/simulator_provider.py
from ..interface import MarketDataProvider, TickerQuote
from .simulator import Simulator


class SimulatorProvider(MarketDataProvider):
    def __init__(self, seed: int | None = None, poll_interval_seconds: float = 0.5):
        self._interval = poll_interval_seconds
        self._sim = Simulator(seed=seed, interval_seconds=poll_interval_seconds)

    @property
    def poll_interval_seconds(self) -> float:
        return self._interval

    async def fetch_quotes(self, tickers: list[str]) -> dict[str, TickerQuote]:
        return self._sim.step(tickers)  # pure CPU-bound, no I/O to await
```

`fetch_quotes` is declared `async` only to satisfy the interface — `Simulator.step` is synchronous and fast enough (O(tracked tickers), no I/O) that it never needs to yield mid-call. `fetch_quote_immediate` uses the interface's default (delegates to `fetch_quote`), which is fine here since the simulator has no rate limit to respect.

## 8. `MassiveProvider`

REST polling against Massive's (formerly Polygon.io) batch snapshot endpoint. No WebSocket, no options/forex/crypto — just US stock snapshots.

### 8.1 Endpoint

```
GET https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT
```

Always the batch form with an explicit ticker list — never `get_snapshot_ticker` in a loop (one call per ticker defeats the entire point of a 5-calls/min budget), and never omit `tickers` (that returns the whole market).

### 8.2 Implementation

Raw `httpx` against the two endpoints actually needed, rather than the official `massive-api-client` package: the client is synchronous (awkward inside FastAPI's async poll loop), and its surface (options, crypto, WebSocket, pagination) is almost entirely unused here. `httpx` is likely already a backend dependency for other purposes.

```python
# backend/app/market/massive_provider.py
from datetime import datetime, timezone

import httpx

from .interface import MarketDataProvider, TickerQuote
from .rate_limiter import TokenBucketLimiter

MASSIVE_BASE_URL = "https://api.massive.com"
FREE_TIER_CALLS_PER_MINUTE = 5


class MassiveProvider(MarketDataProvider):
    def __init__(
        self,
        api_key: str,
        poll_interval_seconds: float = 15.0,
        calls_per_minute: int = FREE_TIER_CALLS_PER_MINUTE,
    ):
        self._api_key = api_key
        self._interval = poll_interval_seconds
        self._client = httpx.AsyncClient(base_url=MASSIVE_BASE_URL, timeout=10.0)
        # ONE limiter shared by fetch_quotes (poll loop) and
        # fetch_quote_immediate (watchlist-add/trade seeding) — this is
        # what makes it structurally impossible for the two call sites to
        # combine into more than `calls_per_minute` calls/min
        # (CHANGE_REVIEW.md finding #1).
        self._limiter = TokenBucketLimiter(capacity=calls_per_minute, period_seconds=60.0)

    @property
    def poll_interval_seconds(self) -> float:
        return self._interval

    async def fetch_quotes(self, tickers: list[str]) -> dict[str, TickerQuote]:
        """Poll-loop path: blocks for rate-limit budget if needed (the
        poll loop has no user waiting on it — running a little late is
        fine; skipping a cycle silently is not)."""
        await self._limiter.acquire()
        return await self._fetch_quotes_raw(tickers)

    async def fetch_quote_immediate(self, ticker: str) -> TickerQuote | None:
        """Watchlist-add / new-position path: NON-blocking. If the budget
        is exhausted, return None right away rather than making a user
        request hang for up to `60/calls_per_minute` seconds — the
        ticker is already in PriceCache._tracked by the time this is
        called (§6), so the next scheduled poll fetches it for free."""
        if not self._limiter.try_acquire():
            return None
        result = await self._fetch_quotes_raw([ticker])
        return result.get(ticker)

    async def _fetch_quotes_raw(self, tickers: list[str]) -> dict[str, TickerQuote]:
        try:
            resp = await self._client.get(
                "/v2/snapshot/locale/us/markets/stocks/tickers",
                params={"tickers": ",".join(tickers)},
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            resp.raise_for_status()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            # Total-call-failure case per the interface contract (§4):
            # PriceCache catches this and keeps serving stale prices.
            raise MarketDataFetchError(str(exc)) from exc

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

    async def aclose(self) -> None:
        """Closes the underlying httpx client. Called once from
        PriceCache.stop() (§6) — the interface explicitly excludes
        general lifecycle methods, but a provider that opens a real
        connection needs a symmetric teardown hook, or the client (and
        its connection pool) leaks for the life of the process
        (CHANGE_REVIEW.md finding #5)."""
        await self._client.aclose()


class MarketDataFetchError(Exception):
    """Raised for a total call failure (network error, non-2xx). Distinct
    from a partial result, which is never an error — see §4."""


def _extract_price(row: dict) -> float | None:
    # Prefer the literal last trade (reflects the true last execution
    # regardless of session); fall back to today's/yesterday's close for
    # pre-market/after-hours/weekends when `day` may be stale or absent.
    if row.get("lastTrade", {}).get("p") is not None:
        return row["lastTrade"]["p"]
    if row.get("day", {}).get("c"):
        return row["day"]["c"]
    return row.get("prevDay", {}).get("c")


def _ns_to_iso(ns: int | None) -> str:
    if not ns:
        from .cache import _iso_now
        return _iso_now()
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
```

### 8.3 Rate Limits and Cadence

- **Free tier: 5 calls/minute.** Not formally documented with an exact number by the client library, but the commonly cited Polygon.io/Massive free-tier ceiling — worth reconfirming against `massive.com/pricing` at implementation time; `FREE_TIER_CALLS_PER_MINUTE = 5` is the conservative default.
- Because the snapshot endpoint is a **batch** call, the limit is on *polls*, not tickers — one call covers the entire tracked set regardless of size.
- `poll_interval_seconds` defaults to 15s (4 calls/min of headroom under the 5/min budget) but is a constructor parameter — a paid-tier deployment passes a shorter interval without a code change. Not wiring a new env var for this now: YAGNI until there's an actual paid-tier deployment to configure (matching the stance already taken for the simulator's interval).
- A `429`/non-2xx response is a transient failure: `MarketDataFetchError` propagates up to `PriceCache._poll_once`, which keeps serving the last cached price and retries next interval — no exponential backoff needed at this call volume; the fixed poll cadence already acts as the retry interval.

### 8.4 End-of-Day Endpoints (Offline Use Only)

`GET /v2/aggs/ticker/{ticker}/prev` (single ticker) and `GET /v2/aggs/grouped/locale/us/market/stocks/{date}` (whole market, one date) are useful for a one-off/offline script to refresh the simulator's seed price table (§7.3) with real recent closes — they are **not** called from the running backend's request path. The live snapshot call (§8.1) already returns `prevDay` inline with every poll, covering "yesterday's close" without a second request.

## 9. Provider Selection and Startup Wiring

```python
# backend/app/market/factory.py
import os

from .interface import MarketDataProvider
from .massive_provider import MassiveProvider
from .simulator.simulator_provider import SimulatorProvider


def create_market_data_provider() -> MarketDataProvider:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveProvider(api_key=api_key)
    seed = os.environ.get("SIMULATOR_SEED")
    return SimulatorProvider(seed=int(seed) if seed else None)
```

This is the **only** place `MASSIVE_API_KEY` is read (PLAN.md §5). No other module checks the environment variable.

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

    # Seed the initial tracked set (watchlist ∪ open positions) as ONE
    # batch call via track_many(), not N calls via a track() loop —
    # see §6's track_many docstring / CHANGE_REVIEW.md finding #1.
    initial_tickers = await load_initial_tracked_tickers()  # from DB
    await cache.track_many(initial_tickers)

    cache.start()
    yield
    await cache.stop()


app = FastAPI(lifespan=lifespan)
```

## 10. Testing Strategy for §6–§9

- **Conformance suite** (`test_provider_conformance.py`), parametrized over `SimulatorProvider(seed=42)` and a mocked-transport `MassiveProvider`: `fetch_quotes` returns a subset of requested tickers with positive prices and ISO timestamps; never raises on an unknown ticker; `MassiveProvider` is tested via `httpx.MockTransport`/`respx` against fixture JSON shaped like §8's response — never a live network call.
- **`PriceCache` semantics** (`test_price_cache.py`), against a scripted fake provider:
  - `track_many` on N tickers issues exactly one `fetch_quotes` call, not N.
  - Removing then re-tracking a ticker preserves its original `opening_price` (finding #3).
  - A seed response with an older timestamp than an already-cached poll result is dropped, not applied (finding #7).
  - A tracked ticker with a failed immediate fetch is still priced correctly on the next `_poll_once()`.
  - `stop()` awaits the poll task to completion and calls `provider.aclose()` exactly once (finding #5).
  - `version` only increments on an applied quote, never on a caught poll exception (finding #4, verified against §11's SSE behavior).
- **Simulator** (`test_simulator.py`): GBM correctness with `σ=0` follows the deterministic drift term exactly; two `Simulator(seed=42, clock=fixed_incrementing_clock)` instances fed the identical `step()` call sequence produce byte-identical `TickerQuote` sequences, including timestamps (finding #2); tech-sector tickers show positive return correlation over many ticks (statistical, not exact); forcing `EVENT_PROBABILITY_PER_TICK=1.0` makes the next `step()` show a move within `EVENT_MAGNITUDE_RANGE`; a never-seen ticker gets `DEFAULT_SEED_PRICE` and evolves from it on subsequent calls (state persists, no reseed).
- **`MassiveProvider`** (`test_massive_provider.py`): `TokenBucketLimiter.try_acquire()` returns `False` once capacity is exhausted within a window and recovers after the refill period; `fetch_quote_immediate` returns `None` (not an exception, not a block) when the bucket is empty; `fetch_quotes` (poll path) waits rather than failing when the bucket is empty; a mocked 429/500 response raises `MarketDataFetchError`; `aclose()` closes the underlying `httpx.AsyncClient`.

## 11. SSE Streaming — `GET /api/stream/prices`

The naive design — resend every tracked ticker's cached state every ~500ms regardless of whether it changed — breaks the moment a provider's poll interval is *slower* than the SSE tick rate. Massive polls every 15s; if the stream still emits a `price` event for every ticker every 500ms, the frontend's price-flash animation fires ~30 times for one real price change, and its accumulated sparkline gets ~30 duplicate points per actual tick (`CHANGE_REVIEW.md` finding #4).

The fix: `CachedPrice.version` (§6) only advances when a provider actually returned a fresh quote for that ticker. The SSE loop tracks the last version it sent *per ticker, per connection*, and only emits an event when the cache's version has moved past that. This decouples the SSE "check" cadence from any provider's actual update cadence — checking every 250ms is cheap and harmless, because nothing is sent unless something genuinely changed.

```python
# backend/app/api/stream.py
import asyncio
import json
import time

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from ..market.cache import CachedPrice, PriceCache

router = APIRouter()

SSE_CHECK_INTERVAL_SECONDS = 0.25  # how often the loop LOOKS for changes —
                                     # independent of any provider's actual
                                     # poll_interval_seconds
HEARTBEAT_SECONDS = 15


@router.get("/api/stream/prices")
async def stream_prices(request: Request):
    cache: PriceCache = request.app.state.price_cache
    return StreamingResponse(_event_source(request, cache), media_type="text/event-stream")


async def _event_source(request: Request, cache: PriceCache):
    last_sent_version: dict[str, int] = {}
    last_activity = time.monotonic()

    # Initial snapshot (PLAN.md §6): send every currently tracked-and-priced
    # ticker once, unconditionally — this is what lets the frontend render
    # the watchlist immediately rather than waiting for the next change.
    for cp in cache.snapshot():
        last_sent_version[cp.ticker] = cp.version
        yield _format_event(cp)
    last_activity = time.monotonic()

    while True:
        if await request.is_disconnected():
            break
        await asyncio.sleep(SSE_CHECK_INTERVAL_SECONDS)

        sent_any = False
        for cp in cache.snapshot():
            if cp.version > last_sent_version.get(cp.ticker, -1):
                last_sent_version[cp.ticker] = cp.version
                yield _format_event(cp)
                sent_any = True

        now = time.monotonic()
        if sent_any:
            last_activity = now
        elif now - last_activity >= HEARTBEAT_SECONDS:
            yield ": ping\n\n"
            last_activity = now


def _format_event(cp: CachedPrice) -> str:
    if cp.price > cp.previous_price:
        change_direction = "up"
    elif cp.price < cp.previous_price:
        change_direction = "down"
    else:
        change_direction = "flat"
    daily_change_pct = (
        (cp.price - cp.opening_price) / cp.opening_price if cp.opening_price else 0.0
    )
    payload = {
        "ticker": cp.ticker,
        "price": cp.price,
        "previous_price": cp.previous_price,
        "change_direction": change_direction,
        "daily_change_pct": round(daily_change_pct, 6),
        "timestamp": cp.timestamp,
    }
    return f"event: price\ndata: {json.dumps(payload)}\n\n"
```

**Behavior this produces:**

- **Simulator-backed stream**: `version` advances roughly every 500ms (every poll produces a fresh quote). The 250ms check interval catches each new version promptly — effectively an event every ~500ms per ticker, matching PLAN.md §6's cadence, with no duplicates.
- **Massive-backed stream**: `version` advances roughly every 15s (only real polls bump it; the SSE loop's 250ms checks between polls simply find nothing new and send nothing). Each real price change flashes exactly once per connected client, not ~30 times.
- **A poll failure** (§6 `_poll_once`'s `except` branch) never bumps `version`, so it's invisible to SSE — exactly the "keep serving stale, don't spuriously flash" behavior PLAN.md §6 asks for.
- **A slow client**: because each tick only sends the *current* cache state (not a queued backlog), a slow consumer silently misses intermediate versions rather than falling further behind — the per-ticker `last_sent_version` check just means "skip if my last real update already got through," so catching up is automatic on the next successful send.
- **Heartbeat**: sent only when the last 15s produced zero real price events across *all* tickers combined (not per-ticker) — keeping intermediary proxies from timing out an idle-looking connection without competing with real traffic.
- **Reconnect**: no event IDs or replay — a reconnecting client gets a fresh initial snapshot (all current versions), exactly as before.

## 12. REST Reads That Depend on the Cache

`/api/watchlist`, `/api/portfolio`, and trade execution (`/api/portfolio/trade`) all call `PriceCache.get(ticker)` synchronously while building their response. Two cases to handle explicitly, both direct consequences of §6's "pending" state:

- **`get(ticker)` returns `None`** for a ticker that's tracked but hasn't received its first price yet (a brand-new watchlist add or trade-opened position, still waiting on its first poll/immediate fetch). Routes render this as `price: null, price_pending: true` in the JSON response rather than erroring — the frontend already has to handle a ticker appearing before its first SSE event arrives, so this is the same state surfaced through REST.
- **Trade execution pricing**: `POST /api/portfolio/trade` uses whatever `PriceCache.get(ticker)` returns at request time as the fill price (PLAN.md §2: instant fill at current price). If that's `None` (pending), the trade is rejected with `{"error": {"code": "ticker_not_found", "message": "..."}}` rather than filling at a synthesized price — a trade must never fill against data that doesn't exist yet.

## 13. Summary of Environment Variables (Market Data Scope)

| Variable | Effect | Default |
|---|---|---|
| `MASSIVE_API_KEY` | If set and non-empty → `MassiveProvider`; else → `SimulatorProvider` | unset |
| `SIMULATOR_SEED` | Integer seed for `SimulatorProvider`'s RNG; deterministic price path when set | unset (nondeterministic) |

No other market-data env vars exist by design — Massive's `calls_per_minute`/`poll_interval_seconds` and the simulator's `poll_interval_seconds` are constructor parameters with sensible hardcoded defaults, not configuration surface, until an actual need (e.g. a paid Massive tier) justifies adding one.

## 14. Findings Resolved

| # | Source | Finding | Resolved by |
|---|---|---|---|
| 1 | `CHANGE_REVIEW.md` #1 | Startup seeding makes N provider calls (one per initially tracked ticker), breaching the 5-calls/min free-tier budget before the first scheduled poll | `PriceCache.track_many()` (§6) batches the entire initial tracked set into one `fetch_quotes()` call; `MassiveProvider` additionally shares one `TokenBucketLimiter` (§5, §8.2) across both the poll loop and single-ticker immediate fetches, so the two paths can't combine to exceed budget |
| 2 | `CHANGE_REVIEW.md` #2 | `SIMULATOR_SEED` doesn't give byte-identical output across processes: `sectors` was a `set` (hash-randomized iteration order) and quotes used wall-clock `datetime.now()` | `_tick_factors` takes a `sorted(...)` list, never a raw `set` (§7.4); `Simulator` takes an injectable `clock` callable, defaulting to real time but swappable for a fixed/incrementing fake in tests (§7.6) |
| 3 | `CHANGE_REVIEW.md` #3 | Re-adding a previously-tracked ticker reset its `opening_price`, violating the "captured once per container lifetime" rule | `PriceCache.track()` checks `ticker in self._prices` before seeding and skips re-seeding entirely for a ticker that already has a cache entry, preserving `opening_price` (§6) |
| 4 | `CHANGE_REVIEW.md` #4 | SSE resending unchanged cached quotes every ~500ms while Massive only refreshes every 15s causes ~30x redundant flash/chart-append events per real price change | `CachedPrice.version` only increments on an applied provider quote (§6); the SSE loop tracks last-sent version per ticker per connection and only emits on a version increase, decoupling "check cadence" from "actual update cadence" (§11) |
| 5 | `CHANGE_REVIEW.md` #5 | `PriceCache.stop()` cancelled the poll task without awaiting it, and `MarketDataProvider` had no way to release a provider-owned resource (`MassiveProvider`'s `httpx.AsyncClient`) | `stop()` cancels, then awaits the task (suppressing `CancelledError`), then calls `provider.aclose()`; the interface gained an `aclose()` hook with a no-op default, overridden by `MassiveProvider` to close its client (§4, §6, §8.2) |
| 7 | `CHANGE_REVIEW.md` #7 | No defined behavior when a ticker's first-quote fetch fails, and concurrent seed/poll writes could apply an out-of-order (stale) quote over a newer one | A ticker enters `_tracked` before its immediate fetch is attempted, so a failed/skipped seed still gets picked up by the next scheduled poll (§6 `track()`); `_apply_if_newer` compares ISO-8601 timestamps and drops any write older than what's already cached (§6) |
| 9 | `CHANGE_REVIEW.md` #9 | Simulator described as "zero-dependency" while importing NumPy | Clarified in §7's opening note: "zero-dependency" means no external network/service call, not zero Python packages; NumPy is a declared runtime dependency |

Findings #6, #8, #10 (`CHANGE_REVIEW.md`) and the `REVIEW.md` findings outside market data (transaction/ordering contracts for trades, `.env`/`.gitignore` hygiene, `npm ci` vs `npm install`, SQLite schema versioning, LLM validation) are out of this document's scope — they're tracked against `PLAN.md` directly and don't touch the market data module boundary defined here (§3).
