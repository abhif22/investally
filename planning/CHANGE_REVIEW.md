# Change Review

## Scope

`main` has no commits, so there is no prior commit to compare against. This review covers the complete untracked working tree as of 2026-08-06. It currently contains planning and editor-configuration files only; no application code or automated tests have been added.

## Findings

### P1 — Correct before implementation

1. **The Massive startup path breaches the documented free-tier request budget.**

   `MARKET_INTERFACE.md:260-263` seeds each initially tracked ticker with `await cache.track(ticker)`, and `:120-127` makes each new ticker execute a separate immediate `fetch_quote()` request. The ten default symbols therefore make ten calls during startup. This already exceeds the stated five-calls-per-minute budget in `MASSIVE_API.md:148-153`, before the regular 15-second poll begins. User additions can cause the same problem. Seed the full initial tracked set in one batch and make immediate refreshes coalesce into the next batch (or route through a shared rate limiter with a pending-quote state).

2. **`SIMULATOR_SEED` does not make the documented output deterministic across processes.**

   The design promises byte-identical sequences in `MARKET_SIMULATOR.md:130-146`, but `step()` constructs `sectors` as a set (`:178`) and `_tick_factors()` iterates that set (`:80-83`). Set iteration order varies with Python hash randomization, so RNG draws can be assigned to different sectors for the same seed. In addition, each returned quote uses wall-clock time (`:181`), so even a fully stable price path cannot yield byte-identical `TickerQuote` results. Draw sector factors in sorted order and inject/use a deterministic clock (or limit the determinism promise to prices rather than full quotes).

3. **Re-adding a ticker resets its stated lifetime opening price.**

   `PriceCache.untrack_if_unused()` retains the old cached price (`MARKET_INTERFACE.md:129-138`), but a later `track()` treats the ticker as new and `_seed_price()` unconditionally creates a new `CachedPrice` with `opening_price=quote.price` (`:120-127`, `:186-199`). That conflicts with the product rule that opening price is captured the first time a ticker is tracked during the container lifetime (`PLAN.md:181-184`). Preserve the existing opening price when refreshing a re-added ticker, or deliberately change the product rule to define a new tracking session.

4. **The specified SSE cadence turns unchanged cached quotes into repeated movements.**

   `PLAN.md:190-195` requires an event for every tracked ticker about every 500 ms, while the free-tier Massive cache is refreshed only every 15 seconds (`:165-169`). The SSE payload contains the old `previous_price` and old `change_direction` on each resend, and the frontend applies a price flash whenever it receives a price (`:409`). One actual price change will consequently flash and append chart points roughly 30 times. Emit events only when a quote changes, include an update sequence/timestamp and have the client deduplicate, or send a separate heartbeat without treating it as a price update.

5. **The cache/provider shutdown contract leaks and can leave work running.**

   `PriceCache.stop()` only calls `cancel()` (`MARKET_INTERFACE.md:157-159`); it never awaits the task, so cancellation/cleanup is not complete when FastAPI lifespan exits. Moreover, `MassiveProvider` owns an `httpx.AsyncClient` (`:281-285`) but the interface explicitly excludes lifecycle methods (`:83-86`), leaving no path to close that client. Await the cancelled poll task while suppressing `CancelledError`, and add an explicit close/async-context lifecycle for providers that own resources.

6. **The repository is ready to accidentally commit the credentials the plan tells users to place in `.env`.**

   `.gitignore` is empty, `.env` is untracked, and `PLAN.md:121-142` instructs users to put `OPENROUTER_API_KEY` there. The currently checked `.env` is empty, so no credential is exposed in this tree, but a routine `git add .` will stage any later key. Ignore `.env` and commit a redacted `.env.example` containing the documented variables.

### P2 — Resolve before relying on the design

7. **The proposed cache mutation flow has no safe first-quote failure state.**

   `track()` records a ticker in `_tracked` before awaiting its first quote (`MARKET_INTERFACE.md:123-127`), and `_seed_price()` lets provider failures propagate (`:186-199`). The planned watchlist/trade flows can therefore persist or track a ticker whose cache entry is absent after a transient Massive failure. Concurrent polling and seeding also write `_prices` without coordination, so an out-of-order seed response may overwrite a newer poll result. Define whether the user mutation is rolled back, accepted as `price_pending`, or retried, and serialize/validate cache writes so an older quote cannot replace a newer one.

8. **The missing-`.env` startup contract is incorrect.**

   `PLAN.md:463` says `docker run --env-file .env` tolerates an absent file. Docker errors when an explicitly supplied env-file path does not exist. Make the start scripts add `--env-file .env` only when the file exists, or provide a committed non-secret defaults file.

9. **The simulator is not “zero-dependency” as documented.**

   `MARKET_SIMULATOR.md:7` and `PLAN.md:161` call the simulator zero-dependency, but the reference implementation imports NumPy (`MARKET_SIMULATOR.md:80`, `:135`, `:154`). Either list NumPy as a backend runtime dependency or clarify that the claim means no external market-data service/network dependency.

10. **The Docker build instructions contain contradictory package-manager commands.**

   The Stage 1 outline says `npm install && npm run build` (`PLAN.md:419-423`), whereas the next paragraph requires `npm ci` (`:433`). The Dockerfile implementer has two incompatible instructions. Keep only `npm ci && npm run build` and state that a lockfile is required.

## Verification

No executable implementation or test suite exists in this change set, so no runtime tests were run. The findings above are based on the documented contracts and reference code snippets.
