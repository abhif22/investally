"""GBM math, correlation, random events, and SIMULATOR_SEED determinism."""

import math

import numpy as np
import pytest

from app.market.simulator import simulator as simulator_module
from app.market.simulator.seed_data import DEFAULT_SEED_PRICE, TickerParams
from app.market.simulator.simulator import (
    EVENT_MAGNITUDE_RANGE,
    SECONDS_PER_TRADING_YEAR,
    Simulator,
    _TickerState,
)


def test_gbm_follows_deterministic_drift_when_volatility_is_zero(monkeypatch):
    monkeypatch.setattr(simulator_module, "EVENT_PROBABILITY_PER_TICK", 0.0)
    # A full trading-year step (dt = 1) makes the drift term large enough
    # to check precisely, rather than lost in float noise at the real
    # ~500ms tick size.
    sim = Simulator(seed=1, interval_seconds=SECONDS_PER_TRADING_YEAR)
    sim._states["TEST"] = _TickerState(
        price=100.0, params=TickerParams(drift=0.1, volatility=0.0, sector="general")
    )

    quote = sim.step(["TEST"])["TEST"]

    expected = 100.0 * math.exp(0.1 * 1.0)
    assert quote.price == pytest.approx(round(expected, 2), abs=1e-2)


def test_determinism_same_seed_and_call_sequence_produce_identical_quotes():
    def make_clock():
        counter = iter(range(10_000))

        def clock() -> str:
            return f"2026-01-01T00:00:{next(counter):04d}Z"

        return clock

    sim1 = Simulator(seed=42, clock=make_clock())
    sim2 = Simulator(seed=42, clock=make_clock())

    call_sequence = [["AAPL", "GOOGL"], ["AAPL", "GOOGL", "MSFT"], ["AAPL"]]
    for tickers in call_sequence:
        quotes1 = sim1.step(tickers)
        quotes2 = sim2.step(tickers)
        assert quotes1 == quotes2


def test_different_seeds_produce_different_price_paths():
    sim1 = Simulator(seed=1)
    sim2 = Simulator(seed=2)

    quotes1 = sim1.step(["AAPL"])
    quotes2 = sim2.step(["AAPL"])

    assert quotes1["AAPL"].price != quotes2["AAPL"].price


def test_tech_sector_tickers_are_positively_correlated():
    sim = Simulator(seed=7, interval_seconds=0.5)
    tickers = ["AAPL", "MSFT"]  # both "tech" sector
    aapl_prices = []
    msft_prices = []

    for _ in range(800):
        quotes = sim.step(tickers)
        aapl_prices.append(quotes["AAPL"].price)
        msft_prices.append(quotes["MSFT"].price)

    aapl_returns = np.diff(np.log(aapl_prices))
    msft_returns = np.diff(np.log(msft_prices))
    correlation = np.corrcoef(aapl_returns, msft_returns)[0, 1]

    assert correlation > 0.2


def test_forced_event_produces_a_move_within_the_magnitude_range(monkeypatch):
    monkeypatch.setattr(simulator_module, "EVENT_PROBABILITY_PER_TICK", 1.0)
    sim = Simulator(seed=3, interval_seconds=0.5)
    sim._states["TEST"] = _TickerState(
        price=100.0, params=TickerParams(drift=0.0, volatility=0.0, sector="general")
    )

    quote = sim.step(["TEST"])["TEST"]

    relative_move = abs(quote.price - 100.0) / 100.0
    low, high = EVENT_MAGNITUDE_RANGE
    assert low - 1e-3 <= relative_move <= high + 1e-3


def test_price_is_floored_above_zero_for_an_extreme_negative_drift(monkeypatch):
    monkeypatch.setattr(simulator_module, "EVENT_PROBABILITY_PER_TICK", 0.0)
    # A huge nominal dt (100 trading years in one step) with a strongly
    # negative drift drives exp(...) towards zero, exercising the
    # PRICE_FLOOR guard against a non-positive/zero price.
    sim = Simulator(seed=3, interval_seconds=SECONDS_PER_TRADING_YEAR * 100)
    sim._states["TEST"] = _TickerState(
        price=100.0, params=TickerParams(drift=-5.0, volatility=0.0, sector="general")
    )

    quote = sim.step(["TEST"])["TEST"]

    assert quote.price > 0
    assert quote.price == pytest.approx(simulator_module.PRICE_FLOOR, abs=1e-9)


def test_unseen_ticker_uses_default_seed_price_and_is_not_reseeded_on_later_calls(monkeypatch):
    # Disable random events here so the assertion below isn't at the mercy
    # of whether this particular seed happens to trigger one.
    monkeypatch.setattr(simulator_module, "EVENT_PROBABILITY_PER_TICK", 0.0)
    calls: list[str] = []
    original_seed_for = simulator_module.seed_for

    def counting_seed_for(ticker: str):
        calls.append(ticker)
        return original_seed_for(ticker)

    monkeypatch.setattr(simulator_module, "seed_for", counting_seed_for)
    sim = Simulator(seed=9, interval_seconds=0.5)

    first = sim.step(["ZZZZ"])["ZZZZ"]
    assert first.price == pytest.approx(DEFAULT_SEED_PRICE, rel=0.01)

    sim.step(["ZZZZ"])

    assert calls.count("ZZZZ") == 1  # seeded once; the second call evolves existing state
