"""Scriptable MarketDataProvider used by PriceCache unit tests."""

from app.market.interface import MarketDataProvider, TickerQuote


class FakeProvider(MarketDataProvider):
    def __init__(self, poll_interval_seconds: float = 0.01):
        self._interval = poll_interval_seconds
        self.fetch_quotes_calls: list[list[str]] = []
        self._queued_responses: list[dict[str, TickerQuote] | Exception] = []
        self._immediate: dict[str, TickerQuote | None | Exception] = {}
        self.aclose_calls = 0

    @property
    def poll_interval_seconds(self) -> float:
        return self._interval

    def queue_response(self, response: dict[str, TickerQuote] | Exception) -> None:
        self._queued_responses.append(response)

    def set_immediate(self, ticker: str, value: TickerQuote | None | Exception) -> None:
        self._immediate[ticker] = value

    async def fetch_quotes(self, tickers: list[str]) -> dict[str, TickerQuote]:
        self.fetch_quotes_calls.append(list(tickers))
        if self._queued_responses:
            response = self._queued_responses.pop(0)
        else:
            response = {}
        if isinstance(response, Exception):
            raise response
        return response

    async def fetch_quote_immediate(self, ticker: str) -> TickerQuote | None:
        if ticker in self._immediate:
            value = self._immediate[ticker]
            if isinstance(value, Exception):
                raise value
            return value
        return None

    async def aclose(self) -> None:
        self.aclose_calls += 1
