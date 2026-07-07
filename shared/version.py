"""
Argus — single source of truth for version and release notes.

The frontend renders this in the header (version chip + release-notes
dialog) and the backend exposes it at GET /version. Keep CHANGELOG.md in
sync when adding a release: newest entry first.
"""

from __future__ import annotations

from typing import Dict, List

__version__ = "2.2.1"

RELEASES: List[Dict[str, object]] = [
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
