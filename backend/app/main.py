"""Minimal FastAPI app wiring for the market data backend.

This wires PriceCache to the FastAPI lifespan and exposes the market-data
HTTP surface (SSE price stream + health check). Portfolio/watchlist/chat
persistence lives outside this module's scope — see
planning/MARKET_DATA_DESIGN.md Sec 9 for how a full app seeds
`track_many()` from the database's watchlist ∪ open-position tickers;
here that seed list comes from the DEFAULT_WATCHLIST_TICKERS env var
(falling back to the ten default seed tickers) so this module stays
self-contained and runnable on its own.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.stream import router as stream_router
from .market.cache import PriceCache
from .market.factory import create_market_data_provider
from .market.simulator.seed_data import KNOWN_SEED_PRICES


def _load_initial_tracked_tickers() -> list[str]:
    raw = os.environ.get("DEFAULT_WATCHLIST_TICKERS", "")
    tickers = [t.strip().upper() for t in raw.split(",") if t.strip()]
    return tickers or sorted(KNOWN_SEED_PRICES)


@asynccontextmanager
async def lifespan(app: FastAPI):
    provider = create_market_data_provider()
    cache = PriceCache(provider)
    app.state.price_cache = cache

    # Seed the initial tracked set as ONE batch call via track_many(), not
    # N calls via a track() loop.
    await cache.track_many(_load_initial_tracked_tickers())

    cache.start()
    yield
    await cache.stop()


app = FastAPI(title="InvestAlly Market Data Backend", lifespan=lifespan)
app.include_router(stream_router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
