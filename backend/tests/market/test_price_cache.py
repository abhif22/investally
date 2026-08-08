"""PriceCache semantics — tested against a scripted FakeProvider so behavior
is verified independently of either real provider implementation.
"""

from app.market.cache import PriceCache
from app.market.interface import TickerQuote

from .fakes import FakeProvider


async def test_track_many_issues_exactly_one_fetch_quotes_call():
    fake = FakeProvider()
    fake.queue_response(
        {
            "AAPL": TickerQuote("AAPL", 100.0, "2026-01-01T00:00:00Z"),
            "MSFT": TickerQuote("MSFT", 200.0, "2026-01-01T00:00:00Z"),
        }
    )
    cache = PriceCache(fake)

    await cache.track_many(["AAPL", "MSFT"])

    assert len(fake.fetch_quotes_calls) == 1
    assert sorted(fake.fetch_quotes_calls[0]) == ["AAPL", "MSFT"]
    assert cache.get("AAPL").price == 100.0
    assert cache.get("AAPL").version == 1
    assert cache.get("MSFT").price == 200.0


async def test_track_many_only_fetches_newly_tracked_tickers():
    fake = FakeProvider()
    fake.queue_response({"AAPL": TickerQuote("AAPL", 100.0, "2026-01-01T00:00:00Z")})
    cache = PriceCache(fake)
    await cache.track_many(["AAPL"])

    fake.queue_response({"MSFT": TickerQuote("MSFT", 200.0, "2026-01-01T00:00:01Z")})
    await cache.track_many(["AAPL", "MSFT"])

    assert len(fake.fetch_quotes_calls) == 2
    assert fake.fetch_quotes_calls[1] == ["MSFT"]


async def test_reading_a_removed_then_retracked_ticker_preserves_opening_price():
    fake = FakeProvider()
    cache = PriceCache(fake)
    fake.set_immediate("AAPL", TickerQuote("AAPL", 100.0, "2026-01-01T00:00:00Z"))

    await cache.track("AAPL")
    assert cache.get("AAPL").opening_price == 100.0

    await cache.untrack_if_unused("AAPL", still_has_position=False)
    assert "AAPL" not in cache.tracked_tickers
    # Last known price/opening_price are retained even while untracked.
    assert cache.get("AAPL").opening_price == 100.0

    fake.set_immediate("AAPL", TickerQuote("AAPL", 150.0, "2026-01-01T00:01:00Z"))
    await cache.track("AAPL")

    cp = cache.get("AAPL")
    assert "AAPL" in cache.tracked_tickers
    # Opening price captured once per container lifetime — preserved.
    assert cp.opening_price == 100.0
    # A ticker with an existing cache entry is not re-seeded; it refreshes
    # in place on the next poll instead.
    assert cp.price == 100.0


async def test_untrack_if_unused_keeps_tracking_when_position_is_open():
    fake = FakeProvider()
    cache = PriceCache(fake)
    fake.set_immediate("AAPL", TickerQuote("AAPL", 100.0, "2026-01-01T00:00:00Z"))
    await cache.track("AAPL")

    await cache.untrack_if_unused("AAPL", still_has_position=True)

    assert "AAPL" in cache.tracked_tickers


async def test_apply_if_newer_drops_an_out_of_order_write():
    fake = FakeProvider()
    cache = PriceCache(fake)

    cache._apply_if_newer("AAPL", 100.0, "2026-01-01T00:00:10Z")
    cache._apply_if_newer("AAPL", 90.0, "2026-01-01T00:00:05Z")  # stale, arrives late

    cp = cache.get("AAPL")
    assert cp.price == 100.0
    assert cp.version == 1


async def test_a_tracked_ticker_with_failed_immediate_fetch_is_priced_on_next_poll():
    fake = FakeProvider()
    cache = PriceCache(fake)
    fake.set_immediate("AAPL", None)  # e.g. rate-limit budget exhausted

    await cache.track("AAPL")
    assert cache.get("AAPL") is None
    assert "AAPL" in cache.tracked_tickers

    fake.queue_response({"AAPL": TickerQuote("AAPL", 120.0, "2026-01-01T00:00:05Z")})
    await cache._poll_once()

    cp = cache.get("AAPL")
    assert cp is not None
    assert cp.price == 120.0


async def test_version_only_increments_on_an_applied_quote_not_on_a_poll_exception():
    fake = FakeProvider()
    cache = PriceCache(fake)
    fake.queue_response({"AAPL": TickerQuote("AAPL", 100.0, "2026-01-01T00:00:00Z")})
    await cache.track_many(["AAPL"])
    assert cache.get("AAPL").version == 1

    fake.queue_response(RuntimeError("provider unreachable"))
    await cache._poll_once()

    cp = cache.get("AAPL")
    assert cp.version == 1
    assert cp.price == 100.0


async def test_a_poll_failure_keeps_serving_the_last_known_price_for_every_ticker():
    fake = FakeProvider()
    cache = PriceCache(fake)
    fake.queue_response(
        {
            "AAPL": TickerQuote("AAPL", 100.0, "2026-01-01T00:00:00Z"),
            "MSFT": TickerQuote("MSFT", 200.0, "2026-01-01T00:00:00Z"),
        }
    )
    await cache.track_many(["AAPL", "MSFT"])

    fake.queue_response(RuntimeError("network error"))
    await cache._poll_once()

    assert cache.get("AAPL").price == 100.0
    assert cache.get("MSFT").price == 200.0


async def test_snapshot_excludes_tracked_but_pending_tickers():
    fake = FakeProvider()
    cache = PriceCache(fake)
    fake.set_immediate("AAPL", None)
    await cache.track("AAPL")

    assert cache.snapshot() == []
    assert "AAPL" in cache.tracked_tickers


async def test_stop_awaits_the_poll_task_and_closes_the_provider_exactly_once():
    fake = FakeProvider(poll_interval_seconds=1000)  # never fires during the test
    cache = PriceCache(fake)
    cache.start()

    await cache.stop()

    assert fake.aclose_calls == 1
