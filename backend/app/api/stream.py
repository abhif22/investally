"""GET /api/stream/prices — SSE stream of live price updates.

See planning/MARKET_DATA_DESIGN.md Sec 11. The SSE "check" cadence is
decoupled from any provider's actual poll cadence: CachedPrice.version
only advances when a provider actually returned a fresh quote, so
checking frequently is cheap and harmless — nothing is sent unless
something genuinely changed.
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

from ..market.cache import CachedPrice, PriceCache

router = APIRouter()

SSE_CHECK_INTERVAL_SECONDS = 0.25  # how often the loop LOOKS for changes —
# independent of any provider's actual poll_interval_seconds
HEARTBEAT_SECONDS = 15


@router.get("/api/stream/prices")
async def stream_prices(request: Request) -> StreamingResponse:
    cache: PriceCache = request.app.state.price_cache
    return StreamingResponse(_event_source(request, cache), media_type="text/event-stream")


async def _event_source(request: Request, cache: PriceCache) -> AsyncGenerator[str, None]:
    last_sent_version: dict[str, int] = {}

    # Initial snapshot: send every currently tracked-and-priced ticker
    # once, unconditionally — this is what lets the frontend render the
    # watchlist immediately rather than waiting for the next change.
    for cp in cache.snapshot():
        last_sent_version[cp.ticker] = cp.version
        yield _format_event(cp)
    last_activity = time.monotonic()

    while True:
        if await request.is_disconnected():
            break
        await asyncio.sleep(SSE_CHECK_INTERVAL_SECONDS)

        sent_any = False
        for cp in cache.snapshot():
            if cp.version > last_sent_version.get(cp.ticker, -1):
                last_sent_version[cp.ticker] = cp.version
                yield _format_event(cp)
                sent_any = True

        now = time.monotonic()
        if sent_any:
            last_activity = now
        elif now - last_activity >= HEARTBEAT_SECONDS:
            yield ": ping\n\n"
            last_activity = now


def _format_event(cp: CachedPrice) -> str:
    if cp.price > cp.previous_price:
        change_direction = "up"
    elif cp.price < cp.previous_price:
        change_direction = "down"
    else:
        change_direction = "flat"
    daily_change_pct = (
        (cp.price - cp.opening_price) / cp.opening_price if cp.opening_price else 0.0
    )
    payload = {
        "ticker": cp.ticker,
        "price": cp.price,
        "previous_price": cp.previous_price,
        "change_direction": change_direction,
        "daily_change_pct": round(daily_change_pct, 6),
        "timestamp": cp.timestamp,
    }
    return f"event: price\ndata: {json.dumps(payload)}\n\n"
