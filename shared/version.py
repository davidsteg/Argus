"""
Argus — single source of truth for version and release notes.

The frontend renders this in the header (version chip + release-notes
dialog) and the backend exposes it at GET /version. Keep CHANGELOG.md in
sync when adding a release: newest entry first.
"""

from __future__ import annotations

from typing import Dict, List

__version__ = "2.27.0"

RELEASES: List[Dict[str, object]] = [
    {
        "version": "2.27.0",
        "date": "2026-07-14",
        "title": "Optimizer: honest friction, more data, multi-fold validation, un-stuck LLM review",
        "notes": [
            "The nightly LLM review no longer rejects every run for a "
            "phantom reason: the winner shown to the reviewer carried three "
            "config keys (news_cutoff, analyst_enabled, short_enabled) that "
            "no candidate had, so the model called it a 'structural "
            "mismatch' and vetoed the whole night's work — including the "
            "Jul 13 run. Those keys are now merged only after the review.",
            "The backtest's stop slippage is now calibrated from your own "
            "realized stop fills (median, clamped, never more optimistic "
            "than the configured floor) instead of a 0.05% guess that "
            "Jul 8–10 measured at 10–200× under reality. The shadow-veto "
            "resolver prices hypothetical fills with the same calibrated "
            "number, so 'what a blocked trade would have made' and 'what "
            "the optimizer thinks' can't drift apart.",
            "More data, spent carefully: 60 days × 15 symbols (from 30 × "
            "10) — about 5.5 h of grid time, still inside the overnight "
            "window — and the single 25% holdout is now three sequential "
            "validation folds. A parameter set only goes live if it made "
            "money in at least 2 of 3 unseen folds AND overall; one lucky "
            "window no longer promotes a coin flip.",
            "Run records and the Optimizer tab now show the fold-by-fold "
            "validation returns and the calibrated slippage each run used.",
        ],
    },
    {
        "version": "2.26.0",
        "date": "2026-07-13",
        "title": "Regime gate on longs, position-protection watchdog, crypto close fix",
        "notes": [
            "New buys are now blocked whenever the index trades below its "
            "EMA — calm or stressed. Jul 13 showed why: SPY drifted down on "
            "quiet volatility (CAUTION) all session and 23 of 28 dip-buys "
            "stopped out. Every blocked BUY is shadow-recorded as a new "
            "'regime' veto gate, so within days the dashboard will show "
            "what the gate saved (or cost) instead of guessing.",
            "Fixed the crypto close-rejection loop: Alpaca charges crypto "
            "fees in the coin itself, leaving 9-decimal balances, and the "
            "close order rounded UP to 8 decimals — requesting one "
            "billionth more than held, rejected forever. (AAVE sat 3.5 h "
            "past its breached stop on Jul 13 retrying every cycle.) Close "
            "quantities are now floored, and after 3 failed limit closes "
            "the engine escalates to a full-position market close where "
            "the market allows it (crypto: always; equities: regular "
            "session).",
            "New position-protection watchdog: every held position must "
            "have working protection every cycle. Stop/target levels now "
            "survive engine restarts (restored from the persisted entry "
            "record — the hole that let VRAX ride untracked to a -$101.46 "
            "EOD flatten), naked positions get ATR-scaled levels attached "
            "at the current price, positions whose exchange bracket legs "
            "vanished get soft enforcement re-armed, and anything "
            "unmanageable for 3 cycles is closed.",
            "Protection incidents are visible, not buried in logs: a red "
            "banner on the Active Positions card flags stops that can't "
            "execute and watchdog interventions (protection_health blob, "
            "also on GET /debug). Repeated close failures no longer flood "
            "the log — one line per streak plus every 10th attempt.",
        ],
    },
    {
        "version": "2.25.1",
        "date": "2026-07-11",
        "title": "Optimizer tab mobile fix",
        "notes": [
            "Optimizer run-history cards no longer force the whole page to "
            "scroll sideways on phones: the chip row (timestamp, trigger, "
            "duration, outcome, analyst verdict) wraps, and the detail text "
            "gets its own truncated line. Desktop layout unchanged.",
        ],
    },
    {
        "version": "2.25.0",
        "date": "2026-07-11",
        "title": "Mobile-first dashboard: bottom tab bar, compact header, no sideways tables",
        "notes": [
            "On phones the six tabs move into a fixed bottom navigation bar "
            "(thumb reach, app-style, safe-area aware) and the header "
            "collapses to a compact banner with icon-only RESUME / HARD STOP "
            "buttons — the controls no longer eat half the viewport.",
            "Active Positions and Trade History no longer scroll sideways on "
            "small screens: they collapse to the columns that matter (symbol, "
            "side, PnL) and the full detail stays one ℹ tap away.",
            "Added home-screen app support (theme-color, standalone mode, "
            "viewport-fit) so the dashboard installs like an app on iOS and "
            "Android.",
            "Polish everywhere: brand-gold active-tab accent matching the "
            "Argus mark, row hover highlights, slim dark scrollbars, and a "
            "live browser-tab title that shows today's PnL from a "
            "backgrounded tab.",
        ],
    },
    {
        "version": "2.24.0",
        "date": "2026-07-11",
        "title": "Shadow-tracked vetoes, defanged optimizer override, visible fail-opens",
        "notes": [
            "Every signal a gate blocks — news sentiment, VWAP re-check, LLM "
            "risk agent, LLM portfolio manager — is now shadow-recorded with "
            "the exact bracket and share count it would have traded, then "
            "resolved against market data with the optimizer's friction "
            "model. The new Shadow-tracked vetoes card on the Analyst tab "
            "(and GET /vetoes) answers, per gate, 'would the blocked trades "
            "have made or lost money?' — previously pure guesswork.",
            "The post-optimization LLM review is now a binary accept/reject "
            "sanity check. Its retired 'override' action let the model pick "
            "any rank from the TRAIN-window list (mostly combinations that "
            "failed or never saw out-of-sample validation, shown with train "
            "stats only) — in-sample cherry-picking by the least-validated "
            "component. The reviewer now also receives the winner's "
            "out-of-sample stats, so its overfitting judgment is informed.",
            "Risk-agent and portfolio-manager fail-opens (agent unreachable "
            "→ signals auto-approved un-gated) are now counted per session "
            "in the analyst_health blob, exposed via GET /debug, and shown "
            "as an amber banner on the Analyst tab — a dead Ollama can no "
            "longer leave the bot silently un-gated for days.",
            "The simulated-fill friction constants (COST_PER_TRADE_PCT, "
            "STOP_SLIPPAGE_PCT) moved from optimizer.py to indicators.py so "
            "the backtest and the shadow-veto resolver share one fill model.",
        ],
    },
    {
        "version": "2.23.0",
        "date": "2026-07-11",
        "title": "Exchange-side brackets in regular hours + quote-first soft stops",
        "notes": [
            "Regular-session equity entries now carry native exchange-side "
            "bracket legs (OCO take-profit limit + stop-market) instead of "
            "relying on the engine's 60-second poll. The Jul 8–10 review "
            "showed polled soft stops filling 2–4× past their level on thin "
            "movers (NVVE: $16 designed risk, $61.54 realized) — with resting "
            "legs the exchange fills the breach immediately. Outside regular "
            "hours and for crypto, where Alpaca rejects brackets, the "
            "soft-level path is unchanged.",
            "Soft stop/target checks now price off the live quote's exit side "
            "(the bid a long sells into, the ask a short buys back) before "
            "falling back to the 1-minute bar close, which can be tens of "
            "seconds stale on a thin book — late detection was the other half "
            "of the stop slippage.",
            "Bracket legs are DAY orders that expire at the regular close; "
            "evaluate_and_close_stops automatically resumes soft enforcement "
            "for those positions in after-hours. Signal exits and the EOD "
            "flatten cancel resting legs before closing, so no orphaned leg "
            "can double-sell.",
            "Trade records and the position info popup now say whether a "
            "position's levels rest on the exchange (native_bracket) or are "
            "enforced as soft levels each cycle.",
        ],
    },
    {
        "version": "2.22.0",
        "date": "2026-07-10",
        "title": "Per-position info popup on the Active Positions card",
        "notes": [
            "Every row in the Active Positions card now has an ℹ button that "
            "opens the same info popup used by Trade History — the entry "
            "rationale ('RSI 27.4 was oversold below the 30 buy level while "
            "price sat below VWAP…'), the entry signal numbers (RSI, VWAP, "
            "ATR, news sentiment), the execution (qty, entry price, now, "
            "market value), and a price chart from entry to now with the "
            "entry marker, dashed take-profit / stop-loss lines, and a faint "
            "band over the hold window.",
            "A new 🎯 Soft stop / target section shows the levels the bot is "
            "enforcing each cycle (polled, not resting on the exchange) "
            "alongside the current price, so you can see how close a live "
            "position is to its stop or target at a glance.",
            "The backend publishes its in-memory _open_entries blob (RSI / "
            "VWAP / ATR / sentiment / stop / target / entry_reason) to the "
            "shared DB every cycle so the frontend can render it — internal "
            "guard fields (prefixed with _) are stripped. Positions adopted "
            "from outside the engine (restart, manual fill) have no entry "
            "metadata; their ℹ button is disabled.",
        ],
    },
    {
        "version": "2.21.0",
        "date": "2026-07-10",
        "title": "Live optimizer progress in the dashboard",
        "notes": [
            "The Optimizer tab now shows live progress while a grid search is "
            "running — phase (fetching / grid search / validation / analyst / "
            "writing), a progress bar with combinations evaluated vs total, "
            "candidate count, and elapsed time — instead of a frozen spinner "
            "that gave no signal the process was alive.",
            "POST /optimize now starts the grid search in a background "
            "thread and returns immediately, so the dashboard's refresh timer "
            "polls GET /optimizer/status (and the optimizer_status "
            "runtime_state blob) to render progress. A 409 is returned if a "
            "run is already in progress, preventing overlapping runs.",
            "run_optimization publishes phase/evaluated/candidates to "
            "runtime_state at each boundary (every 500 combinations during the "
            "grid search, every 50 during validation) and clears it to idle "
            "in the finally block.",
        ],
    },
    {
        "version": "2.20.3",
        "date": "2026-07-10",
        "title": "Fix trade info chart: SIP data feed rejected by paper subscription",
        "notes": [
            "The per-trade info popup's price chart failed for equity trades "
            "with 'subscription does not permit querying recent SIP data' "
            "because fetch_trade_bars didn't specify a data feed, defaulting "
            "to SIP which the paper trading subscription doesn't include.",
            "Fix: StockBarsRequest now passes feed=DataFeed.IEX, the free "
            "feed available on every Alpaca paper account.",
        ],
    },
    {
        "version": "2.20.2",
        "date": "2026-07-10",
        "title": "Re-validate VWAP gate against the live-repriced entry price",
        "notes": [
            "evaluate_signal's VWAP gate ran on the last 1-minute bar close, "
            "but place_limit_buy re-prices the entry off the live ask (and "
            "place_limit_short off the live bid) immediately before "
            "submission. On a volatile symbol that ask can be several "
            "percent above the bar close by the time the order is built, so "
            "a dip that passed the gate on stale bar data can already be "
            "above VWAP at the actual entry (the 2026-07-10 entry passed "
            "the gate at $62.50 bar close, then re-priced to $66.41 ask — "
            "6% above VWAP — and stopped out instantly when price reverted "
            "to $62.12).",
            "New _vwap_revalidate re-checks the same VWAP direction and "
            "max_vwap_dislocation_pct gates against the live-repriced "
            "price in both place_limit_buy and place_limit_short, aborting "
            "the entry with a diagnostic log when the dip/overextension "
            "has reverted or turned into a knife/squeeze between the gate "
            "and the order. Session VWAP drifts slowly (volume-weighted "
            "over the whole day), so re-checking against the signal's VWAP "
            "is sufficient — the failure mode is the price moving, not the "
            "VWAP.",
            "The entry_reason template no longer hardcodes 'sat below "
            "VWAP' / 'sat above VWAP' for every BUY/SELL. It now reports "
            "the actual percentage the live ask/bid sits below/above VWAP, "
            "and includes the bar-close dislocation that first triggered "
            "the gate, so the per-trade rationale reflects the real "
            "entry — not a template that was a lie whenever the live "
            "price had drifted from the bar close.",
        ],
    },
    {
        "version": "2.20.1",
        "date": "2026-07-10",
        "title": "Fix equity engine mis-killing on the crypto engine's position value",
        "notes": [
            "v2.20.0 let the crypto engine hold real positions for the first "
            "time, which exposed a latent bug in the equity engine's per-market "
            "equity isolation: EquityAdapter.compute_equity subtracted the "
            "crypto positions' full MARKET VALUE from the shared account equity, "
            "but the cash that bought them had already left the shared pool — so "
            "it double-counted the cost basis. A ~$220 crypto position read as a "
            "~$220 equity-engine loss and tripped the $100 daily stop, killing "
            "the engine (which then re-killed on every reset).",
            "compute_equity now subtracts only the crypto positions' UNREALIZED "
            "P&L, so a crypto position's cost basis no longer registers as an "
            "equity-engine loss. Residual: a crypto trade's realized P&L briefly "
            "touches the figure until the next daily anchor — dollars, not the "
            "hundreds the old bug injected.",
        ],
    },
    {
        "version": "2.20.0",
        "date": "2026-07-10",
        "title": "Crypto engine: stop fabricating phantom trades, fix fills and brackets",
        "notes": [
            "Root cause of the crypto engine's phantom trades: Alpaca returns "
            "crypto POSITIONS slashless ('PAXGUSD') but orders, the universe "
            "and our signals use the slashed form ('PAXG/USD'). owns_symbol "
            "tested for a bare '/', so the crypto engine couldn't see its own "
            "fills and the equity engine adopted them. Ownership now recognises "
            "both forms and positions are normalised to the slashed symbol.",
            "sync_portfolio recorded a 'closed trade' for any tracked symbol "
            "with no live position — including entry orders that never filled — "
            "fabricating trades (10 identical fake PAXG/USD wins in 12 minutes) "
            "and leaving unfilled GTC orders resting, stacking a duplicate every "
            "cycle. Reconciliation now records a trade only for positions that "
            "actually opened, and cancels unfilled entry orders instead.",
            "Entries/exits now price their marketable limit off the live quote "
            "(ask to buy, bid to sell) instead of the last trade, which could be "
            "minutes stale on a thin book and never fill.",
            "Soft stop/target are rounded to the market's own price tick, not a "
            "flat 2-decimal / 2-cent floor that put a sub-dollar crypto stop "
            "dollars away from the entry.",
            "Secondary fixes: crypto sentiment no longer 404s on an empty model "
            "string; watchlist curation and the equity opportunity screener are "
            "skipped on the crypto engine; POST /optimize is rejected on crypto; "
            "the /signals dry-run replays the floored-stop and VWAP-dislocation "
            "gates; the nightly backtest benches after every close to match the "
            "live engine; dashboard analyst-config changes now reload the "
            "sentiment client without a restart.",
        ],
    },
    {
        "version": "2.19.2",
        "date": "2026-07-10",
        "title": "Fix trade info chart: stale markers bleeding between trades (real fix)",
        "notes": [
            "The v2.18.1 fix (setting markLine=None, markArea=None) didn't "
            "work because NiceGUI's JSON serializer strips None values — they "
            "never reached the ECharts client, so the old markers survived "
            "the merge.",
            "Real fix: the chart reset now calls run_chart_method('setOption', "
            "options, ':true') which invokes ECharts' setOption with "
            "notMerge: true on the client, completely replacing the chart "
            "state instead of merging into it.",
        ],
    },
    {
        "version": "2.19.1",
        "date": "2026-07-10",
        "title": "Fix risk agent rejecting valid signals with wrong RSI threshold",
        "notes": [
            "The risk agent's system prompt never told the LLM what the "
            "configured rsi_buy_signal threshold was, so the model defaulted "
            "to its own generic 'RSI < 30 = oversold' rule and rejected every "
            "signal in the 30-35 range — the crypto engine's configured "
            "threshold is 35, so most valid BUY signals were blocked.",
            "RISK_AGENT_PROMPT now instructs the LLM to use the live "
            "thresholds from the signal data (rsi_buy_signal, rsi_short_signal, "
            "rsi_exit_signal, rsi_short_exit) instead of its own defaults.",
            "BUY and SELL signal dicts in bot.py now carry all four RSI "
            "thresholds from live config, and evaluate_signal_risk passes them "
            "through in prompt_data so the risk agent sees them.",
        ],
    },
    {
        "version": "2.19.0",
        "date": "2026-07-10",
        "title": "Crypto dashboard: hide equity-only features that don't apply",
        "notes": [
            "When the Crypto market is selected, the dashboard now hides "
            "features that are equity-only and have no crypto equivalent: "
            "the entire Optimizer tab (crypto has no nightly grid search), "
            "Short Selling and RSI short parameters (crypto is spot-only, "
            "long-only), EOD Flatten (crypto is 24/7), the Watchlist and "
            "Opportunity Screener cards (crypto universe is static USD pairs, "
            "not screener-based), the Watchlist Model input and Post-"
            "Optimization Review card in the Analyst tab, and the Screener "
            "Candidates card on the Overview tab.",
            "The Optimization Reviewer agent card is also hidden for crypto "
            "since it never fires without an optimizer run.",
            "The market chip tooltip now says 'Crypto market — 24/7' instead "
            "of 'US equity market session' when crypto is selected.",
            "Visibility updates on market switch and initial page load via a "
            "declarative _track_visibility / _update_market_visibility "
            "pattern — no conditional UI duplication.",
        ],
    },
    {
        "version": "2.18.3",
        "date": "2026-07-10",
        "title": "Fix crypto churn: cooldown after every close, not just losses",
        "notes": [
            "Cooldown only triggered on losing trades — a winning exit left "
            "the symbol immediately eligible for re-entry. On low-volatility "
            "crypto pairs (PAXG/USD being the live example), RSI oscillates "
            "around the buy/exit thresholds so the bot churned every minute: "
            "buy → RSI recovers → signal-exit for a tiny profit → no "
            "cooldown → re-buy next cycle, dozens of times in a row.",
            "reconcile_closed_trade now starts a cooldown on every close "
            "regardless of PnL sign. The RSI oscillation that caused the churn "
            "now hits a cooldown wall and the bot sits out for "
            "cooldown_minutes before re-evaluating the symbol.",
            "Defense-in-depth: the dashboard's positions and trade history "
            "render functions now filter by market_owns (crypto symbols carry "
            "a /, equities never do) before displaying, so a cross-market "
            "leak in the DB can never surface in the wrong market's tab.",
        ],
    },
    {
        "version": "2.18.2",
        "date": "2026-07-10",
        "title": "Fix sentiment LLM blank-content error — missing reasoning fallback",
        "notes": [
            "v2.4.10 added a fallback to the non-standard 'reasoning' field when "
            "Ollama Cloud DeepSeek models return empty 'content', but only "
            "applied it to the analyst module — sentiment.py was missed. "
            "Sentiment scoring for every symbol failed with 'LLM returned blank "
            "content' and fell back to the keyword heuristic.",
            "sentiment.py now mirrors analyst.py's response extraction: try "
            "content first, fall back to reasoning, and include finish_reason "
            "and refusal in the error message when both are empty.",
        ],
    },
    {
        "version": "2.18.1",
        "date": "2026-07-10",
        "title": "Fix trade info chart: stale markers bleeding between trades",
        "notes": [
            "The per-trade info popup's price chart showed entry/exit markers "
            "and the hold-window band from the previous trade overlaid on the "
            "new one. ECharts' setOption merges by default, and the chart "
            "reset (options.clear + update) didn't explicitly clear markLine "
            "and markArea — so they persisted from the prior trade.",
            "Fix: the reset now sets markLine=None and markArea=None on the "
            "series before calling update(), telling ECharts to remove those "
            "keys during the merge.",
        ],
    },
    {
        "version": "2.18.0",
        "date": "2026-07-10",
        "title": "Optimizer Run History: structured persistence + dedicated dashboard tab",
        "notes": [
            "Every optimizer run is now recorded as a structured row in a new "
            "optimizer_runs SQLite table: timestamp, trigger (nightly/manual), "
            "duration, outcome (applied/no_change/rejected_validation/"
            "rejected_analyst/no_combination/no_data/error), before→after "
            "parameter diff, train/validation stats, and analyst decision.",
            "New Optimizer tab in the dashboard (after Analyst) with a Run "
            "History card showing every run with outcome chips, trigger badges, "
            "duration, train/val stats, and changed parameters as "
            "key: before → after lines. The Run optimizer now button moved "
            "from Settings to this tab; a pointer remains in Settings.",
            "run_optimization() now always records exactly one run row via a "
            "try/finally block — even on early-return failure paths (no data, "
            "no combination, validation reject, analyst reject, error).",
        ],
    },
    {
        "version": "2.17.4",
        "date": "2026-07-10",
        "title": "Per-market equity isolation: equity engine no longer contaminated by crypto PnL",
        "notes": [
            "Both engines share one Alpaca account, and the equity engine's "
            "compute_equity returned the blended account.equity — so crypto "
            "PnL could trigger the equity engine's daily stop-loss and distort "
            "its equity curve.",
            "The equity engine now subtracts the market value of non-equity "
            "(crypto) positions from the blended account equity, isolating its "
            "risk checks and dashboard display from the crypto engine's "
            "activity — matching the crypto engine's existing per-market "
            "equity computation.",
        ],
    },
    {
        "version": "2.17.3",
        "date": "2026-07-10",
        "title": "Crypto trade history: fix missing exit price, PnL, and price chart",
        "notes": [
            "Crypto trades in the Trade History tab showed '—' for exit price, "
            "PnL, and PnL % because reconcile_closed_trade searched for the "
            "exit fill by side with limit=10 — if the close order wasn't in "
            "the first 10 results (common for crypto pairs with many orders), "
            "the trade was permanently recorded with exit_price=None and "
            "realized_pnl=None.",
            "Fix: _limit_close now records the close order's ID on the entry "
            "tracking dict, and reconcile_closed_trade uses a direct "
            "get_order_by_id lookup — precise, no limit, no side ambiguity. "
            "Falls back to the old side-based search for adopted/legacy "
            "positions that have no tracked close order ID.",
            "The per-trade info popup's price chart also failed for crypto "
            "trades because fetch_trade_bars hardcoded StockHistoricalDataClient "
            "— it now detects crypto symbols (by the '/' in the symbol) and "
            "uses CryptoHistoricalDataClient + CryptoBarsRequest instead.",
        ],
    },
    {
        "version": "2.17.2",
        "date": "2026-07-10",
        "title": "Crypto engine: hard-disable shorts — spot-only, can't borrow",
        "notes": [
            "The crypto engine's MARKET=crypto default overrides didn't force "
            "short_enabled=0, so the optimizer or dashboard could flip it on. "
            "The engine then submitted SELL orders for crypto pairs, which "
            "Alpaca rejected with 'insufficient balance' (spot-only — you can't "
            "short what you don't own).",
            "Two-layer fix: the crypto seed defaults now include "
            "short_enabled=0.0, and get_config() enforces it as a hard "
            "invariant when MARKET=crypto — same pattern as paper=True — so "
            "no code path (optimizer, dashboard, manual DB edit) can weaken it.",
        ],
    },
    {
        "version": "2.17.1",
        "date": "2026-07-10",
        "title": "Diagnostic endpoints report each engine's own regime proxy",
        "notes": [
            "The /regime, /signals and manual trade-review endpoints called the "
            "regime classifier directly, which defaults to the SPY equity proxy "
            "— so on the crypto engine they reported 'UNKNOWN — no SPY bars' "
            "instead of the BTC/USD regime. They now go through the engine's "
            "market adapter, so each backend shows its own proxy (SPY for "
            "equities, BTC/USD for crypto). Diagnostic-only: live trading always "
            "used the correct per-market regime.",
        ],
    },
    {
        "version": "2.17.0",
        "date": "2026-07-10",
        "title": "Crypto trading — a second 24/7 engine alongside equities, one dashboard",
        "notes": [
            "New crypto engine trades all Alpaca-supported USD pairs (BTC/USD, "
            "ETH/USD, …) spot, long-only, 24/7 — the same RSI/VWAP dip strategy "
            "and soft stop/target as equities. It runs as a second, independent "
            "container (MARKET=crypto) with its own SQLite DB, so the live "
            "equities engine is completely untouched.",
            "Both engines share one Alpaca account, so each partitions the "
            "blended book to its own asset class and computes its own equity "
            "(crypto: a notional base + its own realized/unrealized PnL), "
            "keeping the two daily-stops and equity curves independent.",
            "Market-specific behaviour lives behind a new MarketAdapter "
            "(backend/market.py): data client, universe, session hours, order "
            "construction (crypto uses GTC limits with fractional sizing; "
            "equities keep extended-hours DAY limits), regime proxy (BTC/USD "
            "for crypto), and position/equity partitioning. The strategy and "
            "orchestration stay single-sourced so the two markets can't drift.",
            "One dashboard: a header Equities ⇄ Crypto switcher flips every "
            "view, setting, and action (hard-stop, close, resume, optimize) to "
            "the selected engine. Crypto has no scheduled end-of-day flatten "
            "(24/7) and no nightly optimizer in v1 (the backtest is equity-bar "
            "based) — it runs on static/default parameters.",
        ],
    },
    {
        "version": "2.16.1",
        "date": "2026-07-10",
        "title": "Hotfix: extended-hours session clock crashed every cycle",
        "notes": [
            "v2.16.0 aborted every trading cycle with \"combine() argument 2 "
            "must be datetime.time, not datetime.datetime\" — the engine stayed "
            "RUNNING but placed zero orders. The new extended-session window "
            "assumed Alpaca's calendar close field was a datetime.time, but this "
            "alpaca-py version returns a datetime.datetime, so datetime.combine "
            "raised on every cycle before any signal was evaluated.",
            "The regular-close field is now normalised to an ET wall-clock "
            "time-of-day whether Alpaca returns a time, a naive datetime, or a "
            "tz-aware datetime — so the 4:00 AM / 8:00 PM ET (and half-day) "
            "bounds are computed correctly regardless of the field's type.",
        ],
    },
    {
        "version": "2.16.0",
        "date": "2026-07-10",
        "title": "Extended-hours trading: the full 4:00 AM – 8:00 PM ET session",
        "notes": [
            "The engine now trades the entire extended session — pre-market "
            "from 4:00 AM ET, regular hours, and after-hours through 8:00 PM "
            "ET — instead of only the 9:30 AM – 4:00 PM regular session. The "
            "session window is derived from Alpaca's trading calendar, so "
            "holidays and half-days (extended close pulls back to 5:00 PM) are "
            "handled automatically.",
            "Alpaca forbids bracket and market orders outside regular hours, so "
            "every entry and exit is now a marketable extended-hours LIMIT "
            "order (priced entry_slip_pct / exit_slip_pct through the last "
            "trade so it fills in a thin book). The exchange-side OCO bracket "
            "is replaced by a SOFT stop/target that the engine enforces every "
            "poll cycle — a held position is closed when price crosses the "
            "recorded stop or target level.",
            "Tradeoff: soft stops are polled at poll_interval_seconds (default "
            "60s), so price can gap through a level between checks — there is "
            "no resting exchange-side stop anymore. The daily-loss kill and the "
            "end-of-day flatten (now timed to 8:00 PM ET) remain the backstops.",
            "New entry_slip_pct (0.001) and exit_slip_pct (0.002) strategy "
            "parameters control how aggressively limit orders are priced to "
            "guarantee fills; both are shown in the operational environment.",
            "EOD flatten, signal exits, and the emergency kill sequence all use "
            "extended-hours limit closes instead of market liquidation, so they "
            "work in every session.",
            "UI: the per-trade info chart now marks entry and exit with slim "
            "vertical lines (blue in; green on a win, red on a loss out) plus a "
            "faint shaded band over the hold window, instead of the teardrop "
            "pins that floated over and covered the price line.",
        ],
    },
    {
        "version": "2.15.0",
        "date": "2026-07-10",
        "title": "Per-trade info popup: see exactly why the bot took each trade",
        "notes": [
            "Every row in the Trade History now has an ℹ button that opens a "
            "popup explaining the trade end-to-end: a plain-English rationale "
            "('RSI 27.4 was oversold below the 30 buy level while price sat "
            "below VWAP — a genuine dip; sentiment 0.61 cleared the cutoff'), "
            "the entry signal numbers (RSI, VWAP, ATR, news sentiment), the "
            "execution (qty, entry/exit, target/stop), and how it closed "
            "(signal exit, take-profit, or stop-loss).",
            "The popup draws a 1-minute price chart of the exact window the "
            "position was held — fetched live from Alpaca — with pinned entry "
            "and exit markers and dashed take-profit / stop-loss lines, so the "
            "decision can be read against what price actually did.",
            "The decision snapshot is captured at order time and persisted on "
            "the trade (nine new nullable columns on the trades table, added by "
            "migration). Trades closed before this release show the numbers "
            "they have and note that the rationale predates decision capture.",
            "Trade History was rebuilt as a native card grid instead of the "
            "embedded data-grid that overflowed the card — it now fits the "
            "layout, scrolls sideways cleanly on mobile, and matches the "
            "Active Positions styling.",
        ],
    },
    {
        "version": "2.14.0",
        "date": "2026-07-10",
        "title": "Falling-knife gate: skip RSI dips that are really collapses",
        "notes": [
            "Post-mortem of the 2026-07-09 VRAX loss (−$23.40, the day's worst "
            "trade by 8×): the engine bought VRAX at $6.95 while session VWAP "
            "was $9.17 — 24% below fair value, ~10× ATR — on an RSI 25.9 "
            "'dip'. It was a collapse, not a dip: RSI kept bleeding to 13 and "
            "the bracket stopped out. The existing VWAP gate only checked "
            "direction (price below VWAP = 'dip') with no floor on depth, so a "
            "1%-below-VWAP dip and a 24%-below-VWAP crash passed identically.",
            "New max_vwap_dislocation_pct strategy parameter (default 0.15): a "
            "long is skipped when price sits more than this fraction below "
            "VWAP, and — mirrored — a short is skipped when price sits more "
            "than this fraction above VWAP (a parabolic squeeze). Editable "
            "from Settings; the gate runs before the sentiment LLM call, so a "
            "falling knife costs no tokens.",
            "Modelled in the optimizer backtest and added to the nightly grid "
            "as [0.08, 0.15, 999.0] — 999.0 disables the gate, so the "
            "walk-forward out-of-sample step can drop it entirely if it fails "
            "to earn its keep rather than overfitting to a single trade.",
        ],
    },
    {
        "version": "2.13.3",
        "date": "2026-07-09",
        "title": "Risk agent and portfolio manager get enough token budget to stop truncating",
        "notes": [
            "The risk agent and portfolio manager — the two agents in the "
            "live order-placing path, and the two that fail OPEN (auto-"
            "approve) on any error — were capped at max_tokens=1024, tighter "
            "than every other agent's 2048+ default. deepseek-v4-flash's "
            "response was observed getting hard-cut mid-JSON ('...\"warnings\": "
            "[\"RSI not deeply oversold\", \"Caution' — then nothing), which the "
            "parser correctly rejected as invalid JSON, silently letting the "
            "signal through the risk agent instead of the reject it was "
            "actually generating.",
            "Both bumped to max_tokens=2048 to match the module default.",
        ],
    },
    {
        "version": "2.13.2",
        "date": "2026-07-09",
        "title": "Hotfix: risk agent no longer crashes the trading cycle on an open position",
        "notes": [
            "Live outage fix. Every trading cycle that produced a signal was "
            "dying with \"'<' not supported between instances of 'NoneType' and "
            "'int'\" — the engine stayed RUNNING but placed zero orders. The "
            "risk agent's recent-loss lookup used t.get('realized_pnl', 0) < 0, "
            "but for an open position the key exists with value None, so the "
            "default 0 never applied and None < 0 threw. This morning's "
            "hyperactive RSI(7) churn left an open, None-P&L position in the "
            "recent-trades window, so the latent bug (present since v2.5.0) "
            "fired on every cycle.",
            "Now guarded with (t.get('realized_pnl') or 0) < 0, which handles "
            "both a missing key and an explicit None; closed trades are "
            "unaffected.",
        ],
    },
    {
        "version": "2.13.1",
        "date": "2026-07-09",
        "title": "Optimizer backtest sizes trades like the live engine — no more fantasy returns",
        "notes": [
            "Root-cause fix for the optimizer promoting hyperactive parameter "
            "sets on fantasy returns (the 2026-07-09 run reported +2459% train "
            "and put RSI(7) buy<35 live). The backtest compounded full notional "
            "every trade (equity *= exit/entry), so a ~0.2% average edge over "
            "~1500 trades ballooned to +2459% — a number no trading cost could "
            "offset, and the grid therefore always rewarded whatever config "
            "traded the most.",
            "backtest() now sizes every trade in whole shares exactly as the "
            "live engine does (new position_qty mirrors bot.py: constant "
            "risk_per_trade_usd / stop_distance, capped at position_size_usd "
            "notional, skipped when not even one share fits) and accrues P&L in "
            "dollars against a fixed account-equity base. Position size no "
            "longer scales with accumulated equity, so returns are additive and "
            "realistic instead of exponential.",
            "Sizing (position_size_usd, risk_per_trade_usd) and the account "
            "equity base are read from live config/status, so backtest and "
            "engine can never diverge on position sizing.",
            "Incidental correctness fix: the backtest post-loss cooldown now "
            "benches after a genuine loss on either side (exit below entry for "
            "a long, above entry for a short), matching the live engine — the "
            "old side-agnostic check benched winning shorts.",
        ],
    },
    {
        "version": "2.13.0",
        "date": "2026-07-09",
        "title": "Analyst observability: agent roster, review history, LLM call log",
        "notes": [
            "New LLM Agents card in the Analyst tab: all seven LLM call types "
            "(risk agent, portfolio manager, sentiment scorer, watchlist "
            "curator, trade reviewer, optimization reviewer, decision memory) "
            "shown with what they do, when they run, which model serves them, "
            "and live 24h health — call counts, error counts, average latency, "
            "last call. Red dot + hover error when the last call failed.",
            "Review History card: past trade/optimization/watchlist review "
            "verdicts are kept (last 40) and shown as a timeline with "
            "confidence, warning counts and the optimizer accept/override/"
            "reject decision — reviews no longer overwrite each other.",
            "LLM Call Log card: the raw last 25 LLM calls with model, latency "
            "and error, so a silent analyst outage is visible at a glance.",
            "Every LLM call is now recorded to the shared DB (rolling 400 "
            "entries) — including sentiment scoring — with agent, model, "
            "latency, request/response size and error.",
            "New debug endpoints: GET /analyst/activity (call log + per-agent "
            "24h aggregates) and GET /analyst/reviews (review history).",
            "Silent agent failures now surface in the Logs tab: a failed risk "
            "agent call (which auto-approves the signal) and a failed "
            "portfolio manager call (which passes signals through unranked) "
            "write WARNING log entries instead of disappearing into stdout.",
            "Mobile-friendly dashboard: the header, tabs, two-column card "
            "layouts and all Settings/Analyst forms now stack to a single "
            "column on phones (no horizontal scrolling); the positions grid "
            "scrolls sideways within its card instead of squashing.",
        ],
    },
    {
        "version": "2.12.0",
        "date": "2026-07-08",
        "title": "Post-mortem hardening: shorts unblocked, realistic backtests, no more noise trades",
        "notes": [
            "Short-side contract repaired: the risk agent, portfolio manager, "
            "watchlist curator and sentiment prompts still described a long-only "
            "strategy, so every valid SELL signal was rejected as 'overbought, "
            "contradicting mean-reversion'. All prompts now describe the "
            "two-sided strategy and receive the signal's side.",
            "Symmetric short sentiment gate: shorts now need sentiment below "
            "1 − news_cutoff (mirror of the long gate) instead of below "
            "news_cutoff — a no-news 0.5 no longer makes shorting impossible.",
            "Too-quiet gate: signals whose stop would be set by the 0.35% "
            "percentage floor rather than ATR are skipped in the engine and the "
            "backtest — floor-tight brackets inside bar noise produced most of "
            "the 2026-07-08 losers.",
            "Leveraged/inverse ETPs (2x/3x, bull/bear, Direxion, ProShares "
            "Ultra/Short, …) are filtered out of the dynamic watchlist and the "
            "screener pool by asset name — buying an RSI 'dip' in a geared "
            "inverse fund is a leveraged bet against the trend.",
            "Optimizer backtest now pays trading costs: 0.10% round-trip per "
            "trade plus 0.05% adverse slippage on stop fills (OPTIMIZER_COST_PCT "
            "/ OPTIMIZER_STOP_SLIP_PCT). Fantasy '+103% train return' parameter "
            "sets no longer win the grid search.",
            "CAUTION regime now halves the position cap instead of trading at "
            "full throttle — a down-trending tape is traded at half book.",
            "End-of-day flatten: all positions are closed eod_flatten_minutes "
            "(default 10, Settings → Operational Environment) before the bell. "
            "Bracket legs are DAY orders that expire at the close, so an "
            "overnight hold sat completely unprotected.",
            "Short exit reconciliation fixed: the trade recorder matched the "
            "first closed SELL order as the exit fill, which for a short is "
            "its own entry — covers now match the opposite side (BUY) so "
            "short PnL is recorded correctly.",
            "Legacy trade records with side 'LONG' are normalized to 'BUY' so "
            "side-based analytics see one label per direction.",
        ],
    },
    {
        "version": "2.11.0",
        "date": "2026-07-08",
        "title": "Sentiment LLM now uses the same OpenAI-compatible endpoint as the analyst",
        "notes": [
            "Sentiment scoring no longer requires ANTHROPIC_API_KEY — it uses the "
            "same OpenAI-compatible client (Ollama, etc.) as the analyst, configured "
            "from the Analyst tab in the dashboard.",
            "New Sentiment Model field in the Analyst tab — leave empty to use the "
            "same model as the analyst, or set a different one for sentiment scoring.",
            "Screener pool size capped at 100 to match Alpaca's most-actives API limit "
            "(the config can still be set higher; the cap is applied at the API call).",
        ],
    },
    {
        "version": "2.10.0",
        "date": "2026-07-08",
        "title": "Operational environment moved from env vars to DB — tunable from dashboard",
        "notes": [
            "position_size_usd, risk_per_trade_usd, max_positions, daily_stop_loss, "
            "min_price_usd, cooldown_minutes, poll_interval_seconds, "
            "bar_lookback_minutes, and watchlist_size are now stored in bot_config "
            "and editable from Settings → Operational Environment. No more env vars "
            "or restarts needed to tune them.",
            "Only secrets (ALPACA_API_KEY, ANTHROPIC_API_KEY, ANALYST_OLLAMA_API_KEY) "
            "and deployment-level settings (TRADING_SYMBOLS, REGIME_*, OPTIMIZER_*) "
            "remain as env vars.",
        ],
    },
    {
        "version": "2.9.0",
        "date": "2026-07-08",
        "title": "Short selling: symmetric SELL signals on RSI-overbought + VWAP-overextension",
        "notes": [
            "New short selling mode: when short_enabled is ON, the engine "
            "generates SELL signals on RSI-overbought + price-above-VWAP + "
            "bearish-sentiment setups — the mirror image of the existing "
            "BUY logic.",
            "Bracket SELL orders with buy-to-cover take-profit below entry "
            "and stop-loss above entry, same ATR-scaled distances and "
            "risk-based sizing as BUY orders.",
            "Signal-driven covers: a held short closes early when RSI drops "
            "below rsi_short_exit (default 30), symmetric to the long-side "
            "RSI exit.",
            "Regime-aware: SELL signals pass through RISK_OFF (falling "
            "market favours shorts) while BUY signals are blocked.",
            "New strategy parameters rsi_short_signal, rsi_short_exit, "
            "short_enabled — all editable from Settings, tuned nightly by "
            "the optimizer (added to the grid and modelled in the backtest).",
            "Dashboard shows Side column (BUY/SELL) on positions and trade "
            "history with green/red color coding.",
        ],
    },
    {
        "version": "2.8.0",
        "date": "2026-07-08",
        "title": "Opportunity screener: find CRINX-like setups across a wide universe",
        "notes": [
            "New backend/screener.py module scans the top 200 most-active "
            "symbols for the same RSI-oversold + VWAP-dip + bullish-sentiment "
            "pattern the engine trades, ranks by dip depth, and publishes "
            "candidates to the dashboard and GET /screener.",
            "Reuses the exact same indicators.py and sentiment.py code so "
            "screener and live signals never drift.",
            "Runs every 5 minutes as a background task in the engine; "
            "configurable from Settings (toggle, pool size, max candidates).",
            "Candidates shown on the Overview tab with RSI, price, VWAP, "
            "dip depth, and sentiment source.",
        ],
    },
    {
        "version": "2.7.0",
        "date": "2026-07-08",
        "title": "Signal-driven exits: bank the bounce when RSI recovers",
        "notes": [
            "Held longs now close early at market when RSI recovers past the "
            "new rsi_exit_signal level (default 70) — the symmetric mirror of "
            "the RSI-oversold entry. Previously a position only ever exited "
            "when its bracket take-profit or stop-loss filled; a bounce that "
            "stalled below the target just round-tripped. The resting bracket "
            "still guards the downside independently.",
            "The exit path cancels the bracket's OCO take-profit/stop-loss "
            "legs before market-closing the position, so a manual close can "
            "never collide with a resting leg or leave one dangling. The "
            "resulting sell is reconciled into a trade record on the next "
            "cycle, exactly like a bracket exit — one trade-recording path.",
            "Exits run ahead of the entry gates each cycle: a full book "
            "(max positions) or a RISK_OFF tape no longer blocks taking "
            "profit on an exhausted bounce.",
            "rsi_exit_signal is a first-class strategy parameter: editable "
            "from Settings, tuned nightly by the optimizer (added to the grid "
            "and modelled in the backtest at the bar close, so live and "
            "backtest exits stay in lockstep), and shown in the /signals "
            "dry-run and cycle trace.",
        ],
    },
    {
        "version": "2.6.3",
        "date": "2026-07-08",
        "title": "Give trade/optimization/watchlist reviews an explicit JSON schema",
        "notes": [
            "The trade-review, optimization-review, and watchlist-curation "
            "LLM calls never told the model what JSON keys to return — "
            "unlike the risk agent and portfolio manager prompts, which "
            "template the exact structure. A review could technically "
            "succeed (get a reviewed_at timestamp) while returning none of "
            "the summary/warnings/suggestions/confidence fields the "
            "dashboard displays, so the review cards looked empty even "
            "when the analyst was reachable and responding.",
            "Added TRADE_REVIEW_PROMPT, OPTIMIZATION_REVIEW_PROMPT, and "
            "WATCHLIST_REVIEW_PROMPT, matching the existing schema-prompt "
            "pattern, and wired all three into their _call_llm() calls.",
        ],
    },
    {
        "version": "2.6.2",
        "date": "2026-07-08",
        "title": "Fix Trade History grid crashing on mount",
        "notes": [
            "The Trade History AG-Grid never rendered — a leftover "
            "theme=\"balham-dark\" argument crashed the grid's mount hook "
            "(the bundled NiceGUI version only recognizes quartz/balham/"
            "material/alpine; dark styling is applied automatically). The "
            "grid silently failed to initialize, so no rows, headers, or "
            "empty-state text ever showed even though trades were being "
            "recorded correctly. Changed to theme=\"balham\".",
        ],
    },
    {
        "version": "2.6.1",
        "date": "2026-07-08",
        "title": "Watchlist timing moves to the dashboard, not the environment",
        "notes": [
            "Screener refresh interval and analyst watchlist override TTL "
            "are now bot_config values editable from the new Watchlist "
            "card in Settings — no restart, no environment variable.",
            "Watchlist Model and Risk Model fields added to the Analyst "
            "tab — the backend already supported per-agent model overrides "
            "but the dashboard had no inputs for them.",
        ],
    },
    {
        "version": "2.6.0",
        "date": "2026-07-08",
        "title": "Logic audit: closed feedback loops, hardened risk controls",
        "notes": [
            "Cycle counter fix: decision-memory lesson extraction now truly "
            "runs every 50 cycles — a missing increment made it fire every "
            "cycle (one extra LLM call per minute).",
            "Decision memory closed-loop: trade exits now attach their "
            "realized PnL to the original buy decision, so lesson "
            "extraction finally sees decision → outcome pairs.",
            "Watchlist override TTL: LLM-curated watchlists expire after "
            "2× the refresh interval instead of permanently replacing the "
            "screener; curation now picks from the live most-actives pool "
            "and hallucinated tickers are filtered out.",
            "Daily loss baseline persists across restarts — a mid-day "
            "engine restart no longer re-arms a fresh daily loss budget.",
            "Portfolio manager silence no longer trades: signals neither "
            "approved nor rejected are skipped, not executed.",
            "Parallel signal evaluation and risk-agent calls; periodic LLM "
            "reviews moved to a background task so they never delay order "
            "placement; risk reviews capped to the best candidates.",
            "Session-anchored VWAP: the fair-value anchor resets each "
            "US-Eastern trading day instead of blending across the "
            "overnight gap (live engine, /signals and optimizer alike).",
            "Optimizer backtest now applies the VWAP dip gate and the "
            "post-loss cooldown, so nightly parameters are tuned on the "
            "strategy that actually trades.",
        ],
    },
    {
        "version": "2.5.2",
        "date": "2026-07-08",
        "title": "New Argus logo",
        "notes": [
            "New multi-eye Argus mark, inspired by the hundred-eyed "
            "watchman: a central gold eye with six smaller eyes, rendered "
            "as inline SVG in the header and as the browser favicon.",
            "Removed six unused logo/favicon PNGs from frontend/static.",
        ],
    },
    {
        "version": "2.5.1",
        "date": "2026-07-08",
        "title": "Automatic decision memory lesson extraction",
        "notes": [
            "Lesson extraction now runs every 50 cycles instead of requiring "
            "a manual POST /analyst/extract-lessons call. The system is now "
            "fully autonomous.",
        ],
    },
    {
        "version": "2.5.0",
        "date": "2026-07-08",
        "title": "Multi-agent trading: risk agent, portfolio manager, decision memory",
        "notes": [
            "Pre-trade Risk Agent: evaluates each BUY signal for sector "
            "concentration, correlation, recent losses, and regime fit "
            "before execution. Can block marginal trades.",
            "Portfolio Manager Agent: reviews all pending signals and "
            "decides which to execute and in what order, respecting "
            "diversification and opportunity cost.",
            "Decision Memory: stores every trade decision and its outcome, "
            "extracts lessons that are fed back into future trade reviews.",
            "Separate risk_model config field for a cheaper/faster model "
            "on pre-trade checks.",
            "New API endpoints: GET /analyst/memory, POST /analyst/extract-lessons",
        ],
    },
    {
        "version": "2.4.14",
        "date": "2026-07-08",
        "title": "Automatic LLM watchlist curation",
        "notes": [
            "Analyst now reviews the watchlist every hour and can replace "
            "it with a curated list (sector diversification, regime-adaptive "
            "filtering, prune dead symbols). Override is written to "
            "runtime_state and picked up by universe.py on the next cycle.",
            "Separate watchlist_model config field — use a cheaper/faster "
            "model for watchlist curation while keeping the main model for "
            "trade/optimization reviews. Falls back to the main model if "
            "not set.",
        ],
    },
    {
        "version": "2.4.13",
        "date": "2026-07-08",
        "title": "Close the loop: LLM validates and can override optimizer winner",
        "notes": [
            "The analyst now makes a decision (accept/override/reject) on "
            "the optimizer's winning parameter combination. If it rejects, "
            "current params stay unchanged. If it overrides, a different "
            "rank from the grid search is applied instead. The write to "
            "bot_config only happens after the analyst has spoken.",
        ],
    },
    {
        "version": "2.4.12",
        "date": "2026-07-08",
        "title": "Increase LLM max_tokens to 8192 for complete JSON responses",
        "notes": [
            "The LLM now returns valid JSON but gets truncated at 2048 "
            "tokens, producing incomplete JSON that fails to parse. "
            "Increased to 8192.",
        ],
    },
    {
        "version": "2.4.11",
        "date": "2026-07-08",
        "title": "Force JSON output from LLM with explicit template",
        "notes": [
            "DeepSeek models ignore 'respond with JSON only' and write "
            "prose. System prompt now includes an exact JSON template to "
            "fill in, which forces structured output.",
        ],
    },
    {
        "version": "2.4.10",
        "date": "2026-07-08",
        "title": "Fix empty LLM response — DeepSeek uses reasoning field",
        "notes": [
            "Ollama Cloud DeepSeek models put output in the non-standard "
            "\"reasoning\" field instead of \"content\". Fall back to "
            "reasoning when content is empty.",
        ],
    },
    {
        "version": "2.4.9",
        "date": "2026-07-08",
        "title": "Detailed LLM error logging for empty/refused responses",
        "notes": [
            "Log finish_reason and refusal field when LLM returns empty "
            "content, so we can distinguish model-not-found from "
            "content-policy blocks without docker exec",
        ],
    },
    {
        "version": "2.4.8",
        "date": "2026-07-08",
        "title": "Fix toggle loop and expose LLM response in error logs",
        "notes": [
            "Removed set_value from render_analyst — the switch is now "
            "managed by user clicks only, eliminating the async race that "
            "caused the infinite toggle loop",
            "LLM response text (first 500 chars) is now included in the "
            "exception message so it shows up in GET /logs instead of "
            "requiring docker exec",
        ],
    },
    {
        "version": "2.4.7",
        "date": "2026-07-08",
        "title": "Fix analyst toggle infinite loop in UI",
        "notes": [
            "Added _suppress_change guard flag to prevent render_analyst "
            "from triggering the toggle callback when syncing the switch "
            "state from the database",
        ],
    },
    {
        "version": "2.4.6",
        "date": "2026-07-08",
        "title": "Log trade review errors to DB for visibility",
        "notes": [
            "Trade review and optimization review errors are now logged to "
            "the DB logs table so they're visible via GET /logs — no need "
            "for docker exec to see the failure reason",
        ],
    },
    {
        "version": "2.4.5",
        "date": "2026-07-07",
        "title": "Robust LLM response parsing for DeepSeek-R1",
        "notes": [
            "Strip  tags from DeepSeek-R1 chain-of-thought responses",
            "Improved _parse_json with fallback extraction of JSON objects",
            "Log raw LLM response text (truncated) on JSON parse failure",
            "Increased max_tokens to 2048 for longer LLM responses",
        ],
    },
    {
        "version": "2.4.4",
        "date": "2026-07-07",
        "title": "Fix analyst LLM call and config persistence",
        "notes": [
            "Removed response_format from LLM calls (not supported by "
            "Ollama); added JSON extraction from markdown code blocks",
            "Added logging to _persist_config and _call_llm for debugging",
            "Removed unused output schema constants",
        ],
    },
    {
        "version": "2.4.3",
        "date": "2026-07-07",
        "title": "Fix missing openai dependency",
        "notes": [
            "Added openai>=1.0.0 to requirements.txt — the analyst module "
            "imports it for the OpenAI-compatible client but it was missing "
            "from the Docker image, causing a silent ImportError and 503",
        ],
    },
    {
        "version": "2.4.2",
        "date": "2026-07-07",
        "title": "Fix analyst toggle loop",
        "notes": [
            "Fixed infinite toggle loop: render_analyst now tracks enabled "
            "state and only calls set_value when it actually changed, "
            "preventing the toggle callback from firing an API call every "
            "refresh cycle",
        ],
    },
    {
        "version": "2.4.1",
        "date": "2026-07-07",
        "title": "Ollama Cloud API key support",
        "notes": [
            "Analyst now accepts an API key for cloud-hosted Ollama "
            "(e.g. https://ollama.com/v1); falls back to 'ollama' for "
            "local instances",
            "New password field in the Analyst tab UI for the API key",
            "ANALYST_OLLAMA_API_KEY env var and docker-compose wiring",
        ],
    },
    {
        "version": "2.4.0",
        "date": "2026-07-07",
        "title": "LLM strategy analyst",
        "notes": [
            "New Analyst tab with toggleable LLM strategy analyst powered "
            "by cloud Ollama (OpenAI-compatible API, DeepSeek Flash by "
            "default)",
            "Post-optimization review: after the nightly grid search the "
            "LLM analyzes ranked results and flags overfitting, small "
            "samples, and parameter drift",
            "Periodic trade review: every few hours during market hours "
            "the LLM analyzes recent closed trades for failure patterns",
            "All analyst config editable from the dashboard — base URL, "
            "model, review interval, trade lookback — no container restart "
            "needed; config persists in runtime_state",
            "Toggle on/off from the dashboard (bot_config); off by default",
            "Reports are advisory only — never auto-applied; fails silently "
            "when the LLM is unreachable",
            "New API endpoints: GET/POST /analyst/config, "
            "GET /analyst/optimization, GET /analyst/trades, "
            "POST /analyst/review, POST /analyst/toggle",
        ],
    },
    {
        "version": "2.3.0",
        "date": "2026-07-07",
        "title": "Command center pro",
        "notes": [
            "Dashboard rebuilt into four tabs: Overview, Trades, Settings, "
            "Logs",
            "Equity curve chart (1H/1D/1W/1M/ALL) from new per-cycle "
            "equity snapshots",
            "Positions show live price, market value and unrealized PnL, "
            "with a per-position close button",
            "Trade analytics: win rate, profit factor, avg win/loss, "
            "best/worst, cumulative realized-PnL chart, full history grid",
            "Market regime, engine heartbeat, cycle trace, cooldowns and "
            "market session shown live — published by the engine into the "
            "shared DB each cycle",
            "Strategy parameters editable from Settings (the optimizer "
            "still re-tunes nightly); operational env shown read-only",
            "Engine resume + run-optimizer-now buttons via the backend "
            "debug API; hard stop now asks for confirmation",
            "Filterable log terminal: level chips, text search, row count, "
            "adjustable refresh interval",
        ],
    },
    {
        "version": "2.2.5",
        "date": "2026-07-07",
        "title": "Project-specific AGENTS.md",
        "notes": [
            "AGENTS.md rewritten with architecture map, safety invariants, "
            "release pipeline, and known pitfalls for future AI agents",
        ],
    },
    {
        "version": "2.2.4",
        "date": "2026-07-07",
        "title": "Fix repeated bracket order rejections",
        "notes": [
            "Brackets are now re-priced off the latest trade right before "
            "submission instead of a possibly-stale 1-minute bar close, "
            "fixing repeated stop_price rejections on tight-ATR symbols",
        ],
    },
    {
        "version": "2.2.3",
        "date": "2026-07-07",
        "title": "Deploy from Docker Hub",
        "notes": [
            "docker-compose.yml now pulls the published davidsteg/argus-* "
            "images (pin with ARGUS_VERSION); local builds via "
            "docker-compose.dev.yml",
            "Release titles no longer double the version",
        ],
    },
    {
        "version": "2.2.2",
        "date": "2026-07-07",
        "title": "CI: tag-only releases with GitHub Releases",
        "notes": [
            "Docker images publish to Docker Hub only on version tags "
            "(subsyncarr-style pipeline); each tag also creates a GitHub "
            "Release with notes from CHANGELOG.md",
        ],
    },
    {
        "version": "2.2.1",
        "date": "2026-07-07",
        "title": "CI: Docker image builds",
        "notes": [
            "GitHub Actions builds and publishes argus-backend / "
            "argus-frontend images to Docker Hub on master pushes and "
            "version tags",
        ],
    },
    {
        "version": "2.2.0",
        "date": "2026-07-07",
        "title": "Adaptive intelligence",
        "notes": [
            "Market regime filter: SPY trend + realized volatility — no "
            "new entries while the whole tape is falling (RISK_OFF)",
            "VWAP dip confirmation: RSI triggers only count when price "
            "is below the session's volume-weighted fair value",
            "Volatility-adaptive brackets: stop/target distances are now "
            "ATR multiples instead of fixed percentages",
            "Risk-based position sizing: each trade risks ~RISK_PER_TRADE_USD "
            "if the stop is hit, capped by POSITION_SIZE_USD",
            "Loser cooldown: a stopped-out symbol is benched for 30 minutes",
            "Optimizer now validates out-of-sample: 75% train / 25% "
            "validation split — parameters that only memorized the past "
            "are rejected",
            "Shared indicators module: live engine and backtests use the "
            "exact same RSI/ATR/VWAP/bracket math",
            "New GET /regime endpoint; /signals and /debug show the full "
            "upgraded decision pipeline",
        ],
    },
    {
        "version": "2.1.0",
        "date": "2026-07-07",
        "title": "Debug API, whole-market trading & smarter sentiment",
        "notes": [
            "Debug & ops API on port 8000: /health, /status, /config, "
            "/debug, /signals, /positions, /trades, /logs, /version",
            "Operational actions: POST /optimize, /kill, /reset — the "
            "engine can now be recovered from KILLED without touching "
            "the database",
            "Whole-market mode: TRADING_SYMBOLS=ALL trades the most "
            "active US equities by volume (dynamic watchlist)",
            "News sentiment upgraded: Alpaca news headlines scored by "
            "Claude when ANTHROPIC_API_KEY is set, keyword heuristic "
            "otherwise (deterministic stub retired to last-resort)",
            "Version & release notes shown in the dashboard header",
        ],
    },
    {
        "version": "2.0.0",
        "date": "2026-07-07",
        "title": "Argus rewrite",
        "notes": [
            "Complete rebuild: async engine (alpaca-py, paper-forced), "
            "thread-safe SQLite state on a shared volume",
            "Bracket orders (take-profit + stop-loss) from live bot_config",
            "Nightly walk-forward grid-search optimizer at midnight "
            "Europe/Zurich",
            "NiceGUI dark dashboard with EMERGENCY HARD STOP",
            "Daily loss kill-sequence and Swiss-midnight PnL baseline",
        ],
    },
    {
        "version": "1.1.0",
        "date": "2026-07-07",
        "title": "Legacy bot — deployed sync",
        "notes": [
            "AI-powered trading bot with ML, hybrid strategies, yfinance",
        ],
    },
    {
        "version": "0.1.0",
        "date": "2026-07-07",
        "title": "Initial project",
        "notes": [
            "FastAPI backend, Streamlit UI, Docker setup",
        ],
    },
]
