"""GET /api/stream/prices — initial snapshot, version-gated updates, and
heartbeat behavior.
"""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from app.api import stream as stream_module
from app.market.cache import PriceCache

from .fakes import FakeProvider


def _build_app(cache: PriceCache) -> FastAPI:
    app = FastAPI()
    app.include_router(stream_module.router)
    app.state.price_cache = cache
    return app


async def _collect_lines(line_iter, count: int, timeout: float = 3.0) -> list[str]:
    lines: list[str] = []

    async def _reader() -> None:
        async for line in line_iter:
            lines.append(line)
            if len(lines) >= count:
                return

    await asyncio.wait_for(_reader(), timeout=timeout)
    return lines


async def test_initial_snapshot_is_sent_before_the_poll_loop():
    cache = PriceCache(FakeProvider())
    cache._apply_if_newer("AAPL", 190.0, "2026-01-01T00:00:00Z")
    app = _build_app(cache)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream("GET", "/api/stream/prices") as response:
            assert response.status_code == 200
            lines = await _collect_lines(response.aiter_lines(), 3)

    assert lines[0] == "event: price"
    payload = json.loads(lines[1].removeprefix("data: "))
    assert lines[2] == ""

    assert payload["ticker"] == "AAPL"
    assert payload["price"] == 190.0
    assert payload["previous_price"] == 190.0
    assert payload["change_direction"] == "flat"


async def test_a_version_bump_emits_a_new_event_with_updated_fields(monkeypatch):
    monkeypatch.setattr(stream_module, "SSE_CHECK_INTERVAL_SECONDS", 0.05)
    cache = PriceCache(FakeProvider())
    cache._apply_if_newer("AAPL", 190.0, "2026-01-01T00:00:00Z")
    app = _build_app(cache)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream("GET", "/api/stream/prices") as response:
            line_iter = response.aiter_lines()
            await _collect_lines(line_iter, 3)  # initial snapshot

            cache._apply_if_newer("AAPL", 195.0, "2026-01-01T00:00:01Z")

            lines = await _collect_lines(line_iter, 3, timeout=2.0)

    payload = json.loads(lines[1].removeprefix("data: "))
    assert payload["price"] == 195.0
    assert payload["previous_price"] == 190.0
    assert payload["change_direction"] == "up"


async def test_no_event_is_sent_when_nothing_changes_between_checks(monkeypatch):
    monkeypatch.setattr(stream_module, "SSE_CHECK_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(stream_module, "HEARTBEAT_SECONDS", 10_000)  # effectively disabled
    cache = PriceCache(FakeProvider())
    cache._apply_if_newer("AAPL", 190.0, "2026-01-01T00:00:00Z")
    app = _build_app(cache)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream("GET", "/api/stream/prices") as response:
            line_iter = response.aiter_lines()
            await _collect_lines(line_iter, 3)  # initial snapshot

            # No cache mutation here — several check cycles elapse with no
            # change, so nothing further should arrive.
            with pytest.raises(asyncio.TimeoutError):
                await _collect_lines(line_iter, 1, timeout=0.5)


async def test_heartbeat_is_sent_when_idle(monkeypatch):
    monkeypatch.setattr(stream_module, "SSE_CHECK_INTERVAL_SECONDS", 0.02)
    monkeypatch.setattr(stream_module, "HEARTBEAT_SECONDS", 0.1)
    cache = PriceCache(FakeProvider())  # no tracked tickers -> no price events at all
    app = _build_app(cache)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream("GET", "/api/stream/prices") as response:
            lines = await _collect_lines(response.aiter_lines(), 2, timeout=2.0)

    assert lines[0] == ": ping"
    assert lines[1] == ""
