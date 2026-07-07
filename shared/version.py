"""
Argus — single source of truth for version and release notes.

The frontend renders this in the header (version chip + release-notes
dialog) and the backend exposes it at GET /version. Keep CHANGELOG.md in
sync when adding a release: newest entry first.
"""

from __future__ import annotations

from typing import Dict, List

__version__ = "2.4.1"

RELEASES: List[Dict[str, object]] = [
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
