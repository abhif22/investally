"""_event_source — initial snapshot, version-gated updates, and heartbeat
behavior.

Drives the generator directly rather than through an HTTP/ASGI round-trip:
httpx.ASGITransport (and Starlette's own TestClient, which hits the same
issue) only returns a response once the ASGI app call fully completes, but
this endpoint's generator runs forever until the client disconnects — so a
transport-level test deadlocks waiting for a response that can't arrive
before the generator it's waiting on exits. See _FakeRequest below.
"""

import asyncio
import json

import pytest

from app.api import stream as stream_module
from app.market.cache import PriceCache
from app.market.interface import TickerQuote

from .fakes import FakeProvider


class _FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


async def _collect_lines(line_iter, count: int, timeout: float = 3.0) -> list[str]:
    lines: list[str] = []

    async def _reader() -> None:
        async for chunk in line_iter:
            lines.extend(chunk.splitlines())
            if len(lines) >= count:
                return

    await asyncio.wait_for(_reader(), timeout=timeout)
    return lines


async def test_initial_snapshot_is_sent_before_the_poll_loop():
    fake = FakeProvider()
    fake.set_immediate("AAPL", TickerQuote("AAPL", 190.0, "2026-01-01T00:00:00Z"))
    cache = PriceCache(fake)
    await cache.track("AAPL")

    gen = stream_module._event_source(_FakeRequest(), cache)
    lines = await _collect_lines(gen, 3)

    assert lines[0] == "event: price"
    payload = json.loads(lines[1].removeprefix("data: "))
    assert lines[2] == ""

    assert payload["ticker"] == "AAPL"
    assert payload["price"] == 190.0
    assert payload["previous_price"] == 190.0
    assert payload["change_direction"] == "flat"


async def test_a_version_bump_emits_a_new_event_with_updated_fields(monkeypatch):
    monkeypatch.setattr(stream_module, "SSE_CHECK_INTERVAL_SECONDS", 0.05)
    fake = FakeProvider()
    fake.set_immediate("AAPL", TickerQuote("AAPL", 190.0, "2026-01-01T00:00:00Z"))
    cache = PriceCache(fake)
    await cache.track("AAPL")

    gen = stream_module._event_source(_FakeRequest(), cache)
    await _collect_lines(gen, 3)  # initial snapshot

    cache._apply_if_newer("AAPL", 195.0, "2026-01-01T00:00:01Z")

    lines = await _collect_lines(gen, 3, timeout=2.0)

    payload = json.loads(lines[1].removeprefix("data: "))
    assert payload["price"] == 195.0
    assert payload["previous_price"] == 190.0
    assert payload["change_direction"] == "up"


async def test_no_event_is_sent_when_nothing_changes_between_checks(monkeypatch):
    monkeypatch.setattr(stream_module, "SSE_CHECK_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(stream_module, "HEARTBEAT_SECONDS", 10_000)  # effectively disabled
    fake = FakeProvider()
    fake.set_immediate("AAPL", TickerQuote("AAPL", 190.0, "2026-01-01T00:00:00Z"))
    cache = PriceCache(fake)
    await cache.track("AAPL")

    gen = stream_module._event_source(_FakeRequest(), cache)
    await _collect_lines(gen, 3)  # initial snapshot

    # No cache mutation here — several check cycles elapse with no
    # change, so nothing further should arrive.
    with pytest.raises(asyncio.TimeoutError):
        await _collect_lines(gen, 1, timeout=0.5)


async def test_heartbeat_is_sent_when_idle(monkeypatch):
    monkeypatch.setattr(stream_module, "SSE_CHECK_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(stream_module, "HEARTBEAT_SECONDS", 0.1)
    cache = PriceCache(FakeProvider())  # no tracked tickers -> no price events at all

    gen = stream_module._event_source(_FakeRequest(), cache)
    lines = await _collect_lines(gen, 2, timeout=2.0)

    assert lines[0] == ": ping"
    assert lines[1] == ""
