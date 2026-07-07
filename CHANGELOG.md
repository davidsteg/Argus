# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release notes are also maintained in code at `shared/version.py` — the
dashboard shows them via the version chip in the header, and the backend
serves them at `GET /version`. Keep both in sync.

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
