"""The MarketDataProvider abstraction.

Both MassiveProvider and SimulatorProvider implement this interface;
PriceCache is the only caller of it. See planning/MARKET_DATA_DESIGN.md
Sec 4 for the authoritative design this implements.
"""

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
        the simulator can run much faster (~0.5s)."""
        raise NotImplementedError

    @abstractmethod
    async def fetch_quotes(self, tickers: list[str]) -> dict[str, TickerQuote]:
        """Fetch the latest price for every ticker in `tickers`, in a single
        batched operation — never one call per ticker (rate limits are
        per-call, not per-ticker).

        Contract:
        - `tickers` is deduplicated, non-empty, already normalized
          (uppercase, per PLAN.md Sec 8 conventions) by the caller.
        - Returns a dict keyed by ticker. A ticker the provider has no data
          for is simply ABSENT from the returned dict — never a
          null/placeholder entry. Callers must handle partial results.
        - Never raises for a partial/degraded result. Only raises for a
          total failure of the call itself (network error, auth error,
          rate-limited with no data at all) — PriceCache catches that and
          falls back to stale cached values.
        - May block waiting for rate-limit budget (MassiveProvider does).
          This method is only ever called from PriceCache's poll loop or
          its batch-seed path, never from a path a user request is
          waiting on synchronously.
        """
        raise NotImplementedError

    async def fetch_quote(self, ticker: str) -> TickerQuote | None:
        """Convenience wrapper. Default implementation just calls
        fetch_quotes([ticker]) — providers don't need to override this."""
        result = await self.fetch_quotes([ticker])
        return result.get(ticker)

    async def fetch_quote_immediate(self, ticker: str) -> TickerQuote | None:
        """Best-effort, NON-BLOCKING immediate fetch for a single newly
        tracked ticker (a new ticker should get its first price right away
        rather than waiting for the next poll).

        Default implementation delegates to fetch_quote — fine for a
        provider with no rate limit (the simulator: this always succeeds
        instantly). A rate-limited provider (MassiveProvider) overrides
        this to return None immediately, without waiting or blocking, if
        satisfying it would spend budget the scheduled poll loop needs.
        PriceCache treats None as "no price yet — will arrive on the next
        scheduled poll," not as an error."""
        return await self.fetch_quote(ticker)

    async def aclose(self) -> None:
        """Release any held resources (e.g. an HTTP client). Default
        no-op — only MassiveProvider overrides this. Called exactly once,
        from PriceCache.stop() during FastAPI shutdown."""
        return None
