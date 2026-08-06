# Massive API Reference (for InvestAlly)

*Research doc — authoritative for how InvestAlly's backend talks to Massive. Consumed by [`MARKET_INTERFACE.md`](./MARKET_INTERFACE.md), which defines the concrete `MassiveProvider` built on top of what's documented here. See [`PLAN.md`](./PLAN.md) §6 for how this fits the overall market-data architecture.*

## 1. What Massive Is

[Massive](https://massive.com) is the company formerly known as **Polygon.io**, rebranded October 2025. It provides REST and WebSocket APIs for real-time and historical market data (stocks, options, indices, forex, crypto). For InvestAlly we only need US stock **REST** endpoints — no WebSocket, no options/forex/crypto.

- New base URL: `https://api.massive.com`
- Legacy base URL `https://api.polygon.io` still works during a transition period, but new code should target `api.massive.com`
- Official Python client: [`massive-com/client-python`](https://github.com/massive-com/client-python) (PyPI package `massive-api-client`, importable as `massive`) — actively maintained, wraps both REST and WebSocket
- Docs referenced throughout this file come from that client's source and docs (`docs/source/*.md`, `massive/rest/*.py`, `massive/rest/models/*.py`)

InvestAlly only needs two things from this API, both covered below:

1. **Real-time-ish prices for a set of tickers at once** — a batch snapshot call (§3)
2. **End-of-day / previous-close prices** — for seeding or reference (§4)

## 2. Authentication & Setup

Get an API key from the Massive dashboard. It can be supplied to the official client two ways:

```python
from massive import RESTClient

client = RESTClient()               # reads MASSIVE_API_KEY from the environment
client = RESTClient(api_key="...")  # or pass explicitly
```

`RESTClient()` with no key set (and no `MASSIVE_API_KEY` in the environment) doesn't fail at construction time — it fails on the first request, raising `massive.exceptions.AuthError` ("Empty or invalid API key"). This matters for InvestAlly's startup behavior (§5, §6 below): the environment variable check must happen *before* constructing/using the client, not rely on the client to fail fast.

Under the hood, the API key is sent as an `Authorization: Bearer <key>` header — not a query parameter.

### Raw HTTP (no client library)

Since InvestAlly's `MarketDataProvider` interface (see `MARKET_INTERFACE.md`) only needs 1-2 endpoints, it's a reasonable choice to skip the `massive-api-client` dependency entirely and call the REST API directly with `httpx`. Both styles are shown below so the implementer can pick.

```python
import httpx

MASSIVE_BASE_URL = "https://api.massive.com"

async def _get(path: str, api_key: str, params: dict | None = None) -> dict:
    async with httpx.AsyncClient(base_url=MASSIVE_BASE_URL, timeout=10.0) as client:
        resp = await client.get(
            path,
            params=params,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        return resp.json()
```

## 3. Realtime Prices for Multiple Tickers — Batch Snapshot

This is the endpoint InvestAlly's poller uses (PLAN.md §6: "REST API polling via a batch snapshot endpoint"). It returns the latest trade, latest quote, most recent minute bar, and previous day's OHLC for **many tickers in one HTTP call** — critical for staying under rate limits when tracking 10+ tickers.

### Endpoint

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT
```

- `market_type` = `stocks` in the URL for our use case
- `tickers` — comma-separated list, no hard documented cap for a watchlist-sized set (tens of tickers); omit the param entirely to get the whole market (tens of thousands of rows) — always pass an explicit list for InvestAlly, never omit it
- `include_otc` (bool, default `false`) — leave at default; InvestAlly doesn't track OTC tickers

### Official client: `get_snapshot_all`

```python
from massive import RESTClient

client = RESTClient()  # MASSIVE_API_KEY from env

snapshots = client.get_snapshot_all(
    market_type="stocks",
    tickers=["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"],
)

for s in snapshots:
    print(s.ticker, s.day.close, s.prev_day.close, s.todays_change_percent, s.updated)
```

`tickers` accepts either a `List[str]` or a pre-joined comma-separated `str` — the client joins a list for you. Returns `List[TickerSnapshot]` (or the raw `HTTPResponse` if `raw=True`).

Related methods on the same client, not needed for InvestAlly's polling loop but useful to know about:

- `get_snapshot_ticker(market_type, ticker)` — single-ticker snapshot, `/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}`. Avoid this in a loop — one HTTP call per ticker defeats the point of batching and burns through the rate limit fast.
- `get_snapshot_direction(market_type, direction)` — top 20 gainers/losers, not applicable here.
- `list_universal_snapshots(type=..., ticker_any_of=[...])` — newer cross-asset-class endpoint (`/v3/snapshot`), supports up to 250 tickers via `ticker_any_of`. `get_snapshot_all` is simpler and sufficient for our stocks-only, watchlist-sized use case; `list_universal_snapshots` is worth revisiting only if InvestAlly ever needs to batch more than what a single `tickers` snapshot call comfortably supports.

### Raw HTTP equivalent

```python
data = await _get(
    "/v2/snapshot/locale/us/markets/stocks/tickers",
    api_key=api_key,
    params={"tickers": "AAPL,GOOGL,MSFT,AMZN,TSLA"},
)
for row in data["tickers"]:
    print(row["ticker"], row["day"]["c"], row["prevDay"]["c"], row["todaysChangePerc"])
```

### Response Shape (`TickerSnapshot`)

Raw JSON is compact single-letter keys; the client's `from_dict` decodes them. Field reference (raw key → client attribute):

| Raw key | Client attribute | Meaning |
|---|---|---|
| `ticker` | `ticker` | Symbol, e.g. `"AAPL"` |
| `day` | `day` (`Agg`) | Today's running OHLCV bar (updates live during market hours) |
| `prevDay` | `prev_day` (`Agg`) | Yesterday's full OHLCV bar |
| `min` | `min` (`MinuteSnapshot`) | Most recent minute bar |
| `lastTrade` | `last_trade` (`LastTrade`) | Most recent individual trade |
| `lastQuote` | `last_quote` (`LastQuote`) | Most recent bid/ask quote |
| `todaysChange` | `todays_change` | `day.close - prevDay.close`, absolute |
| `todaysChangePerc` | `todays_change_percent` | Same, as a percentage |
| `updated` | `updated` | Nanosecond Unix timestamp of last update |
| `fmv` | `fair_market_value` | Fair market value (business/enterprise plans only; `null` otherwise) |

`Agg` (used for both `day` and `prevDay`), raw keys:

| Raw key | Field | 
|---|---|
| `o` | open |
| `h` | high |
| `l` | low |
| `c` | close |
| `v` | volume |
| `vw` | vwap |
| `t` | timestamp (ms) |
| `n` | transactions |
| `otc` | otc flag |

`LastTrade`, raw keys: `T` ticker, `p` price, `s` size, `x` exchange, `t` sip_timestamp, `c` conditions, `i` id, `e` correction, `z` tape.

`LastQuote`, raw keys: `T` ticker, `P` ask_price, `S` ask_size, `X` ask_exchange, `p` bid_price, `s` bid_size, `x` bid_exchange, `t` sip_timestamp.

### What InvestAlly Actually Needs From This Response

Per PLAN.md §6, the price cache stores `latest_price`, `previous_price`, `opening_price`, `timestamp` per ticker. Mapping:

- `latest_price` ← `day.close` if the market is open and today's bar has trades, else fall back to `prev_day.close` (pre-market / after-hours / weekends — `day` may be all-zero or stale on a batch call made outside market hours). A more robust choice used by many Polygon/Massive integrations is `last_trade.price` when present, since it reflects the literal last executed trade regardless of session; prefer that over `day.close` if it's non-null.
- `previous_price` ← whatever the cache held before this poll (computed by the provider layer, not sourced from this response — see `MARKET_INTERFACE.md`)
- reference/"opening" price for daily-change % ← captured once per ticker per PLAN.md §6, not re-derived from `prevDay` every poll
- `timestamp` ← `updated` (nanoseconds — divide by 1e9 for a Unix timestamp, or by 1e6 for ms) converted to ISO 8601 UTC

### Rate Limits

- **Free tier: 5 API calls/minute.** This is the commonly documented Polygon.io/Massive free-tier ceiling; it is not reproduced in the client library's docs (which only say "the free tier ... may result in rate limit errors" without a number), so treat the exact figure as worth reconfirming against `massive.com/pricing` at implementation time, but plan around 5/min as the conservative default.
- Because the snapshot endpoint is a **batch** call (all tracked tickers in one request), the 5/min limit is on *polls*, not tickers — this is exactly why PLAN.md §6 mandates a single batched call per poll interval rather than one call per ticker.
- PLAN.md §6's polling cadence follows directly from this: free tier → poll every 15s (4/min, headroom under 5/min); paid tiers → poll every 2-15s depending on plan.
- A `429` response should be treated as a transient failure: keep serving the last cached price for every ticker (per PLAN.md §6) and retry on the next scheduled interval — no special backoff needed at this call volume (fixed-interval retry, not exponential).

### Error Handling

The official client only defines two exceptions (`massive/exceptions.py`):

- `AuthError(Exception)` — empty/invalid API key
- `BadResponse(Exception)` — any non-200 response (this covers 429, 404, 500, etc. — the client does not subclass per status code)

The client's internal `urllib3` transport auto-retries `413, 429, 499, 500, 502, 503, 504` with exponential backoff (`0.1` factor: `0.0s, 0.2s, 0.4s, 0.8s, 1.6s, ...`) *before* ever raising `BadResponse` — so a `BadResponse` you catch has already exhausted the client's own retry budget. If InvestAlly builds its own raw-`httpx` provider instead of using the official client (§2), it should catch `httpx.HTTPStatusError` / `httpx.RequestError` around each poll and apply the "keep serving stale cache, retry next interval" behavior from PLAN.md §6 rather than reimplementing urllib3's retry ladder — the poll loop's own fixed 2-15s cadence already acts as the retry interval.

## 4. End-of-Day Prices

Two options depending on whether you want one ticker or the whole market for a given date.

### 4a. Previous Close — Single Ticker

```
GET /v2/aggs/ticker/{ticker}/prev
```

```python
prev = client.get_previous_close_agg("AAPL", adjusted=True)
print(prev.ticker, prev.close, prev.open, prev.high, prev.low, prev.volume, prev.timestamp)
```

Raw HTTP: `GET https://api.massive.com/v2/aggs/ticker/AAPL/prev`

Response fields (`PreviousCloseAgg`), raw key → field: `T` ticker, `o` open, `h` high, `l` low, `c` close, `v` volume, `vw` vwap, `t` timestamp (ms).

### 4b. Grouped Daily Bars — Whole Market, One Date

Fetches EOD OHLC for **every** US stock ticker on a given date in a single call — useful for seeding realistic reference/seed prices (e.g. the simulator's seed price table, see `MARKET_SIMULATOR.md`) without one request per ticker.

```
GET /v2/aggs/grouped/locale/us/market/stocks/{date}
```

```python
bars = client.get_grouped_daily_aggs(date="2026-08-05", adjusted=True)
by_ticker = {b.ticker: b for b in bars}
print(by_ticker["AAPL"].close)
```

Response is a list of `GroupedDailyAgg`, raw key → field: `T` ticker, `o` open, `h` high, `l` low, `c` close, `v` volume, `vw` vwap, `t` timestamp, `n` transactions, `otc` flag.

### 4c. Open/Close for a Specific Ticker + Date

```
GET /v1/open-close/{ticker}/{date}
```

```python
oc = client.get_daily_open_close_agg("AAPL", "2026-08-05", adjusted=True)
print(oc.open, oc.close, oc.pre_market, oc.after_hours)
```

Less useful for InvestAlly than 4a/4b (it's single-ticker *and* requires a date), included for completeness.

### Which One Does InvestAlly Use?

Per `MARKET_INTERFACE.md`, EOD prices aren't part of the live poll loop at all — the live snapshot call (§3) already returns `prevDay` inline with every poll, which covers "yesterday's close" without a second request. 4a/4b are only relevant for one-off/offline use (e.g. a script to refresh the simulator's seed price table using real recent closes) — not called from the running backend's request path.

## 5. Dependency Choice: Official Client vs Raw HTTP

| | `massive-api-client` (official) | Raw `httpx` |
|---|---|---|
| Setup | `pip install massive-api-client`, `from massive import RESTClient` | No new dependency — InvestAlly's backend almost certainly already has `httpx` for other purposes |
| Typed models | Yes (`TickerSnapshot`, `Agg`, etc.) | No — parse dicts directly |
| Retry/backoff | Built in (urllib3, see §3) | Must implement (though PLAN.md §6's "keep last cache, retry next poll" already covers this at the application level, making client-level retry redundant) |
| Surface area | Large (options, crypto, WebSocket, pagination) — InvestAlly uses ~2 of dozens of methods | Minimal — exactly the 1-2 calls needed |
| Async | Sync only (the official client is a synchronous `urllib3`-based client; would need `run_in_executor` inside FastAPI's async poll loop) | Native `httpx.AsyncClient`, fits FastAPI's async background task directly |

**Recommendation** (carried into `MARKET_INTERFACE.md`): use raw `httpx` against the two documented endpoints (§3, §4a) rather than adding the `massive-api-client` package. The integration surface is tiny (one batch snapshot call on a poll loop), the official client is synchronous (awkward inside an async FastAPI background task), and skipping it avoids a dependency whose scope (options, crypto, WebSocket) is almost entirely unused here.

## 6. Practical Notes for the Poller Implementation

- Always call the batch endpoint (§3) with the **full tracked ticker set** (watchlist ∪ open positions, per PLAN.md §6) in one request — never loop per-ticker.
- A newly tracked ticker (watchlist add or new position) should get an immediate out-of-cycle fetch rather than waiting for the next scheduled interval (PLAN.md §6) — this can reuse the same batch endpoint with a one-ticker list, accepting the minor rate-limit cost since it's a rare, user-triggered event rather than the steady-state poll.
- Treat `tickers` param order as irrelevant — match rows back to tickers by the `ticker`/`T` field in each response row, not by array position.
- A ticker with no `day` data (e.g. requested outside market hours before any trades) should not be treated as an error — fall back through `last_trade.price` → `prev_day.close` as described in §3.

## Sources

- https://github.com/massive-com/client-python (README.md, docs/source/Getting-Started.md, docs/source/Snapshot.md)
- https://github.com/massive-com/client-python/blob/master/massive/rest/snapshot.py
- https://github.com/massive-com/client-python/blob/master/massive/rest/aggs.py
- https://github.com/massive-com/client-python/blob/master/massive/rest/models/snapshot.py
- https://github.com/massive-com/client-python/blob/master/massive/rest/models/aggs.py
- https://github.com/massive-com/client-python/blob/master/massive/rest/models/quotes.py
- https://github.com/massive-com/client-python/blob/master/massive/rest/models/trades.py
- https://github.com/massive-com/client-python/blob/master/massive/exceptions.py
- https://github.com/massive-com/client-python/blob/master/massive/rest/base.py
