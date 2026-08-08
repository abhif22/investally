"""SimulatorProvider — the MarketDataProvider adapter around Simulator."""

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
        # Pure CPU-bound, no I/O to await — declared async only to satisfy
        # the MarketDataProvider interface.
        return self._sim.step(tickers)

    # fetch_quote_immediate uses the interface's default (delegates to
    # fetch_quote), which is fine here: the simulator has no rate limit to
    # respect, so an immediate fetch always succeeds instantly.
