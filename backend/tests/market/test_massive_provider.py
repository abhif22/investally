"""TokenBucketLimiter behavior and MassiveProvider's use of it, plus
response parsing/error handling — all against a mocked httpx transport,
never a live network call.
"""

import asyncio
import time

import httpx
import pytest

from app.market.massive_provider import MassiveProvider, MarketDataFetchError
from app.market.rate_limiter import TokenBucketLimiter


def _ok_transport(price_map: dict[str, float]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        tickers = [t for t in request.url.params.get("tickers", "").split(",") if t]
        rows = [
            {
                "ticker": ticker,
                "lastTrade": {"p": price_map[ticker]},
                "updated": 1_700_000_000_000_000_000,
            }
            for ticker in tickers
            if ticker in price_map
        ]
        return httpx.Response(200, json={"tickers": rows})

    return httpx.MockTransport(handler)


def _error_transport(status_code: int = 500) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "boom"})

    return httpx.MockTransport(handler)


async def test_try_acquire_exhausts_capacity_and_refills_over_time():
    limiter = TokenBucketLimiter(capacity=1, period_seconds=0.2)

    assert limiter.try_acquire() is True
    assert limiter.try_acquire() is False

    await asyncio.sleep(0.25)

    assert limiter.try_acquire() is True


async def test_acquire_blocks_until_a_token_refills_instead_of_failing():
    limiter = TokenBucketLimiter(capacity=1, period_seconds=0.2)
    assert limiter.try_acquire() is True  # consume the only token

    start = time.monotonic()
    await asyncio.wait_for(limiter.acquire(), timeout=1.0)
    elapsed = time.monotonic() - start

    assert elapsed >= 0.1  # had to wait for a refill, not return instantly


async def test_fetch_quote_immediate_returns_none_without_blocking_when_budget_exhausted():
    provider = MassiveProvider(
        api_key="key", calls_per_minute=1, transport=_ok_transport({"AAPL": 190.0})
    )
    first = await provider.fetch_quote_immediate("AAPL")
    assert first is not None
    assert first.price == 190.0

    start = time.monotonic()
    second = await provider.fetch_quote_immediate("AAPL")
    elapsed = time.monotonic() - start

    assert second is None
    assert elapsed < 0.5  # returned immediately rather than waiting on the 60s budget window
    await provider.aclose()


async def test_fetch_quotes_waits_for_budget_rather_than_failing():
    provider = MassiveProvider(api_key="key", transport=_ok_transport({"AAPL": 190.0}))
    # Swap in a fast limiter so the "waits, doesn't fail" behavior can be
    # observed without the real 60s budget window.
    provider._limiter = TokenBucketLimiter(capacity=1, period_seconds=0.2)
    assert provider._limiter.try_acquire() is True  # exhaust it up front

    start = time.monotonic()
    result = await asyncio.wait_for(provider.fetch_quotes(["AAPL"]), timeout=1.0)
    elapsed = time.monotonic() - start

    assert result["AAPL"].price == 190.0
    assert elapsed >= 0.1
    await provider.aclose()


async def test_fetch_quotes_raises_market_data_fetch_error_on_bad_response():
    provider = MassiveProvider(api_key="key", transport=_error_transport(500))

    with pytest.raises(MarketDataFetchError):
        await provider.fetch_quotes(["AAPL"])

    await provider.aclose()


async def test_fetch_quotes_omits_tickers_with_no_trade_data():
    provider = MassiveProvider(api_key="key", transport=_ok_transport({"AAPL": 190.0}))

    quotes = await provider.fetch_quotes(["AAPL", "UNKNOWN"])

    assert set(quotes) == {"AAPL"}
    await provider.aclose()


async def test_aclose_closes_the_underlying_http_client():
    provider = MassiveProvider(api_key="key", transport=_ok_transport({"AAPL": 190.0}))

    await provider.aclose()

    assert provider._client.is_closed is True
