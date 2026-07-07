# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release notes are also maintained in code at `shared/version.py` — the
dashboard shows them via the version chip in the header, and the backend
serves them at `GET /version`. Keep both in sync.

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
