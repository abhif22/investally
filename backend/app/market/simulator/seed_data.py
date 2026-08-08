"""Per-ticker simulator parameters and seed prices.

See planning/MARKET_DATA_DESIGN.md Sec 7.3.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TickerParams:
    drift: float  # mu, annualized, e.g. 0.08 for a mild uptrend
    volatility: float  # sigma, annualized, e.g. 0.30 for a typically volatile tech name
    sector: str  # correlation grouping


KNOWN_SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT": 420.00,
    "AMZN": 185.00,
    "TSLA": 250.00,
    "NVDA": 120.00,
    "META": 500.00,
    "JPM": 210.00,
    "V": 280.00,
    "NFLX": 650.00,
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
