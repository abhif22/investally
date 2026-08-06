# InvestAlly — AI Trading Workstation

## Project Specification

## 1. Vision

InvestAlly (Investment Ally) is a visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot.

This is the capstone project for an agentic AI coding course. It is built entirely by Coding Agents demonstrating how orchestrated AI agents can produce a production-quality full-stack application. Agents interact through files in `planning/`.

## 2. User Experience

### First Launch

The user runs a single Docker command (or a provided start script), which builds/starts the container and prints the URL once it's ready. The user then opens `http://localhost:8000` in their browser themselves — the script does not auto-open it. No login, no signup. They immediately see:

- A watchlist of 10 default tickers with live-updating prices in a grid
- $10,000 in virtual cash
- A dark, data-rich trading terminal aesthetic
- An AI chat panel ready to assist

### What the User Can Do

- **Watch prices stream** — prices flash green (uptick) or red (downtick) with subtle CSS animations that fade
- **View sparkline mini-charts** — price action beside each ticker in the watchlist, accumulated on the frontend from the SSE stream since page load (sparklines fill in progressively)
- **Click a ticker** to see a larger detailed chart in the main chart area
- **Buy and sell shares** — market orders only, instant fill at current price, no fees, no confirmation dialog
- **Monitor their portfolio** — a heatmap (treemap) showing positions sized by weight and colored by P&L, plus a P&L chart tracking total portfolio value over time
- **View a positions table** — ticker, quantity, average cost, current price, unrealized P&L, % change
- **Chat with the AI assistant** — ask about their portfolio, get analysis, and have the AI execute trades and manage the watchlist through natural language
- **Manage the watchlist** — add/remove tickers manually or via the AI chat

### Visual Design

- **Dark theme**: backgrounds around `#0d1117` or `#1a1a2e`, muted gray borders, no pure black
- **Price flash animations**: brief green/red background highlight on price change, fading over ~500ms via CSS transitions
- **Connection status indicator**: a small colored dot (green = connected, yellow = reconnecting, red = disconnected) visible in the header
- **Professional, data-dense layout**: inspired by Bloomberg/trading terminals — every pixel earns its place
- **Responsive but desktop-first**: optimized for wide screens, functional on tablet

### Color Scheme
- Accent Yellow: `#ecad0a`
- Blue Primary: `#209dd7`
- Purple Secondary: `#753991` (submit buttons)

## 3. Architecture Overview

### Single Container, Single Port

```
┌─────────────────────────────────────────────────┐
│  Docker Container (port 8000)                   │
│                                                 │
│  FastAPI (Python/uv)                            │
│  ├── /api/*          REST endpoints             │
│  ├── /api/stream/*   SSE streaming              │
│  └── /*              Static file serving         │
│                      (Next.js export)            │
│                                                 │
│  SQLite database (volume-mounted)               │
│  Background task: market data polling/sim        │
└─────────────────────────────────────────────────┘
```

- **Frontend**: Next.js with TypeScript, built as a static export (`output: 'export'`), served by FastAPI as static files
- **Backend**: FastAPI (Python), managed as a `uv` project
- **Database**: SQLite, single file at `/app/db/InvestAlly.db` inside the container, persisted via a named Docker volume (`InvestAlly-data`)
- **Real-time data**: Server-Sent Events (SSE) — simpler than WebSockets, one-way server→client push, works everywhere
- **AI integration**: LiteLLM → OpenRouter (Cerebras for fast inference), with structured outputs for trade execution
- **Market data**: Environment-variable driven — simulator by default, real data via Massive API if key provided

### Why These Choices

| Decision | Rationale |
|---|---|
| SSE over WebSockets | One-way push is all we need; simpler, no bidirectional complexity, universal browser support |
| Static Next.js export | Single origin, no CORS issues, one port, one container, simple deployment |
| SQLite over Postgres | No auth = no multi-user = no need for a database server; self-contained, zero config |
| Single Docker container | Students run one command; no docker-compose for production, no service orchestration |
| uv for Python | Fast, modern Python project management; reproducible lockfile; what students should learn |
| Market orders only | Eliminates order book, limit order logic, partial fills — dramatically simpler portfolio math |

---

## 4. Directory Structure

```
InvestAlly/
├── frontend/                 # Next.js TypeScript project (static export)
├── backend/                  # FastAPI uv project (Python)
│   └── db/                   # Schema definitions, seed data, migration logic
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── scripts/
│   ├── start_mac.sh          # Launch Docker container (macOS/Linux)
│   ├── stop_mac.sh           # Stop Docker container (macOS/Linux)
│   ├── start_windows.ps1     # Launch Docker container (Windows PowerShell)
│   └── stop_windows.ps1      # Stop Docker container (Windows PowerShell)
├── test/                     # Playwright E2E tests + docker-compose.test.yml
├── Dockerfile                # Multi-stage build (Node → Python)
├── docker-compose.yml        # Optional convenience wrapper
├── .env                      # Environment variables (gitignored, .env.example committed)
└── .gitignore
```

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- **`backend/db/`** contains schema SQL definitions and seed logic. The backend lazily initializes the database on first request — creating tables and seeding default data if the SQLite file doesn't exist or is empty.
- SQLite persistence uses a named Docker volume (`InvestAlly-data`, mounted at `/app/db` in the container — see §11). There is no host-visible `db/` directory in the repo; the volume's storage is managed entirely by Docker.
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests and supporting infrastructure (e.g., `docker-compose.test.yml`). Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
- **`scripts/`** contains start/stop scripts that wrap Docker commands.

---

## 5. Environment Variables

```bash
# Required unless LLM_MOCK=true: OpenRouter API key for LLM chat functionality (see §11 Startup Configuration)
OPENROUTER_API_KEY=your-openrouter-api-key-here

# Optional: Massive (Polygon.io) API key for real market data
# If not set, the built-in market simulator is used (recommended for most users)
MASSIVE_API_KEY=

# Optional: Set to "true" for deterministic mock LLM responses (testing)
LLM_MOCK=false

# Optional: Max number of trades the LLM can auto-execute in a single chat response
MAX_TRADES_PER_MESSAGE=20
```

### Behavior

- If `MASSIVE_API_KEY` is set and non-empty → backend uses Massive REST API for market data
- If `MASSIVE_API_KEY` is absent or empty → backend uses the built-in market simulator
- If `LLM_MOCK=true` → backend returns deterministic mock LLM responses (for E2E tests)
- `MAX_TRADES_PER_MESSAGE` caps how many trades a single LLM chat response can auto-execute (default 20); trades beyond the cap appear in the response's `results` array as rejected (see §9)
- The backend reads `.env` from the project root (mounted into the container or read via docker `--env-file`)

---

## 6. Market Data

*Full design detail: [`MASSIVE_API.md`](./MASSIVE_API.md) (Massive/Polygon.io API reference), [`MARKET_INTERFACE.md`](./MARKET_INTERFACE.md) (the `MarketDataProvider` interface both implementations conform to), [`MARKET_SIMULATOR.md`](./MARKET_SIMULATOR.md) (the GBM simulator's internals). This section summarizes; those documents are authoritative on implementation detail.*

### Two Implementations, One Interface

Both the simulator and the Massive client implement the same abstract interface. The backend selects which to use based on the environment variable. All downstream code (SSE streaming, price cache, frontend) is agnostic to the source.

### Simulator (Default)

- Generates prices using geometric Brownian motion (GBM) with configurable drift and volatility per ticker
- Updates at ~500ms intervals
- Correlated moves across tickers (e.g., tech stocks move together)
- Occasional random "events" — sudden 2-5% moves on a ticker for drama
- Starts from realistic seed prices (e.g., AAPL ~$190, GOOGL ~$175, etc.)
- Runs as an in-process background task — no external dependencies

### Massive API (Optional)

- REST API polling via a batch snapshot endpoint (not WebSocket) — simpler, works on all tiers
- Polls for the union of all tracked tickers (see Shared Price Cache below) on a configurable interval
- Free tier (5 calls/min): poll every 15 seconds
- Paid tiers: poll every 2-15 seconds depending on tier
- On a failed/errored poll, the cache keeps serving the last known price for each ticker rather than blocking or clearing it; a poll failure is retried on the next interval with simple fixed-interval backoff (no exponential backoff needed at this call volume)
- Parses REST response into the same format as the simulator

### Shared Price Cache

- A single background task (simulator or Massive poller) writes to an in-memory price cache
- The cache holds the latest price, previous price, opening price (see "Daily Change %" below), and timestamp (ISO 8601 UTC) for each ticker
- SSE streams read from this cache and push updates to connected clients
- This architecture supports future multi-user scenarios without changes to the data layer
- **Tracked ticker set = watchlist ∪ tickers with an open position.** Adding a watchlist ticker (or opening a position in a ticker not yet tracked) immediately expands what's tracked; removing a watchlist ticker only contracts tracking if no open position remains in it. This guarantees a held position always has a live price for P&L, even after its ticker is removed from the watchlist — see §8 for how `DELETE /api/watchlist/{ticker}` behaves in that case. This matters most for the Massive API path, where poll frequency is rate-limited by ticker count (5 calls/min on the free tier)
- A ticker newly added to the tracked set (via watchlist add or a trade) gets its first price immediately: from the simulator's seed price table if known, or a synthesized seed (e.g., a fixed reference price) if not; for the Massive API it's the ticker's next poll result, fetched immediately rather than waiting for the next scheduled interval

### Daily Change %

- Each ticker's "opening price" is captured the first time that ticker is tracked in the running container's lifetime (not tied to real market-session boundaries, since the simulator runs continuously) and held fixed until the container restarts
- "Daily change %" = `(latest_price - opening_price) / opening_price`; this is distinct from tick-to-tick "change direction" (up/down vs. the immediately previous price), which both the watchlist flash animation and SSE events use

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API
- Server pushes a price update event for every tracked ticker (see Shared Price Cache) at a regular cadence (~500ms)
- Event name: `price`. `data` payload (JSON): `{ticker, price, previous_price, change_direction, daily_change_pct, timestamp}`
- On connect, the server immediately sends one event per tracked ticker with its current cached state (initial snapshot), before falling into the regular cadence — this is what lets the frontend render the watchlist immediately instead of waiting for the next tick
- A heartbeat comment (`: ping`) is sent every 15s on an otherwise-idle stream to keep intermediary proxies/load balancers from timing out the connection
- No event IDs or replay on reconnect — SSE is a live feed of current state, not an event log; a reconnecting client simply receives a fresh initial snapshot
- If a client can't keep up (slow consumer), the server does not queue backlog — each tick, it sends the latest state only, so a slow client silently misses intermediate ticks rather than falling further behind
- Client handles reconnection automatically (EventSource has built-in retry); see §10 for how the frontend detects and surfaces a stalled connection

---

## 7. Database

### SQLite with Lazy Initialization

The backend checks for the SQLite database on startup (or first request). If the file doesn't exist or tables are missing, it creates the schema and seeds default data. This means:

- No separate migration step
- No manual database setup
- Fresh Docker volumes start with a clean, seeded database automatically

### Schema

All tables include a `user_id` column defaulting to `"default"`. This is hardcoded for now (single-user) but enables future multi-user support without schema migration.

**Money & precision**: `REAL` is kept (not integer cents) to avoid complicating the fractional-share math, but every write path rounds consistently — monetary values (`cash_balance`, `avg_cost`, `price`, `total_value`) to 2 decimal places, `quantity` to 6 decimal places — so float drift never becomes visible in the UI. All backend P&L/total-value math is computed from these already-rounded values, not re-derived from unrounded intermediates.

**Schema versioning**: a single-row `schema_meta` table (`version INTEGER`) is created alongside the rest of the schema. This project does not build a migration system — the course scope is a single evolving schema, and if it changes during development the expectation is a fresh Docker volume. `schema_meta` exists so a future migration system has something to check against; it is not read by anything yet. Lazy seeding is idempotent (only inserts default rows when the relevant table is empty) and never overwrites an existing `users_profile`, `positions`, or `trades` row.

**users_profile** — User state (cash balance)
- `id` TEXT PRIMARY KEY (default: `"default"`)
- `cash_balance` REAL (default: `10000.0`)
- `created_at` TEXT (ISO timestamp)

**watchlist** — Tickers the user is watching
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `added_at` TEXT (ISO timestamp)
- UNIQUE constraint on `(user_id, ticker)`

**positions** — Current holdings (one row per ticker per user)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `quantity` REAL (fractional shares supported)
- `avg_cost` REAL
- `updated_at` TEXT (ISO timestamp)
- UNIQUE constraint on `(user_id, ticker)`

A position row is deleted when a sell brings its `quantity` to exactly 0 (rather than kept at zero). A subsequent buy of the same ticker creates a fresh row with a new `avg_cost` — it does not resurrect the old row.

**trades** — Trade history (append-only log)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `side` TEXT (`"buy"` or `"sell"`)
- `quantity` REAL (fractional shares supported)
- `price` REAL
- `executed_at` TEXT (ISO timestamp)

**portfolio_snapshots** — Portfolio value over time (for P&L chart). Recorded every 30 seconds by a background task, and immediately after each trade execution.
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `total_value` REAL
- `recorded_at` TEXT (ISO timestamp)

**Seeding**: the first snapshot is recorded at startup, as soon as the price cache has an initial value for every position/watchlist ticker (immediately after the initial SSE snapshot described in §6) — not after the first 30-second tick. This ensures the P&L chart always has at least one point instead of starting empty for up to 30 seconds.

**Retention**: full 30-second resolution is kept for the most recent 24 hours. A periodic background task (hourly, aligned to the top of the UTC hour) downsamples snapshots older than 24 hours: for each past UTC-hour bucket, it keeps only the single snapshot closest to the end of that hour and deletes the rest of the raw rows in the bucket. This keeps `/api/portfolio/history` and the P&L chart fast regardless of how long a demo session runs, while preserving the long-term trend as one clean point per hour rather than an average that could mask intra-hour swings.

**chat_messages** — Conversation history with LLM
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON object `{"trades": [...], "watchlist_changes": [...]}` matching the §9 structured-output shape, containing only what was actually executed; `null` when no actions were taken — always `null` for user messages, and also `null` for assistant messages where the LLM proposed no trades/watchlist changes)
- `created_at` TEXT (ISO timestamp)

### Default Seed Data

- One user profile: `id="default"`, `cash_balance=10000.0`
- Ten watchlist entries: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX

---

## 8. API Endpoints

### Conventions

- **Ticker format**: uppercase letters only, 1-5 characters. The backend normalizes (`.upper().strip()`) on input; a ticker outside this shape is rejected with a validation error rather than passed through to the market-data layer
- **Quantity**: must be a positive number with at most 6 decimal places (see §7's precision rule); zero or negative is rejected
- **Timestamps**: ISO 8601, UTC, e.g. `2026-08-05T14:30:00Z`
- **Errors**: non-2xx responses use a common envelope: `{"error": {"code": "insufficient_cash", "message": "..."}}`. `code` is a stable machine-readable slug (e.g. `insufficient_cash`, `insufficient_shares`, `invalid_ticker`, `invalid_quantity`, `ticker_not_found`); `message` is human-readable. `400` for validation/business-rule failures, `404` for missing resources (e.g. removing a ticker not on the watchlist), `500` for unexpected errors

### Market Data
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/stream/prices` | SSE stream of live price updates |

### Portfolio
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/portfolio` | Current positions, cash balance, total value, unrealized P&L |
| POST | `/api/portfolio/trade` | Execute a trade: `{ticker, quantity, side}` |
| GET | `/api/portfolio/history` | Portfolio value snapshots over time, filterable via `?start=`/`?end=` time-range query params (for P&L chart) |

### Watchlist
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/watchlist` | Current watchlist tickers with latest prices |
| POST | `/api/watchlist` | Add a ticker: `{ticker}`. `action` values for the LLM's `watchlist_changes` (§9) are `"add"` \| `"remove"`, mapping to this and the endpoint below |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker from the watchlist. If a position is still open in that ticker, the removal still succeeds (the ticker just stops appearing in the watchlist UI) — per §6, it remains tracked and priced as long as the position is open |

### Chat
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/chat` | Send a message, receive `{message, results}` — see §9 for the `results` array shape |

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (for Docker/deployment) |

---

## 9. LLM Integration

When writing code to make calls to LLMs, use cerebras-inference skill to use LiteLLM via OpenRouter to the `openrouter/openai/gpt-oss-120b` model with Cerebras as the inference provider. Structured Outputs should be used to interpret the results.

There is an OPENROUTER_API_KEY in the .env file in the project root.

### How It Works

When the user sends a chat message, the backend:

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
2. Loads recent conversation history from the `chat_messages` table, capped to roughly 100K tokens of context (oldest messages dropped first once the cap is reached)
3. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
4. Calls the LLM via LiteLLM → OpenRouter, requesting structured output, using the cerebras-inference skill
5. Parses the complete structured JSON response, rejecting it (and retrying once) if it doesn't validate against the schema below
6. Executes each entry in `trades` and `watchlist_changes`, in array order, independently — one entry failing validation (e.g. insufficient cash) does not block the rest. Each individual trade executes as a single atomic DB transaction (cash, `positions`, `trades`, and the immediate `portfolio_snapshots` row all update together, or none do). If the same ticker appears more than once (e.g. two buys, or an add then a remove), each is still executed independently in order — the plan does not net or dedupe them
7. Builds a `results` array — one entry per action attempted, each `{type: "trade"|"watchlist_change", ...action fields, status: "executed"|"rejected", reason}` — recording what actually happened
8. Stores the message and `results` (not the LLM's raw unvalidated intent) in `chat_messages.actions`
9. Returns `{message, results}` to the frontend (no token-by-token streaming — Cerebras inference is fast enough that a loading indicator is sufficient)

Because the LLM's `message` text is generated *before* the backend validates and executes the actions (step 6 happens after step 5), the model cannot know the real outcome while writing its prose — it can only state intent ("I'll buy 10 shares of AAPL"). The `results` array from step 7 is the authoritative record of what happened and is what the frontend renders as the inline trade/watchlist confirmations described in §10; it is not re-narrated by a second LLM call. The system prompt instructs the model to describe actions it intends to take rather than asserting they've already succeeded, so its phrasing doesn't contradict the `results`-driven UI when something fails.

### Structured Output Schema

The LLM is instructed to respond with JSON matching this schema:

```json
{
  "message": "Your conversational response to the user",
  "trades": [
    {"ticker": "AAPL", "side": "buy", "quantity": 10}
  ],
  "watchlist_changes": [
    {"ticker": "PYPL", "action": "add"}
  ]
}
```

- `message` (required): The conversational text shown to the user
- `trades` (optional): Array of trades to auto-execute, each `{ticker, side, quantity}` (`quantity` may be fractional — see §7). Each trade goes through the same validation and normalization as manual trades (§8 conventions: ticker format, positive quantity; sufficient cash for buys, sufficient shares for sells)
- `watchlist_changes` (optional): Array of watchlist modifications, each `{ticker, action}` with `action` being `"add"` or `"remove"` (§8)

The backend treats every field of the structured output as untrusted input: it is schema-validated on parse (step 5 above), and each `trades`/`watchlist_changes` entry is independently re-validated against the same rules manual API calls go through — the model proposing an action is never sufficient on its own to bypass normal validation.

### Auto-Execution

Trades specified by the LLM execute automatically — no confirmation dialog. This is a deliberate design choice:
- It's a simulated environment with fake money, so the stakes are zero
- It creates an impressive, fluid demo experience
- It demonstrates agentic AI capabilities — the core theme of the course

If a trade fails validation (e.g., insufficient cash), it's recorded as `status: "rejected"` with a `reason` in the `results` array (see "How It Works" above) rather than blocking the rest of the batch.

The number of trades a single LLM response can auto-execute is capped by `MAX_TRADES_PER_MESSAGE` (default 20, configurable — see §5). Entries beyond the cap are not attempted and appear in `results` with `status: "rejected"`, `reason: "trade_limit_exceeded"`.

### System Prompt Guidance

The LLM should be prompted as "InvestAlly, an AI trading assistant" with instructions to:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Manage the watchlist proactively
- Be concise and data-driven in responses
- Always respond with valid structured JSON

### LLM Mock Mode

When `LLM_MOCK=true`, the backend returns deterministic mock responses instead of calling OpenRouter. This enables:
- Fast, free, reproducible E2E tests
- Development without an API key
- CI/CD pipelines

---

## 10. Frontend Design

### Layout

The frontend is a single-page application with a dense, terminal-inspired layout. The specific component architecture and layout system is up to the Frontend Engineer, but the UI should include these elements:

- **Watchlist panel** — grid/table of watched tickers with: ticker symbol, current price (flashing green/red on change), daily change %, and a sparkline mini-chart (accumulated from SSE since page load, capped per ticker to the most recent ~500 points — a ring buffer that drops the oldest point as new ones arrive, so a long-running session doesn't grow memory unbounded)
- **Main chart area** — larger chart for the currently selected ticker, with at minimum price over time. Clicking a ticker in the watchlist selects it here. Like the sparklines, price history is accumulated client-side from the SSE stream since page load, with the same point cap — there is no backend history endpoint, so the chart starts empty on a fresh page load and fills in live.
- **Portfolio heatmap** — treemap visualization where each rectangle is a position, sized by portfolio weight, colored by P&L (green = profit, red = loss)
- **P&L chart** — line chart showing total portfolio value over time, using data from `portfolio_snapshots`
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change
- **Trade bar** — simple input area: ticker field, quantity field, buy button, sell button. Market orders, instant fill.
- **AI chat panel** — docked/collapsible sidebar. Message input, scrolling conversation history, loading indicator while waiting for LLM response. Trade executions and watchlist changes shown inline as confirmations.
- **Header** — portfolio total value (updating live), connection status indicator, cash balance

### Technical Notes

- Use `EventSource` for SSE connection to `/api/stream/prices`. On reconnect (automatic, built into `EventSource`), the client keeps its existing accumulated sparkline/chart points rather than clearing them — the fresh initial snapshot (§6) just resumes appending
- Connection status (§2: green/yellow/red) needs client-side logic beyond raw `EventSource.readyState`, since the browser retries indefinitely on a dropped connection without ever reaching `CLOSED` — track time since the last received event with a watchdog timer; no event for >3s while `readyState` is `CONNECTING` → yellow, >15s → red
- Recharts (SVG-based) for all charts — watchlist sparklines, main chart, heatmap, and P&L chart — for consistency across the app
- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it
- All API calls go to the same origin (`/api/*`) — no CORS configuration needed
- Tailwind CSS for styling with a custom dark theme

---

## 11. Docker & Deployment

### Multi-Stage Dockerfile

```
Stage 1: Node 20 slim
  - Copy frontend/
  - npm install && npm run build (produces static export)

Stage 2: Python 3.12 slim
  - Install uv
  - Copy backend/
  - uv sync (install Python dependencies from lockfile)
  - Copy frontend build output into a static/ directory
  - Expose port 8000
  - CMD: uvicorn serving FastAPI app, single worker (see below)
```

Stage 1 uses `npm ci` (not `npm install`) so the build is reproducible against `frontend/package-lock.json`.

FastAPI serves the static frontend files and all API routes on port 8000. Since the app is a single-page UI (§10) with no client-side router, this is mostly moot, but as a safety net any unmatched non-API `GET` request falls back to serving `index.html` rather than a 404.

**Uvicorn runs a single worker.** The in-memory price cache (§6) and the background simulator/poller/snapshot tasks are process-local — running multiple workers would each start its own duplicate simulator and hold its own disconnected price cache. A single worker is sufficient for this single-user app; this is a deliberate scope boundary, not an oversight, and would need to change (e.g., moving the cache to Redis) before adding multi-worker support.

**SQLite access**: WAL mode and a busy-timeout (e.g. 5s) are enabled on the connection so concurrent reads (SSE handlers, `GET` routes) don't block on the writes made by the background snapshot/downsample tasks and trade/chat requests. Every multi-statement mutation (a trade's cash+position+trade-row+snapshot update, per §9) runs inside a single transaction. Background tasks (simulator, snapshot recorder, hourly downsampler) are started once via FastAPI's lifespan handler, not per-request, so they aren't duplicated across reloads.

### Docker Volume

The SQLite database persists via a named Docker volume:

```bash
docker run --name investally-app -v InvestAlly-data:/app/db -p 8000:8000 --env-file .env investally
```

`InvestAlly-data` is a named volume — Docker manages its storage location; it does not appear as a folder in the project directory. The backend writes `InvestAlly.db` to `/app/db` inside the container, which this volume backs.

The Dockerfile ensures the app's runtime user has write permissions on `/app/db` (e.g., via `chown` during the build) so SQLite writes succeed regardless of host OS or whether the container runs as a non-root user.

### Startup Configuration

`OPENROUTER_API_KEY` is only required when `LLM_MOCK` is not `true`. Startup behavior:

| `OPENROUTER_API_KEY` | `LLM_MOCK` | Boots? | `/api/chat` behavior |
|---|---|---|---|
| set | `false`/unset | Yes | Calls OpenRouter normally |
| unset | `true` | Yes | Returns mock responses, never calls OpenRouter |
| unset | `false`/unset | Yes | Boots and serves everything except chat; `/api/chat` returns a clear `500`-with-envelope error (§8) rather than crashing at startup |

A missing `.env` file is tolerated the same way as an `.env` with no keys set — `docker run --env-file .env` simply passes nothing, and the app falls into the third row above (simulator + no chat) rather than failing to start.

The container/image are always named `investally` (image) and `investally-app` (container). The start script checks for an existing container with that name before running: if one is already running, it prints its URL instead of erroring; if one exists but is stopped, it's removed and recreated. If port 8000 is already bound by something else, the script fails with a clear message rather than silently picking a different port.

### Start/Stop Scripts

**`scripts/start_mac.sh`** (macOS/Linux):
- Builds the Docker image if not already built (or if `--build` flag passed)
- Runs the container with the volume mount, port mapping, and `.env` file
- Prints the URL to access the app (does not auto-open a browser — the user opens it themselves once the app is up)

**`scripts/stop_mac.sh`** (macOS/Linux):
- Stops and removes the running container
- Does NOT remove the volume (data persists)

**`scripts/start_windows.ps1`** / **`scripts/stop_windows.ps1`**: PowerShell equivalents for Windows.

All scripts should be idempotent — safe to run multiple times.

### Optional Cloud Deployment

The container is designed to deploy to AWS App Runner, Render, or any container platform. A Terraform configuration for App Runner may be provided in a `deploy/` directory as a stretch goal, but is not part of the core build.

**Warning**: the app has no authentication (§2) and the chat assistant can execute trades against the single shared `"default"` account (§9's auto-execution). If this stretch goal is pursued, it must not be exposed on a public URL without adding an access-control layer in front of it (e.g., basic auth at a reverse proxy, or an IP allowlist) — otherwise any visitor with the URL can view and mutate the same portfolio.

---

## 12. Testing Strategy

### Unit Tests (within `frontend/` and `backend/`)

**Backend (pytest)**:
- Market data: simulator generates valid prices, GBM math is correct, Massive API response parsing works, both implementations conform to the abstract interface
- Portfolio: trade execution logic, P&L calculations, edge cases (selling more than owned, buying with insufficient cash, selling at a loss)
- LLM: structured output parsing handles all valid schemas, graceful handling of malformed responses, trade validation within chat flow
- API routes: correct status codes, response shapes, error handling

**Frontend (React Testing Library or similar)**:
- Component rendering with mock data
- Price flash animation triggers correctly on price changes
- Watchlist CRUD operations
- Portfolio display calculations
- Chat message rendering and loading state

### E2E Tests (in `test/`)

**Infrastructure**: A separate `docker-compose.test.yml` in `test/` that spins up the app container plus a Playwright container. This keeps browser dependencies out of the production image.

**Environment**: Tests run with `LLM_MOCK=true` by default for speed and determinism. An additional `SIMULATOR_SEED` env var (integer) seeds the simulator's RNG for deterministic price sequences in tests — same seed, same price path every run. Between `LLM_MOCK` and `SIMULATOR_SEED`, nothing in the app's normal request/response path needs test-only endpoints. Forced SSE disconnect/reconnect scenarios are driven from the Playwright side via network interception (aborting the `/api/stream/prices` request mid-test), not a backend control endpoint. Interactive elements use stable `data-testid` attributes so selectors don't depend on CSS classes or text content.

**Key Scenarios**:
- Fresh start: default watchlist appears, $10k balance shown, prices are streaming
- Add and remove a ticker from the watchlist
- Buy shares: cash decreases, position appears, portfolio updates
- Sell shares: cash increases, position updates or disappears
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): send a message, receive a response, trade execution appears inline
- SSE resilience: disconnect and verify reconnection

### Acceptance Criteria

- Removing a watchlist ticker with an open position still leaves that position continuously priced and sellable (§6, §8)
- A rejected trade changes neither cash, positions, trade history, nor snapshots; an executed trade changes all four atomically (§7, §9)
- A chat response's `results` array exposes the exact outcome of every proposed action, and `chat_messages.actions` persists only that — never the model's unvalidated intent (§9)
- A freshly started app has a non-empty `/api/portfolio/history` as soon as initial prices are available, not after the first 30-second tick (§7)
- With a fixed `SIMULATOR_SEED` and `LLM_MOCK=true`, the E2E suite deterministically verifies price streaming, trade behavior, and SSE reconnection
- Restarting against an existing `InvestAlly-data` volume preserves all data with no reseeding or resetting of the portfolio (§7, §11)

---

## 13. Decisions Log

*Doc review conducted 2026-08-05. Findings below were resolved and are now reflected inline in the sections referenced; this table exists as a record of why.*

| # | Question | Decision |
|---|---|---|
| 1 | Docker volume: host bind mount vs named volume? (§4 vs §11 conflict) | Named volume (`InvestAlly-data`). No host-visible `db/` directory — see §4, §11. |
| 2 | Main chart's price-history data source? (§10 vs §6/§8) | No backend endpoint — client accumulates from SSE since page load, same as sparklines. See §10. |
| 3 | Recharts mislabeled as canvas-based — switch to Lightweight Charts? | Keep Recharts (SVG); description corrected. See §10. |
| 4 | `chat_messages.actions` shape when no action was taken? | `null` for no action (user or assistant); JSON object matching the §9 structured-output schema otherwise. See §7. |
| 5 | Does the app auto-open a browser on launch? (§2 vs §11 wording) | No — start script prints the URL; user opens the browser manually. See §2, §11. |
| 6 | Does the watchlist scope what the Massive poller/simulator tracks? | Yes — adding/removing a ticker expands/contracts tracking live. See §6. |
| 7 | Bound on conversation history loaded per chat turn? | Capped at ~100K tokens of context. See §9. |
| 8 | Does `/api/portfolio/history` take a time-range param? | Yes, `?start=`/`?end=`. See §8. |
| 9 | Cap on trades the LLM can auto-execute per message? | Default 20 via `MAX_TRADES_PER_MESSAGE`, configurable. See §5, §9. |
| 10 | Does the non-root container user have SQLite write permissions? | Yes — Dockerfile ensures write access to the volume path. See §11. |
| 11 | Retention policy for `portfolio_snapshots`? | Downsample: keep 30s resolution for 24h, then collapse older rows to one per hour via an hourly background task. See §7. |

### Round 2 — Codex review (2026-08-05)

A second review pass was run via the `codex` CLI (see `planning/REVIEW.md` for the full findings) and cross-checked against an earlier internal pass. All findings below are now reflected inline; this table records the resolution.

| # | Finding | Resolution |
|---|---|---|
| 12 | Removing a watchlist ticker could leave an open position with no live price | Tracked-ticker set is now `watchlist ∪ open-position tickers`; removal only stops tracking if no position remains. See §6, §8. |
| 13 | No transaction/ordering contract for multi-action LLM responses | Each action executes independently, in array order, atomically per-action; a `results` array records executed/rejected + reason for every action. See §9. |
| 14 | LLM's message text can't reflect execution outcomes that happen after it's generated | `results` (not the LLM's prose) is the authoritative, frontend-rendered record of what happened; system prompt asks the model to phrase actions as intent, not confirmed fact. See §9. |
| 15 | Codex flagged `cerebras-inference` as an unavailable skill | **False positive** — the skill exists at `.claude/skills/cerebras/SKILL.md` with `name: cerebras-inference`; Codex's sandbox couldn't see the skills directory. No change made. |
| 16 | REST/SSE contracts too thin for independent frontend/backend/test implementation | Added ticker/quantity/timestamp/error-envelope conventions (§8) and full SSE event shape, initial snapshot, heartbeat, and slow-client behavior (§6). |
| 17 | SQLite concurrency and background-task lifecycle unspecified | Single Uvicorn worker (in-memory cache is process-local), WAL + busy-timeout, per-trade transactions, tasks started once via lifespan handler. See §11. |
| 18 | `REAL` risks visible float drift in money fields | Kept `REAL` (avoids an integer-cents migration that complicates fractional-share math) but mandated consistent rounding (2dp money, 6dp quantity) at every write. See §7. |
| 19 | "Create tables if missing" isn't an upgrade strategy | Added a `schema_meta` version row; migrations explicitly declared out of scope for this course project (fresh volume expected on schema change). See §7. |
| 20 | Market-data edge cases (new ticker, provider failure, daily-change reference) undefined | Added immediate-price-on-first-track rule, stale-cache-on-poll-failure behavior, and an explicit "opening price captured on first track" basis for daily change %. See §6. |
| 21 | Portfolio history could start empty and downsampling was imprecise | Initial snapshot now seeded at startup once first prices exist; downsample keeps one UTC-hour-aligned snapshot per hour rather than an ambiguous aggregate. See §7. |
| 22 | LLM output treated as insufficiently validated | Explicit statement that structured output is schema-validated then every action re-validated against the same rules as manual trades; no netting/dedup of repeated actions. See §9. |
| 23 | Static export serving / lockfile / SPA fallback undefined | `npm ci` in the build stage; unmatched non-API `GET` falls back to `index.html`. See §11. |
| 24 | `OPENROUTER_API_KEY` "required" conflicted with mock-mode/dev use | Added a startup matrix: key only required when `LLM_MOCK≠true`; missing `.env` tolerated. See §11. |
| 25 | Frontend chart memory could grow unbounded over a long session | Sparkline/main-chart points capped to a ~500-point ring buffer per ticker; reconnect keeps existing points. See §10. |
| 26 | Connection-status "disconnected" state may never trigger via raw `EventSource.readyState` | Added a client-side watchdog timer (time since last event) driving yellow/red instead of relying on `readyState` alone. See §10. |
| 27 | E2E scenarios (price determinism, forced disconnect) had no test controls | Added `SIMULATOR_SEED` env var and Playwright-side network interception for disconnects; no bespoke backend test endpoint needed. See §12. |
| 28 | No warning against exposing the no-auth app publicly | Added an explicit warning to the Optional Cloud Deployment section. See §11. |
| — | `watchlist_changes.action` values unenumerated (from the earlier internal review) | Confirmed `"add"` \| `"remove"`, cross-referenced in §8. |
| — | Position lifecycle on sell-to-zero unspecified (from the earlier internal review) | Row is deleted at zero quantity; a later buy starts a fresh row. See §7. |
