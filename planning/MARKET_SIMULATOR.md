# Market Simulator Design

*Design doc — authoritative on `SimulatorProvider`'s internals: the GBM price model, correlation structure, random events, seed data, and determinism. Implements the `MarketDataProvider` contract defined in [`MARKET_INTERFACE.md`](./MARKET_INTERFACE.md) §2; product requirements are in [`PLAN.md`](./PLAN.md) §6.*

## 1. Why a Simulator

This is the default, zero-dependency market data source (PLAN.md §5: used whenever `MASSIVE_API_KEY` is unset — which is the expected common case for most users). It needs to feel alive on a Bloomberg-style terminal: prices tick frequently, related stocks drift together, and every so often something moves sharply enough to be interesting to watch — without ever calling out to the network.

## 2. Where It Runs

PLAN.md §6 describes the simulator as running "as an in-process background task." In this design, that's satisfied by `PriceCache`'s existing poll loop (`MARKET_INTERFACE.md` §3) calling `SimulatorProvider.fetch_quotes()` every `poll_interval_seconds` (~500ms, per §6 below) — **there is no second, separate background task.** The simulator is not driven differently from `MassiveProvider`; it just has a much shorter interval and never leaves the process. This keeps the "one poll loop drives whichever provider is active" property from `MARKET_INTERFACE.md` exactly true for both implementations, rather than special-casing the simulator with its own scheduler.

## 3. Price Model: Geometric Brownian Motion

Each ticker's price evolves as discrete-time GBM:

```
S(t+dt) = S(t) * exp((μ - ½σ²)·dt + σ·√dt·Z)
```

- `S(t)` — current price
- `μ` (mu) — annualized drift (expected return)
- `σ` (sigma) — annualized volatility
- `dt` — time step as a fraction of a trading year
- `Z` — a standard normal random draw (this is where correlation across tickers gets injected — §4)

### Choosing `dt`

`dt` is a **fixed nominal step**, not measured wall-clock time between polls: `dt = poll_interval_seconds / SECONDS_PER_TRADING_YEAR`. Using measured elapsed time would make the price path depend on real scheduling jitter, breaking the determinism `SIMULATOR_SEED` is supposed to guarantee (PLAN.md §12: "same seed, same price path every run"). A fixed nominal `dt` means the path depends only on the sequence of RNG draws, which the seed fully controls.

```python
SECONDS_PER_TRADING_YEAR = 252 * 6.5 * 3600  # 252 trading days, 6.5h/day
POLL_INTERVAL_SECONDS = 0.5

dt = POLL_INTERVAL_SECONDS / SECONDS_PER_TRADING_YEAR  # ≈ 8.47e-8
```

### Per-Ticker Parameters

```python
@dataclass
class TickerParams:
    drift: float        # μ, annualized, e.g. 0.08 for a mild uptrend
    volatility: float    # σ, annualized, e.g. 0.30 for a typically volatile tech name
    sector: str          # correlation grouping — see §4
```

Reasonable defaults for the ten seeded tickers (drift/vol are illustrative — tuned for a lively but not absurd demo, not calibrated to real historical values):

| Ticker | Seed Price | μ (drift) | σ (volatility) | Sector |
|---|---|---|---|---|
| AAPL | 190.00 | 0.10 | 0.25 | tech |
| GOOGL | 175.00 | 0.10 | 0.28 | tech |
| MSFT | 420.00 | 0.10 | 0.24 | tech |
| AMZN | 185.00 | 0.12 | 0.32 | tech |
| TSLA | 250.00 | 0.05 | 0.55 | tech |
| NVDA | 120.00 | 0.20 | 0.50 | tech |
| META | 500.00 | 0.12 | 0.35 | tech |
| JPM | 210.00 | 0.08 | 0.22 | finance |
| V | 280.00 | 0.09 | 0.20 | finance |
| NFLX | 650.00 | 0.10 | 0.38 | media |

These match PLAN.md §6's "realistic seed prices (e.g., AAPL ~$190, GOOGL ~$175, etc.)" and its "correlated moves across tickers (e.g., tech stocks move together)" example directly — seven of the ten default tickers share the `tech` sector.

## 4. Correlated Moves

Real markets move together — a broad selloff drags almost everything down, a sector-specific narrative moves only related names. This is modeled as a **three-factor decomposition** of each ticker's random draw `Z`, computed fresh every tick:

```
Z_i = β_market · Z_market + β_sector · Z_sector[sector(i)] + β_idio · Z_idio_i
```

where `β_market² + β_sector² + β_idio² = 1` so `Z_i` remains standard normal (variance-preserving mix).

```python
BETA_MARKET = 0.5
BETA_SECTOR = 0.3
BETA_IDIO = (1 - BETA_MARKET**2 - BETA_SECTOR**2) ** 0.5  # ≈ 0.812

def _tick_factors(rng: np.random.Generator, sectors: set[str]) -> tuple[float, dict[str, float]]:
    z_market = rng.standard_normal()
    z_sector = {s: rng.standard_normal() for s in sectors}
    return z_market, z_sector

def _combined_z(rng: np.random.Generator, sector: str, z_market: float, z_sector: dict[str, float]) -> float:
    z_idio = rng.standard_normal()
    return BETA_MARKET * z_market + BETA_SECTOR * z_sector[sector] + BETA_IDIO * z_idio
```

`z_market` is drawn **once per tick** and shared by every ticker — this is what makes "everything drifts down together" happen. `z_sector[s]` is drawn once per tick per distinct sector present among tracked tickers, shared by every ticker in that sector — this is the "tech stocks move together, independent of finance" behavior. `z_idio` is drawn per-ticker, giving each stock its own independent wiggle on top of the shared factors.

Tickers with no assigned sector (a symbol not in the seed table — see §6) default to a synthetic sector, e.g. `"general"`, still sharing the market factor but with no sector-mates to correlate against beyond itself.

## 5. Random Events

PLAN.md §6: "Occasional random 'events' — sudden 2-5% moves on a ticker for drama." This is a **discrete jump applied on top of the GBM step**, not part of the continuous model — it represents a headline (earnings surprise, exec departure, product announcement) rather than routine volatility.

```python
EVENT_PROBABILITY_PER_TICK = 0.0003   # per ticker, per tick
EVENT_MAGNITUDE_RANGE = (0.02, 0.05)  # 2-5%, direction chosen independently

def _maybe_apply_event(rng: np.random.Generator, price: float) -> float:
    if rng.random() >= EVENT_PROBABILITY_PER_TICK:
        return price
    magnitude = rng.uniform(*EVENT_MAGNITUDE_RANGE)
    direction = 1 if rng.random() < 0.5 else -1
    return price * (1 + direction * magnitude)
```

At `POLL_INTERVAL_SECONDS = 0.5` (2 ticks/sec), `p = 0.0003` works out to roughly one event per ticker every ~28 minutes, or — across a 10-ticker watchlist — one visible "spike" somewhere on the board every ~3 minutes. That's frequent enough to be noticeable during a demo without every price update looking like an event. Tune `EVENT_PROBABILITY_PER_TICK` directly if that cadence feels off; it's the only knob that controls event frequency.

Events are applied **after** the GBM step each tick, multiplicatively on the post-GBM price, so they compound with (rather than replace) the ongoing random walk.

## 6. Seeding New Tickers

A ticker not in the table in §3 (user adds an arbitrary symbol to the watchlist, or the LLM buys something exotic) needs a starting price with no real-world reference. Per PLAN.md §6 ("a synthesized seed... if not [known]"):

```python
DEFAULT_SEED_PRICE = 100.00
DEFAULT_PARAMS = TickerParams(drift=0.08, volatility=0.30, sector="general")

def seed_for(ticker: str) -> tuple[float, TickerParams]:
    if ticker in KNOWN_SEED_PRICES:
        return KNOWN_SEED_PRICES[ticker], KNOWN_PARAMS[ticker]
    return DEFAULT_SEED_PRICE, DEFAULT_PARAMS
```

A fixed `$100.00` reference price is arbitrary but predictable — good enough for a simulated ticker whose whole point is that it isn't a real, priceable instrument.

## 7. Determinism (`SIMULATOR_SEED`)

PLAN.md §12 requires the E2E suite to run with a fixed `SIMULATOR_SEED` for reproducible price sequences. All randomness in this design — market/sector/idiosyncratic normal draws (§4) and event triggers/magnitudes (§5) — flows through a single `numpy.random.Generator` constructed once at `SimulatorProvider` startup:

```python
import numpy as np

class SimulatorProvider(MarketDataProvider):
    def __init__(self, seed: int | None = None, poll_interval_seconds: float = 0.5):
        self._rng = np.random.default_rng(seed)   # seed=None → nondeterministic, normal operation
        self._interval = poll_interval_seconds
        self._states: dict[str, _TickerState] = {}
```

Because `_rng` is a single stateful `Generator` advanced in a fixed order (market factor → per-sector factors → per-ticker idiosyncratic + event draws, in a stable ticker iteration order), the exact same `seed` reproduces the exact same sequence of prices for the exact same sequence of `fetch_quotes(tickers)` calls. Test code sets `SIMULATOR_SEED` as an integer env var; `create_market_data_provider()` (`MARKET_INTERFACE.md` §4) reads it and passes it through. Production runs leave it unset, so `default_rng(None)` seeds from OS entropy and every container run looks different.

**Stable iteration order matters for determinism**: ticker draws within a tick must happen in a consistent order (e.g. sorted ticker symbols) — iterating a `set` or a dict with insertion-order-dependent history would make the draw sequence (and therefore the resulting prices) depend on the order tickers were tracked, not just the seed.

## 8. Full `step()` Implementation

Ties §3-§6 together into the method `SimulatorProvider.fetch_quotes` delegates to:

```python
# backend/app/market/simulator.py
import numpy as np
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class _TickerState:
    price: float
    params: "TickerParams"


class Simulator:
    def __init__(self, seed: int | None = None, interval_seconds: float = 0.5):
        self._rng = np.random.default_rng(seed)
        self._dt = interval_seconds / SECONDS_PER_TRADING_YEAR
        self._states: dict[str, _TickerState] = {}

    def step(self, tickers: list[str]) -> dict[str, "TickerQuote"]:
        # Ensure every requested ticker has state, seeding new ones (§6)
        for ticker in sorted(tickers):
            if ticker not in self._states:
                price, params = seed_for(ticker)
                self._states[ticker] = _TickerState(price=price, params=params)

        sectors = {self._states[t].params.sector for t in tickers}
        z_market, z_sector = _tick_factors(self._rng, sectors)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        out: dict[str, TickerQuote] = {}
        for ticker in sorted(tickers):
            state = self._states[ticker]
            z = _combined_z(self._rng, state.params.sector, z_market, z_sector)
            mu, sigma = state.params.drift, state.params.volatility
            new_price = state.price * np.exp((mu - 0.5 * sigma**2) * self._dt + sigma * (self._dt ** 0.5) * z)
            new_price = _maybe_apply_event(self._rng, new_price)
            new_price = max(new_price, 0.01)  # floor — GBM is theoretically positive but guard float edge cases
            state.price = new_price
            out[ticker] = TickerQuote(ticker=ticker, price=round(new_price, 2), timestamp=now)
        return out
```

`SimulatorProvider` (the `MarketDataProvider` implementation referenced in `MARKET_INTERFACE.md` §6) is a thin wrapper: it owns one `Simulator` instance and calls `.step(tickers)` from `fetch_quotes`, matching the return type (`dict[str, TickerQuote]`) the interface requires — same shape `MassiveProvider` returns, so `PriceCache` and everything above it is unaware which one is active.

```python
# backend/app/market/simulator_provider.py
from .interface import MarketDataProvider, TickerQuote
from .simulator import Simulator

class SimulatorProvider(MarketDataProvider):
    def __init__(self, seed: int | None = None, poll_interval_seconds: float = 0.5):
        self._interval = poll_interval_seconds
        self._sim = Simulator(seed=seed, interval_seconds=poll_interval_seconds)

    @property
    def poll_interval_seconds(self) -> float:
        return self._interval

    async def fetch_quotes(self, tickers: list[str]) -> dict[str, TickerQuote]:
        return self._sim.step(tickers)  # pure CPU-bound, no actual await needed
```

`fetch_quotes` is declared `async` only to satisfy the `MarketDataProvider` interface (`MARKET_INTERFACE.md` §2) — `Simulator.step` itself is synchronous, CPU-bound, and fast enough (O(number of tracked tickers), no I/O) that it never needs to yield to the event loop mid-call.

## 9. Module Layout

```
backend/app/market/simulator/
├── __init__.py
├── simulator.py            # Simulator class, GBM math, correlation, events (§3-§6, §8)
├── simulator_provider.py    # SimulatorProvider — MarketDataProvider adapter (§8)
└── seed_data.py             # KNOWN_SEED_PRICES, KNOWN_PARAMS, DEFAULT_SEED_PRICE (§3, §6)
```

## 10. Testing

- **GBM correctness**: given a fixed seed and a single ticker with `σ=0` (no volatility), the price should follow the deterministic drift term exactly — a good sanity check that the exponent is computed correctly.
- **Determinism**: two `Simulator(seed=42)` instances, fed the identical sequence of `step()` calls, must produce byte-identical price sequences (PLAN.md §12).
- **Correlation**: with a fixed seed, tech-sector tickers should show positive correlation in their per-tick returns over many ticks (statistical assertion, not exact equality — e.g. Pearson correlation coefficient above some threshold across a simulated run).
- **Events**: with `EVENT_PROBABILITY_PER_TICK` temporarily forced to `1.0` in a test, the very next `step()` call must show a move within `EVENT_MAGNITUDE_RANGE`.
- **New-ticker seeding**: requesting a ticker never seen before returns `DEFAULT_SEED_PRICE`, and a second call for the same ticker evolves from that price (not a fresh reseed) — state persistence across calls.
- **Conformance to `MarketDataProvider`**: covered by the shared conformance suite in `MARKET_INTERFACE.md` §7, run against `SimulatorProvider` directly (no mocking needed, unlike `MassiveProvider`).
