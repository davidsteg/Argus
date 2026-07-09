"""
Argus — single source of truth for version and release notes.

The frontend renders this in the header (version chip + release-notes
dialog) and the backend exposes it at GET /version. Keep CHANGELOG.md in
sync when adding a release: newest entry first.
"""

from __future__ import annotations

from typing import Dict, List

__version__ = "2.13.3"

RELEASES: List[Dict[str, object]] = [
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
