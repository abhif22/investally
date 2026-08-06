"""Provider selection — the ONLY place MASSIVE_API_KEY is read.

No other module checks this environment variable; everything else
depends on MarketDataProvider/PriceCache, never on which branch was
taken. See planning/MARKET_DATA_DESIGN.md Sec 9.
"""

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
