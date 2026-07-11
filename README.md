# Argus — Adaptive Short-Term Trading Bot

A fully containerized, asynchronous paper-trading engine on Alpaca with a
NiceGUI command center. Argus buys confirmed intraday dips in the most
active US equities, exits through volatility-adaptive bracket orders, and
re-tunes itself every night with an out-of-sample-validated optimizer.

**Paper trading is hardcoded.** Argus never touches live money unless the
code is consciously changed and reviewed.

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                        Docker Compose                            │
│                                                                  │
│  ┌────────────────────────┐          ┌───────────────────────┐   │
│  │  argus_backend  :8000  │          │  argus_frontend :8080 │   │
│  │                        │          │                       │   │
│  │ • Trading engine       │  SQLite  │ • NiceGUI dashboard   │   │
│  │ • Regime filter (SPY)  │◄────────►│ • Equity/PnL charts   │   │
│  │ • Nightly optimizer    │  db_data │ • Strategy settings   │   │
│  │ • Debug & ops API      │          │ • EMERGENCY HARD STOP │   │
│  └────────────────────────┘          └───────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
```

All dashboard *data* comes straight from the shared SQLite volume — the
engine publishes its cycle trace, market regime and environment into the
`runtime_state` table every cycle. Only *actions* that must reach the
running engine process (resume from KILLED, run the optimizer now) call
the backend debug API (`BACKEND_API_URL`); the EMERGENCY HARD STOP uses
its own Alpaca client and works even if the engine is dead.

## The strategy — layered decision pipeline

Every minute, for each symbol on the watchlist (default: top-50 most
active US equities, refreshed every 15 minutes):

1. **Regime gate** — SPY trend + realized volatility (`regime.py`).
   When the index trades below its short-term EMA *and* volatility is
   stressed (`TREND_DOWN`), no new longs are opened — in a broad
   sell-off every dip is a knife (shorts remain allowed). In `CAUTION`
   (trend down *or* vol elevated) the position cap is halved.
2. **RSI trigger** — Wilder RSI on 1-minute bars drops below the buy
   level (nightly-optimized).
3. **Loser cooldown** — a symbol that just stopped out is benched for 30
   minutes; re-buying the same falling knife each minute is how mean
   reversion bleeds out.
4. **VWAP confirmation** — price must sit below the session VWAP: the
   dip has to be real, not an RSI artifact.
5. **News sentiment** — Alpaca headlines scored by Claude (keyword
   heuristic without an `ANTHROPIC_API_KEY`); bearish news blocks the
   trade.
6. **Adaptive exit & sizing** — stop/target distances are ATR multiples
   (a quiet megacap gets a tight bracket, a high-beta mover a wide one),
   and share count is chosen so hitting the stop loses about
   `RISK_PER_TRADE_USD`, capped by `POSITION_SIZE_USD` notional. During
   the regular session the levels rest on the exchange as native bracket
   legs (OCO take-profit + stop-market); pre/post-market — where Alpaca
   rejects brackets — they are soft levels enforced by the engine each
   poll cycle against the live quote.
7. **Signal exit** — on top of the stop/target, a held long is closed
   early when RSI recovers past `rsi_exit_signal` (the mirror of the
   entry trigger): the mean reversion has played out, so bank the
   bounce instead of waiting for the take-profit. The stop still
   guards the downside independently.

### Safety nets
- Hard **daily loss limit** → kill-sequence: cancel all orders, liquidate
  everything, persist `KILLED`.
- Dashboard **EMERGENCY HARD STOP** talks directly to Alpaca,
  independent of the engine.
- Every position always has a stop and a target — resting on the exchange
  during regular hours, engine-enforced soft levels otherwise.
- **End-of-day flatten** — entries and bracket legs are DAY orders that
  expire at the (extended) close, so everything is closed
  `eod_flatten_minutes` (default 10) before it; no position is ever held
  overnight without a stop.
- **Too-quiet gate** — symbols whose ATR-scaled stop would collapse onto
  the percentage floor are skipped: a stop inside bar noise is a coin
  flip that loses the spread.
- **Universe hygiene** — leveraged/inverse ETPs are filtered out of the
  dynamic watchlist; their decay fights mean reversion.

### Nightly self-optimization (out-of-sample validated)

At midnight Europe/Zurich the optimizer replays the exact live strategy
(same indicator code, `indicators.py`) over 30 days of 1-minute bars
across a 192-combination grid. Bars are split chronologically — 75 %
train / 25 % validation. Candidates are ranked by yield-to-drawdown on
the train window, but only go live if they *also* made money on the
validation window they never saw. Parameters that merely memorized the
past are rejected, and the previous configuration is kept.

## Quick start

```bash
cp .env.example .env      # fill in your Alpaca paper keys
docker compose up -d      # pulls davidsteg/argus-* from Docker Hub
```

- Dashboard: http://localhost:8080
- Debug API (interactive docs): http://localhost:8000/docs

Images are published by CI whenever a version tag is pushed; update with
`docker compose pull && docker compose up -d`. Pin a specific release with
`ARGUS_VERSION=2.2.3` in `.env` (default `latest`). To develop against
local source instead:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

## Dashboard (port 8080)

Four tabs, refreshed live (interval adjustable in Settings):

- **Overview** — equity curve (1H/1D/1W/1M/ALL), open positions with
  live price and unrealized PnL plus a per-position close button, market
  regime, engine cycle trace, active cooldowns, current strategy
  parameters, recent activity.
- **Trades** — all-time analytics (realized PnL, win rate, profit
  factor, avg win/loss, best/worst), cumulative realized-PnL curve, and
  a paginated trade-history grid.
- **Settings** — edit strategy parameters (bounds-checked; the nightly
  optimizer re-tunes and overwrites them), run the optimizer on demand,
  view the read-only operational environment, tune dashboard refresh.
- **Logs** — filterable log terminal: level chips, text search, row
  count.

The header shows regime / market-session / engine-heartbeat chips, total
balance, daily PnL and the EMERGENCY HARD STOP (with confirmation). When
the bot is KILLED, a RESUME button appears that restarts the engine via
the backend API.

## Debug & operations API (port 8000)

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | liveness: process, database, Alpaca connectivity |
| `GET /status` | bot status, equity, daily PnL, engine task state |
| `GET /config` | live strategy parameters + last optimization time |
| `GET /debug` | engine internals: env, last cycle trace, cooldowns |
| `GET /regime` | current market regime (SPY trend + realized vol) |
| `GET /signals` | dry-run of the full decision pipeline per symbol |
| `GET /positions` `/trades` `/logs` `/version` | state snapshots |
| `GET /vetoes` | shadow ledger of gate-blocked signals + per-gate hypothetical P&L |
| `GET /optimizer/status` | live progress of a running grid search (phase, combinations, elapsed) |
| `POST /optimize` | start the walk-forward grid search in the background |
| `POST /kill` | emergency kill-sequence |
| `POST /reset` | recover from KILLED and restart the engine |
| `GET /analyst/activity` | every LLM call with model, latency, outcome + per-agent 24h stats |
| `GET /analyst/reviews` | history of past trade/optimization/watchlist review verdicts |
| `GET /analyst/config` `/optimization` `/trades` `/memory` | analyst state snapshots |
| `POST /analyst/review` `/toggle` `/config` `/extract-lessons` | analyst actions |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | — | Alpaca paper credentials (required) |
| `ANTHROPIC_API_KEY` | — | enables Claude-scored news sentiment |
| `SENTIMENT_MODEL` | `claude-opus-4-8` | sentiment model (`claude-haiku-4-5` = cheapest) |
| `TRADING_SYMBOLS` | `ALL` | `ALL` = dynamic most-actives watchlist, or `AAPL,MSFT,…` |
| `REGIME_MAX_ANN_VOL` | `35` | annualized SPY vol (%) above which the tape is stressed |
| `BACKEND_API_URL` | `http://trading_backend:8000` | how the dashboard reaches the debug API for actions |

Operational environment (`position_size_usd`, `risk_per_trade_usd`,
`max_positions`, `daily_stop_loss`, `min_price_usd`, `cooldown_minutes`,
`poll_interval_seconds`, `bar_lookback_minutes`, `watchlist_size`,
`eod_flatten_minutes`) is now tunable from the dashboard's
**Settings → Operational Environment** card — no longer env vars.
Changes take effect on the next engine cycle. Backtest friction is tunable
via `OPTIMIZER_COST_PCT` (round-trip, default 0.10) and
`OPTIMIZER_STOP_SLIP_PCT` (stop slippage, default 0.05).

Strategy parameters (`rsi_period`, `rsi_buy_signal`, `rsi_exit_signal`,
`atr_stop_mult`, `atr_target_mult`, `news_cutoff`) live in the shared
`bot_config` table and are rewritten nightly by the optimizer — not env vars.

## Project structure

```
backend/
  bot.py          # async trading engine + engine controller
  api.py          # debug & operations FastAPI app (port 8000)
  optimizer.py    # nightly grid search with out-of-sample validation
  indicators.py   # shared RSI / ATR / VWAP + bracket math (live == backtest)
  regime.py       # SPY market-regime filter (RISK_ON / CAUTION / RISK_OFF)
  sentiment.py    # layered news sentiment (Claude → keywords → neutral)
  universe.py     # static list or dynamic most-actives watchlist
frontend/
  app.py          # NiceGUI dashboard (port 8080)
shared/
  database.py     # thread-safe SQLite layer (WAL) on a shared volume
  version.py      # version + release notes (rendered in the dashboard)
```

## Security notes

- `.env` is git-ignored — commit `.env.example` only, never credentials.
- Paper trading only; keys should be Alpaca *paper* keys.
- The debug API has no auth — do not expose port 8000 publicly.

## Disclaimer

Educational prototype. Trading involves risk; past performance does not
guarantee future results.
