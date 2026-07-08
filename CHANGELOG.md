# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release notes are also maintained in code at `shared/version.py` — the
dashboard shows them via the version chip in the header, and the backend
serves them at `GET /version`. Keep both in sync.

## [v2.9.0] - 2026-07-08

### Added
- **Short selling mode** — when `short_enabled` is ON (toggle in Settings), the
  engine generates **SELL** signals on RSI-overbought + price-above-VWAP +
  bearish-sentiment setups, the mirror image of the existing BUY logic.
- `place_bracket_short()` submits bracket SELL orders with buy-to-cover
  take-profit below entry and stop-loss above entry, using the same ATR-scaled
  distances and risk-based sizing as BUY orders.
- **Signal-driven covers**: a held short closes early when RSI drops below
  `rsi_short_exit` (default 30), symmetric to the long-side RSI exit.
- **Regime-aware gating**: SELL signals pass through `RISK_OFF` (falling market
  favours shorts) while BUY signals are blocked.
- New strategy parameters: `rsi_short_signal`, `rsi_short_exit`, `short_enabled`
  — all editable from Settings, tuned nightly by the optimizer (added to
  `PARAMETER_GRID` and modelled in `backtest()`).
- Dashboard **Side column** (BUY/SELL) on the Active Positions table and Trade
  History grid, with green/red color coding.

## [v2.8.0] - 2026-07-08

### Added
- **Opportunity screener** (`backend/screener.py`) — scans the top 200
  most-active symbols for RSI-oversold + VWAP-dip + bullish-sentiment
  setups, the same pattern the engine trades. Ranks by dip depth and
  publishes candidates to the dashboard and `GET /screener`. Reuses the
  exact same `indicators.py` and `sentiment.py` code so screener and live
  signals never drift.
- Runs every 5 minutes as a background task in the engine; configurable
  from Settings (toggle, pool size, max candidates).
- Candidates shown on the Overview tab with RSI, price, VWAP, dip depth,
  and sentiment source.
- New config params: `screener_enabled`, `screener_pool_size`,
  `screener_max_candidates` in `DEFAULT_CONFIG`.

## [v2.7.0] - 2026-07-08

### Added
- **Signal-driven exits** — a held long now closes early at market when RSI
  recovers past the new `rsi_exit_signal` level (default `70`), the symmetric
  mirror of the RSI-oversold entry. Until now a position only ever exited when
  its bracket take-profit or stop-loss filled, so a bounce that stalled below
  the take-profit just round-tripped back down. The resting bracket still
  guards the downside independently — this only banks the reversion sooner.
- `rsi_exit_signal` is a first-class strategy parameter: it lives in
  `bot_config` (editable from the Settings tab), is tuned nightly by the
  optimizer (added to `PARAMETER_GRID` and modelled in `backtest()` as a fill
  at the bar close when neither bracket leg triggers intra-bar, so live and
  backtest exits never drift), and is surfaced in the `/signals` dry-run and
  the per-cycle trace (`signal_exits`).

### Changed
- The exit path cancels the bracket's OCO take-profit/stop-loss legs before
  market-closing the position, so a manual close can never collide with a
  resting leg or leave one dangling to sell shares the position no longer
  holds. The resulting sell is reconciled into a trade record on the next
  cycle by `sync_portfolio`/`reconcile_closed_trade`, exactly like a bracket
  exit — a single trade-recording path.
- Signal exits are evaluated ahead of the entry gates each cycle, so a full
  book (`MAX_POSITIONS`) or a `RISK_OFF` tape no longer blocks taking profit
  on an exhausted bounce. Open-slot accounting still counts a closing position
  as held until its sell fills, so the freed slot is never double-allocated.

## [v2.6.3] - 2026-07-08

### Fixed
- **Trade/optimization/watchlist reviews had no enforced JSON schema**:
  `review_trades()`, `review_optimization()`, and `review_watchlist()`
  called `_call_llm()` without a `system_prompt`, falling back to a
  generic "respond with JSON only" instruction — unlike the risk agent
  and portfolio manager prompts, which explicitly template the expected
  keys. A review could succeed (get a `reviewed_at` timestamp written to
  `runtime_state`) while the model's JSON omitted `summary`/`warnings`/
  `suggestions`/`confidence` entirely, so the dashboard's review cards
  rendered blank even when the analyst was reachable and responding.
  Added `TRADE_REVIEW_PROMPT`, `OPTIMIZATION_REVIEW_PROMPT`, and
  `WATCHLIST_REVIEW_PROMPT` (same schema-prompt pattern as the existing
  agents) and wired all three into their respective `_call_llm()` calls.

## [v2.6.2] - 2026-07-08

### Fixed
- **Trade History grid never rendered**: `ui.aggrid(..., theme="balham-dark")`
  crashed the grid's Vue `mounted()` hook — the bundled NiceGUI version's
  aggrid wrapper only maps `quartz`/`balham`/`material`/`alpine` to a
  theme object; looking up `"balham-dark"` returned `undefined`, and
  calling `.withPart(...)` on it threw a `TypeError` before
  `AgGrid.createGrid()` ever ran. Dark styling is already applied
  automatically via a `MutationObserver` on the page's dark-mode class,
  so the `-dark` suffix was both invalid and unnecessary. The stats
  tiles (which read `trade_stats` independently) showed correct closed-
  trade counts the whole time, masking that the grid itself was silently
  dead — no rows, no column headers, no empty-state fallback text.
  Changed to `theme="balham"`.

## [v2.6.1] - 2026-07-08

### Changed
- **Watchlist timing is now dashboard-editable, not environment-only**:
  `watchlist_refresh_minutes` and `watchlist_override_ttl_minutes` moved
  into the shared `bot_config` table (same mechanism as RSI/ATR strategy
  parameters) with a new **📡 Watchlist** card in the Settings tab.
  Changes take effect on the next screener check or override lookup — no
  restart, no `.env` edit. `WATCHLIST_SIZE` and `TRADING_SYMBOLS` remain
  environment variables since they define the deployment's universe
  mode/size rather than a live-tunable dial.
- Added **Watchlist Model** and **Risk Model** inputs to the Analyst tab's
  Connection & Schedule card. The backend has supported per-agent model
  overrides since v2.4.14/v2.5.0, but the dashboard never exposed input
  fields for them — they were only reachable via a raw API call.

## [v2.6.0] - 2026-07-08

### Fixed
- **Lesson extraction cadence**: `_cycle_count` was never incremented, so
  the "every 50 cycles" guard was always true and decision-memory lesson
  extraction fired an LLM call every cycle (~every 60 s). Now it truly
  runs every 50th cycle.
- **Decision memory closed-loop**: trade exits now feed their realized
  PnL back into decision memory, attaching the outcome to the original
  buy decision. Previously every stored decision had a null outcome and
  lesson extraction had nothing real to learn from.
- **Watchlist override feedback loop**: an LLM watchlist override was
  honored forever (persisted, no TTL) and the most-actives screener was
  never consulted again, so the universe drifted on the LLM's own
  previous output. Overrides now expire after 2× the refresh interval
  (`WATCHLIST_OVERRIDE_TTL_MINUTES`), curation receives the live
  screener pool as candidates, and suggested symbols are validated
  against it — hallucinated tickers can no longer go live. Legacy
  plain-list overrides in existing databases are ignored.
- **Daily loss baseline persists across restarts**: the anchor date is
  now stored in the database, so restarting the engine mid-drawdown no
  longer resets `daily_start_balance` and re-arms a fresh daily loss
  budget.
- **Portfolio manager silence no longer trades**: signals the manager
  neither approved nor rejected were still executed. They are now
  skipped and logged; full fail-open remains only when the manager call
  itself errors.
- Missing `List` typing import in `bot.py`.

### Changed
- **Session-anchored VWAP**: `compute_vwap` resets its cumulative sums at
  each US-Eastern trading date instead of cumulating over the fetched
  window, so the fair-value anchor no longer blends across the overnight
  gap or shifts with `BAR_LOOKBACK_MINUTES`. Shared by the live engine,
  `GET /signals` and the optimizer.
- **Optimizer backtest aligned with the live strategy**: the nightly grid
  search now applies the VWAP dip-confirmation gate and the post-loss
  cooldown (mirroring `COOLDOWN_MINUTES`), so parameters are tuned on
  much closer to the system that actually trades. Sentiment and regime
  gates cannot be replayed from bars alone and remain excluded.
- **Faster cycles**: signal evaluation runs concurrently across symbols
  (semaphore-bounded), risk-agent calls run in parallel and are capped to
  the best `open_slots × 2` candidates, and the periodic trade review /
  watchlist curation / lesson extraction moved to a background task so
  they can never delay order placement.
- `regime.py` reuses a single Alpaca data client instead of building one
  per classification.

## [v2.5.2] - 2026-07-08

### Changed
- New multi-eye Argus logo: a central gold eye with six smaller eyes,
  inspired by the hundred-eyed watchman. Rendered as inline SVG in the
  dashboard header and served as the browser favicon (previously a 🛡️
  emoji in both places).

### Removed
- Six unused logo/favicon PNGs in `frontend/static/` — nothing
  referenced them.

## [v2.5.1] - 2026-07-08

### Fixed
- Decision memory lesson extraction now runs every 50 cycles automatically
  instead of requiring a manual API call. The system is fully autonomous.

## [v2.5.0] - 2026-07-08

### Added
- **Pre-trade Risk Agent**: evaluates each BUY signal for sector
  concentration, correlation, recent losses, and regime fit before
  execution. Can block marginal trades.
- **Portfolio Manager Agent**: reviews all pending signals and decides
  which to execute and in what order, respecting diversification and
  opportunity cost.
- **Decision Memory**: stores every trade decision and its outcome,
  extracts lessons that are fed back into future trade reviews.
- Separate `risk_model` config field for a cheaper/faster model on
  pre-trade checks.
- New API endpoints: `GET /analyst/memory`, `POST /analyst/extract-lessons`

## [v2.4.14] - 2026-07-08

### Added
- Automatic LLM watchlist curation: analyst reviews the watchlist every
  hour and can replace it with a curated list (sector diversification,
  regime-adaptive filtering, prune dead symbols). Override is written to
  `runtime_state` and picked up by `universe.py` on the next cycle.
- Separate `watchlist_model` config field — use a cheaper/faster model
  for watchlist curation while keeping the main model for trade and
  optimization reviews. Falls back to the main model if not set.

## [v2.4.13] - 2026-07-08

### Added
- The analyst now validates the optimizer's winning parameter combination
  and can **accept**, **override** (pick a different rank), or **reject**
  it. If rejected, current params stay unchanged. The write to `bot_config`
  only happens after the analyst has spoken.

## [v2.4.12] - 2026-07-08

### Fixed
- LLM now returns valid JSON but gets truncated at 2048 tokens, producing
  incomplete JSON that fails to parse. Increased `max_tokens` to 8192.

## [v2.4.11] - 2026-07-08

### Fixed
- DeepSeek models ignore "respond with JSON only" and write prose. System
  prompt now includes an exact JSON template to fill in, which forces
  structured output.

## [v2.4.10] - 2026-07-08

### Fixed
- Ollama Cloud DeepSeek models put output in the non-standard `reasoning`
  field instead of `content`. Fall back to `reasoning` when `content` is
  empty — this was causing the "LLM returned blank content" error

## [v2.4.9] - 2026-07-08

### Fixed
- Log `finish_reason` and `refusal` field when LLM returns empty content,
  so we can distinguish model-not-found from content-policy blocks without
  `docker exec`

## [v2.4.8] - 2026-07-08

### Fixed
- Toggle infinite loop: removed `set_value` from `render_analyst` — the
  switch is now managed by user clicks only, eliminating the async race
  that caused the callback to fire on every refresh cycle
- LLM response text (first 500 chars) is now included in the exception
  message so it shows up in `GET /logs` instead of requiring `docker exec`

## [v2.4.7] - 2026-07-08

### Fixed
- Analyst toggle infinite loop in UI: added `_suppress_change` guard flag
  to prevent `render_analyst` from triggering the toggle callback when
  syncing the switch state from the database

## [v2.4.6] - 2026-07-08

### Fixed
- Trade review and optimization review errors are now logged to the DB
  logs table so they're visible via `GET /logs` — no need for `docker exec`
  to see the failure reason

## [v2.4.5] - 2026-07-07

### Fixed
- Strip  tags from DeepSeek-R1 chain-of-thought responses before
  JSON parsing; the model was returning thought traces that made
  `json.loads` fail
- Improved `_parse_json` with three-stage fallback: (1) strip  tags,
  (2) extract markdown code blocks, (3) find any `{...}` JSON object in
  the text — handles every response format we've seen
- Log the raw LLM response (first 2000 chars) on parse failure so we can
  debug without container exec access
- Increased `max_tokens` from 1024 to 2048 for longer responses

## [v2.4.4] - 2026-07-07

### Fixed
- Removed `response_format={"type": "json_object"}` from LLM calls (not
  supported by Ollama Cloud); added JSON extraction from markdown code
  blocks in responses instead, plus better error logging for debugging
- Analyst config not persisting: added explicit logging to `_persist_config`
  so silent failures are visible in container logs
- Removed unused `output_schema` parameters from `_call_llm` signature

## [v2.4.3] - 2026-07-07

### Fixed
- Missing `openai` dependency in `requirements.txt` — the analyst module
  imports it for the OpenAI-compatible client but it wasn't installed in the
  Docker image, causing a silent `ImportError` and 503 on every review attempt

## [v2.4.2] - 2026-07-07

### Fixed
- Infinite toggle loop on the Analyst tab: `render_analyst` now tracks the
  enabled state and only calls `set_value` when it actually changed,
  preventing the toggle callback from firing an API call every refresh cycle

## [v2.4.1] - 2026-07-07

### Added
- Analyst API key field for cloud-hosted Ollama (e.g. `https://ollama.com/v1`);
  falls back to `"ollama"` for local instances
- Password field in the Analyst tab UI for the API key
- `ANALYST_OLLAMA_API_KEY` env var and docker-compose wiring

## [v2.4.0] - 2026-07-07

### Added
- **LLM strategy analyst** (`backend/analyst.py`): an advisory module that
  uses a cloud-hosted Ollama model (OpenAI-compatible API, DeepSeek Flash
  by default) to review the bot's own performance and suggest improvements
- **Post-optimization review**: after the nightly walk-forward grid search
  the LLM analyzes the ranked results and the winning combination, flagging
  overfitting, small validation samples, and parameter drift
- **Periodic trade review**: every few hours during market hours the LLM
  analyzes recent closed trades for failure patterns (symbol clusters,
  regime mismatches, stop/target calibration)
- **Analyst tab** in the dashboard (between Trades and Settings): toggle
  switch (on/off, persisted in `bot_config`), connection config card (base
  URL, model, review interval, trade lookback — all editable with no
  restart), optimization review card, trade review card with manual
  "Run review now" trigger
- **Runtime config**: analyst settings (base URL, model, interval,
  lookback) are stored in `runtime_state` and can be changed from the
  dashboard without touching env vars or restarting the container; the
  OpenAI client is rebuilt on URL/model change
- New API endpoints: `GET /analyst/optimization`, `GET /analyst/trades`,
  `POST /analyst/review`, `POST /analyst/toggle`, `GET /analyst/config`,
  `POST /analyst/config`
- `analyst_enabled` key in `DEFAULT_CONFIG` (0.0 = off by default)
- New env vars: `ANALYST_OLLAMA_BASE_URL`, `ANALYST_OLLAMA_MODEL`,
  `ANALYST_TRADE_REVIEW_INTERVAL_HOURS`, `ANALYST_TRADE_LOOKBACK`

### Changed
- Optimizer now carries `analyst_enabled` forward (like `news_cutoff`) so
  a config read after a grid search stays complete
- Engine main loop calls the analyst's periodic trade review (gated by
  `should_review_trades()` which enforces the configured interval)

## [v2.3.0] - 2026-07-07

### Added
- **Dashboard rebuilt into a four-tab command center** (Overview, Trades,
  Settings, Logs) with a live header: market-regime chip, market-session
  chip, engine heartbeat (LIVE / STALE), daily PnL in $ and %
- **Equity curve chart** with 1H/1D/1W/1M/ALL ranges, backed by a new
  `equity_history` table — the engine records an equity snapshot every
  cycle (flat stretches are compressed)
- **Positions with live PnL**: current price, market value and unrealized
  PnL per position (new columns on `positions`, migrated in place), plus
  a per-position close button (cancels the bracket legs, then
  market-closes — dashboard-side Alpaca client, paper-forced)
- **Trade analytics tab**: all-time realized PnL, realized today, win
  rate, profit factor, average win/loss, best/worst trade, a cumulative
  realized-PnL chart and a paginated trade-history grid with PnL %, hold
  duration
- **Engine internals on the dashboard without an HTTP hop**: the engine
  publishes its cycle trace, market regime, cooldowns and operational
  environment into a new `runtime_state` table each cycle
- **Settings tab**: strategy parameters editable with bounds-checked
  inputs (Apply / Reload from DB / Restore defaults — the nightly
  optimizer still re-tunes and overwrites them), read-only operational
  environment, dashboard refresh-interval and log-row preferences
- **Engine actions from the dashboard**: resume-from-KILLED (header
  button, `POST /reset`) and run-optimizer-now (`POST /optimize`) via the
  backend debug API — new `BACKEND_API_URL` env var (compose default:
  `http://trading_backend:8000`)
- **Filterable log terminal**: per-level filter, text search, adjustable
  row count; recent-activity feed on the Overview tab

### Changed
- EMERGENCY HARD STOP now asks for confirmation before flattening (the
  stop itself is unchanged: dashboard-side Alpaca client, independent of
  engine state)
- `Database.get_trade_stats()` provides all-time win/loss aggregates in
  SQL instead of the dashboard recomputing from recent rows

## [v2.2.5] - 2026-07-07

### Changed
- Rewrote `AGENTS.md` from generic boilerplate into project-specific
  rules for AI agents: architecture map, hard safety invariants
  (`paper=True`, bracket-only entries, independent kill-switch paths),
  where strategy parameters actually live (`bot_config`, not env vars),
  the release/CI pipeline and tag format, secrets handling, how to test
  changes locally without a preinstalled Python environment, and the
  known pitfalls already hit once this project (stale-bar bracket
  rejections, the `news_cutoff` no-news trap, retired-config-key
  filtering) so they aren't reintroduced

## [v2.2.4] - 2026-07-07

### Fixed
- Bracket orders were being rejected repeatedly on cheap, low-ATR symbols
  (e.g. NVD, OPEN) with `stop_price must be <= base_price - 0.01`. The
  bracket was priced off the last completed 1-minute bar, which can be up
  to `POLL_INTERVAL_SECONDS` stale — on a tight ATR-scaled stop that's
  often narrower than the price movement since the bar closed. The
  engine now re-prices off the latest trade immediately before submitting
  the order (falling back to the bar price if that fetch fails), and the
  penny-rounding floor was widened from 1 cent to 2 cents of slack

## [v2.2.3] - 2026-07-07

### Changed
- `docker-compose.yml` now runs the published Docker Hub images
  (`davidsteg/argus-*`) instead of building locally; pin a release with
  `ARGUS_VERSION` (default `latest`), update via
  `docker compose pull && docker compose up -d`
- Local source builds moved to `docker-compose.dev.yml`
  (`docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build`)

### Fixed
- GitHub Release titles no longer double the version (the dedup compare
  in the publish workflow is now case-insensitive)

## [v2.2.2] - 2026-07-07

### Changed
- CI now mirrors the subsyncarr release pipeline
  (`.github/workflows/docker-publish.yml`): builds and pushes
  `davidsteg/argus-backend` / `davidsteg/argus-frontend` to Docker Hub
  **only when a version tag is pushed** (no branch/PR/ad-hoc builds), then
  auto-creates a GitHub Release whose notes are extracted from the
  matching section of this file — keep it current when tagging
- Replaces the v2.2.1 `docker-build.yml` (branch/PR triggers removed on
  purpose: only real releases reach Docker Hub)
- Still requires `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` repository
  secrets in GitHub → Settings → Secrets and variables → Actions

## [v2.2.1] - 2026-07-07

### Added
- GitHub Actions workflow (`.github/workflows/docker-build.yml`): builds
  `argus-backend` / `argus-frontend` Docker images on pushes to master,
  version tags and PRs, and publishes them to Docker Hub
  (`davidsteg/argus-*`) with semver, branch, SHA and `latest` tags
  (requires `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` repo secrets)

## [v2.2.0] - 2026-07-07

### Added
- Market regime filter (`backend/regime.py`): SPY trend (EMA) + realized
  volatility classify the tape as RISK_ON / CAUTION / RISK_OFF; in
  RISK_OFF the engine opens no new positions (existing brackets and the
  daily kill-switch still manage open trades). Exposed at `GET /regime`
- VWAP dip confirmation: an RSI trigger only becomes a BUY when price is
  below the session VWAP — filters out RSI artifacts that aren't real dips
- Volatility-adaptive bracket exits: stop-loss and take-profit distances
  are now ATR multiples (`atr_stop_mult`, `atr_target_mult` in
  bot_config) with shared sanity floors, replacing fixed percentages
- Risk-based position sizing: share count targets a constant dollar loss
  at the stop (`RISK_PER_TRADE_USD`, default 20), capped by
  `POSITION_SIZE_USD` notional
- Loser cooldown: after a losing exit a symbol is benched for
  `COOLDOWN_MINUTES` (default 30) so the engine stops re-buying the same
  falling knife every minute
- Shared `backend/indicators.py` (RSI, ATR, VWAP, bracket math) used by
  both the live engine and the optimizer — backtests can no longer drift
  from live signal math
- `.gitignore` + `.env.example`; `.env` is no longer tracked by git

### Changed
- The nightly optimizer now validates out-of-sample: bars are split
  chronologically (75% train / 25% validation, `OPTIMIZER_TRAIN_FRACTION`);
  candidates are ranked on train and only go live if also profitable on
  the unseen validation window, otherwise current parameters are kept
- Optimizer grid now searches ATR multiples instead of fixed stop/target
  percentages (still 192 combinations)
- `/signals` mirrors the full upgraded pipeline (regime, cooldown, VWAP,
  ATR) and `/debug` reports active cooldowns and the new risk knobs
- README rewritten to describe the current system (was still documenting
  the retired Streamlit prototype)

### Removed
- `stop_loss_pct` / `take_profit_pct` config keys (retired; filtered out
  of reads if they linger in an existing database)

## [v2.1.0] - 2026-07-07

### Added
- Debug & operations API served by the backend container on port 8000
  (interactive docs at `/docs`): `GET /health`, `/status`, `/config`,
  `/debug` (engine internals + last cycle trace), `/signals` (live dry-run
  of the decision logic per symbol), `/positions`, `/trades`, `/logs`,
  `/version`
- Operational actions: `POST /optimize` (run the walk-forward grid search
  now), `POST /kill` (emergency kill-sequence), `POST /reset` (recover
  from KILLED and restart the engine — no container bounce needed)
- Whole-market trading: `TRADING_SYMBOLS=ALL` (now the default) trades the
  top-N most active US equities by volume via Alpaca's screener, refreshed
  every 15 minutes (`WATCHLIST_SIZE`, default 50); `MIN_PRICE_USD` filter
  keeps penny stocks out
- Real news sentiment: Alpaca News API headlines scored by Claude
  (`ANTHROPIC_API_KEY` + optional `SENTIMENT_MODEL`) with structured JSON
  output, falling back to a transparent keyword heuristic without a key;
  scores cached 15 minutes and only computed after the technical trigger
- Version chip + release-notes dialog in the dashboard header, backed by
  `shared/version.py`

### Changed
- The engine now keeps running as an API server even after a kill, so the
  state can be inspected and reset remotely
- The optimizer optimizes over the most active subset of the dynamic
  universe (`OPTIMIZER_MAX_SYMBOLS`, default 10)

## [v2.0.0] - 2026-07-07

### Changed
- Complete rewrite as "Argus": async trading engine on `alpaca-py` with
  hardcoded paper trading, bracket orders (TP/SL) from live `bot_config`,
  daily-loss kill-sequence, Swiss-midnight PnL baseline
- Streamlit dashboard replaced by a custom NiceGUI dark dashboard with
  EMERGENCY HARD STOP (direct Alpaca liquidation)
- State moved to a thread-safe SQLite database (`argus_state.db`) on a
  shared Docker volume; backend/frontend communicate through it
- Nightly walk-forward grid-search optimizer at midnight Europe/Zurich
  writes tuned parameters to `bot_config`

### Removed
- Old FastAPI `backend/api.py`, `shared/models.py`, Streamlit frontend

## [v0.1.1] - 2026-07-07

### Added
- AGENTS.md with development rules (versioning, code quality, documentation)
- Development workflow documentation

## [v0.1.0] - 2026-07-07

### Added
- Initial trading bot project structure
- Backend API (FastAPI)
- Frontend UI (Streamlit)
- Shared models for database state
- Docker configuration (docker-compose.yml)
- Dockerfiles for backend and frontend
- .dockerignore files for both services
- DEBUG.md troubleshooting guide
- README.md with setup instructions
- .env template for Alpaca API credentials
- requirements.txt for Python dependencies
