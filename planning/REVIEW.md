# Plan Review — 2026-08-05

The plan has a clear product direction and sensible high-level boundaries. The items below should be resolved before parallel implementation begins; without them, independently built frontend, backend, and test pieces will make incompatible assumptions.

## Findings

### Blockers

1. **A removed watchlist ticker can make an existing position impossible to value.**

   Section 6 says the tracked-ticker set contracts when an item is removed and that SSE updates are equivalent to the watchlist. A user can remove a ticker they still hold, leaving no current price for `/api/portfolio`, snapshots, P&L, or a later sell. Define the tracked universe as at least `watchlist ∪ open-position tickers` (and perhaps currently selected ticker), or prohibit removal while a position is open. The API should state which behavior it implements.

2. **Trade and chat batch execution have no transaction/ordering contract.**

   Cash, positions, trades, and the immediate portfolio snapshot must change atomically. Also specify whether a multi-trade LLM response is all-or-nothing or each action is independently attempted in listed order, and return a per-action result (`executed`/`rejected`, fill price, reason). This is needed for cases such as two buys that individually fit the starting balance but not the balance after the first one.

3. **The plan asks the model to explain execution failures after its response has already been generated.**

   In §9, failures are said to be included “so the LLM can inform the user,” but the LLM call is complete before server-side validation/execution. Choose one explicit behavior: append deterministic execution-status text to the displayed assistant message, perform a second model call with the results, or make the UI render statuses separately. Persist exactly the final user-visible message and actual action results.

### Major

5. **The REST and SSE contracts are too thin for separate clients and tests.**

   Each endpoint needs request/response JSON examples or typed schemas, status codes, validation rules, and a common error envelope. Define ticker normalization/allow-list behavior, permitted quantity precision and positivity, timestamp format/timezone, rounding rules, and whether a position at effectively zero quantity is deleted. For SSE, specify event name, `data` payload, initial snapshot behavior, heartbeat, retry interval, event IDs/replay policy, and behavior when a client is slow. “Change direction” and “daily change %” also need a definition distinct from the previous 500 ms tick.

6. **SQLite concurrency and lifecycle are unspecified.**

   Background price/snapshot/retention tasks, SSE requests, and trade/chat requests will write or read concurrently. Choose a database access strategy (for example, one async driver/connection policy), enable/configure WAL and busy timeout as appropriate, and require transactions for portfolio mutations. Define startup/shutdown ownership of background tasks so reloads and multiple workers do not duplicate the simulator or snapshots. The intended Uvicorn worker count should be stated; in-memory price caches are not shared across workers.

7. **`REAL` is unsuitable as the final source of truth for cash and accounting.**

   Floating-point arithmetic will produce visible balance/P&L drift. Store monetary amounts as integer minor units or validated decimal strings/numeric values, specify display rounding, and define the allowed fractional-share precision. Ensure all server-side calculations use the same convention.

8. **Database initialization is not an upgrade strategy.**

   “Create tables if missing” works only for a fresh volume. It cannot safely evolve a persisted schema. Add a schema-version table and ordered, idempotent migrations (or explicitly declare database compatibility unsupported for this course project). Seed operations must be idempotent and must never reset existing balances or positions.

9. **Market-data semantics need edge-case rules.**

   Define initial price behavior for a newly added/unknown ticker, provider failures/stale-cache behavior, price timestamps, market-closed behavior, and whether a trade may fill on stale data. State the exact Massive endpoint(s), batch limits, and rate-limit/backoff strategy. The plan’s daily-change display needs an opening/reference price; `previous_price` only provides tick-to-tick change.

10. **Portfolio history needs a reliable initial point and a precise retention algorithm.**

   The P&L chart can be empty for the first 30 seconds if no trade occurs. Seed an initial snapshot after prices are ready, and define how its value is computed. For downsampling, specify the bucket timezone/alignment and which representative/aggregate row is retained; otherwise retention can create duplicate or misleading timeline points.

11. **LLM action safety and validation are underspecified.**

   The backend must treat model output as untrusted: strict schema validation, max message/action sizes, ticker and quantity validation, deduplication/conflict rules (e.g., add then remove the same ticker), and a deterministic action order. Define whether the server permits trades merely because the model emitted them or additionally checks user intent. Even in a simulated account, an unsolicited model trade is a poor UX. Do not allow the LLM to invent a price or bypass the normal trade path.

### Moderate

12. **The static-export serving behavior needs definition.**

   Specify the Next.js version/export configuration, package-manager lockfile policy (`npm ci` rather than unconstrained `npm install`), static asset path/base-path assumptions, and FastAPI’s SPA fallback for non-API routes. Without fallback, directly loading or refreshing a client-side route can return 404.

13. **Startup and configuration expectations conflict slightly.**

   `OPENROUTER_API_KEY` is labelled required, while mock mode and simulator development should work without it. State the startup validation matrix: which combinations boot, whether missing `.env` is acceptable, and whether the script passes an absent `.env` safely. Define stable image/container names and the behavior if port 8000 or an existing container is already in use.

14. **Frontend state growth and reconnect behavior need bounds.**

   Client-accumulated sparkline and main-chart points grow indefinitely over a long session. Set a per-ticker point/time cap and downsampling rule. On reconnect, define whether the client preserves existing points, replaces them with an SSE initial snapshot, and how it detects stale connection state.

15. **Some E2E scenarios require test controls not currently designed.**

   Deterministic mock LLM alone does not make prices, provider failures, or forced SSE disconnect/reconnect deterministic. Add a test-only simulator seed/clock/control endpoint or equivalent injectable market-data source, and document that it is unavailable in normal runs. Also specify stable selectors/accessibility requirements for Playwright.

16. **Local-only assumptions should be made explicit for deployment.**

   The app intentionally has no authentication and lets chat execute simulated trades. If cloud deployment remains a stretch goal, state that it must not be publicly exposed without an access-control layer. Otherwise any visitor shares and mutates the single default portfolio.

## Recommended acceptance criteria to add

- Removing a watchlist ticker with an open position still leaves that position continuously priced and sellable.
- A failed trade changes neither cash, positions, trade history, nor snapshots; a successful trade changes all required records atomically.
- A chat response exposes the exact outcome of every proposed action and persists only actions actually executed.
- A newly started app provides a non-empty portfolio-history baseline once initial prices are available.
- With a fixed simulator seed and mock LLM, the E2E suite can deterministically verify price streaming, trade behavior, and SSE reconnection.
- Restarting against an existing named volume preserves data and applies any required schema migration without reseeding or resetting the portfolio.
