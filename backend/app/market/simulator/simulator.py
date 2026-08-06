"""Geometric Brownian Motion price simulator.

Generates correlated, occasionally-eventful price paths for a set of
tickers. See planning/MARKET_DATA_DESIGN.md Sec 7 for the full design.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import numpy as np

from ..interface import TickerQuote
from .seed_data import TickerParams, seed_for

SECONDS_PER_TRADING_YEAR = 252 * 6.5 * 3600  # 252 trading days, 6.5h/day

BETA_MARKET = 0.5
BETA_SECTOR = 0.3
BETA_IDIO = (1 - BETA_MARKET**2 - BETA_SECTOR**2) ** 0.5  # ~= 0.812

EVENT_PROBABILITY_PER_TICK = 0.0003  # per ticker, per tick
EVENT_MAGNITUDE_RANGE = (0.02, 0.05)  # 2-5%, direction chosen independently

PRICE_FLOOR = 0.01  # guards float edge cases; GBM is theoretically positive


@dataclass
class _TickerState:
    price: float
    params: TickerParams


def _default_clock() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tick_factors(
    rng: np.random.Generator, sorted_sectors: list[str]
) -> tuple[float, dict[str, float]]:
    z_market = rng.standard_normal()
    # `sorted_sectors` MUST be sorted, not iterated from a set. A Python
    # `set`'s iteration order depends on hash randomization, which varies
    # PER PROCESS — the same seed would draw z_sector[s] in a different
    # order across two runs, assigning different random values to
    # different sectors even though the RNG itself is deterministic.
    z_sector = {s: rng.standard_normal() for s in sorted_sectors}
    return z_market, z_sector


def _combined_z(
    rng: np.random.Generator,
    sector: str,
    z_market: float,
    z_sector: dict[str, float],
) -> float:
    z_idio = rng.standard_normal()
    return BETA_MARKET * z_market + BETA_SECTOR * z_sector[sector] + BETA_IDIO * z_idio


def _maybe_apply_event(rng: np.random.Generator, price: float) -> float:
    if rng.random() >= EVENT_PROBABILITY_PER_TICK:
        return price
    magnitude = rng.uniform(*EVENT_MAGNITUDE_RANGE)
    direction = 1 if rng.random() < 0.5 else -1
    return price * (1 + direction * magnitude)


class Simulator:
    """Owns per-ticker GBM state and advances it one tick at a time.

    All randomness flows through a single `numpy.random.Generator`,
    advanced in a fixed, sorted order every tick (market factor -> sorted
    sector factors -> sorted-ticker idiosyncratic + event draws). This is
    what makes a fixed `seed` reproduce a byte-identical sequence of
    prices, independent of Python's hash randomization or dict/set
    iteration order.
    """

    def __init__(
        self,
        seed: int | None = None,
        interval_seconds: float = 0.5,
        clock: Callable[[], str] | None = None,
    ):
        self._rng = np.random.default_rng(seed)  # seed=None -> nondeterministic
        self._dt = interval_seconds / SECONDS_PER_TRADING_YEAR
        self._states: dict[str, _TickerState] = {}
        # `clock` is injectable so tests can get byte-identical
        # TickerQuote objects (including timestamp), not just identical
        # prices. Production leaves this as the real UTC clock.
        self._clock = clock or _default_clock

    def step(self, tickers: list[str]) -> dict[str, TickerQuote]:
        sorted_tickers = sorted(set(tickers))
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
                (mu - 0.5 * sigma**2) * self._dt + sigma * (self._dt**0.5) * z
            )
            new_price = _maybe_apply_event(self._rng, new_price)
            new_price = max(new_price, PRICE_FLOOR)
            state.price = new_price
            out[ticker] = TickerQuote(ticker=ticker, price=round(new_price, 2), timestamp=now)
        return out
