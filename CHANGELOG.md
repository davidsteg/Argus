# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Release notes are also maintained in code at `shared/version.py` — the
dashboard shows them via the version chip in the header, and the backend
serves them at `GET /version`. Keep both in sync.

## [v2.27.2] - 2026-07-14

### Fixed
- **Crypto dust is not a position.** Alpaca charges crypto fees in the base
  asset and the engine floors close quantities to 8 dp (v2.26.0), so ~1e-9
  coin residues survive every real close. Treated as positions, they (a)
  held `max_positions` slots hostage — the risk agent rejected a real PEPE
  entry because two of the "3 held crypto positions" were 2e-9 YFI and 9e-9
  AAVE; (b) kept the protection watchdog busy attaching stops to
  unmanageable balances; (c) produced the `-$0.00` / `-2.33%` rows in Trade
  History (dust qty × real price move); and (d) — the real bug — swallowed
  real trade records: `sync_portfolio` only reconciles a close once the
  symbol *leaves* the account, and dust kept it alive, so the real AAVE
  stop-out of Jul 13 (≈ -$27) and the YFI close of Jul 14 (≈ -$5) were never
  ledgered; after the next restart their history rows show the dust close
  instead. New `market.is_dust` (below the asset's `min_order_size`;
  $0.01-notional fallback) filters dust out of the portfolio snapshot before
  slots, watchdog, `live_symbols`, or the dashboard see it, and
  `_sweep_dust` liquidates it via `close_position` once per session —
  logged, but never recorded as a trade. The missing AAVE/YFI ledger rows
  cannot be backfilled retroactively; full account-activities reconciliation
  remains a backlog item.

## [v2.27.1] - 2026-07-14

### Fixed
- **One flaky LLM response no longer defeats a gate or a review.**
  `_call_llm` now makes one automatic retry before raising: the crypto
  engine's risk agent / portfolio manager failed open 6× in the first 12 h of
  v2.26.0 on one-off `blank content (finish_reason=stop)` replies from
  Ollama, and the watchlist curator lost two runs to truncated JSON. When the
  failure signature is a spent reasoning budget (`finish_reason=length` or a
  non-JSON/cut-off body), the retry doubles the response budget (capped at
  16384). Each attempt is recorded in the llm_log individually; a failure of
  the final attempt raises exactly as before, so fail-open accounting
  (`analyst_health`) keeps meaning what it meant.
- **Watchlist curator response budget 4096 → 8192.** Its answer is a
  ~50-symbol JSON list, but reasoning models (deepseek-r1) burn most of the
  budget thinking before emitting it — 4096 produced `LLM returned non-JSON
  response` failures that were actually truncation (Jul 13 23:00, Jul 14
  08:00). Non-JSON errors now include the `finish_reason` so truncation and
  disobedience are distinguishable in the log.

## [v2.27.0] - 2026-07-14

### Fixed
- **Nightly optimization was being nullified by its own reviewer.** The
  post-optimization LLM review compares the validated winner against the top
  train-window candidates — but the winner dict had `news_cutoff`,
  `analyst_enabled` and `short_enabled` merged in *before* the review (they
  are carried forward into the config write), while candidates carry grid
  keys only. The reviewer read the extra keys as a "structural mismatch
  between grid search and validation configuration" and rejected the run —
  e.g. the entire Jul 13 nightly (1h48m of compute) produced nothing. The
  carry-forward merge now happens after the review, and
  `analyst.review_optimization` additionally filters the winner to the
  candidates' keys (belt and braces).

### Added
- **Fill-calibrated stop slippage.** Each optimizer run now measures realized
  stop slippage from the ledger's own stop exits (median over up to 200
  recent trades, minimum 8 samples, clamped to
  `[OPTIMIZER_STOP_SLIP_PCT, 2%]` — calibration may make the backtest more
  honest, never more optimistic) and uses it in every simulated fill instead
  of the raw env guess. The calibrated value is published in the
  `optimizer_friction` state blob and logged per run; the shadow-veto
  resolver reads the same blob (fallback: env defaults, e.g. on the crypto
  engine, which runs no optimizer) so both fill models stay identical.
- **Multi-fold out-of-sample validation.** The 25 % holdout is now cut into
  `OPTIMIZER_VALIDATION_FOLDS` (default 3) sequential folds; a candidate is
  only promoted if profitable in a majority of folds AND in aggregate.
  Fold-by-fold returns are recorded in the run detail and the OPTIMIZER log
  line.
- **Wider training data within the runtime budget.** Defaults raised from 30
  days × 10 symbols to 60 days × 15 symbols. Measured baseline: ~108 min at
  30 × 10, scaling linearly with days × symbols → ~5.5 h at the new
  defaults, still inside the 22:00 UTC → 08:00 UTC overnight window. The
  optimizer env knobs are now documented in `.env.example`.

## [v2.26.0] - 2026-07-13

### Added
- **Regime gate on long entries, shadow-tracked.** New BUY entries are blocked
  whenever the regime proxy (SPY / BTC-USD) trades below its EMA — not just in
  the stressed `TREND_DOWN` regime. Motivation: on Jul 13 SPY drifted below its
  EMA on quiet volatility (`CAUTION`) for the whole session and 23 of 28
  dip-buys stopped out (-$54.45); an orderly fall knifes a dip-buyer just as
  surely as a violent one. Every blocked BUY is recorded in the shadow-veto
  ledger under a new `regime` gate (with the exact bracket and size it would
  have traded), so `GET /vetoes` and the Analyst tab measure what the gate
  saves or costs instead of assuming. The engine no longer short-circuits the
  cycle on a long-blocking regime — signals are still evaluated so the ledger
  fills. `GET /regime` now also reports `blocks_long_entries`; the `/signals`
  dry-run uses the same rule. Shorts remain allowed; `CAUTION` still halves the
  position cap; `UNKNOWN` still fails open.
- **Position-protection watchdog.** Every cycle, every held position must have
  working protection, closing the two holes that let VRAX (-$101.46, equity)
  and XTZ (-$16.96, crypto) run naked:
  - Stop/target levels now **survive engine restarts**: adoption of an
    existing position first restores its levels and entry context from the
    `open_entries` state blob the engine already persists each cycle.
  - Positions still tracked without levels get ATR-scaled stop/target
    **attached at the current price** (protecting from here, not locking in
    the past drawdown); if no usable price/ATR is available for 3 consecutive
    cycles the position is closed — unmanageable is worse than closed.
  - Positions marked `native_bracket` are verified against actually-resting
    exchange orders (one batched lookup); if the legs are gone, soft
    enforcement is re-armed instead of assuming the exchange still covers
    them.
- **Protection incidents are now visible.** New `protection_health` state blob
  (session-reset like `analyst_health`, exposed on `GET /debug`): active
  close-failure streaks per symbol, levels attached, forced/protective closes,
  last event. The dashboard's Active Positions card shows a red banner when a
  stop cannot execute (close rejected ≥3×) or the watchdog intervened.

### Fixed
- **Crypto closes could be rejected forever (position stuck past its breached
  stop).** Alpaca charges crypto fees in the base asset, so a filled buy
  leaves a 9-decimal balance (e.g. AAVE `5.037726129`); the close order
  quantity used `round(qty, 8)`, which rounds half-up to `5.03772613` —
  one billionth more than held → error 40310000 "insufficient balance" →
  the soft-stop close retried and failed every 60 s cycle (AAVE sat 3.5 h and
  ~200 log lines past its breached $98.60 stop on Jul 13). Close and entry
  quantities are now floored to 8 dp, never rounded up.
- **Close failures now escalate instead of looping silently.** After 3
  consecutive failed limit closes the engine falls back to a full-position
  market close (`close_position`, no quantity → no precision rejections) where
  the market accepts one — crypto always, equities only in the regular
  session. This is a deliberate, narrow exception to the "limit closes only"
  rule: an unenforceable stop is strictly worse than paying a market order's
  spread. Repeated failures no longer flood the log (one `ERROR` line per
  streak start plus every 10th attempt, and the `SOFT STOP` trade line only
  logs when the close was actually submitted).

## [v2.25.1] - 2026-07-11

### Fixed
- **Optimizer tab on phones.** The run-history header row (timestamp, trigger
  badge, duration, outcome chip, analyst verdict, detail) was `flex-nowrap`
  with rigid chips, so real runs — e.g. `NIGHTLY · 2h 19m · REJECTED_ANALYST ·
  analyst: reject` — were wider than the screen and dragged the whole panel
  into sideways scrolling. The chips now wrap, and the detail text moves to
  its own truncated full-width line on small screens (tooltip still shows the
  full text). Desktop keeps the single-line layout.

## [v2.25.0] - 2026-07-11

### Changed
- **Mobile-first dashboard.** On screens ≤640 px the six tabs move into a
  fixed bottom navigation bar (thumb-reach, safe-area aware, frosted-glass
  backdrop) and the header collapses to a compact banner: subtitle hidden,
  smaller balance/PnL/status figures, and icon-only RESUME / EMERGENCY HARD
  STOP buttons with tooltips. The header is no longer sticky on phones —
  the bottom nav owns navigation, so content gets the full viewport.
- **No more sideways table scrolling on phones.** Active Positions collapses
  to Symbol / Side / PnL + action buttons, Trade History to Closed / Symbol /
  PnL / PnL% + ℹ; the hidden columns (qty, entry, exit, value, hold time)
  remain one ℹ tap away in the existing info popup. Desktop layouts are
  unchanged.

### Added
- **Home-screen app support.** `theme-color`, `mobile-web-app-capable` /
  `apple-mobile-web-app-capable` and `viewport-fit=cover` meta tags — added
  to the home screen, the dashboard now launches standalone with dark chrome
  and respects notch safe areas.
- **Visual polish.** Brand-gold active-tab indicator matching the Argus mark,
  hover highlight on position/trade rows, slim dark scrollbars, and a live
  browser-tab title (`▲ +$123.45 · Argus`) so today's PnL is readable from a
  pinned tab.

## [v2.24.0] - 2026-07-11

### Added
- **Shadow-tracking of vetoed signals.** Every signal a gate blocks — news
  sentiment, VWAP re-check, LLM risk agent, LLM portfolio manager — is
  recorded in a new `vetoed_signals` table with the exact bracket and share
  count the engine would have traded (deduped per symbol+gate within the
  cooldown window). A background resolver replays them against minute bars
  every ~10 minutes: pessimistic first-touch stop/target, the optimizer's
  friction model, end-of-day close for equities, 24 h timeout for crypto. The
  per-gate hypothetical P&L is served at `GET /vetoes` and rendered as a
  "Shadow-tracked vetoes" card on the Analyst tab — the gates were previously
  running blind, with no evidence whether their vetoes save money or cost it.
- **Fail-open visibility.** When the risk agent or portfolio manager is
  unreachable, signals auto-approve (availability over gating) — now counted
  per engine session in the `analyst_health` state blob, exposed via
  `GET /debug`, and shown as an amber banner on the Analyst tab with the last
  error. Previously the bot could run un-gated for days with only per-signal
  log lines as evidence.

### Changed
- **The post-optimization LLM review is a binary accept/reject sanity check.**
  The retired "override" action let the model install any rank from the
  train-window list — mostly combinations that failed (or never saw) the
  out-of-sample validation gate, shown to the model with train stats only.
  That was in-sample parameter selection by the least-validated component,
  overriding the most-validated one. An "override" (or any unknown action)
  from the model now degrades to accept with a warning log. The reviewer also
  receives the winner's out-of-sample validation stats, making the
  overfitting call it's asked to make informed rather than blind.
- **Simulated-fill friction constants moved to `indicators.py`**
  (`COST_PER_TRADE_PCT`, `STOP_SLIPPAGE_PCT`, still env-tunable via
  `OPTIMIZER_COST_PCT` / `OPTIMIZER_STOP_SLIP_PCT`) so the optimizer backtest
  and the shadow-veto resolver can never drift onto different fill models.

## [v2.23.0] - 2026-07-11

### Changed
- **Regular-session equity entries use native exchange-side brackets.** The
  entry is submitted as an Alpaca bracket order: an OCO take-profit limit and
  stop-market leg rest on the exchange, so a breached level fills immediately
  instead of waiting out the engine's 60-second poll. The Jul 8–10 trade review
  showed polled soft stops filling 2–4× past their level on thin movers (NVVE:
  $16 designed risk, $61.54 realized loss); 13 such exits accounted for
  -$145.73 of -$152.94 total. Outside regular hours and for crypto — where
  Alpaca rejects bracket orders — entries keep the existing marketable-limit +
  soft-level path.
- **Soft stop/target checks are quote-first and side-aware.** The check now
  prices off the live quote's exit side (the bid a long sells into, the ask a
  short buys back), falling back to the 1-minute bar close, the latest trade,
  then the position's last synced price. Bar closes can be tens of seconds
  stale on a thin book, which is how breaches were detected far past the level.
- **Bracket legs are DAY orders that expire at the regular close.**
  `evaluate_and_close_stops` skips natively-bracketed positions only while the
  regular session is open; after the close it automatically resumes soft
  enforcement for them. Signal exits and the EOD flatten cancel resting legs
  before closing (existing `_cancel_symbol_orders` path), so no orphaned leg
  can double-sell.
- **Trade records and the position info popup carry a `native_bracket` flag**
  and the entry rationale now states whether the stop/target rest on the
  exchange or are enforced as soft levels each cycle.

## [v2.22.0] - 2026-07-10

### Added
- **Per-position info popup on the Active Positions card.** Every row in
  Active Positions now has an ℹ button that opens the same info popup used by
  Trade History — the entry rationale, entry signal numbers (RSI, VWAP, ATR,
  news sentiment), execution (qty, entry price, now, market value), and a
  price chart from entry to now with the entry marker, dashed take-profit /
  stop-loss lines, and a faint band over the hold window.
- **Soft stop / target section** in the popup shows the polled levels the bot
  is enforcing each cycle alongside the current price, so you can see how
  close a live position is to its stop or target at a glance.

### Changed
- **Backend publishes `_open_entries` to the shared DB** every cycle so the
  frontend can render entry metadata for live positions. Internal guard fields
  (prefixed with `_`) are stripped. Positions adopted from outside the engine
  (restart, manual fill) have no entry metadata; their ℹ button is disabled.

## [v2.21.0] - 2026-07-10

### Added
- **Live optimizer progress in the dashboard.** The Optimizer tab now shows
  real-time progress while a grid search is running — phase (fetching / grid
  search / validation / analyst / writing), a progress bar with combinations
  evaluated vs total, candidate count, and elapsed time — instead of a frozen
  spinner that gave no signal the process was alive. The status is polled every
  refresh cycle (2s default) from the `optimizer_status` runtime_state blob.

### Changed
- **`POST /optimize` is now fire-and-forget.** It launches the grid search in
  a background thread and returns immediately, so the dashboard can poll
  `GET /optimizer/status` for live progress. Returns 409 if a run is already
  in progress, preventing overlapping runs. `run_optimization` publishes
  phase/evaluated/candidates to runtime_state at each boundary (every 500
  combinations during the grid search, every 50 during validation) and clears
  it to idle in the `finally` block.

## [v2.20.3] - 2026-07-10

### Fixed
- **Trade info chart: SIP data feed rejected by paper subscription.** The
  per-trade info popup's price chart failed for equity trades with
  "subscription does not permit querying recent SIP data" because
  `fetch_trade_bars` didn't specify a data feed, defaulting to SIP which the
  paper trading subscription doesn't include. `StockBarsRequest` now passes
  `feed=DataFeed.IEX`, the free feed available on every Alpaca paper account.

## [v2.20.2] - 2026-07-10

### Fixed
- **VWAP gate passed on stale bar data, then entry re-priced off the live
  quote.** `evaluate_signal` checked `latest_close > vwap` using the last
  1-minute bar close, but `place_limit_buy` re-prices the entry off the live
  ask (`_entry_reference_price`) immediately before submission. On a volatile
  symbol the ask can be several percent above the bar close by the time the
  order is built, so a dip that passed the gate can already be above VWAP at
  the actual entry. The 2026-07-10 loss passed the gate at $62.50 bar close
  (below VWAP $62.60), then re-priced to $66.41 ask — 6% above VWAP — and
  stopped out instantly when price reverted to $62.12. New
  `_vwap_revalidate` re-checks the same VWAP direction and
  `max_vwap_dislocation_pct` gates against the live-repriced price in both
  `place_limit_buy` and `place_limit_short`, aborting the entry with a
  diagnostic log when the dip/overextension has reverted or turned into a
  knife/squeeze between the gate and the order.
- **`entry_reason` was a hardcoded lie.** The per-trade rationale template
  always said "sat below VWAP" for BUY signals and "sat above VWAP" for SELL
  signals, regardless of the actual re-priced price's relationship to VWAP.
  It now reports the actual percentage the live ask/bid sits below/above VWAP
  and includes the bar-close dislocation that first triggered the gate, so
  the rationale reflects the real entry.

## [v2.20.1] - 2026-07-10

### Fixed
- **Equity engine mis-killed on the crypto engine's position value.** v2.20.0
  let the crypto engine hold real positions for the first time, exposing a
  latent bug in the per-market equity isolation: `EquityAdapter.compute_equity`
  subtracted the crypto positions' full *market value* from the shared account
  equity, but the cash that bought them had already left the shared pool — so it
  double-counted the cost basis. A ~$220 crypto position read as a ~$220
  equity-engine loss, tripped the $100 daily stop, and the engine re-killed on
  every reset. `compute_equity` now subtracts only the crypto positions'
  *unrealized* P&L. (Residual: a crypto trade's realized P&L briefly touches the
  figure until the next daily anchor — dollars, not hundreds.)

## [v2.20.0] - 2026-07-10

### Fixed
- **Crypto engine fabricated phantom trades.** Alpaca returns crypto *positions*
  slashless (`PAXGUSD`) but orders, the universe and our own signals use the
  slashed form (`PAXG/USD`). `owns_symbol` tested for a bare `/`, so the crypto
  engine could not see its own filled positions and the equity engine adopted
  them (its trades table filled with `AAVEUSD`/`PAXGUSD` rows). Ownership now
  recognises both forms via a shared `_is_crypto_symbol` helper, and positions
  are normalised to the slashed symbol so tracking keys and close orders match.
- **Unfilled entry orders were recorded as closed trades.** `sync_portfolio`
  treated any tracked symbol with no live position as "closed", so an entry
  order that never filled produced a fake trade (10 identical +$0.55 PAXG/USD
  wins in 12 minutes) *and* left the GTC order resting, stacking a fresh
  duplicate every cycle. Reconciliation now records a trade only for positions
  that actually opened (tracked via the entry order id / a confirmed-live flag)
  and cancels unfilled entry orders, benching the symbol briefly.
- **Marketable limits priced off a stale last trade.** Entries and closes now
  price off the live quote (ask to buy, bid to sell) rather than the last
  trade, which could be minutes old on a thin book and never cross.
- **Sub-dollar bracket rounding.** The soft stop/target used a flat
  `round(2)` / 2¢ floor, which on a $0.056 pair produced a −29% / +42% bracket.
  Levels are now rounded to the market's own price increment via the adapter.

### Changed
- Crypto sentiment no longer 404s on an empty configured model string (falls
  through to the main model), and dashboard analyst-config changes reload the
  sentiment client without a restart.
- LLM watchlist curation and the equity opportunity screener are skipped on the
  crypto engine (fixed USD-pair universe); `POST /optimize` is rejected on the
  crypto engine (its backtester replays equity bars).
- The `/signals` dry-run now replays the floored-stop and VWAP-dislocation
  gates, and the nightly backtest benches after every close (not just losses)
  to match the live engine.

## [v2.19.3] - 2026-07-10

### Fixed
- **Trade info chart: SIP data feed rejected by paper subscription.** The
  per-trade info popup's price chart failed for equity trades with
  "subscription does not permit querying recent SIP data" because
  `fetch_trade_bars` didn't specify a data feed, defaulting to SIP which the
  paper trading subscription doesn't include. `StockBarsRequest` now passes
  `feed=DataFeed.IEX`, the free feed available on every Alpaca paper account.

## [v2.19.2] - 2026-07-10

### Fixed
- **Trade info chart: stale markers bleeding between trades (real fix).** The
  v2.18.1 fix (setting `markLine=None`, `markArea=None`) didn't work because
  NiceGUI's JSON serializer strips `None` values — they never reached the
  ECharts client, so the old markers survived the merge. The chart reset now
  calls `run_chart_method('setOption', options, ':true')` which invokes
  ECharts' `setOption` with `notMerge: true` on the client, completely
  replacing the chart state instead of merging into it.

## [v2.19.1] - 2026-07-10

### Fixed
- **Risk agent rejecting valid signals with wrong RSI threshold.** The risk
  agent's system prompt never told the LLM what the configured `rsi_buy_signal`
  threshold was, so the model defaulted to its own generic "RSI < 30 =
  oversold" rule and rejected every signal in the 30-35 range. The crypto
  engine's configured threshold is 35, so most valid BUY signals were blocked.
  `RISK_AGENT_PROMPT` now instructs the LLM to use the live thresholds from the
  signal data instead of its own defaults, and BUY/SELL signal dicts now carry
  all four RSI thresholds from live config through to `evaluate_signal_risk`.

## [v2.19.0] - 2026-07-10

### Added
- **Crypto dashboard: hide equity-only features that don't apply.** When the
  Crypto market is selected, the dashboard now hides features that are
  equity-only and have no crypto equivalent: the entire Optimizer tab (crypto
  has no nightly grid search), Short Selling and RSI short parameters (crypto
  is spot-only, long-only), EOD Flatten (crypto is 24/7), the Watchlist and
  Opportunity Screener cards (crypto universe is static USD pairs, not
  screener-based), the Watchlist Model input and Post-Optimization Review card
  in the Analyst tab, and the Screener Candidates card on the Overview tab.
  The Optimization Reviewer agent card is also hidden for crypto since it
  never fires without an optimizer run.
- The market chip tooltip now says "Crypto market — 24/7" instead of "US
  equity market session" when crypto is selected.
- Visibility updates on market switch and initial page load via a declarative
  `_track_visibility` / `_update_market_visibility` pattern — no conditional
  UI duplication.

## [v2.18.3] - 2026-07-10

### Fixed
- **Crypto churn: cooldown after every close, not just losses.** Cooldown
  only triggered on losing trades — a winning exit left the symbol immediately
  eligible for re-entry. On low-volatility crypto pairs (PAXG/USD being the
  live example), RSI oscillates around the buy/exit thresholds so the bot
  churned every minute: buy → RSI recovers → signal-exit for a tiny profit →
  no cooldown → re-buy next cycle, dozens of times in a row.
  `reconcile_closed_trade` now starts a cooldown on every close regardless of
  PnL sign.
- **Dashboard cross-market leak guard.** The positions and trade history
  render functions now filter by `market_owns` (crypto symbols carry a `/`,
  equities never do) before displaying, so a cross-market leak in the DB can
  never surface in the wrong market's tab.

## [v2.18.2] - 2026-07-10

### Fixed
- **Sentiment LLM blank-content error — missing reasoning fallback.** v2.4.10
  added a fallback to the non-standard `reasoning` field when Ollama Cloud
  DeepSeek models return empty `content`, but only applied it to the analyst
  module — `sentiment.py` was missed. Sentiment scoring for every symbol failed
  with "LLM returned blank content" and fell back to the keyword heuristic.
  `sentiment.py` now mirrors `analyst.py`'s response extraction: try `content`
  first, fall back to `reasoning`, and include `finish_reason` and `refusal` in
  the error message when both are empty.

## [v2.18.1] - 2026-07-10

### Fixed
- **Trade info chart: stale markers bleeding between trades.** The per-trade
  info popup's price chart showed entry/exit markers and the hold-window band
  from the previous trade overlaid on the new one. ECharts' `setOption` merges
  by default, and the chart reset (`options.clear` + `update`) didn't explicitly
  clear `markLine` and `markArea` — so they persisted from the prior trade. The
  reset now sets `markLine=None` and `markArea=None` on the series before calling
  `update()`, telling ECharts to remove those keys during the merge.

## [v2.18.0] - 2026-07-10

### Added
- **Optimizer Run History** — every optimizer run is now persisted as a
  structured row in a new `optimizer_runs` SQLite table: timestamp, trigger
  (nightly/manual), duration, outcome (`applied`/`no_change`/`rejected_validation`/
  `rejected_analyst`/`no_combination`/`no_data`/`error`), before→after parameter
  diff, train/validation stats, and analyst decision.
- **Optimizer tab** in the dashboard (after Analyst) with a Run History card
  showing every run with outcome chips, trigger badges, duration, train/val
  stats, and changed parameters as `key: before → after` lines.
- The Run optimizer now button moved from Settings to the Optimizer tab; a
  pointer remains in the Engine & Optimizer card.
- `run_optimization()` now always records exactly one run row via a
  `try/finally` block — even on early-return failure paths (no data, no
  combination, validation reject, analyst reject, error).

## [v2.17.4] - 2026-07-10

### Fixed
- **Equity engine's daily stop-loss and equity curve contaminated by crypto
  activity.** Both engines share one Alpaca account, and the equity engine's
  `compute_equity` returned the blended `account.equity` — so crypto PnL
  could trigger the equity engine's daily stop-loss and distort its equity
  curve. The equity engine now subtracts the market value of non-equity
  (crypto) positions from the blended account equity, isolating its risk
  checks and dashboard display from the crypto engine's activity — matching
  the crypto engine's existing per-market equity computation.

## [v2.17.3] - 2026-07-10

### Fixed
- **Crypto trade history: missing exit price, PnL, and price chart.** Crypto
  trades in the Trade History tab showed "—" for exit price, PnL, and PnL %
  because `reconcile_closed_trade` searched for the exit fill by side with
  `limit=10` — if the close order wasn't in the first 10 results (common for
  crypto pairs with many orders), the trade was permanently recorded with
  `exit_price=None` and `realized_pnl=None`.
- `_limit_close` now records the close order's ID on the entry tracking dict,
  and `reconcile_closed_trade` uses a direct `get_order_by_id` lookup — precise,
  no limit, no side ambiguity. Falls back to the old side-based search for
  adopted/legacy positions that have no tracked close order ID.
- The per-trade info popup's price chart also failed for crypto trades because
  `fetch_trade_bars` hardcoded `StockHistoricalDataClient` — it now detects
  crypto symbols (by the `/` in the symbol) and uses `CryptoHistoricalDataClient`
  + `CryptoBarsRequest` instead.

## [v2.17.2] - 2026-07-10

### Fixed
- **Crypto engine: hard-disable shorts — spot-only, can't borrow.** The crypto
  engine's `MARKET=crypto` default overrides didn't force `short_enabled=0`, so
  the optimizer or dashboard could flip it on. The engine then submitted SELL
  orders for crypto pairs, which Alpaca rejected with "insufficient balance"
  (spot-only — you can't short what you don't own). Two-layer fix: the crypto
  seed defaults now include `short_enabled=0.0`, and `get_config()` enforces it
  as a hard invariant when `MARKET=crypto` — same pattern as `paper=True` — so
  no code path (optimizer, dashboard, manual DB edit) can weaken it.

## [v2.17.1] - 2026-07-10

### Fixed
- **Diagnostic endpoints now report each engine's own regime proxy.** `/regime`,
  `/signals`, and the manual trade-review endpoint called the regime classifier
  directly (defaults to the SPY equity proxy), so on the crypto engine they
  showed `UNKNOWN — no SPY bars returned` instead of the BTC/USD regime. They
  now route through the engine's market adapter. Diagnostic-only — live trading
  always used the correct per-market regime (the crypto cycle's `/debug` already
  showed `TREND_UP` from BTC/USD).

## [v2.17.0] - 2026-07-10

### Added
- **Crypto trading — a second 24/7 engine alongside equities.** A new engine
  (`MARKET=crypto`) trades all Alpaca-supported USD pairs spot, long-only, 24/7,
  using the same RSI/VWAP dip strategy and polled soft stop/target as equities.
  It runs as an independent container with its own SQLite DB
  (`argus_crypto.db`), so the live equities engine is untouched.
- **One dashboard, market switcher.** A header Equities ⇄ Crypto toggle points
  every view, setting, and action (hard-stop, close, resume, optimize) at the
  selected engine's DB/API.
- New `backend/market.py` `MarketAdapter` (Equity/Crypto) encapsulates every
  asset-class seam: data client, universe, session hours, order construction
  (crypto = GTC limits + fractional sizing; equities = extended-hours DAY
  limits), regime proxy (BTC/USD for crypto), position partitioning, and
  per-market equity. Strategy/orchestration stay single-sourced.
- `docker-compose.yml` gains a `trading_crypto_backend` service; the frontend
  mounts both DBs and learns `CRYPTO_DB_PATH` / `CRYPTO_BACKEND_API_URL`.

### Notes
- Both engines share one Alpaca account, so each keeps only its own asset
  class's positions/orders and computes its own equity (crypto: a notional base
  + own realized/unrealized PnL), keeping the two daily-stops independent.
- Crypto has no scheduled end-of-day flatten (24/7) and no nightly optimizer in
  v1 (the backtest is equity-bar based) — it runs on static/default parameters.

## [v2.16.1] - 2026-07-10

### Fixed
- **Hotfix — the extended-hours session clock crashed every cycle.** v2.16.0
  aborted every trading cycle with `combine() argument 2 must be datetime.time,
  not datetime.datetime`; the engine stayed `RUNNING` but placed **zero orders**.
  The new session-window code assumed Alpaca's calendar `close` field was a
  `datetime.time`, but this alpaca-py version returns a `datetime.datetime`, so
  `datetime.combine` raised before any signal was evaluated. The regular-close
  field is now normalised to an ET wall-clock time-of-day whether Alpaca returns
  a `time`, a naive `datetime`, or a tz-aware `datetime`, so the 4:00 AM / 8:00
  PM ET (and half-day) bounds compute correctly regardless of the field's type.

## [v2.16.0] - 2026-07-10

### Changed
- **Extended-hours trading — the engine now trades the full 4:00 AM – 8:00 PM ET
  session** (pre-market + regular + after-hours) instead of only the 9:30 AM –
  4:00 PM regular session. The window is derived from Alpaca's trading calendar,
  so holidays and half-days (extended close pulls back to 5:00 PM) are handled
  automatically.
- **Entries and exits are now marketable extended-hours limit orders.** Alpaca
  forbids bracket and market orders outside regular hours, so the exchange-side
  OCO bracket is replaced by a **soft stop/target** the engine enforces every
  poll cycle — a held position is closed when price crosses the recorded stop or
  target level. Limit prices are set `entry_slip_pct` / `exit_slip_pct` through
  the last trade so they fill in a thin book.
- EOD flatten (now timed to 8:00 PM ET), signal exits, and the emergency kill
  sequence all use extended-hours limit closes instead of market liquidation.
- **Cleaner entry/exit markers on the per-trade info chart** — slim vertical
  lines at the entry (blue) and exit (green on a win, red on a loss) timestamps
  labelled `IN` / `OUT`, plus a faint shaded band over the held window, instead
  of the teardrop pins that floated over and covered the price line.

### Added
- `entry_slip_pct` (0.001) and `exit_slip_pct` (0.002) strategy parameters
  controlling how aggressively limit orders are priced to guarantee fills;
  shown in the operational environment.

### Risk note
- Soft stops are polled at `poll_interval_seconds` (default 60s), so price can
  gap through a level between checks — there is no resting exchange-side stop
  anymore. The daily-loss kill and the end-of-day flatten remain the backstops.

## [v2.15.0] - 2026-07-10

### Added
- **Per-trade info popup — see exactly why the bot took each trade.** Every row
  in the Trade History carries an ℹ button that opens a dialog explaining the
  trade end-to-end: a plain-English rationale, the entry signal numbers (RSI,
  VWAP, ATR, news sentiment), the execution (qty, entry/exit price, target and
  stop), and how it closed (signal exit, take-profit leg, or stop-loss leg).
- The popup draws a **1-minute price chart of the exact hold window**, fetched
  live from Alpaca, with pinned entry/exit markers and dashed take-profit /
  stop-loss lines so the decision can be read against what price actually did.
- The decision snapshot is captured at order time and persisted on the trade:
  nine new nullable columns on the `trades` table (`entry_rsi`, `entry_vwap`,
  `entry_atr`, `entry_sentiment`, `sentiment_source`, `stop_loss`,
  `take_profit`, `entry_reason`, `exit_reason`), added by migration. Trades
  recorded before this release simply carry `NULL`s and the popup says so.

### Changed
- **Trade History rebuilt as a native card grid** instead of the embedded
  data-grid that overflowed the card. It now fits the layout, scrolls sideways
  cleanly on mobile, and matches the Active Positions styling.

## [v2.14.0] - 2026-07-10

### Added
- **Falling-knife gate: skip RSI-oversold entries that sit too far past VWAP.**
  Post-mortem of the 2026-07-09 VRAX loss — the day's worst trade by 8×. The
  engine bought VRAX at `$6.95` while session VWAP was `$9.17` (24% below fair
  value, ~10× ATR) on an RSI 25.9 "dip". It was a collapse, not a dip: RSI kept
  bleeding to 13 and the bracket stopped out for `−$23.40`. The existing VWAP
  gate only checked *direction* (price below VWAP = "dip") with no floor on
  *depth*, so a 1%-below-VWAP dip and a 24%-below-VWAP crash passed identically.
- New `max_vwap_dislocation_pct` strategy parameter (default `0.15`): a long is
  skipped when price sits more than this fraction below VWAP, and — mirrored — a
  short is skipped when price sits more than this fraction above VWAP (a
  parabolic squeeze). Editable from Settings → Strategy; the gate runs before
  the sentiment LLM call, so a falling knife costs no tokens.
- Modelled in the optimizer backtest and added to the nightly grid as
  `[0.08, 0.15, 999.0]` — `999.0` disables the gate, so the walk-forward
  out-of-sample validation step can drop it entirely if it fails to earn its
  keep rather than overfitting to a single trade.

## [v2.13.3] - 2026-07-09

### Fixed
- **Risk agent and portfolio manager get enough token budget to stop
  truncating.** These two agents run in the live order-placing path and both
  fail *open* (auto-approve) on any error — yet they were capped at
  `max_tokens=1024`, tighter than every other agent's 2048+ default.
  `deepseek-v4-flash`'s response was observed getting hard-cut mid-JSON
  (`..."warnings": ["RSI not deeply oversold", "Caution` — then nothing),
  which the parser correctly rejected as invalid JSON — silently letting the
  signal through the risk agent instead of the reject it was actually
  generating. Both bumped to `max_tokens=2048` to match the module default.

## [v2.13.2] - 2026-07-09

### Fixed
- **Hotfix — the risk agent crashed the whole trading cycle whenever an open
  position was in the recent-trades window.** Every cycle that produced a
  signal died with `'<' not supported between instances of 'NoneType' and
  'int'`; the engine stayed `RUNNING` but placed **zero orders** (equity flat)
  until restarted. The risk agent's recent-loss lookup used
  `t.get("realized_pnl", 0) < 0`, but `.get(..., 0)` only substitutes the
  default when the *key is absent* — for an open position the key exists with
  value `None`, so it returned `None` and `None < 0` threw. The bug has been
  latent since v2.5.0; it fired now because the 2026-07-09 hyperactive
  `RSI(7)` optimizer episode left an open, `None`-P&L position in the last 50
  trades. Fixed with `(t.get("realized_pnl") or 0) < 0`, which handles both a
  missing key and an explicit `None`; closed trades are unaffected.

## [v2.13.1] - 2026-07-09

### Fixed
- **Optimizer backtest sizes trades like the live engine — no more fantasy
  returns.** The nightly grid search kept promoting the most hyperactive
  parameter set on absurd backtest numbers (the 2026-07-09 run reported
  **+2459% train / +117% validation** and put `RSI(7) buy<35` live). Root
  cause: `backtest()` compounded *full notional* on every trade
  (`equity *= exit_price / entry_price`), so a ~0.2 % average per-trade edge
  over ~1500 trades compounded into +2459 % — a figure the v2.12.0 trading
  costs were far too small to offset, and the grid therefore always favoured
  whatever configuration simply traded the most (shortest RSI period, loosest
  thresholds).
- `backtest()` now sizes each trade in **whole shares exactly as the live
  engine** (new `optimizer.position_qty` mirrors `bot.py`'s
  `place_bracket` sizing: a roughly constant `risk_per_trade_usd /
  stop_distance`, capped at `position_size_usd` notional, and **skipped when
  not even one share fits**) and accrues P&L in dollars against a fixed
  account-equity base. Because position size is driven by the constant
  `risk_per_trade_usd` and never scales with accumulated equity, returns are
  additive and realistic instead of exponential. Verified: the same 199
  winning trades that reported ~9.8×10¹¹ under full-notional compounding now
  report ~5.9 % (≈ 199 × $29.80 on a $100k base).
- Position sizing (`position_size_usd`, `risk_per_trade_usd`) and the account
  equity base are read from live config/status inside `run_optimization`, so
  the backtest and the engine can never diverge on how a trade is sized.
- The backtest's post-loss cooldown now benches after a genuine loss on
  **either** side (exit below entry for a long, above entry for a short),
  matching the live engine (`bot.py` benches whenever `realized_pnl < 0`).
  The old side-agnostic `exit_price < entry_price` check benched winning
  shorts and let losing shorts re-enter immediately.

## [v2.13.0] - 2026-07-09

### Added
- **LLM Agents card** (Analyst tab) — all seven LLM call types (risk agent,
  portfolio manager, sentiment scorer, watchlist curator, trade reviewer,
  optimization reviewer, decision memory) are shown with a plain-language
  description of what each one does, when it runs, which model serves it
  (resolving the per-agent overrides), and live 24h health: call count,
  error count, average latency and time of the last call. A red status dot
  with the error text on hover marks an agent whose last call failed.
- **Review History card** (Analyst tab) — trade, optimization and watchlist
  reviews now append to a bounded history (`analyst_review_history`, last
  40) instead of only overwriting the "latest" blob. Shown as a timeline
  with per-review confidence, warning count, summary, and the optimizer
  accept / override / reject decision chip.
- **LLM Call Log card** (Analyst tab) — the raw last 25 LLM calls with
  timestamp, agent, model, latency and error text, so "is the analyst
  actually working?" is answerable at a glance.
- **LLM call recording** — every LLM call in the system, including
  sentiment scoring, is recorded to the shared DB via the new
  `backend/llm_log.py` (rolling 400 entries in `runtime_state`): agent,
  model, latency, success/error, request/response size.
- **New debug endpoints** — `GET /analyst/activity` (call log + per-agent
  24h aggregates) and `GET /analyst/reviews` (review history).
- **Mobile-friendly dashboard** — the header (logo / condition chips /
  balance-PnL-status / emergency button), the tab bar, every two-column
  card layout, and all Settings and Analyst forms now collapse to a single
  column below the `md` breakpoint, so nothing overflows horizontally on a
  phone. Fixed-width inputs become full-width on small screens; the active
  positions grid scrolls sideways inside its own card instead of squashing
  its eight columns into unreadable slivers.

### Fixed
- **Silent agent failures are now visible** — a failed risk-agent call
  auto-approves the signal and a failed portfolio-manager call passes
  signals through unranked; both previously vanished into container stdout.
  They now also write WARNING entries to the DB log shown in the Logs tab.

## [v2.12.0] - 2026-07-08

### Fixed
- **Short-side contract repaired** — the risk agent, portfolio manager,
  watchlist curator and sentiment LLM prompts still described a *long-only*
  strategy after v2.9.0 enabled shorts, so every valid SELL signal was
  rejected as "overbought, contradicting mean-reversion entry criteria"
  (~80 rejections on 2026-07-08 alone). All prompts now describe the
  two-sided strategy, and the risk agent / portfolio manager receive each
  signal's `side`.
- **Symmetric short sentiment gate** — shorts now require sentiment below
  `1 − news_cutoff` (the mirror of the long gate) instead of below
  `news_cutoff`; a no-news 0.5 score no longer makes shorting effectively
  impossible.
- **Short exit reconciliation** — the trade recorder matched the first
  closed SELL order as the exit fill; for a short position that is its own
  entry order, so every covered short would have recorded ~zero PnL. Covers
  now match the opposite side (BUY) of the entry.
- Legacy trade records with side `LONG` are normalized to `BUY` at DB init.

### Added
- **Too-quiet gate** — signals whose stop would be set by the 0.35 %
  percentage floor rather than ATR are skipped (engine and backtest via the
  shared `indicators.stop_is_floored`). Floor-tight brackets inside ordinary
  bar noise produced most of the 2026-07-08 losses.
- **Leveraged/inverse ETP filter** — 2x/3x, bull/bear, Direxion and
  ProShares Ultra/Short products are excluded from the dynamic watchlist and
  the screener pool by asset name (cached bulk metadata, fail-open).
- **Backtest trading costs** — the optimizer now charges 0.10 % round-trip
  per trade plus 0.05 % adverse slippage on stop fills
  (`OPTIMIZER_COST_PCT` / `OPTIMIZER_STOP_SLIP_PCT` env overrides), so
  spread-bleeding parameter sets stop winning the grid search.
- **End-of-day flatten** — all positions are closed `eod_flatten_minutes`
  (default 10, editable in Settings → Operational Environment) before the
  close. Bracket legs are DAY orders that expire at the bell; an overnight
  hold sat completely unprotected.

### Changed
- **CAUTION regime halves the position cap** — previously CAUTION (trend
  down *or* elevated vol) changed nothing and the bot bought dips at full
  throttle into a falling tape all day.
- Stale docs corrected: optimizer grid comment (576 → 5184 combinations),
  sentiment docstring's retired "never trades on silence" claim.

## [v2.11.0] - 2026-07-08

### Changed
- **Sentiment LLM now uses the same OpenAI-compatible endpoint as the analyst** —
  no longer requires `ANTHROPIC_API_KEY`. Sentiment scoring uses the same
  Ollama/OpenAI-compatible client configured from the Analyst tab in the dashboard.
- New **Sentiment Model** field in the Analyst tab — leave empty to use the same
  model as the analyst, or set a different one for news sentiment scoring.
- Screener pool size capped at 100 to match Alpaca's most-actives API limit
  (the config can still be set higher; the cap is applied at the API call).

## [v2.10.0] - 2026-07-08

### Changed
- **Operational environment moved from env vars to DB** — `position_size_usd`,
  `risk_per_trade_usd`, `max_positions`, `daily_stop_loss`, `min_price_usd`,
  `cooldown_minutes`, `poll_interval_seconds`, `bar_lookback_minutes`, and
  `watchlist_size` are now stored in `bot_config` and editable from
  **Settings → Operational Environment**. No more env vars or restarts needed
  to tune them.
- Only secrets (`ALPACA_API_KEY`, `ANTHROPIC_API_KEY`, `ANALYST_OLLAMA_API_KEY`)
  and deployment-level settings (`TRADING_SYMBOLS`, `REGIME_*`, `OPTIMIZER_*`)
  remain as env vars.
- `docker-compose.yml` cleaned up — removed 8 operational env vars from the
  backend service.
- `.env.example` and `README.md` updated to reflect the change.

## [v2.9.1] - 2026-07-08

### Changed
- **Regime states renamed**: `RISK_ON` → `TREND_UP`, `RISK_OFF` → `TREND_DOWN`
  to describe market conditions without directional bias — clearer for a
  long+short strategy where `TREND_DOWN` blocks BUY but allows SELL.
  `CAUTION` unchanged (one of trend down OR high vol).

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
