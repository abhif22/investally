"""MassiveProvider — REST polling against Massive's (formerly Polygon.io)
batch snapshot endpoint. No WebSocket, no options/forex/crypto — just US
stock snapshots. See planning/MARKET_DATA_DESIGN.md Sec 8 and
planning/MASSIVE_API.md.
"""

from datetime import datetime, timezone

import httpx

from .cache import _iso_now
from .interface import MarketDataProvider, TickerQuote
from .rate_limiter import TokenBucketLimiter

MASSIVE_BASE_URL = "https://api.massive.com"
FREE_TIER_CALLS_PER_MINUTE = 5


class MarketDataFetchError(Exception):
    """Raised for a total call failure (network error, non-2xx). Distinct
    from a partial result, which is never an error — see the
    MarketDataProvider.fetch_quotes contract."""


class MassiveProvider(MarketDataProvider):
    def __init__(
        self,
        api_key: str,
        poll_interval_seconds: float = 15.0,
        calls_per_minute: int = FREE_TIER_CALLS_PER_MINUTE,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._api_key = api_key
        self._interval = poll_interval_seconds
        self._client = httpx.AsyncClient(
            base_url=MASSIVE_BASE_URL, timeout=10.0, transport=transport
        )
        # ONE limiter shared by fetch_quotes (poll loop) and
        # fetch_quote_immediate (watchlist-add/trade seeding) — this is
        # what makes it structurally impossible for the two call sites to
        # combine into more than `calls_per_minute` calls/min.
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
        request hang for up to `60/calls_per_minute` seconds — the ticker
        is already in PriceCache._tracked by the time this is called, so
        the next scheduled poll fetches it for free."""
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
            # Total-call-failure case: PriceCache catches this and keeps
            # serving stale prices.
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
        PriceCache.stop() — the interface explicitly excludes general
        lifecycle methods, but a provider that opens a real connection
        needs a symmetric teardown hook, or the client (and its
        connection pool) leaks for the life of the process."""
        await self._client.aclose()


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
        return _iso_now()
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
