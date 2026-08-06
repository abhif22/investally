"""Both MarketDataProvider implementations must conform to the same
contract: fetch_quotes returns a subset of requested tickers with
positive prices and ISO timestamps, and never raises for an unknown
ticker. MassiveProvider is exercised via a mocked httpx transport —
never a live network call.
"""

import httpx
import pytest

from app.market.massive_provider import MassiveProvider
from app.market.simulator.simulator_provider import SimulatorProvider


def _massive_transport(price_map: dict[str, float]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        tickers_param = request.url.params.get("tickers", "")
        requested = [t for t in tickers_param.split(",") if t]
        rows = [
            {
                "ticker": ticker,
                "lastTrade": {"p": price_map[ticker]},
                "updated": 1_700_000_000_000_000_000,
            }
            for ticker in requested
            if ticker in price_map
        ]
        return httpx.Response(200, json={"tickers": rows})

    return httpx.MockTransport(handler)


def _make_simulator() -> SimulatorProvider:
    return SimulatorProvider(seed=42)


def _make_massive() -> MassiveProvider:
    return MassiveProvider(
        api_key="test-key",
        transport=_massive_transport({"AAPL": 190.0, "GOOGL": 175.0}),
    )


PROVIDER_FACTORIES = [_make_simulator, _make_massive]


@pytest.mark.parametrize("make_provider", PROVIDER_FACTORIES)
async def test_fetch_quotes_returns_requested_tickers(make_provider):
    provider = make_provider()
    quotes = await provider.fetch_quotes(["AAPL", "GOOGL"])

    assert set(quotes) <= {"AAPL", "GOOGL"}  # subset — partial results allowed
    for quote in quotes.values():
        assert quote.price > 0
        assert quote.timestamp

    await provider.aclose()


@pytest.mark.parametrize("make_provider", PROVIDER_FACTORIES)
async def test_fetch_quotes_never_raises_on_unknown_ticker(make_provider):
    provider = make_provider()

    quotes = await provider.fetch_quotes(["ZZZZZ_NOT_REAL"])

    if isinstance(provider, SimulatorProvider):
        # The simulator synthesizes a price for any symbol.
        assert "ZZZZZ_NOT_REAL" in quotes
    else:
        assert quotes == {}

    await provider.aclose()


@pytest.mark.parametrize("make_provider", PROVIDER_FACTORIES)
async def test_poll_interval_seconds_is_positive(make_provider):
    provider = make_provider()
    assert provider.poll_interval_seconds > 0
    await provider.aclose()


@pytest.mark.parametrize("make_provider", PROVIDER_FACTORIES)
async def test_fetch_quote_convenience_wrapper_matches_fetch_quotes(make_provider):
    provider = make_provider()
    quote = await provider.fetch_quote("AAPL")
    assert quote is not None
    assert quote.ticker == "AAPL"
    await provider.aclose()
