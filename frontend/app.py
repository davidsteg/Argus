"""
Argus — NiceGUI command center.

A fully custom, asynchronous dark-themed dashboard organized in four tabs:

Overview   equity curve, live positions with unrealized PnL and per-position
           close, market regime, engine cycle trace, cooldowns
Trades     all-time performance tiles (win rate, profit factor, …),
           cumulative realized-PnL curve, full trade history
Settings   editable strategy parameters (written to the shared bot_config
           table), engine resume + manual optimizer trigger via the backend
           debug API, read-only operational environment, dashboard prefs
Logs       filterable live system log (level chips, text search, row count)

All *data* comes straight from the shared SQLite database (no HTTP hop):
the engine publishes its cycle trace, market regime and environment into
the runtime_state table each cycle. Only *actions* that must reach the
running engine process (resume from KILLED, run the optimizer now) go
through the backend debug API. The EMERGENCY HARD STOP button talks
straight to Alpaca with its own client: cancel all open orders, liquidate
all positions, persist KILLED — independent of whether the backend engine
is alive.

Design follows the original Argus dashboard: near-black canvas, elevated
slate cards, a status dot, and green/red PnL accents.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from nicegui import run, ui

from shared.database import (
    DEFAULT_CONFIG,
    STATUS_KILLED,
    STATUS_RUNNING,
    get_db,
)
from shared.version import RELEASES, __version__

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("argus.frontend")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
# Actions that must reach the running engine (resume, optimize) go through
# the backend debug API on the compose network. Data never does.
BACKEND_API_URL = os.getenv("BACKEND_API_URL", "http://trading_backend:8000")
# Crypto engine (optional): its own DB file in the shared volume + its own debug
# API. When CRYPTO_DB_PATH is set, the dashboard shows an Equities ⇄ Crypto
# switcher; otherwise it's equities-only exactly as before.
CRYPTO_DB_PATH = os.getenv("CRYPTO_DB_PATH", "")
CRYPTO_BACKEND_API_URL = os.getenv("CRYPTO_BACKEND_API_URL", "")
MARKETS = ["equity", "crypto"] if CRYPTO_DB_PATH else ["equity"]


def db_for(market: str):
    """The Database for the selected market (crypto reads its own DB file)."""
    if market == "crypto" and CRYPTO_DB_PATH:
        return get_db(CRYPTO_DB_PATH)
    return get_db()


def api_for(market: str) -> str:
    """The backend debug API base URL for the selected market."""
    if market == "crypto" and CRYPTO_BACKEND_API_URL:
        return CRYPTO_BACKEND_API_URL
    return BACKEND_API_URL


def market_owns(symbol: str, market: str) -> bool:
    """Crypto symbols carry a slash (BTC/USD); equities never do."""
    return ("/" in symbol) if market == "crypto" else ("/" not in symbol)

DEFAULT_REFRESH_SECONDS = 2.0
DEFAULT_LOG_ROWS = 50
TRADES_LIMIT = 200
OPTIMIZER_RUNS_LIMIT = 50

# Palette (Tailwind arbitrary values) — matches the dark look of the
# original dashboard: near-black app background, elevated slate cards.
# Chart hexes validated ≥ 3:1 on the card surface #161b26.
BG_APP = "bg-[#0e1117]"
BG_CARD = "bg-[#161b26]"
BORDER_CARD = "border border-[#2a3140]"
TEXT_MUTED = "text-[#8b93a7]"

SERIES_BLUE = "#3987e5"
PNL_GREEN = "#34d399"
PNL_RED = "#f87171"
CHART_GRID = "#232938"

# Argus mark — the hundred-eyed watchman, reduced to seven: one central
# eye flanked by six smaller ones, in gold on the near-black canvas.
# A single SVG body serves both the header logo and (with a dark rounded
# tile behind it) the browser favicon.
_LOGO_GOLD = """
<defs>
  <linearGradient id="au" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#eecf7a"/>
    <stop offset="0.55" stop-color="#d4a94a"/>
    <stop offset="1" stop-color="#9a7422"/>
  </linearGradient>
  <g id="mini">
    <path d="M-11 0 Q0 -8.5 11 0 Q0 8.5 -11 0 Z" fill="none"
      stroke="url(#au)" stroke-width="2.6" stroke-linejoin="round"/>
    <circle r="3.4" fill="url(#au)"/>
    <circle r="1.5" fill="#10141c"/>
  </g>
</defs>
<use href="#mini" transform="translate(50 13)"/>
<use href="#mini" transform="translate(22 27)"/>
<use href="#mini" transform="translate(78 27)"/>
<use href="#mini" transform="translate(22 73)"/>
<use href="#mini" transform="translate(78 73)"/>
<use href="#mini" transform="translate(50 87)"/>
<path d="M11 50 Q50 23 89 50 Q50 77 11 50 Z" fill="none"
  stroke="url(#au)" stroke-width="3.4" stroke-linejoin="round"/>
<circle cx="50" cy="50" r="13.5" fill="url(#au)"/>
<circle cx="50" cy="50" r="7" fill="#10141c"/>
<circle cx="54.5" cy="45.5" r="2.4" fill="#f6ead0"/>
"""

ARGUS_LOGO_SVG = (
    '<svg viewBox="0 0 100 100" width="40" height="40" '
    'xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Argus">'
    f"{_LOGO_GOLD}</svg>"
)
ARGUS_FAVICON_SVG = (
    '<svg viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">'
    '<rect width="100" height="100" rx="22" fill="#10141c"/>'
    f"{_LOGO_GOLD}</svg>"
)
CHART_INK = "#8b93a7"

LEVEL_COLORS: Dict[str, str] = {
    "CRITICAL": "text-red-500",
    "ERROR": "text-red-400",
    "WARNING": "text-amber-400",
    "TRADE": "text-emerald-400",
    "OPTIMIZER": "text-violet-400",
    "INFO": "text-sky-300",
}

REGIME_STYLES: Dict[str, Dict[str, str]] = {
    "TREND_UP": {"dot": "bg-emerald-400", "text": "text-emerald-400"},
    "CAUTION": {"dot": "bg-yellow-400", "text": "text-yellow-400"},
    "TREND_DOWN": {"dot": "bg-red-500", "text": "text-red-400"},
    "UNKNOWN": {"dot": "bg-gray-500", "text": "text-gray-400"},
}

# Editable strategy parameters — one entry per DEFAULT_CONFIG key. The
# bounds mirror sensible strategy ranges; the nightly optimizer rewrites
# these values anyway, so the UI guards against typos, not bad strategy.
PARAM_META: Dict[str, Dict[str, Any]] = {
    "rsi_period": {
        "label": "RSI Period",
        "hint": "RSI lookback in 1-minute bars",
        "min": 2, "max": 60, "step": 1, "int": True,
    },
    "rsi_buy_signal": {
        "label": "RSI Buy Signal",
        "hint": "enter long when RSI drops below this level",
        "min": 5.0, "max": 50.0, "step": 0.5, "int": False,
    },
    "rsi_exit_signal": {
        "label": "RSI Exit Signal",
        "hint": "close a long early when RSI recovers above this level",
        "min": 50.0, "max": 95.0, "step": 0.5, "int": False,
    },
    "rsi_short_signal": {
        "label": "RSI Short Signal",
        "hint": "enter short when RSI rises above this level",
        "min": 50.0, "max": 95.0, "step": 0.5, "int": False,
    },
    "rsi_short_exit": {
        "label": "RSI Short Exit",
        "hint": "cover a short early when RSI drops below this level",
        "min": 5.0, "max": 50.0, "step": 0.5, "int": False,
    },
    "short_enabled": {
        "label": "Short Selling",
        "hint": "enable short selling (SELL) signals alongside BUY signals",
        "min": 0.0, "max": 1.0, "step": 1.0, "int": True, "toggle": True,
    },
    "news_cutoff": {
        "label": "News Cutoff",
        "hint": "minimum sentiment score to trade (0.50 = neutral/no news)",
        "min": 0.0, "max": 1.0, "step": 0.01, "int": False,
    },
    "max_vwap_dislocation_pct": {
        "label": "Max VWAP Dislocation",
        "hint": "skip entries more than this fraction past VWAP — deeper is a falling knife, not a dip (999 = off)",
        "min": 0.02, "max": 999.0, "step": 0.01, "int": False,
    },
    "atr_stop_mult": {
        "label": "Stop Loss (× ATR)",
        "hint": "bracket stop-loss distance in ATR multiples",
        "min": 0.5, "max": 6.0, "step": 0.1, "int": False,
    },
    "atr_target_mult": {
        "label": "Take Profit (× ATR)",
        "hint": "bracket take-profit distance in ATR multiples",
        "min": 0.5, "max": 10.0, "step": 0.1, "int": False,
    },
}

# Editable watchlist/universe timing — same bot_config table, own card since
# these govern the dynamic universe rather than the entry/exit signal.
WATCHLIST_PARAM_META: Dict[str, Dict[str, Any]] = {
    "watchlist_refresh_minutes": {
        "label": "Screener Refresh",
        "hint": "minutes between most-actives screener refreshes (whole-market mode)",
        "min": 1.0, "max": 120.0, "step": 1.0, "int": True,
    },
    "watchlist_override_ttl_minutes": {
        "label": "Analyst Override TTL",
        "hint": "minutes an LLM-curated watchlist stays live before reverting to the screener",
        "min": 1.0, "max": 240.0, "step": 1.0, "int": True,
    },
}

SCREENER_PARAM_META: Dict[str, Dict[str, Any]] = {
    "screener_enabled": {
        "label": "Opportunity Screener",
        "hint": "scan a wide pool for RSI-oversold + VWAP-dip setups",
        "min": 0.0, "max": 1.0, "step": 1.0, "int": True,
    },
    "screener_pool_size": {
        "label": "Pool Size",
        "hint": "how many most-active symbols to scan each pass",
        "min": 50.0, "max": 500.0, "step": 10.0, "int": True,
    },
    "screener_max_candidates": {
        "label": "Max Candidates",
        "hint": "top N candidates to surface to the dashboard",
        "min": 1.0, "max": 50.0, "step": 1.0, "int": True,
    },
}

ENVIRONMENT_LABELS: Dict[str, str] = {
    "universe_mode": "Universe",
    "regime_symbol": "Regime symbol",
    "engine_version": "Engine version",
    "engine_started_at": "Engine started",
    "paper_trading": "Paper trading",
}

# Editable operational environment — these used to be env vars, now live
# in bot_config so they can be tuned from the dashboard without a restart.
OPERATIONAL_PARAM_META: Dict[str, Dict[str, Any]] = {
    "position_size_usd": {
        "label": "Position Size (USD)",
        "hint": "max notional per trade",
        "min": 100.0, "max": 100000.0, "step": 100.0, "int": True,
    },
    "risk_per_trade_usd": {
        "label": "Risk per Trade (USD)",
        "hint": "dollar amount to risk per trade (0 = notional-only sizing)",
        "min": 0.0, "max": 10000.0, "step": 5.0, "int": True,
    },
    "max_positions": {
        "label": "Max Positions",
        "hint": "concurrent position slots",
        "min": 1.0, "max": 50.0, "step": 1.0, "int": True,
    },
    "daily_stop_loss": {
        "label": "Daily Stop Loss (USD)",
        "hint": "hard daily loss limit before emergency kill",
        "min": 10.0, "max": 100000.0, "step": 10.0, "int": True,
    },
    "min_price_usd": {
        "label": "Min Price (USD)",
        "hint": "skip symbols below this price",
        "min": 1.0, "max": 100.0, "step": 0.5, "int": False,
    },
    "cooldown_minutes": {
        "label": "Loser Cooldown (min)",
        "hint": "minutes to bench a symbol after a losing exit",
        "min": 0.0, "max": 480.0, "step": 5.0, "int": True,
    },
    "poll_interval_seconds": {
        "label": "Poll Interval (s)",
        "hint": "seconds between trading cycles",
        "min": 10.0, "max": 300.0, "step": 5.0, "int": True,
    },
    "bar_lookback_minutes": {
        "label": "Bar Lookback (min)",
        "hint": "minutes of 1-minute bars to fetch per cycle",
        "min": 30.0, "max": 1440.0, "step": 10.0, "int": True,
    },
    "watchlist_size": {
        "label": "Watchlist Size",
        "hint": "top N most-active symbols (whole-market mode, max 100)",
        "min": 5.0, "max": 100.0, "step": 5.0, "int": True,
    },
    "eod_flatten_minutes": {
        "label": "EOD Flatten (min)",
        "hint": "close all positions this many minutes before the bell (0 = allow unprotected overnight holds)",
        "min": 0.0, "max": 60.0, "step": 1.0, "int": True,
    },
}

EQUITY_RANGES: Dict[str, Optional[timedelta]] = {
    "1H": timedelta(hours=1),
    "1D": timedelta(days=1),
    "1W": timedelta(weeks=1),
    "1M": timedelta(days=30),
    "ALL": None,
}


# ---------------------------------------------------------------------- #
# blocking data access & actions (run via run.io_bound)
# ---------------------------------------------------------------------- #


def fetch_snapshot(
    log_limit: int, equity_since: Optional[str], market: str = "equity"
) -> Dict[str, Any]:
    """Blocking read of everything the dashboard shows (run via io_bound)."""
    db = db_for(market)
    status = db.get_status()
    local_midnight = (
        datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    )
    return {
        "status": status,
        "daily_pnl": status["equity"] - status["daily_start_balance"],
        "positions": db.get_positions(),
        "config": db.get_config(),
        "config_updated_at": db.get_config_updated_at(),
        "logs": db.get_logs(log_limit),
        "equity_history": db.get_equity_history(since=equity_since),
        "trades": db.get_trades(TRADES_LIMIT),
        "trade_stats": db.get_trade_stats(),
        "realized_today": db.realized_pnl_since(
            local_midnight.astimezone(timezone.utc).isoformat(timespec="seconds")
        ),
        "last_cycle": db.get_state("last_cycle", {}) or {},
        "open_entries": db.get_state("open_entries", {}) or {},
        "environment": db.get_state("environment", {}) or {},
        "analyst_optimization": db.get_state("analyst_optimization"),
        "analyst_trades": db.get_state("analyst_trades"),
        "analyst_health": db.get_state("analyst_health"),
        "protection_health": db.get_state("protection_health"),
        "veto_stats": db.get_veto_stats(),
        "analyst_config": db.get_state("analyst_config") or {},
        "analyst_call_log": db.get_state("analyst_call_log") or [],
        "analyst_review_history": db.get_state("analyst_review_history") or [],
        "screener_candidates": db.get_state("screener_candidates") or [],
        "optimizer_runs": db.get_optimizer_runs(OPTIMIZER_RUNS_LIMIT),
        "optimizer_status": db.get_state("optimizer_status", {"phase": "idle"}),
    }


def _limit_close_order(trading, position, market: str = "equity") -> None:
    """Submit a marketable limit close for one position.

    Market/close_all_positions liquidation is rejected outside the regular
    session (and for crypto entirely), so the dashboard controls — like the
    engine — close with a limit priced exit_slip_pct through the position's
    price. Equities: extended-hours DAY. Crypto: GTC, no extended_hours."""
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import LimitOrderRequest

    raw_qty = float(position.qty)
    qty = abs(raw_qty)
    if qty <= 0:
        return
    is_long = raw_qty > 0
    ref = (
        position.current_price
        if position.current_price is not None
        else position.avg_entry_price
    )
    price = float(ref)
    slip = db_for(market).get_config().get("exit_slip_pct", 0.002)
    is_crypto = market == "crypto"
    if is_long:
        limit_price = round(price * (1 - slip), 2)
        if not is_crypto:
            limit_price = max(limit_price, 0.01)
    else:
        limit_price = round(price * (1 + slip), 2)
    kwargs = dict(
        symbol=position.symbol,
        qty=qty,
        side=OrderSide.SELL if is_long else OrderSide.BUY,
        time_in_force=TimeInForce.GTC if is_crypto else TimeInForce.DAY,
        limit_price=limit_price,
    )
    if not is_crypto:
        kwargs["extended_hours"] = True
    trading.submit_order(LimitOrderRequest(**kwargs))


def execute_hard_stop(market: str = "equity") -> Dict[str, Any]:
    """Blocking emergency intervention (run via io_bound).

    Talks directly to Alpaca — cancel and flatten only THIS market's orders and
    positions (one account serves both engines) — and persists KILLED to this
    market's DB so its engine stops on the next cycle and refuses to restart.
    """
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    db = db_for(market)
    db.add_log("CRITICAL", "EMERGENCY HARD STOP triggered from dashboard")
    errors = []
    try:
        trading = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
        try:
            # Cancel only this market's open orders — cancel_orders() is
            # account-wide and would kill the other engine's orders too.
            for order in trading.get_orders(
                GetOrdersRequest(status=QueryOrderStatus.OPEN)
            ):
                if market_owns(order.symbol, market):
                    trading.cancel_order_by_id(order.id)
            db.add_log("CRITICAL", "Open orders cancelled (dashboard)")
        except Exception as exc:
            errors.append(f"cancel orders: {exc}")
            db.add_log("ERROR", f"Dashboard cancel-all failed: {exc}")
        try:
            # Close each of this market's positions with a marketable limit
            # order (market liquidation is rejected pre/post-market and crypto).
            for position in trading.get_all_positions():
                if market_owns(position.symbol, market):
                    _limit_close_order(trading, position, market)
            db.add_log("CRITICAL", "All positions liquidated (dashboard)")
        except Exception as exc:
            errors.append(f"liquidate: {exc}")
            db.add_log("ERROR", f"Dashboard liquidation failed: {exc}")
    except Exception as exc:
        errors.append(f"alpaca client: {exc}")
        db.add_log("ERROR", f"Dashboard could not reach Alpaca: {exc}")

    # KILLED is written unconditionally: even if Alpaca was unreachable the
    # engine must stop trading immediately.
    db.set_status(status=STATUS_KILLED)
    db.add_log("CRITICAL", "Bot state set to KILLED (dashboard)")
    return {"errors": errors}


def close_single_position(symbol: str, market: str = "equity") -> Optional[str]:
    """Blocking marketable-limit close of one position via the dashboard's own
    Alpaca client (paper trading, like the hard stop). Returns an error message
    or None on success."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    db = db_for(market)
    db.add_log("WARNING", f"{symbol}: manual close requested from dashboard")
    try:
        trading = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
        # Cancel any resting orders first (an unfilled entry limit would hold
        # the shares), then submit a marketable limit close.
        open_orders = trading.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        )
        for order in open_orders:
            trading.cancel_order_by_id(order.id)
        position = next(
            (p for p in trading.get_all_positions() if p.symbol == symbol), None
        )
        if position is None:
            return f"{symbol}: no open position to close"
        _limit_close_order(trading, position, market)
        db.add_log("TRADE", f"{symbol}: manual close submitted (dashboard)")
        return None
    except Exception as exc:
        db.add_log("ERROR", f"{symbol}: dashboard close failed: {exc}")
        return str(exc)


def call_backend(path: str, timeout: float, market: str = "equity") -> Dict[str, Any]:
    """Blocking POST to the selected market's backend debug API (run via
    io_bound)."""
    url = f"{api_for(market).rstrip('/')}{path}"
    request = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode() or "{}")
            return {"ok": True, "data": payload}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode()).get("detail", str(exc))
        except Exception:
            detail = str(exc)
        return {"ok": False, "error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def call_backend_json(
    path: str, body: Dict[str, Any], timeout: float, market: str = "equity"
) -> Dict[str, Any]:
    """Blocking POST with JSON body to the selected market's backend debug API."""
    url = f"{api_for(market).rstrip('/')}{path}"
    data = json.dumps(body).encode()
    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode() or "{}")
            return {"ok": True, "data": payload}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode()).get("detail", str(exc))
        except Exception:
            detail = str(exc)
        return {"ok": False, "error": f"HTTP {exc.code}: {detail}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def fetch_trade_bars(
    symbol: str, entry_iso: str, exit_iso: Optional[str]
) -> List[List[float]]:
    """Blocking fetch of 1-minute bars spanning a closed trade's hold window
    (run via io_bound). Returns ``[[epoch_ms, close], …]`` for the per-trade
    info chart, padded a few minutes either side so the entry and exit sit
    inside the frame. Any failure (no data, feed hiccup) returns an empty list
    and the dialog falls back to a 'chart unavailable' note."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import (
        CryptoHistoricalDataClient,
        StockHistoricalDataClient,
    )
    from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    start = to_local(entry_iso)
    end = to_local(exit_iso) if exit_iso else None
    if start is None:
        return []
    end = end or (start + timedelta(hours=1))
    pad = timedelta(minutes=10)
    is_crypto = "/" in symbol
    try:
        if is_crypto:
            client = CryptoHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
            request = CryptoBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=(start - pad).astimezone(timezone.utc),
                end=(end + pad).astimezone(timezone.utc),
            )
            bars = client.get_crypto_bars(request)
        else:
            client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=(start - pad).astimezone(timezone.utc),
                end=(end + pad).astimezone(timezone.utc),
                feed=DataFeed.IEX,
            )
            bars = client.get_stock_bars(request)
    except Exception as exc:  # noqa: BLE001 — best-effort, chart is optional
        logger.warning("Trade-bar fetch failed for %s: %s", symbol, exc)
        return []

    series: List[List[float]] = []
    for bar in bars.data.get(symbol, []):
        ts = bar.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        series.append([ts.timestamp() * 1000.0, round(float(bar.close), 4)])
    return series


# ---------------------------------------------------------------------- #
# formatting helpers
# ---------------------------------------------------------------------- #


def money(value: Optional[float], signed: bool = False) -> str:
    if value is None:
        return "—"
    prefix = "-" if value < 0 else ("+" if signed else "")
    return f"{prefix}${abs(value):,.2f}"


def pnl_text_class(value: Optional[float], neutral: str = "text-gray-300") -> str:
    if value is None or value == 0:
        return neutral
    return "text-emerald-400" if value > 0 else "text-red-400"


def to_local(iso: Optional[str]) -> Optional[datetime]:
    if not iso:
        return None
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone()


def fmt_clock(iso: Optional[str]) -> str:
    local = to_local(iso)
    return local.strftime("%H:%M:%S") if local else "—"


def fmt_short(iso: Optional[str]) -> str:
    local = to_local(iso)
    return local.strftime("%d.%m %H:%M") if local else "—"


def age_seconds(iso: Optional[str]) -> Optional[float]:
    local = to_local(iso)
    if local is None:
        return None
    return (datetime.now().astimezone() - local).total_seconds()


def humanize_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "never"
    if seconds < 90:
        return f"{seconds:.0f}s ago"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"


# The seven LLM call types, keyed by the `agent` field written to the
# analyst_call_log by the backend. `model_field` names the analyst-config
# override that selects the model (empty override → main `model`).
ANALYST_AGENTS: List[Dict[str, str]] = [
    {
        "key": "risk", "icon": "🛡️", "name": "Risk Agent",
        "what": "Evaluates every signal before execution — can block the trade",
        "when": "per signal, each cycle",
        "model_field": "risk_model",
    },
    {
        "key": "portfolio", "icon": "🧭", "name": "Portfolio Manager",
        "what": "Ranks approved signals and decides which get a position slot",
        "when": "each cycle with signals",
        "model_field": "model",
    },
    {
        "key": "sentiment", "icon": "📰", "name": "Sentiment Scorer",
        "what": "Scores news headlines 0–1 bearish→bullish, gates entries",
        "when": "per symbol, cached 15m",
        "model_field": "sentiment_model",
    },
    {
        "key": "watchlist", "icon": "📋", "name": "Watchlist Curator",
        "what": "Curates the trading watchlist from the live screener pool",
        "when": "hourly",
        "model_field": "watchlist_model",
    },
    {
        "key": "trades", "icon": "📊", "name": "Trade Reviewer",
        "what": "Reviews recent closed trades for failure patterns",
        "when": "every few hours (market)",
        "model_field": "model",
    },
    {
        "key": "optimization", "icon": "🔬", "name": "Optimization Reviewer",
        "what": "Accepts, overrides or rejects the nightly optimizer winner",
        "when": "after the nightly grid search",
        "model_field": "model",
    },
    {
        "key": "memory", "icon": "🧠", "name": "Decision Memory",
        "what": "Extracts lessons from past decision → outcome pairs",
        "when": "every 50 cycles",
        "model_field": "model",
    },
]


def agent_call_stats(call_log: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per-agent aggregates from the raw analyst_call_log blob: 24h call and
    error counts, average latency, and the most recent call's outcome."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    stats: Dict[str, Dict[str, Any]] = {}
    for entry in call_log:
        agent = entry.get("agent", "unknown")
        agg = stats.setdefault(agent, {
            "calls": 0, "errors": 0, "latency_sum": 0.0,
            "last_ts": None, "last_ok": None, "last_error": None,
        })
        agg["last_ts"] = entry.get("ts")
        agg["last_ok"] = entry.get("ok", False)
        agg["last_error"] = entry.get("error") if not entry.get("ok", False) else None
        local = to_local(entry.get("ts"))
        if local is None or local < cutoff.astimezone():
            continue
        agg["calls"] += 1
        if not entry.get("ok", False):
            agg["errors"] += 1
        agg["latency_sum"] += float(entry.get("latency_ms", 0))
    for agg in stats.values():
        agg["avg_latency_ms"] = (
            agg.pop("latency_sum") / agg["calls"] if agg["calls"] else None
        )
    return stats


def fmt_latency(ms: Optional[float]) -> str:
    if ms is None:
        return "—"
    return f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms:.0f}ms"


def humanize_duration(entry_iso: Optional[str], exit_iso: Optional[str]) -> str:
    start, end = to_local(entry_iso), to_local(exit_iso)
    if start is None or end is None:
        return "—"
    minutes = max((end - start).total_seconds() / 60.0, 0)
    if minutes < 60:
        return f"{minutes:.0f}m"
    return f"{int(minutes // 60)}h {int(minutes % 60):02d}m"


def epoch_ms(iso: str) -> Optional[float]:
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp() * 1000.0


# ---------------------------------------------------------------------- #
# chart configuration (echarts on the dark card surface)
# ---------------------------------------------------------------------- #


def _base_time_chart(color: str, area: bool) -> Dict[str, Any]:
    options: Dict[str, Any] = {
        "backgroundColor": "transparent",
        "animation": False,
        "grid": {
            "left": 8, "right": 16, "top": 24, "bottom": 8, "containLabel": True,
        },
        "tooltip": {
            "trigger": "axis",
            "backgroundColor": "#1d2432",
            "borderColor": "#2a3140",
            "textStyle": {"color": "#e5e7eb", "fontSize": 12},
            "axisPointer": {
                "type": "line",
                "lineStyle": {"color": color, "opacity": 0.5},
            },
            ":formatter": (
                "(p) => {"
                " const d = Array.isArray(p) ? p[0] : p;"
                " const t = new Date(d.value[0]);"
                " const v = d.value[1];"
                " const money = (v < 0 ? '-$' : '$') +"
                "   Math.abs(v).toLocaleString(undefined,"
                "     {minimumFractionDigits: 2, maximumFractionDigits: 2});"
                " return t.toLocaleString([], {month: 'short', day: 'numeric',"
                "   hour: '2-digit', minute: '2-digit'})"
                "   + '<br/><b>' + money + '</b>';"
                "}"
            ),
        },
        "xAxis": {
            "type": "time",
            "axisLine": {"lineStyle": {"color": CHART_GRID}},
            "axisLabel": {"color": CHART_INK, "fontSize": 11},
            "splitLine": {"show": False},
        },
        "yAxis": {
            "type": "value",
            "scale": True,
            "axisLabel": {
                "color": CHART_INK,
                "fontSize": 11,
                ":formatter": (
                    "(v) => (v < 0 ? '-$' : '$') +"
                    " Math.abs(v).toLocaleString(undefined,"
                    "   {maximumFractionDigits: 0})"
                ),
            },
            "splitLine": {"lineStyle": {"color": CHART_GRID}},
        },
        "series": [
            {
                "type": "line",
                "showSymbol": False,
                "lineStyle": {"width": 2, "color": color},
                "itemStyle": {"color": color},
                "data": [],
            }
        ],
    }
    if area:
        options["series"][0]["areaStyle"] = {
            "color": {
                "type": "linear",
                "x": 0, "y": 0, "x2": 0, "y2": 1,
                "colorStops": [
                    {"offset": 0, "color": "rgba(57, 135, 229, 0.28)"},
                    {"offset": 1, "color": "rgba(57, 135, 229, 0.0)"},
                ],
            }
        }
    return options


def equity_chart_options() -> Dict[str, Any]:
    return _base_time_chart(SERIES_BLUE, area=True)


def trade_hold_chart_options() -> Dict[str, Any]:
    """Price chart for a single trade's hold window (per-trade info popup).

    Reuses the dark time-axis base but formats both the axis and the tooltip
    as share prices rather than account dollars; entry/exit markers and the
    stop/target lines are layered on at populate time in show_trade_info."""
    options = _base_time_chart(SERIES_BLUE, area=True)
    price_fmt = (
        "(v) => '$' + Number(v).toLocaleString(undefined,"
        " {minimumFractionDigits: 2, maximumFractionDigits: 2})"
    )
    options["yAxis"]["axisLabel"][":formatter"] = price_fmt
    options["tooltip"][":formatter"] = (
        "(p) => {"
        " const d = Array.isArray(p) ? p[0] : p;"
        " const t = new Date(d.value[0]);"
        " const v = d.value[1];"
        " return t.toLocaleString([], {month: 'short', day: 'numeric',"
        "   hour: '2-digit', minute: '2-digit'})"
        "   + '<br/><b>$' + Number(v).toLocaleString(undefined,"
        "     {minimumFractionDigits: 2, maximumFractionDigits: 2}) + '</b>';"
        "}"
    )
    return options


def cumulative_pnl_chart_options() -> Dict[str, Any]:
    options = _base_time_chart(PNL_GREEN, area=False)
    # Zero baseline: cumulative PnL is read against it (above = net profit).
    options["series"][0]["markLine"] = {
        "silent": True,
        "symbol": "none",
        "label": {"show": False},
        "lineStyle": {"color": "#5a6274", "type": "dashed", "width": 1},
        "data": [{"yAxis": 0}],
    }
    return options


# ---------------------------------------------------------------------- #
# page
# ---------------------------------------------------------------------- #


@ui.page("/")
def dashboard() -> None:
    ui.dark_mode().enable()
    ui.query("body").classes(f"{BG_APP}")
    # Home-screen/app-shell hints: dark browser chrome, safe-area support on
    # notched phones, standalone mode when added to the home screen.
    ui.add_head_html(
        '<meta name="viewport" content="width=device-width, initial-scale=1,'
        ' viewport-fit=cover"/>'
        '<meta name="theme-color" content="#0e1117"/>'
        '<meta name="mobile-web-app-capable" content="yes"/>'
        '<meta name="apple-mobile-web-app-capable" content="yes"/>'
        '<meta name="apple-mobile-web-app-status-bar-style"'
        ' content="black-translucent"/>'
    )
    ui.add_head_html(
        """<style>
        .nicegui-content { padding: 0 !important; }
        .pnl-pos { color: #34d399 !important; }
        .pnl-neg { color: #f87171 !important; }
        .pos-grid { display: grid;
          grid-template-columns: 1.1fr 0.5fr 0.7fr 1fr 1fr 1.1fr 1.5fr 2.75rem 2.75rem;
          column-gap: 0.5rem; align-items: center; width: 100%; }
        /* Trade-history grid: a hand-built grid (not aggrid) so it lives inside
           the card cleanly and carries a per-row info button. A trailing fixed
           slot holds the ℹ action; the body scrolls vertically past ~32rem. */
        .trades-grid { display: grid;
          grid-template-columns:
            1.3fr 0.9fr 0.55fr 0.55fr 0.85fr 0.85fr 1.1fr 0.85fr 0.8fr 2.5rem;
          column-gap: 0.5rem; align-items: center; width: 100%; }
        .trades-scroll { max-height: 32rem; overflow-y: auto; }
        .row-hover { transition: background-color .15s; }
        .row-hover:hover { background: rgba(57, 135, 229, .07); }
        /* Brand-gold active tab — ties the nav to the Argus mark. */
        .argus-tabs .q-tab__indicator { background: #d4a94a; }
        .argus-tabs .q-tab--active { color: #eecf7a; }
        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-thumb { background: #2a3140; border-radius: 4px; }
        ::-webkit-scrollbar-track { background: transparent; }

        @media (max-width: 640px) {
          /* The bottom nav owns navigation on phones — the header can scroll
             away instead of eating a third of the viewport. */
          .argus-header { position: static !important; }
          /* Compact header actions (RESUME / HARD STOP) on phones — the
             label element inside hides itself via Tailwind (hidden sm:block). */
          .btn-compact { padding: 6px 10px !important; }
          /* Positions collapse to Symbol / Side / PnL + actions; Qty, Entry,
             Now and Value live in the ℹ popup. No sideways scrolling. */
          .pos-grid { grid-template-columns: 1.2fr 0.7fr 1.8fr 2.4rem 2.4rem; }
          .pos-grid > :nth-child(3), .pos-grid > :nth-child(4),
          .pos-grid > :nth-child(5), .pos-grid > :nth-child(6) { display: none; }
          /* Trades collapse to Closed / Symbol / PnL / PnL% + ℹ. */
          .trades-grid { grid-template-columns: 1.1fr 0.9fr 1.1fr 0.9fr 2.2rem; }
          .trades-grid > :nth-child(3), .trades-grid > :nth-child(4),
          .trades-grid > :nth-child(5), .trades-grid > :nth-child(6),
          .trades-grid > :nth-child(9) { display: none; }
          .scroll-x-mobile { overflow-x: auto; -webkit-overflow-scrolling: touch; }
          /* Tabs become a fixed bottom navigation bar — thumb reach. */
          .argus-tabs {
            position: fixed; bottom: 0; left: 0; right: 0; z-index: 60;
            background: rgba(19, 23, 34, .92);
            -webkit-backdrop-filter: blur(12px); backdrop-filter: blur(12px);
            border-top: 1px solid #2a3140; border-bottom: none !important;
            padding-bottom: env(safe-area-inset-bottom);
          }
          .argus-tabs .q-tab { min-width: 0; padding: 0 2px; }
          .argus-tabs .q-tab__label { font-size: 10px; }
          .argus-tabs .q-tab__icon { font-size: 22px; }
          /* Keep the last card clear of the bottom nav. */
          .q-tab-panel { padding-bottom: calc(64px + env(safe-area-inset-bottom)) !important; }
        }
        </style>"""
    )

    # Active market for this browser session (equities by default). The header
    # switcher flips it; every DB read/write and backend action below resolves
    # through cur_db()/ui_state so the whole dashboard follows the selection.
    ui_state = {"market": MARKETS[0]}

    def is_crypto() -> bool:
        return ui_state["market"] == "crypto"

    _market_vis_tracked: list = []

    def _track_visibility(element, show_for_crypto: bool = False):
        _market_vis_tracked.append((element, show_for_crypto))

    def _update_market_visibility():
        crypto = is_crypto()
        for element, show_for_crypto in _market_vis_tracked:
            try:
                element.set_visibility(show_for_crypto if crypto else True)
            except Exception:
                pass

    def cur_db():
        return db_for(ui_state["market"])

    initial_config = cur_db().get_config()

    # ------------------------------------------------------------------ #
    # small builders
    # ------------------------------------------------------------------ #

    def card(extra: str = "") -> ui.card:
        return ui.card().classes(
            f"w-full {BG_CARD} {BORDER_CARD} rounded-xl shadow-lg {extra}"
        )

    def card_title(text: str, caption: str = "") -> None:
        with ui.row().classes("w-full items-baseline justify-between"):
            ui.label(text).classes("text-lg font-semibold text-white")
            if caption:
                ui.label(caption).classes(f"text-xs {TEXT_MUTED}")

    def kv_row(title: str) -> ui.label:
        with ui.row().classes(
            "w-full justify-between py-1 border-b border-[#222938] flex-nowrap"
        ):
            ui.label(title).classes(f"text-sm {TEXT_MUTED} shrink-0")
            value = ui.label("—").classes("text-sm font-mono text-white text-right")
        return value

    def stat_tile(label: str) -> Dict[str, ui.label]:
        with ui.column().classes(
            f"gap-0 {BG_CARD} {BORDER_CARD} rounded-xl px-4 py-3 grow basis-0 min-w-[9rem]"
        ):
            ui.label(label.upper()).classes(f"text-xs {TEXT_MUTED}")
            value = ui.label("—").classes("text-xl font-semibold text-white")
            sub = ui.label("").classes(f"text-xs {TEXT_MUTED}")
        return {"value": value, "sub": sub}

    async def confirm(title: str, message: str, action: str) -> bool:
        with ui.dialog() as dialog, ui.card().classes(
            f"{BG_CARD} {BORDER_CARD} rounded-xl max-w-md"
        ):
            ui.label(title).classes("text-lg font-semibold text-white")
            ui.label(message).classes("text-sm text-gray-300")
            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("Cancel", on_click=lambda: dialog.submit(False)).props(
                    "flat no-caps"
                )
                ui.button(action, on_click=lambda: dialog.submit(True)).props(
                    "no-caps color=red-10 push"
                ).classes("bg-red-700 text-white font-bold")
        result = await dialog
        dialog.clear()
        return bool(result)

    # ------------------------------------------------------------------ #
    # release notes dialog (opened from the version chip in the header)
    # ------------------------------------------------------------------ #
    with ui.dialog() as release_dialog, ui.card().classes(
        f"{BG_CARD} {BORDER_CARD} rounded-xl w-full max-w-2xl"
    ):
        with ui.row().classes("w-full items-center justify-between"):
            ui.label("📦 Release Notes").classes("text-lg font-semibold text-white")
            ui.button(icon="close", on_click=release_dialog.close).props(
                "flat round dense"
            )
        with ui.column().classes("w-full gap-0 max-h-[32rem] overflow-y-auto"):
            for release in RELEASES:
                with ui.column().classes(
                    "w-full gap-1 py-3 border-b border-[#222938]"
                ):
                    with ui.row().classes("items-baseline gap-3"):
                        ui.label(f"v{release['version']}").classes(
                            "font-mono font-bold text-emerald-400"
                        )
                        ui.label(str(release["date"])).classes(f"text-xs {TEXT_MUTED}")
                        ui.label(str(release.get("title", ""))).classes(
                            "text-sm font-semibold text-white"
                        )
                    for note in release["notes"]:
                        ui.label(f"• {note}").classes("text-xs text-gray-300")

    # ------------------------------------------------------------------ #
    # trade info dialog (opened from the ℹ button on any trade-history row)
    # ------------------------------------------------------------------ #
    # One reusable dialog, refilled per trade — cheaper and cleaner than
    # minting 200 dialogs up front. show_trade_info() populates the header,
    # the hold-window price chart and the details body, then fetches bars.
    with ui.dialog() as trade_info_dialog, ui.card().classes(
        f"{BG_CARD} {BORDER_CARD} rounded-xl w-full max-w-2xl"
    ):
        with ui.row().classes("w-full items-center justify-between"):
            trade_info_title = ui.row().classes("items-center gap-3 min-w-0")
            ui.button(icon="close", on_click=trade_info_dialog.close).props(
                "flat round dense"
            )
        with ui.column().classes("w-full gap-3 max-h-[80vh] overflow-y-auto"):
            with ui.element("div").classes("w-full"):
                ui.label("Price during the hold").classes(
                    f"text-xs font-semibold uppercase {TEXT_MUTED}"
                )
                trade_info_chart = ui.echart(
                    trade_hold_chart_options()
                ).classes("w-full h-56")
                trade_info_chart_empty = ui.label("").classes(
                    f"text-sm {TEXT_MUTED}"
                )
            trade_info_body = ui.column().classes("w-full gap-3")

    # ------------------------------------------------------------------ #
    # header banner
    # ------------------------------------------------------------------ #
    with ui.row().classes(
        "argus-header w-full items-center justify-between gap-y-2 sm:gap-y-3 "
        "flex-wrap px-3 sm:px-6 py-2.5 sm:py-4 "
        "bg-gradient-to-r from-[#131722] to-[#1a2030] "
        "border-b border-[#2a3140] sticky top-0 z-50"
    ):
        with ui.row().classes("items-center gap-3"):
            ui.html(ARGUS_LOGO_SVG).classes("shrink-0")
            with ui.column().classes("gap-0"):
                with ui.row().classes("items-center gap-2"):
                    ui.label("ARGUS").classes(
                        "text-xl sm:text-2xl font-bold tracking-widest text-white"
                    )
                    ui.button(f"v{__version__}", on_click=release_dialog.open).props(
                        "flat dense no-caps"
                    ).classes(
                        "text-xs font-mono text-emerald-400 bg-[#1d2432] "
                        "px-2 py-0.5 rounded border border-[#2a3140]"
                    ).tooltip("Release notes")
                # max-sm:hidden, not `hidden sm:block`: Quasar's own .hidden
                # is !important in an earlier cascade layer and would keep
                # the element hidden at every width.
                ui.label("Short-Term Algorithmic Trading — Paper").classes(
                    f"text-xs {TEXT_MUTED} max-sm:hidden"
                )

        # Market switcher — only shown when a crypto engine is configured.
        # Flips which engine's DB/API the whole dashboard reads and acts on.
        if len(MARKETS) > 1:
            async def on_market_change(event: Any) -> None:
                ui_state["market"] = event.value or MARKETS[0]
                _update_market_visibility()
                await refresh()

            ui.toggle(
                {"equity": "Equities", "crypto": "Crypto"},
                value=ui_state["market"],
                on_change=on_market_change,
            ).props("no-caps dense").classes("text-xs")

        # live condition chips: regime, market session, engine heartbeat
        with ui.row().classes("items-center gap-2 flex-wrap"):
            def chip() -> Dict[str, Any]:
                holder = ui.row().classes(
                    f"items-center gap-2 {BG_CARD} {BORDER_CARD} "
                    "rounded-full px-3 py-1"
                )
                with holder:
                    dot = ui.element("div").classes(
                        "w-2 h-2 rounded-full bg-gray-500"
                    )
                    text = ui.label("…").classes("text-xs font-semibold text-gray-300")
                return {"holder": holder, "dot": dot, "text": text}

            regime_chip = chip()
            market_chip = chip()
            engine_chip = chip()
            regime_chip["holder"].tooltip(
                "Market regime gates new entries only (index below its EMA "
                "= no new buys, stressed or calm; blocked signals are "
                "shadow-tracked). Evaluated while the market is open."
            )
            market_chip["holder"].tooltip(
                "Crypto market — 24/7" if is_crypto() else "US equity market session"
            )
            engine_chip["holder"].tooltip(
                "Heartbeat of the backend trading engine — LIVE while cycle "
                "traces keep arriving within 3× the poll interval"
            )

        with ui.row().classes(
            "items-center gap-2 sm:gap-8 flex-wrap "
            "w-full sm:w-auto justify-between sm:justify-end"
        ):
            with ui.column().classes("gap-0 items-start sm:items-end"):
                ui.label("TOTAL BALANCE").classes(f"text-xs {TEXT_MUTED}")
                balance_label = ui.label("$0.00").classes(
                    "text-lg sm:text-xl font-semibold text-white"
                )
            with ui.column().classes("gap-0 items-start sm:items-end"):
                ui.label("DAILY PNL").classes(f"text-xs {TEXT_MUTED}")
                pnl_label = ui.label("$0.00").classes(
                    "text-lg sm:text-xl font-semibold text-green-400"
                )
                pnl_pct_label = ui.label("").classes(f"text-xs {TEXT_MUTED}")
            with ui.column().classes("gap-0 items-start sm:items-end"):
                ui.label("STATUS").classes(f"text-xs {TEXT_MUTED}")
                status_label = ui.label("● …").classes(
                    "text-lg sm:text-xl font-semibold text-green-400"
                )

            async def on_emergency_click() -> None:
                if not await confirm(
                    "EMERGENCY HARD STOP",
                    "Cancel ALL open orders, liquidate ALL positions and set "
                    "the bot state to KILLED. The engine stops trading "
                    "immediately and stays down until resumed.",
                    "FLATTEN EVERYTHING",
                ):
                    return
                emergency_button.disable()
                ui.notify(
                    "EMERGENCY HARD STOP — cancelling orders and liquidating…",
                    type="warning",
                )
                result = await run.io_bound(execute_hard_stop, ui_state["market"])
                if result["errors"]:
                    ui.notify(
                        "Hard stop finished with errors: "
                        + "; ".join(result["errors"]),
                        type="negative",
                        timeout=10000,
                    )
                else:
                    ui.notify(
                        "Hard stop complete — all flat, state KILLED",
                        type="negative",
                    )

            async def on_resume_click() -> None:
                if not await confirm(
                    "Resume engine",
                    "Clear the KILLED state and restart the trading engine "
                    "via the backend API (POST /reset).",
                    "RESUME",
                ):
                    return
                resume_button.disable()
                ui.notify("Resuming engine…", type="info")
                result = await run.io_bound(
                    call_backend, "/reset", 15.0, ui_state["market"]
                )
                if result["ok"]:
                    ui.notify("Engine resumed — state RUNNING", type="positive")
                else:
                    ui.notify(
                        f"Resume failed: {result['error']}",
                        type="negative",
                        timeout=10000,
                    )
                    resume_button.enable()

            # The label lives inside the button as a Tailwind-responsive
            # element so phones get an icon-only button (the icon + tooltip
            # carry the meaning) while desktop keeps the full wording.
            resume_button = ui.button(
                icon="play_arrow", on_click=on_resume_click
            ).classes(
                "btn-compact bg-emerald-700 hover:bg-emerald-600 text-white "
                "font-bold px-3 sm:px-5 py-2 sm:py-3 rounded-lg shadow-lg "
                "shadow-emerald-900/40"
            ).props("color=green-10 push").tooltip("Resume the trading engine")
            with resume_button:
                ui.label("RESUME").classes("max-sm:hidden ml-2")
            resume_button.set_visibility(False)

            emergency_button = ui.button(
                icon="dangerous", on_click=on_emergency_click
            ).classes(
                "btn-compact bg-red-700 hover:bg-red-600 text-white font-bold "
                "px-3 sm:px-5 py-2 sm:py-3 rounded-lg shadow-lg "
                "shadow-red-900/40"
            ).props("color=red-10 push").tooltip(
                "Cancel all orders, liquidate everything, stop the engine"
            )
            with emergency_button:
                ui.label("EMERGENCY HARD STOP").classes("max-sm:hidden ml-2")

    # ------------------------------------------------------------------ #
    # tabs
    # ------------------------------------------------------------------ #
    with ui.tabs().classes(
        "argus-tabs w-full px-2 sm:px-6 bg-[#131722] "
        "border-b border-[#2a3140] text-gray-300"
    ) as tabs:
        overview_tab = ui.tab("Overview", icon="dashboard")
        trades_tab = ui.tab("Trades", icon="receipt_long")
        analyst_tab = ui.tab("Analyst", icon="psychology")
        optimizer_tab = ui.tab("Optimizer", icon="science")
        _track_visibility(optimizer_tab)
        settings_tab = ui.tab("Settings", icon="tune")
        logs_tab = ui.tab("Logs", icon="terminal")

    with ui.tab_panels(tabs, value=overview_tab).classes(
        "w-full bg-transparent"
    ):
        # ============================ OVERVIEW ========================= #
        with ui.tab_panel(overview_tab).classes("p-3 sm:p-6"):
            with ui.row().classes(
                "w-full gap-4 items-stretch flex-col md:flex-row"
            ):
                with ui.column().classes("gap-4 grow-[3] basis-0 min-w-0"):
                    with card():
                        with ui.row().classes(
                            "w-full items-center justify-between "
                            "flex-wrap gap-y-2"
                        ):
                            ui.label("📈 Equity Curve").classes(
                                "text-lg font-semibold text-white"
                            )
                            equity_range = ui.toggle(
                                list(EQUITY_RANGES), value="1D"
                            ).props("dense no-caps unelevated toggle-color=primary")
                        equity_chart = ui.echart(equity_chart_options()).classes(
                            "w-full h-72"
                        )
                        equity_empty = ui.label(
                            "No equity history yet — snapshots are recorded "
                            "every engine cycle"
                        ).classes(f"text-sm {TEXT_MUTED}")

                    with card():
                        card_title(
                            "📊 Active Positions",
                            "live from Alpaca sync — tap ℹ for the full story",
                        )
                        # Protection alarm: a position running without an
                        # enforceable stop (close rejected repeatedly, levels
                        # missing) must be impossible to miss — the Jul 13
                        # AAVE close failed silently every cycle for 3.5 h
                        # while the position sat past its breached stop.
                        protection_banner = ui.label("").classes(
                            "w-full text-xs font-semibold text-rose-300 "
                            "bg-rose-950 rounded border border-rose-800 "
                            "px-2 py-1 mb-1"
                        )
                        protection_banner.set_visibility(False)
                        with ui.element("div").classes("w-full scroll-x-mobile"):
                            with ui.element("div").classes(
                                "pos-grid text-xs font-semibold uppercase "
                                f"{TEXT_MUTED} border-b border-[#222938] pb-1"
                            ):
                                for header in (
                                    "Symbol", "Side", "Qty", "Entry", "Now",
                                    "Value", "Unrealized PnL", "", "",
                                ):
                                    ui.label(header)
                            positions_container = ui.column().classes("w-full gap-0")
                        positions_empty = ui.label("No open positions").classes(
                            f"text-sm {TEXT_MUTED}"
                        )

                with ui.column().classes("gap-4 grow-[2] basis-0 min-w-0"):
                    with card():
                        card_title("🌐 Market Regime")
                        regime_headline = ui.label("—").classes(
                            "text-2xl font-bold text-gray-400"
                        )
                        regime_detail_trend = kv_row("Trend (close vs EMA)")
                        regime_detail_vol = kv_row("Realized vol (ann.)")
                        regime_checked_label = ui.label("").classes(
                            f"text-xs {TEXT_MUTED} mt-1"
                        )

                    with card():
                        card_title("⚙️ Engine")
                        engine_rows = {
                            "heartbeat": kv_row("Last cycle"),
                            "stage": kv_row("Cycle stage"),
                            "market": kv_row("Market"),
                            "watchlist": kv_row("Watchlist"),
                            "slots": kv_row("Position slots"),
                        }
                        ui.label("Cooldowns (post-loss bench)").classes(
                            f"text-xs {TEXT_MUTED} mt-2"
                        )
                        cooldown_container = ui.row().classes("w-full gap-1")

                    with card():
                        card_title("🧠 Strategy Parameters", "walk-forward optimized")
                        param_labels: Dict[str, ui.label] = {
                            key: kv_row(meta["label"])
                            for key, meta in PARAM_META.items()
                            if not (is_crypto() and key in {"short_enabled", "rsi_short_signal", "rsi_short_exit"})
                        }
                        config_updated_label = ui.label(
                            "Last optimization: —"
                        ).classes(f"text-xs {TEXT_MUTED} mt-2")

                    with card() as screener_overview_card:
                        card_title("🔍 Screener Candidates", "RSI-oversold + VWAP-dip setups")
                        screener_container = ui.column().classes("w-full gap-0")
                        screener_empty = ui.label(
                            "Screener disabled or no candidates yet — enable it in Settings"
                        ).classes(f"text-sm {TEXT_MUTED}")

                    _track_visibility(screener_overview_card)

                    with card():
                        card_title("🕑 Recent Activity", "full log in Logs tab")
                        activity_container = ui.column().classes(
                            "w-full gap-0 font-mono"
                        )

        # ============================= TRADES ========================== #
        with ui.tab_panel(trades_tab).classes("p-3 sm:p-6"):
            with ui.column().classes("w-full gap-4"):
                with ui.row().classes("w-full gap-3 flex-wrap"):
                    tiles = {
                        "total_pnl": stat_tile("Realized PnL (all-time)"),
                        "today": stat_tile("Realized today"),
                        "win_rate": stat_tile("Win rate"),
                        "profit_factor": stat_tile("Profit factor"),
                        "count": stat_tile("Closed trades"),
                        "avg_win": stat_tile("Avg win"),
                        "avg_loss": stat_tile("Avg loss"),
                        "extremes": stat_tile("Best / Worst"),
                    }

                with card():
                    card_title(
                        "📉 Cumulative Realized PnL",
                        "closed trades, dashed line = break-even",
                    )
                    pnl_chart = ui.echart(cumulative_pnl_chart_options()).classes(
                        "w-full h-64"
                    )
                    pnl_chart_empty = ui.label("No closed trades yet").classes(
                        f"text-sm {TEXT_MUTED}"
                    )

                with card():
                    card_title(
                        "📜 Trade History",
                        f"latest {TRADES_LIMIT} — tap ℹ for the full story",
                    )
                    with ui.element("div").classes("w-full scroll-x-mobile"):
                        with ui.element("div").classes(
                            "trades-grid text-xs font-semibold uppercase "
                            f"{TEXT_MUTED} border-b border-[#222938] pb-1"
                        ):
                            for header in (
                                "Closed", "Symbol", "Side", "Qty", "Entry",
                                "Exit", "PnL", "PnL %", "Held", "",
                            ):
                                ui.label(header)
                        trades_container = ui.column().classes(
                            "w-full gap-0 trades-scroll"
                        )
                    trades_empty = ui.label("No trades recorded yet").classes(
                        f"text-sm {TEXT_MUTED}"
                    )

        # ============================ ANALYST ========================== #
        with ui.tab_panel(analyst_tab).classes("p-3 sm:p-6"):
            with ui.column().classes("w-full gap-4"):
                # Toggle bar
                with card():
                    with ui.row().classes("w-full items-center justify-between"):
                        with ui.row().classes("items-center gap-3"):
                            ui.label("🧠 Strategy Analyst").classes(
                                "text-lg font-semibold text-white"
                            )
                            analyst_model_label = ui.label("").classes(
                                f"text-xs {TEXT_MUTED}"
                            )
                        analyst_toggle = ui.switch(
                            "Enabled", value=False
                        ).props("color=emerald")
                        analyst_toggle._suppress_change = False
                        analyst_toggle.on_value_change(
                            lambda e: on_analyst_toggle(e.value)
                        )

                # Agent roster — what each LLM call does + live health
                with card():
                    card_title(
                        "🤖 LLM Agents — who does what",
                        "call counts and errors over the last 24h",
                    )
                    ui.label(
                        "Every LLM call the system makes belongs to one of "
                        "these seven agents. Green dot: last call succeeded. "
                        "Red: last call failed (hover for the error). Gray: "
                        "no calls recorded yet."
                    ).classes(f"text-xs {TEXT_MUTED}")
                    # Fail-open badge: the risk agent / portfolio manager
                    # auto-approve when unreachable (availability over
                    # gating) — that must be visible, not just a log line,
                    # or the bot can run un-gated for days on a dead Ollama.
                    analyst_failopen_banner = ui.label("").classes(
                        "w-full text-xs font-semibold text-amber-300 "
                        "bg-amber-950 rounded border border-amber-800 "
                        "px-2 py-1 mt-1"
                    )
                    analyst_failopen_banner.set_visibility(False)
                    agents_grid = ui.element("div").classes(
                        "w-full grid grid-cols-1 lg:grid-cols-2 "
                        "2xl:grid-cols-3 gap-3 mt-2"
                    )

                # Shadow-tracked vetoes — measured hypothetical P&L of the
                # signals each gate blocked, so "is this gate worth it?" is
                # a number instead of a guess.
                with card():
                    card_title(
                        "🚫 Shadow-tracked vetoes",
                        "what blocked signals would have done",
                    )
                    ui.label(
                        "Every signal a gate blocks (news sentiment, VWAP "
                        "re-check, risk agent, portfolio manager) is recorded "
                        "with the bracket it would have traded, then resolved "
                        "against market data using the optimizer's friction "
                        "model. Negative blocked P&L = the gate saved money. "
                        "No RSI early exit is simulated, so results, if "
                        "anything, flatter the vetoed trades."
                    ).classes(f"text-xs {TEXT_MUTED}")
                    vetoes_headline = ui.label("").classes(
                        "text-sm font-semibold text-white mt-1"
                    )
                    vetoes_box = ui.column().classes("w-full gap-1")
                    vetoes_empty = ui.label(
                        "No vetoed signals recorded yet."
                    ).classes(f"text-xs {TEXT_MUTED}")

                # Connection & schedule config
                with card():
                    card_title("🔌 Connection & Schedule")
                    ui.label(
                        "Changes take effect immediately — no restart needed. "
                        "The base URL must point to an OpenAI-compatible API "
                        "(e.g. cloud Ollama endpoint)."
                    ).classes(f"text-xs {TEXT_MUTED}")
                    with ui.row().classes(
                        "w-full items-center justify-between gap-4 flex-wrap"
                    ):
                        with ui.column().classes("gap-0 min-w-0"):
                            ui.label("Base URL").classes("text-sm text-white")
                            ui.label(
                                "OpenAI-compatible API endpoint"
                            ).classes(f"text-xs {TEXT_MUTED}")
                        analyst_base_url_input = ui.input(
                            value="", placeholder="https://..."
                        ).props("dense outlined").classes("w-full sm:w-80 sm:shrink-0")
                    with ui.row().classes(
                        "w-full items-center justify-between gap-4 flex-wrap"
                    ):
                        with ui.column().classes("gap-0 min-w-0"):
                            ui.label("Model").classes("text-sm text-white")
                            ui.label(
                                "Model name as known by the API"
                            ).classes(f"text-xs {TEXT_MUTED}")
                        analyst_model_input = ui.input(
                            value="deepseek-r1", placeholder="deepseek-r1"
                        ).props("dense outlined").classes("w-full sm:w-56 sm:shrink-0")
                    with ui.row().classes(
                        "w-full items-center justify-between gap-4 flex-wrap"
                    ):
                        with ui.column().classes("gap-0 min-w-0"):
                            ui.label("Sentiment Model").classes(
                                "text-sm text-white"
                            )
                            ui.label(
                                "Model for news sentiment scoring — "
                                "leave empty to use Model"
                            ).classes(f"text-xs {TEXT_MUTED}")
                        analyst_sentiment_model_input = ui.input(
                            value="", placeholder="(same as Model)"
                        ).props("dense outlined").classes("w-full sm:w-56 sm:shrink-0")
                    with ui.row().classes(
                        "w-full items-center justify-between gap-4 flex-wrap"
                    ) as watchlist_model_row:
                        with ui.column().classes("gap-0 min-w-0"):
                            ui.label("Watchlist Model").classes(
                                "text-sm text-white"
                            )
                            ui.label(
                                "Cheaper/faster model for hourly watchlist "
                                "curation — leave empty to use Model"
                            ).classes(f"text-xs {TEXT_MUTED}")
                        analyst_watchlist_model_input = ui.input(
                            value="", placeholder="(same as Model)"
                        ).props("dense outlined").classes("w-full sm:w-56 sm:shrink-0")
                    _track_visibility(watchlist_model_row)
                    with ui.row().classes(
                        "w-full items-center justify-between gap-4 flex-wrap"
                    ):
                        with ui.column().classes("gap-0 min-w-0"):
                            ui.label("Risk Model").classes("text-sm text-white")
                            ui.label(
                                "Cheaper/faster model for pre-trade risk "
                                "checks — leave empty to use Model"
                            ).classes(f"text-xs {TEXT_MUTED}")
                        analyst_risk_model_input = ui.input(
                            value="", placeholder="(same as Model)"
                        ).props("dense outlined").classes("w-full sm:w-56 sm:shrink-0")
                    with ui.row().classes(
                        "w-full items-center justify-between gap-4 flex-wrap"
                    ):
                        with ui.column().classes("gap-0 min-w-0"):
                            ui.label("API Key").classes("text-sm text-white")
                            ui.label(
                                "Bearer token (required for Ollama Cloud, "
                                "leave empty for local)"
                            ).classes(f"text-xs {TEXT_MUTED}")
                        analyst_api_key_input = ui.input(
                            value="", placeholder="sk-..."
                        ).props("dense outlined type=password").classes(
                            "w-full sm:w-64 sm:shrink-0"
                        )
                    with ui.row().classes(
                        "w-full items-center justify-between gap-4 flex-wrap"
                    ):
                        with ui.column().classes("gap-0 min-w-0"):
                            ui.label("Trade review interval (hours)").classes(
                                "text-sm text-white"
                            )
                            ui.label(
                                "How often to review trades during market hours"
                            ).classes(f"text-xs {TEXT_MUTED}")
                        analyst_interval_input = ui.number(
                            value=4, min=1, max=24, step=1
                        ).props("dense outlined").classes("w-full sm:w-24 sm:shrink-0")
                    with ui.row().classes(
                        "w-full items-center justify-between gap-4 flex-wrap"
                    ):
                        with ui.column().classes("gap-0 min-w-0"):
                            ui.label("Trade lookback").classes(
                                "text-sm text-white"
                            )
                            ui.label(
                                "Number of recent trades to analyze"
                            ).classes(f"text-xs {TEXT_MUTED}")
                        analyst_lookback_input = ui.number(
                            value=50, min=10, max=500, step=10
                        ).props("dense outlined").classes("w-full sm:w-24 sm:shrink-0")

                    async def apply_analyst_config() -> None:
                        updates = {
                            "base_url": analyst_base_url_input.value or "",
                            "api_key": analyst_api_key_input.value or "",
                            "model": analyst_model_input.value or "deepseek-r1",
                            "sentiment_model": analyst_sentiment_model_input.value or "",
                            "watchlist_model": analyst_watchlist_model_input.value or "",
                            "risk_model": analyst_risk_model_input.value or "",
                            "trade_review_interval_hours": float(
                                analyst_interval_input.value or 4
                            ),
                            "trade_lookback": int(
                                analyst_lookback_input.value or 50
                            ),
                        }
                        result = await run.io_bound(
                            call_backend_json, "/analyst/config", updates, 10.0,
                            ui_state["market"],
                        )
                        if result["ok"]:
                            ui.notify(
                                "Analyst config applied — "
                                + (
                                    "client connected"
                                    if result["data"].get("available")
                                    else "base URL may be unreachable"
                                ),
                                type="positive",
                            )
                        else:
                            ui.notify(
                                f"Config failed: {result['error']}",
                                type="negative",
                                timeout=10000,
                            )

                    with ui.row().classes("w-full gap-2 mt-2"):
                        ui.button(
                            "Apply config", on_click=apply_analyst_config
                        ).props("no-caps unelevated color=primary")
                        analyst_status_label = ui.label("").classes(
                            f"text-xs {TEXT_MUTED}"
                        )

                with ui.row().classes("w-full gap-4 items-start flex-col md:flex-row"):
                    # Optimization review card
                    with ui.column().classes("gap-4 grow basis-0 min-w-0") as opt_review_col:
                        with card():
                            card_title("🔬 Post-Optimization Review")
                            opt_summary = ui.label("").classes(
                                "text-sm text-gray-300 leading-relaxed"
                            )
                            opt_warnings_label = ui.label("Warnings").classes(
                                f"text-xs font-semibold {TEXT_MUTED} mt-3"
                            )
                            opt_warnings = ui.column().classes("w-full gap-1")
                            opt_suggestions_label = ui.label(
                                "Suggestions"
                            ).classes(f"text-xs font-semibold {TEXT_MUTED} mt-3")
                            opt_suggestions = ui.column().classes("w-full gap-1")
                            with ui.row().classes(
                                "w-full items-center justify-between mt-3"
                            ):
                                opt_confidence = ui.label("").classes(
                                    f"text-xs {TEXT_MUTED}"
                                )
                                opt_timestamp = ui.label("").classes(
                                    f"text-xs {TEXT_MUTED}"
                                )
                            opt_empty = ui.label(
                                "No optimization review yet — runs after the "
                                "nightly grid search"
                            ).classes(f"text-sm {TEXT_MUTED}")

                    _track_visibility(opt_review_col)

                    # Trade review card
                    with ui.column().classes("gap-4 grow basis-0 min-w-0"):
                        with card():
                            with ui.row().classes(
                                "w-full items-center justify-between"
                            ):
                                card_title("📊 Trade Performance Review")
                                async def run_trade_review() -> None:
                                    review_button.disable()
                                    review_spinner.set_visibility(True)
                                    ui.notify(
                                        "Running trade review…", type="info"
                                    )
                                    result = await run.io_bound(
                                        call_backend, "/analyst/review", 120.0,
                                        ui_state["market"],
                                    )
                                    review_spinner.set_visibility(False)
                                    review_button.enable()
                                    if result["ok"]:
                                        ui.notify(
                                            "Trade review complete",
                                            type="positive",
                                        )
                                    else:
                                        ui.notify(
                                            f"Review failed: {result['error']}",
                                            type="negative",
                                            timeout=10000,
                                        )

                                with ui.row().classes("items-center gap-2"):
                                    review_spinner = ui.spinner(size="sm")
                                    review_spinner.set_visibility(False)
                                    review_button = ui.button(
                                        "Run review now",
                                        on_click=run_trade_review,
                                    ).props("no-caps flat dense")
                            trade_summary = ui.label("").classes(
                                "text-sm text-gray-300 leading-relaxed"
                            )
                            trade_warnings_label = ui.label("Warnings").classes(
                                f"text-xs font-semibold {TEXT_MUTED} mt-3"
                            )
                            trade_warnings = ui.column().classes("w-full gap-1")
                            trade_suggestions_label = ui.label(
                                "Suggestions"
                            ).classes(f"text-xs font-semibold {TEXT_MUTED} mt-3")
                            trade_suggestions = ui.column().classes("w-full gap-1")
                            with ui.row().classes(
                                "w-full items-center justify-between mt-3"
                            ):
                                trade_confidence = ui.label("").classes(
                                    f"text-xs {TEXT_MUTED}"
                                )
                                trade_timestamp = ui.label("").classes(
                                    f"text-xs {TEXT_MUTED}"
                                )
                            trade_empty = ui.label(
                                "No trade review yet — runs every few hours "
                                "during market hours"
                            ).classes(f"text-sm {TEXT_MUTED}")

                with ui.row().classes("w-full gap-4 items-start flex-col md:flex-row"):
                    # Review history timeline
                    with ui.column().classes("gap-4 grow basis-0 min-w-0"):
                        with card():
                            card_title(
                                "📜 Review History",
                                "past verdicts, newest first",
                            )
                            review_history_col = ui.column().classes(
                                "w-full gap-2"
                            )
                            review_history_empty = ui.label(
                                "No reviews recorded yet — the history "
                                "starts with the next review"
                            ).classes(f"text-sm {TEXT_MUTED}")

                    # Raw LLM call log
                    with ui.column().classes("gap-4 grow basis-0 min-w-0"):
                        with card():
                            card_title(
                                "📡 LLM Call Log",
                                "latest 25 calls, newest first",
                            )
                            call_log_col = ui.column().classes("w-full gap-0")
                            call_log_empty = ui.label(
                                "No LLM calls recorded yet"
                            ).classes(f"text-sm {TEXT_MUTED}")

        # =========================== OPTIMIZER ========================= #
        with ui.tab_panel(optimizer_tab).classes("p-3 sm:p-6") as optimizer_panel:
            with ui.column().classes("w-full gap-4"):
                with card():
                    card_title("🗓️ Run History", "every optimizer run, newest first")
                    with ui.row().classes(
                        "w-full items-center justify-between gap-4 flex-wrap"
                    ):
                        with ui.column().classes("gap-0 min-w-0"):
                            ui.label("Last applied parameters").classes(
                                "text-sm text-white"
                            )
                            config_updated_opt_label = ui.label("—").classes(
                                f"text-xs {TEXT_MUTED}"
                            )
                        with ui.row().classes("items-center gap-2"):
                            optimize_button_opt = ui.button(
                                "Run optimizer now",
                                on_click=lambda: run_optimizer_btn(
                                    optimize_button_opt, optimize_spinner_opt
                                ),
                            ).props("no-caps unelevated color=deep-purple")
                            optimize_spinner_opt = ui.spinner(size="sm")
                            optimize_spinner_opt.set_visibility(False)
                    # Live progress card — shown only while a grid search is
                    # running. Populated by render_optimizer_status() on each
                    # refresh cycle from the optimizer_status runtime_state blob.
                    optimizer_live_card = ui.column().classes("w-full gap-2")
                    optimizer_live_card.set_visibility(False)
                    optimizer_runs_col = ui.column().classes("w-full gap-2")
                    optimizer_runs_empty = ui.label(
                        "No optimizer runs recorded yet — runs are recorded "
                        "after the first grid search"
                    ).classes(f"text-sm {TEXT_MUTED}")

        _track_visibility(optimizer_panel)

        # ============================ SETTINGS ========================= #
        with ui.tab_panel(settings_tab).classes("p-3 sm:p-6"):
            with ui.row().classes("w-full gap-4 items-start flex-col md:flex-row"):
                with ui.column().classes("gap-4 grow basis-0 min-w-0"):
                    with card():
                        card_title("🧠 Strategy Parameters")
                        ui.label(
                            "Written to the shared bot_config table and picked "
                            "up by the engine on its next cycle. The nightly "
                            "walk-forward optimizer (midnight Europe/Zurich) "
                            "re-tunes and overwrites these values."
                        ).classes(f"text-xs {TEXT_MUTED}")
                        param_inputs: Dict[str, Any] = {}
                        _CRYPTO_SKIP_PARAMS = {"short_enabled", "rsi_short_signal", "rsi_short_exit"}
                        for key, meta in PARAM_META.items():
                            if is_crypto() and key in _CRYPTO_SKIP_PARAMS:
                                continue
                            with ui.row().classes(
                                "w-full items-center justify-between gap-4 "
                                "flex-wrap"
                            ):
                                with ui.column().classes("gap-0 min-w-0"):
                                    ui.label(meta["label"]).classes(
                                        "text-sm text-white"
                                    )
                                    ui.label(meta["hint"]).classes(
                                        f"text-xs {TEXT_MUTED}"
                                    )
                                if meta.get("toggle"):
                                    sw = ui.switch(
                                        value=bool(int(initial_config.get(key, 0.0)))
                                    ).props("dense")
                                    param_inputs[key] = sw
                                else:
                                    param_inputs[key] = ui.number(
                                        value=(
                                            int(initial_config[key])
                                            if meta["int"]
                                            else round(float(initial_config[key]), 2)
                                        ),
                                        min=meta["min"],
                                        max=meta["max"],
                                        step=meta["step"],
                                    ).props("dense outlined").classes("w-full sm:w-32 sm:shrink-0")

                        async def apply_parameters() -> None:
                            updates: Dict[str, float] = {}
                            for key, meta in PARAM_META.items():
                                if is_crypto() and key in _CRYPTO_SKIP_PARAMS:
                                    continue
                                raw = param_inputs[key].value
                                if raw is None:
                                    ui.notify(
                                        f"{meta['label']} is empty",
                                        type="warning",
                                    )
                                    return
                                if meta.get("toggle"):
                                    updates[key] = 1.0 if raw else 0.0
                                else:
                                    value = max(
                                        float(meta["min"]),
                                        min(float(meta["max"]), float(raw)),
                                    )
                                    if meta["int"]:
                                        value = float(int(value))
                                    updates[key] = value
                            if updates["atr_target_mult"] <= updates["atr_stop_mult"]:
                                ui.notify(
                                    "Take profit ≤ stop loss (× ATR): negative "
                                    "expectancy bracket — applied anyway, "
                                    "double-check this is intended",
                                    type="warning",
                                    timeout=8000,
                                )
                            await run.io_bound(cur_db().set_config, updates)
                            await run.io_bound(
                                cur_db().add_log,
                                "WARNING",
                                "Strategy parameters changed manually from the "
                                "dashboard: "
                                + ", ".join(
                                    f"{k}={v:g}" for k, v in updates.items()
                                ),
                            )
                            ui.notify(
                                "Parameters applied — engine picks them up "
                                "on its next cycle",
                                type="positive",
                            )

                        async def restore_defaults() -> None:
                            if not await confirm(
                                "Restore defaults",
                                "Overwrite all strategy parameters with the "
                                "built-in defaults.",
                                "RESTORE",
                            ):
                                return
                            for key, meta in PARAM_META.items():
                                if is_crypto() and key in _CRYPTO_SKIP_PARAMS:
                                    continue
                                if meta.get("toggle"):
                                    param_inputs[key].value = bool(int(DEFAULT_CONFIG[key]))
                                else:
                                    param_inputs[key].value = (
                                        int(DEFAULT_CONFIG[key])
                                        if meta["int"]
                                        else DEFAULT_CONFIG[key]
                                    )
                            await apply_parameters()

                        def reload_from_db() -> None:
                            live = cur_db().get_config()
                            for key, meta in PARAM_META.items():
                                if is_crypto() and key in _CRYPTO_SKIP_PARAMS:
                                    continue
                                if meta.get("toggle"):
                                    param_inputs[key].value = bool(int(live.get(key, 0.0)))
                                else:
                                    param_inputs[key].value = (
                                        int(live[key])
                                        if meta["int"]
                                        else round(float(live[key]), 2)
                                    )
                            ui.notify("Reloaded live values from the database")

                        with ui.row().classes("w-full gap-2 mt-2"):
                            ui.button(
                                "Apply", on_click=apply_parameters
                            ).props("no-caps unelevated color=primary")
                            ui.button(
                                "Reload from DB", on_click=reload_from_db
                            ).props("no-caps flat")
                            ui.button(
                                "Restore defaults", on_click=restore_defaults
                            ).props("no-caps flat color=orange")

                    with card() as watchlist_card:
                        card_title("📡 Watchlist")
                        ui.label(
                            "Whole-market mode only. Written to the shared "
                            "bot_config table and read on the next screener "
                            "check or watchlist override lookup — no restart "
                            "needed."
                        ).classes(f"text-xs {TEXT_MUTED}")
                        watchlist_param_inputs: Dict[str, ui.number] = {}
                        for key, meta in WATCHLIST_PARAM_META.items():
                            with ui.row().classes(
                                "w-full items-center justify-between gap-4 "
                                "flex-wrap"
                            ):
                                with ui.column().classes("gap-0 min-w-0"):
                                    ui.label(meta["label"]).classes(
                                        "text-sm text-white"
                                    )
                                    ui.label(meta["hint"]).classes(
                                        f"text-xs {TEXT_MUTED}"
                                    )
                                watchlist_param_inputs[key] = ui.number(
                                    value=int(initial_config[key]),
                                    min=meta["min"],
                                    max=meta["max"],
                                    step=meta["step"],
                                ).props("dense outlined").classes("w-full sm:w-32 sm:shrink-0")

                        async def apply_watchlist_parameters() -> None:
                            updates: Dict[str, float] = {}
                            for key, meta in WATCHLIST_PARAM_META.items():
                                raw = watchlist_param_inputs[key].value
                                if raw is None:
                                    ui.notify(
                                        f"{meta['label']} is empty",
                                        type="warning",
                                    )
                                    return
                                value = max(
                                    float(meta["min"]),
                                    min(float(meta["max"]), float(raw)),
                                )
                                updates[key] = float(int(value))
                            await run.io_bound(cur_db().set_config, updates)
                            ui.notify(
                                "Watchlist settings applied",
                                type="positive",
                            )

                        def reload_watchlist_from_db() -> None:
                            live = cur_db().get_config()
                            for key in WATCHLIST_PARAM_META:
                                watchlist_param_inputs[key].value = int(live[key])
                            ui.notify("Reloaded live values from the database")

                        with ui.row().classes("w-full gap-2 mt-2"):
                            ui.button(
                                "Apply", on_click=apply_watchlist_parameters
                            ).props("no-caps unelevated color=primary")
                            ui.button(
                                "Reload from DB", on_click=reload_watchlist_from_db
                            ).props("no-caps flat")

                    _track_visibility(watchlist_card)

                    with card() as screener_card:
                        card_title("🔍 Opportunity Screener")
                        ui.label(
                            "Scans a wide pool of most-active symbols for "
                            "RSI-oversold + VWAP-dip + bullish-sentiment "
                            "setups — the same pattern the engine trades. "
                            "Results appear on the Overview tab and at "
                            "GET /screener. Runs every 5 minutes in the "
                            "background."
                        ).classes(f"text-xs {TEXT_MUTED}")
                        screener_param_inputs: Dict[str, ui.number] = {}
                        for key, meta in SCREENER_PARAM_META.items():
                            with ui.row().classes(
                                "w-full items-center justify-between gap-4 "
                                "flex-wrap"
                            ):
                                with ui.column().classes("gap-0 min-w-0"):
                                    ui.label(meta["label"]).classes(
                                        "text-sm text-white"
                                    )
                                    ui.label(meta["hint"]).classes(
                                        f"text-xs {TEXT_MUTED}"
                                    )
                                if key == "screener_enabled":
                                    screener_param_inputs[key] = ui.number(
                                        value=int(initial_config.get(key, 0.0)),
                                        min=meta["min"],
                                        max=meta["max"],
                                        step=meta["step"],
                                    ).props("dense outlined").classes("w-full sm:w-32 sm:shrink-0")
                                else:
                                    screener_param_inputs[key] = ui.number(
                                        value=int(initial_config.get(key, 200.0)),
                                        min=meta["min"],
                                        max=meta["max"],
                                        step=meta["step"],
                                    ).props("dense outlined").classes("w-full sm:w-32 sm:shrink-0")

                        async def apply_screener_parameters() -> None:
                            updates: Dict[str, float] = {}
                            for key, meta in SCREENER_PARAM_META.items():
                                raw = screener_param_inputs[key].value
                                if raw is None:
                                    ui.notify(
                                        f"{meta['label']} is empty",
                                        type="warning",
                                    )
                                    return
                                value = max(
                                    float(meta["min"]),
                                    min(float(meta["max"]), float(raw)),
                                )
                                updates[key] = float(int(value))
                            await run.io_bound(cur_db().set_config, updates)
                            ui.notify(
                                "Screener settings applied",
                                type="positive",
                            )

                        def reload_screener_from_db() -> None:
                            live = cur_db().get_config()
                            for key in SCREENER_PARAM_META:
                                screener_param_inputs[key].value = int(
                                    live.get(key, 0.0 if key == "screener_enabled" else 200.0)
                                )
                            ui.notify("Reloaded live values from the database")

                        with ui.row().classes("w-full gap-2 mt-2"):
                            ui.button(
                                "Apply", on_click=apply_screener_parameters
                            ).props("no-caps unelevated color=primary")
                            ui.button(
                                "Reload from DB", on_click=reload_screener_from_db
                            ).props("no-caps flat")

                    _track_visibility(screener_card)

                    with card():
                        card_title("🖥️ Dashboard")
                        with ui.row().classes(
                            "w-full items-center justify-between"
                        ):
                            with ui.column().classes("gap-0"):
                                ui.label("Refresh interval").classes(
                                    "text-sm text-white"
                                )
                                ui.label("seconds between data refreshes").classes(
                                    f"text-xs {TEXT_MUTED}"
                                )
                            refresh_select = ui.select(
                                [1, 2, 5, 10], value=int(DEFAULT_REFRESH_SECONDS)
                            ).props("dense outlined").classes("w-full sm:w-24")
                        with ui.row().classes(
                            "w-full items-center justify-between"
                        ):
                            with ui.column().classes("gap-0"):
                                ui.label("Log rows").classes("text-sm text-white")
                                ui.label("events shown in the Logs tab").classes(
                                    f"text-xs {TEXT_MUTED}"
                                )
                            log_rows_select = ui.select(
                                [20, 50, 100, 200, 500], value=DEFAULT_LOG_ROWS
                            ).props("dense outlined").classes("w-full sm:w-24")

                with ui.column().classes("gap-4 grow basis-0 min-w-0"):
                    with card():
                        card_title("🔧 Engine & Optimizer")
                        ui.label(
                            f"Actions call the backend debug API at "
                            f"{BACKEND_API_URL}. Everything else on this "
                            "dashboard reads the shared database directly."
                        ).classes(f"text-xs {TEXT_MUTED}")

                        async def run_optimizer_btn(
                            button: ui.button, spinner: ui.spinner
                        ) -> None:
                            if not await confirm(
                                "Run optimizer now",
                                "Trigger the walk-forward grid search "
                                "immediately instead of waiting for midnight. "
                                "This can take several minutes; validated "
                                "parameters go live automatically. Progress is "
                                "shown live in the Optimizer tab.",
                                "OPTIMIZE",
                            ):
                                return
                            button.disable()
                            spinner.set_visibility(True)
                            ui.notify(
                                "Optimizer started — watch the Optimizer tab "
                                "for live progress",
                                type="info",
                            )
                            # /optimize now returns immediately after launching
                            # the grid search in a background thread; the live
                            # status is polled via the normal refresh timer and
                            # rendered in render_optimizer_status().
                            result = await run.io_bound(
                                call_backend, "/optimize", 10.0,
                                ui_state["market"],
                            )
                            if not result["ok"]:
                                spinner.set_visibility(False)
                                button.enable()
                                ui.notify(
                                    f"Optimization failed to start: "
                                    f"{result['error']}",
                                    type="negative",
                                    timeout=10000,
                                )
                            # On success the button stays disabled and the
                            # spinner stays visible until the next refresh
                            # detects the running status and takes over the
                            # UI; render_optimizer_status re-enables the button
                            # when the run finishes.

                        with ui.row().classes("items-center gap-2 mt-1") as optimize_row:
                            optimize_button = ui.button(
                                "Run optimizer now",
                                on_click=lambda: run_optimizer_btn(
                                    optimize_button, optimize_spinner
                                ),
                            ).props("no-caps unelevated color=deep-purple")
                            optimize_spinner = ui.spinner(size="sm")
                            optimize_spinner.set_visibility(False)
                        ui.label(
                            "Resume-from-KILLED lives in the header — it "
                            "appears when the bot is stopped. The Run "
                            "optimizer now button has moved to the "
                            "Optimizer tab."
                        ).classes(f"text-xs {TEXT_MUTED} mt-1")
                        _track_visibility(optimize_row)

                    with card():
                        card_title("🌍 Operational Environment")
                        ui.label(
                            "These used to be env vars — now live in bot_config "
                            "so you can tune them from the dashboard. Changes "
                            "take effect on the next engine cycle."
                        ).classes(f"text-xs {TEXT_MUTED}")
                        op_param_inputs: Dict[str, Any] = {}
                        _CRYPTO_SKIP_OP = {"eod_flatten_minutes"}
                        for key, meta in OPERATIONAL_PARAM_META.items():
                            if is_crypto() and key in _CRYPTO_SKIP_OP:
                                continue
                            with ui.row().classes(
                                "w-full items-center justify-between gap-4 "
                                "flex-wrap"
                            ):
                                with ui.column().classes("gap-0 min-w-0"):
                                    ui.label(meta["label"]).classes(
                                        "text-sm text-white"
                                    )
                                    ui.label(meta["hint"]).classes(
                                        f"text-xs {TEXT_MUTED}"
                                    )
                                op_param_inputs[key] = ui.number(
                                    value=(
                                        int(initial_config.get(key, meta["min"]))
                                        if meta["int"]
                                        else round(float(initial_config.get(key, meta["min"])), 2)
                                    ),
                                    min=meta["min"],
                                    max=meta["max"],
                                    step=meta["step"],
                                ).props("dense outlined").classes("w-full sm:w-32 sm:shrink-0")

                        async def apply_operational() -> None:
                            updates: Dict[str, float] = {}
                            for key, meta in OPERATIONAL_PARAM_META.items():
                                if is_crypto() and key in _CRYPTO_SKIP_OP:
                                    continue
                                raw = op_param_inputs[key].value
                                if raw is None:
                                    ui.notify(
                                        f"{meta['label']} is empty",
                                        type="warning",
                                    )
                                    return
                                value = max(
                                    float(meta["min"]),
                                    min(float(meta["max"]), float(raw)),
                                )
                                if meta["int"]:
                                    value = float(int(value))
                                updates[key] = value
                            await run.io_bound(cur_db().set_config, updates)
                            await run.io_bound(
                                cur_db().add_log,
                                "WARNING",
                                "Operational environment changed from dashboard: "
                                + ", ".join(f"{k}={v:g}" for k, v in updates.items()),
                            )
                            ui.notify(
                                "Operational settings applied — engine picks them "
                                "up on its next cycle",
                                type="positive",
                            )

                        def reload_operational_from_db() -> None:
                            live = cur_db().get_config()
                            for key, meta in OPERATIONAL_PARAM_META.items():
                                if is_crypto() and key in _CRYPTO_SKIP_OP:
                                    continue
                                op_param_inputs[key].value = (
                                    int(live.get(key, meta["min"]))
                                    if meta["int"]
                                    else round(float(live.get(key, meta["min"])), 2)
                                )
                            ui.notify("Reloaded live values from the database")

                        with ui.row().classes("w-full gap-2 mt-2"):
                            ui.button(
                                "Apply", on_click=apply_operational
                            ).props("no-caps unelevated color=primary")
                            ui.button(
                                "Reload from DB", on_click=reload_operational_from_db
                            ).props("no-caps flat")

                    with card():
                        card_title("🌍 Operational Environment", "read-only")
                        ui.label(
                            "Read-only info published by the engine."
                        ).classes(f"text-xs {TEXT_MUTED}")
                        environment_labels: Dict[str, ui.label] = {
                            key: kv_row(label)
                            for key, label in ENVIRONMENT_LABELS.items()
                        }

        # ============================== LOGS =========================== #
        with ui.tab_panel(logs_tab).classes("p-3 sm:p-6"):
            with card():
                with ui.row().classes("w-full items-center justify-between gap-4"):
                    ui.label("🖥️ Live System Log").classes(
                        "text-lg font-semibold text-white"
                    )
                    with ui.row().classes(
                        "items-center gap-3 flex-wrap w-full sm:w-auto"
                    ):
                        log_level_select = ui.select(
                            list(LEVEL_COLORS),
                            multiple=True,
                            value=list(LEVEL_COLORS),
                            label="levels",
                        ).props(
                            "dense outlined use-chips options-dense"
                        ).classes("w-full sm:w-auto sm:min-w-[16rem]")
                        log_search = ui.input(placeholder="filter text…").props(
                            "dense outlined clearable"
                        ).classes("w-full sm:w-56")
                        log_count_label = ui.label("").classes(
                            f"text-xs {TEXT_MUTED}"
                        )
                log_container = ui.column().classes(
                    "w-full gap-0 bg-[#0a0d13] rounded-lg p-3 "
                    "font-mono overflow-x-auto min-h-[28rem]"
                )

    # ------------------------------------------------------------------ #
    # per-section renderers
    # ------------------------------------------------------------------ #
    render_state: Dict[str, Any] = {
        "equity_key": None,
        "trades_key": None,
        "closing": set(),
        "analyst_enabled": None,
        # Bumped each time a trade info popup opens; the async bar fetch checks
        # it before drawing so a slow fetch can't paint onto a later trade.
        "trade_info_token": 0,
    }

    def render_header(snapshot: Dict[str, Any]) -> None:
        status = snapshot["status"]
        balance_label.set_text(f"${status['equity']:,.2f}")

        daily_pnl = snapshot["daily_pnl"]
        pnl_label.set_text(money(daily_pnl, signed=True))
        pnl_label.classes(
            replace="text-lg sm:text-xl font-semibold "
            + ("text-green-400" if daily_pnl >= 0 else "text-red-400")
        )
        baseline = status["daily_start_balance"]
        pnl_pct_label.set_text(
            f"{daily_pnl / baseline * 100.0:+.2f}% today" if baseline > 0 else ""
        )

        # Browser-tab ticker: today's PnL readable from a pinned/backgrounded
        # tab without opening the dashboard.
        try:
            arrow = "▲" if daily_pnl >= 0 else "▼"
            ui.run_javascript(
                "document.title = "
                + json.dumps(f"{arrow} {money(daily_pnl, signed=True)} · Argus")
            )
        except Exception:
            pass

        running = status["status"] == STATUS_RUNNING
        status_label.set_text(f"● {status['status']}")
        status_label.classes(
            replace="text-lg sm:text-xl font-semibold "
            + ("text-green-400" if running else "text-red-500")
        )
        killed = status["status"] == STATUS_KILLED
        if killed:
            emergency_button.disable()
        else:
            emergency_button.enable()
        resume_button.set_visibility(killed)
        if killed:
            resume_button.enable()

        # regime chip
        cycle = snapshot["last_cycle"]
        regime_info = cycle.get("regime") or {}
        regime_name = regime_info.get("regime")
        style = REGIME_STYLES.get(regime_name or "", REGIME_STYLES["UNKNOWN"])
        regime_chip["dot"].classes(
            replace=f"w-2 h-2 rounded-full {style['dot']}"
        )
        regime_chip["text"].set_text(regime_name or "REGIME —")
        regime_chip["text"].classes(
            replace=f"text-xs font-semibold {style['text']}"
        )

        # market chip
        market_open = cycle.get("market_open")
        if market_open is None:
            market_chip["text"].set_text("MARKET —")
            market_chip["dot"].classes(replace="w-2 h-2 rounded-full bg-gray-500")
            market_chip["text"].classes(
                replace="text-xs font-semibold text-gray-400"
            )
        elif market_open:
            market_chip["text"].set_text("MARKET OPEN")
            market_chip["dot"].classes(
                replace="w-2 h-2 rounded-full bg-emerald-400"
            )
            market_chip["text"].classes(
                replace="text-xs font-semibold text-emerald-400"
            )
        else:
            market_chip["text"].set_text("MARKET CLOSED")
            market_chip["dot"].classes(replace="w-2 h-2 rounded-full bg-gray-500")
            market_chip["text"].classes(
                replace="text-xs font-semibold text-gray-400"
            )

        # engine heartbeat chip
        poll_interval = float(
            snapshot["environment"].get("poll_interval_seconds", 60) or 60
        )
        cycle_age = age_seconds(cycle.get("finished_at"))
        if cycle_age is None:
            engine_chip["text"].set_text("ENGINE —")
            engine_chip["dot"].classes(replace="w-2 h-2 rounded-full bg-gray-500")
            engine_chip["text"].classes(
                replace="text-xs font-semibold text-gray-400"
            )
        elif cycle_age <= poll_interval * 3:
            engine_chip["text"].set_text("ENGINE LIVE")
            engine_chip["dot"].classes(
                replace="w-2 h-2 rounded-full bg-emerald-400"
            )
            engine_chip["text"].classes(
                replace="text-xs font-semibold text-emerald-400"
            )
        else:
            engine_chip["text"].set_text(
                f"ENGINE STALE ({humanize_age(cycle_age)})"
            )
            engine_chip["dot"].classes(replace="w-2 h-2 rounded-full bg-amber-400")
            engine_chip["text"].classes(
                replace="text-xs font-semibold text-amber-400"
            )

    def render_equity(snapshot: Dict[str, Any]) -> None:
        history = snapshot["equity_history"]
        key = (
            equity_range.value,
            len(history),
            history[-1]["ts"] if history else None,
        )
        if key == render_state["equity_key"]:
            return
        render_state["equity_key"] = key
        data = [
            [ms, point["equity"]]
            for point in history
            if (ms := epoch_ms(point["ts"])) is not None
        ]
        equity_chart.options["series"][0]["data"] = data
        equity_chart.update()
        equity_empty.set_visibility(not data)

    async def on_close_position(symbol: str) -> None:
        if not await confirm(
            f"Close {symbol}",
            f"Cancel {symbol}'s resting orders and close the position now "
            "with an extended-hours limit order.",
            f"CLOSE {symbol}",
        ):
            return
        render_state["closing"].add(symbol)
        ui.notify(f"Closing {symbol}…", type="warning")
        error = await run.io_bound(close_single_position, symbol, ui_state["market"])
        render_state["closing"].discard(symbol)
        if error is None:
            ui.notify(f"{symbol} close submitted", type="positive")
        else:
            ui.notify(f"{symbol} close failed: {error}", type="negative",
                      timeout=10000)

    def render_positions(snapshot: Dict[str, Any]) -> None:
        market = ui_state["market"]
        positions = [
            p for p in snapshot["positions"]
            if market_owns(p["symbol"], market)
        ]
        positions_empty.set_visibility(not positions)
        open_entries = snapshot.get("open_entries") or {}

        # Protection alarm — active close-failure streaks and this session's
        # watchdog interventions (levels attached / forced closes).
        health = snapshot.get("protection_health") or {}
        stuck = {
            s: n for s, n in (health.get("close_failures") or {}).items()
            if market_owns(s, market) and int(n or 0) >= 3
        }
        attached = int(health.get("levels_attached", 0) or 0)
        forced = int(health.get("forced_market_closes", 0) or 0)
        protective = int(health.get("protective_closes", 0) or 0)
        if stuck:
            worst = max(stuck.items(), key=lambda kv: kv[1])
            protection_banner.set_text(
                f"🚨 Stop can't execute: {', '.join(sorted(stuck))} — the "
                f"close has been rejected {worst[1]}× and the position is "
                f"running without an enforceable stop "
                f"(last: {(health.get('last_event') or '?')[:100]})"
            )
            protection_banner.set_visibility(True)
        elif attached or forced or protective:
            parts = []
            if attached:
                parts.append(f"{attached} naked position(s) given stops")
            if forced:
                parts.append(f"{forced} forced market close(s)")
            if protective:
                parts.append(f"{protective} protective close(s)")
            protection_banner.set_text(
                "⚠️ Protection watchdog acted this session: "
                + ", ".join(parts)
            )
            protection_banner.set_visibility(True)
        else:
            protection_banner.set_visibility(False)
        positions_container.clear()
        with positions_container:
            for pos in positions:
                qty = pos["qty"]
                side = "BUY" if qty > 0 else "SELL"
                pnl = pos["unrealized_pnl"]
                cost_basis = pos["avg_entry_price"] * abs(qty)
                pnl_pct = (
                    pnl / cost_basis * 100.0
                    if pnl is not None and cost_basis
                    else None
                )
                with ui.element("div").classes(
                    "pos-grid row-hover py-1.5 border-b border-[#222938] text-sm"
                ):
                    ui.label(pos["symbol"]).classes("font-bold text-white")
                    side_color = "text-green-400" if side == "BUY" else "text-red-400"
                    ui.label(side).classes(f"font-mono font-semibold {side_color}")
                    ui.label(f"{abs(qty):g}").classes("font-mono text-gray-300")
                    ui.label(f"${pos['avg_entry_price']:,.2f}").classes(
                        "font-mono text-gray-300"
                    )
                    ui.label(
                        "—" if pos["current_price"] is None
                        else f"${pos['current_price']:,.2f}"
                    ).classes("font-mono text-gray-300")
                    ui.label(
                        "—" if pos["market_value"] is None
                        else f"${pos['market_value']:,.2f}"
                    ).classes("font-mono text-gray-300")
                    pnl_text = "—" if pnl is None else (
                        money(pnl, signed=True)
                        + (f" ({pnl_pct:+.2f}%)" if pnl_pct is not None else "")
                    )
                    ui.label(pnl_text).classes(
                        f"font-mono font-semibold {pnl_text_class(pnl)}"
                    )
                    entry_meta = open_entries.get(pos["symbol"])
                    info_button = ui.button(
                        icon="info",
                        on_click=lambda _, p=dict(pos), e=dict(entry_meta) if entry_meta else None: show_position_info(p, e),
                    ).props("flat round dense color=blue-4").tooltip(
                        "Why this position?"
                    )
                    if entry_meta is None:
                        info_button.disable()
                    close_button = ui.button(
                        icon="close",
                        on_click=lambda _, s=pos["symbol"]: on_close_position(s),
                    ).props("flat round dense color=red-4").tooltip(
                        f"Close {pos['symbol']} now"
                    )
                    if pos["symbol"] in render_state["closing"]:
                        close_button.disable()

    def render_regime_and_engine(snapshot: Dict[str, Any]) -> None:
        cycle = snapshot["last_cycle"]
        regime_info = cycle.get("regime") or {}
        regime_name = regime_info.get("regime")
        style = REGIME_STYLES.get(regime_name or "", REGIME_STYLES["UNKNOWN"])
        regime_headline.set_text(regime_name or "not evaluated yet")
        regime_headline.classes(replace=f"text-2xl font-bold {style['text']}")
        if regime_info.get("close") is not None:
            arrow = "▼ below" if regime_info.get("trend_down") else "▲ above"
            regime_detail_trend.set_text(
                f"{regime_info.get('symbol', 'SPY')} ${regime_info['close']:,.2f} "
                f"{arrow} EMA ${regime_info.get('ema', 0):,.2f}"
            )
            vol = regime_info.get("realized_vol_pct")
            limit = regime_info.get("vol_threshold_pct")
            stressed = " (stressed)" if regime_info.get("stressed") else ""
            regime_detail_vol.set_text(
                f"{vol:.0f}% vs limit {limit:.0f}%{stressed}"
                if vol is not None and limit is not None
                else "—"
            )
            regime_checked_label.set_text(
                f"Checked {fmt_clock(regime_info.get('checked_at'))} — a "
                "down-trend blocks new buys (shadow-tracked); the regime "
                "never forces exits"
            )
        else:
            regime_detail_trend.set_text("—")
            regime_detail_vol.set_text("—")
            regime_checked_label.set_text(
                "Regime is evaluated while the market is open"
            )

        cycle_age = age_seconds(cycle.get("finished_at"))
        engine_rows["heartbeat"].set_text(
            f"{humanize_age(cycle_age)} ({fmt_clock(cycle.get('finished_at'))})"
            if cycle_age is not None
            else "no cycle recorded yet"
        )
        engine_rows["stage"].set_text(cycle.get("stage", "—"))
        if cycle.get("market_open") is False and cycle.get("next_open"):
            engine_rows["market"].set_text(
                f"closed — next open {fmt_short(cycle['next_open'])}"
            )
        elif cycle.get("market_open"):
            engine_rows["market"].set_text("open")
        else:
            engine_rows["market"].set_text("—")
        engine_rows["watchlist"].set_text(
            f"{cycle.get('watchlist_size', '—')} symbols "
            f"({cycle.get('symbols_with_bars', '—')} with bars)"
        )
        held = cycle.get("held_symbols") or []
        slots = cycle.get("open_slots")
        engine_rows["slots"].set_text(
            f"{len(held)} held / {slots} free"
            + (f" — {', '.join(held)}" if held else "")
            if slots is not None
            else "—"
        )

        cooldown_container.clear()
        cooldowns: Dict[str, float] = cycle.get("cooldowns") or {}
        with cooldown_container:
            if not cooldowns:
                ui.label("none").classes(f"text-xs {TEXT_MUTED}")
            for symbol, minutes in sorted(cooldowns.items()):
                ui.label(f"{symbol} {minutes:.0f}m").classes(
                    "text-xs font-mono text-amber-400 bg-[#1d2432] "
                    "border border-[#2a3140] rounded-full px-2 py-0.5"
                )

    def render_parameters(snapshot: Dict[str, Any]) -> None:
        config = snapshot["config"]
        for key, label in param_labels.items():
            value = config.get(key)
            if value is None:
                label.set_text("—")
            elif key == "rsi_period":
                label.set_text(f"{int(value)}")
            else:
                label.set_text(f"{value:.2f}")
        updated_at = snapshot["config_updated_at"]
        config_updated_label.set_text(
            f"Last optimization: {fmt_short(updated_at)}" if updated_at
            else "Last optimization: —"
        )
        config_updated_opt_label.set_text(
            f"Last applied: {fmt_short(updated_at)}" if updated_at
            else "Last applied: —"
        )

    def render_screener(snapshot: Dict[str, Any]) -> None:
        candidates = snapshot.get("screener_candidates") or []
        screener_empty.set_visibility(not candidates)
        screener_container.clear()
        with screener_container:
            for c in candidates:
                with ui.row().classes(
                    "w-full items-center justify-between py-1 "
                    "border-b border-[#222938] text-sm"
                ):
                    with ui.row().classes("items-center gap-2"):
                        ui.label(c["symbol"]).classes(
                            "font-bold text-white"
                        )
                        ui.label(f"RSI {c['rsi']}").classes(
                            "font-mono text-amber-400"
                        )
                        ui.label(f"${c['price']}").classes(
                            "font-mono text-gray-300"
                        )
                    with ui.row().classes("items-center gap-3"):
                        ui.label(f"VWAP ${c['vwap']}").classes(
                            f"text-xs {TEXT_MUTED}"
                        )
                        ui.label(f"depth {c['rsi_depth']}").classes(
                            "text-xs text-emerald-400"
                        )
                        ui.label(c["sentiment_source"]).classes(
                            "text-xs text-sky-300"
                        )

    def render_trades(snapshot: Dict[str, Any]) -> None:
        market = ui_state["market"]
        trades = [
            t for t in snapshot["trades"]
            if market_owns(t["symbol"], market)
        ]
        stats = snapshot["trade_stats"]
        key = (len(trades), trades[0]["id"] if trades else None,
               round(snapshot["realized_today"], 4))
        if key == render_state["trades_key"]:
            return
        render_state["trades_key"] = key

        decided = stats["wins"] + stats["losses"]
        tiles["total_pnl"]["value"].set_text(money(stats["total_pnl"], signed=True))
        tiles["total_pnl"]["value"].classes(
            replace="text-xl font-semibold "
            + pnl_text_class(stats["total_pnl"], neutral="text-white")
        )
        tiles["total_pnl"]["sub"].set_text(f"{stats['total']} closed trades")

        tiles["today"]["value"].set_text(
            money(snapshot["realized_today"], signed=True)
        )
        tiles["today"]["value"].classes(
            replace="text-xl font-semibold "
            + pnl_text_class(snapshot["realized_today"], neutral="text-white")
        )
        tiles["today"]["sub"].set_text("since local midnight")

        tiles["win_rate"]["value"].set_text(
            f"{stats['wins'] / decided * 100.0:.1f}%" if decided else "—"
        )
        tiles["win_rate"]["sub"].set_text(
            f"{stats['wins']} W / {stats['losses']} L"
        )

        gross_loss = abs(stats["gross_loss"])
        if gross_loss > 0:
            tiles["profit_factor"]["value"].set_text(
                f"{stats['gross_profit'] / gross_loss:.2f}"
            )
        else:
            tiles["profit_factor"]["value"].set_text(
                "∞" if stats["gross_profit"] > 0 else "—"
            )
        tiles["profit_factor"]["sub"].set_text(
            f"+{money(stats['gross_profit'])} / -{money(gross_loss)}"
        )

        tiles["count"]["value"].set_text(str(stats["total"]))
        tiles["count"]["sub"].set_text("with known PnL")

        tiles["avg_win"]["value"].set_text(
            money(stats["gross_profit"] / stats["wins"], signed=True)
            if stats["wins"] else "—"
        )
        tiles["avg_win"]["value"].classes(
            replace="text-xl font-semibold text-emerald-400"
        )
        tiles["avg_win"]["sub"].set_text("per winning trade")

        tiles["avg_loss"]["value"].set_text(
            money(stats["gross_loss"] / stats["losses"], signed=True)
            if stats["losses"] else "—"
        )
        tiles["avg_loss"]["value"].classes(
            replace="text-xl font-semibold text-red-400"
        )
        tiles["avg_loss"]["sub"].set_text("per losing trade")

        tiles["extremes"]["value"].set_text(
            f"{money(stats['best'], signed=True)} / "
            f"{money(stats['worst'], signed=True)}"
            if stats["best"] is not None else "—"
        )
        tiles["extremes"]["sub"].set_text("single-trade extremes")

        # cumulative realized PnL over closed trades, oldest first
        closed = sorted(
            (t for t in trades if t["realized_pnl"] is not None and t["exit_time"]),
            key=lambda t: t["exit_time"],
        )
        cumulative = 0.0
        curve = []
        for trade in closed:
            cumulative += trade["realized_pnl"]
            ms = epoch_ms(trade["exit_time"])
            if ms is not None:
                curve.append([ms, round(cumulative, 2)])
        pnl_chart.options["series"][0]["data"] = curve
        pnl_chart.update()
        pnl_chart_empty.set_visibility(not curve)

        trades_empty.set_visibility(not trades)
        trades_container.clear()
        with trades_container:
            for trade in trades:
                pnl = trade["realized_pnl"]
                side = trade.get("side", "BUY")
                cost_basis = trade["entry_price"] * trade["qty"]
                pnl_pct = (
                    pnl / cost_basis * 100.0
                    if pnl is not None and cost_basis
                    else None
                )
                with ui.element("div").classes(
                    "trades-grid row-hover py-1.5 border-b border-[#222938] "
                    "text-sm"
                ):
                    ui.label(fmt_short(trade["exit_time"])).classes(
                        "font-mono text-gray-300"
                    )
                    ui.label(trade["symbol"]).classes("font-bold text-white")
                    side_color = (
                        "text-green-400" if side == "BUY" else "text-red-400"
                    )
                    ui.label(side).classes(
                        f"font-mono font-semibold {side_color}"
                    )
                    ui.label(f"{trade['qty']:g}").classes(
                        "font-mono text-gray-300"
                    )
                    ui.label(f"${trade['entry_price']:,.2f}").classes(
                        "font-mono text-gray-300"
                    )
                    ui.label(
                        "—" if trade["exit_price"] is None
                        else f"${trade['exit_price']:,.2f}"
                    ).classes("font-mono text-gray-300")
                    ui.label(
                        "—" if pnl is None else money(pnl, signed=True)
                    ).classes(f"font-mono font-semibold {pnl_text_class(pnl)}")
                    ui.label(
                        "—" if pnl_pct is None else f"{pnl_pct:+.2f}%"
                    ).classes(f"font-mono {pnl_text_class(pnl)}")
                    ui.label(
                        humanize_duration(trade["entry_time"], trade["exit_time"])
                    ).classes("font-mono text-gray-300")
                    ui.button(
                        icon="info",
                        on_click=lambda _, t=dict(trade): show_trade_info(t),
                    ).props("flat round dense color=blue-4").tooltip(
                        "Why this trade?"
                    )

    async def show_trade_info(trade: Dict[str, Any]) -> None:
        """Open the per-trade info popup: the decision rationale, the numbers,
        and a price chart of the window the position was held."""
        render_state["trade_info_token"] += 1
        token = render_state["trade_info_token"]

        symbol = trade["symbol"]
        side = trade.get("side", "BUY")
        pnl = trade["realized_pnl"]
        cost_basis = trade["entry_price"] * trade["qty"]
        pnl_pct = (
            pnl / cost_basis * 100.0 if pnl is not None and cost_basis else None
        )

        # --- header: symbol, side chip, PnL ---
        trade_info_title.clear()
        with trade_info_title:
            ui.label(symbol).classes("text-xl font-bold text-white")
            side_color = "text-green-400" if side == "BUY" else "text-red-400"
            ui.label(("▲ LONG" if side == "BUY" else "▼ SHORT")).classes(
                f"text-xs font-mono font-semibold {side_color} "
                "bg-[#1d2432] px-2 py-0.5 rounded border border-[#2a3140]"
            )
            if pnl is not None:
                ui.label(
                    money(pnl, signed=True)
                    + (f" ({pnl_pct:+.2f}%)" if pnl_pct is not None else "")
                ).classes(f"text-sm font-mono font-semibold {pnl_text_class(pnl)}")

        # --- reset the chart to a clean slate for this trade ---
        trade_info_chart.options.clear()
        trade_info_chart.options.update(trade_hold_chart_options())
        # ECharts setOption merges by default and NiceGUI strips None
        # values during JSON serialisation, so setting markLine/markArea
        # to None never reaches the client. Force a full replacement with
        # notMerge: true via run_chart_method instead.
        trade_info_chart.run_chart_method(
            "setOption", trade_info_chart.options, ":true"
        )
        trade_info_chart.set_visibility(True)
        trade_info_chart_empty.set_text("Loading price history…")

        # --- details body ---
        def info_row(label: str, value: str, value_class: str = "text-white") -> None:
            with ui.row().classes(
                "w-full justify-between py-1 border-b border-[#222938] "
                "flex-nowrap gap-3"
            ):
                ui.label(label).classes(f"text-sm {TEXT_MUTED} shrink-0")
                ui.label(value).classes(
                    f"text-sm font-mono {value_class} text-right"
                )

        held = humanize_duration(trade["entry_time"], trade["exit_time"])
        trade_info_body.clear()
        with trade_info_body:
            # Why the bot entered — the headline of the whole popup.
            with ui.column().classes(
                f"w-full gap-1 {BG_APP} rounded-lg p-3 border border-[#222938]"
            ):
                ui.label("🧠 Why the bot took this trade").classes(
                    "text-sm font-semibold text-white"
                )
                ui.label(
                    trade.get("entry_reason")
                    or "No rationale on record — this trade predates decision "
                    "capture (v2.15.0) or was adopted from an external fill."
                ).classes("text-sm text-gray-300 leading-relaxed")

            if trade.get("exit_reason"):
                with ui.column().classes(
                    f"w-full gap-1 {BG_APP} rounded-lg p-3 border border-[#222938]"
                ):
                    ui.label("🏁 How it closed").classes(
                        "text-sm font-semibold text-white"
                    )
                    ui.label(str(trade["exit_reason"])).classes(
                        "text-sm text-gray-300 leading-relaxed"
                    )

            with ui.element("div").classes(
                "w-full grid grid-cols-1 sm:grid-cols-2 gap-x-6"
            ):
                with ui.column().classes("gap-0"):
                    ui.label("Entry signal").classes(
                        f"text-xs font-semibold uppercase {TEXT_MUTED} mt-1 mb-1"
                    )
                    rsi = trade.get("entry_rsi")
                    info_row("RSI at entry", f"{rsi:.1f}" if rsi is not None else "—")
                    vwap = trade.get("entry_vwap")
                    info_row("VWAP", f"${vwap:,.2f}" if vwap is not None else "—")
                    atr = trade.get("entry_atr")
                    info_row("ATR", f"${atr:,.3f}" if atr is not None else "—")
                    sent = trade.get("entry_sentiment")
                    src = trade.get("sentiment_source")
                    info_row(
                        "News sentiment",
                        f"{sent:.2f} ({src})" if sent is not None else "—",
                    )
                with ui.column().classes("gap-0"):
                    ui.label("Execution").classes(
                        f"text-xs font-semibold uppercase {TEXT_MUTED} mt-1 mb-1"
                    )
                    info_row("Qty", f"{trade['qty']:g} sh")
                    info_row("Entry price", f"${trade['entry_price']:,.2f}")
                    info_row(
                        "Exit price",
                        "—" if trade["exit_price"] is None
                        else f"${trade['exit_price']:,.2f}",
                    )
                    tp = trade.get("take_profit")
                    sl = trade.get("stop_loss")
                    info_row(
                        "Target / Stop",
                        (f"${tp:,.2f}" if tp is not None else "—")
                        + " / "
                        + (f"${sl:,.2f}" if sl is not None else "—"),
                    )

            with ui.element("div").classes(
                "w-full grid grid-cols-1 sm:grid-cols-3 gap-x-6"
            ):
                info_row("Opened", fmt_short(trade["entry_time"]))
                info_row("Closed", fmt_short(trade["exit_time"]))
                info_row("Held", held)

        trade_info_dialog.open()

        # --- price chart of the hold window (best-effort, async) ---
        bars = await run.io_bound(
            fetch_trade_bars, symbol, trade["entry_time"], trade["exit_time"]
        )
        # A newer popup opened while we were fetching — its draw wins, drop ours.
        if token != render_state["trade_info_token"]:
            return
        if not bars:
            trade_info_chart.set_visibility(False)
            trade_info_chart_empty.set_text(
                "Price history unavailable for this window."
            )
            return

        trade_info_chart_empty.set_text("")
        series = trade_info_chart.options["series"][0]
        series["data"] = bars

        entry_ms = epoch_ms(trade["entry_time"])
        exit_ms = epoch_ms(trade["exit_time"]) if trade["exit_time"] else None
        exit_color = PNL_GREEN if (pnl or 0) >= 0 else PNL_RED

        # Entry/exit are marked as vertical lines at their timestamps (cleaner
        # than floating pins — they mark *when* without hiding the price line);
        # TP/SL are horizontal level lines. Both share one markLine series.
        mark_lines = []
        if entry_ms is not None:
            mark_lines.append({
                "xAxis": entry_ms,
                "lineStyle": {"color": SERIES_BLUE, "width": 1.5, "opacity": 0.9},
                "label": {
                    "formatter": "IN", "color": SERIES_BLUE, "position": "end",
                    "fontSize": 10, "fontWeight": "bold",
                },
            })
        if exit_ms is not None:
            mark_lines.append({
                "xAxis": exit_ms,
                "lineStyle": {"color": exit_color, "width": 1.5, "opacity": 0.9},
                "label": {
                    "formatter": "OUT", "color": exit_color, "position": "end",
                    "fontSize": 10, "fontWeight": "bold",
                },
            })
        tp = trade.get("take_profit")
        sl = trade.get("stop_loss")
        if tp is not None:
            mark_lines.append({
                "yAxis": tp,
                "lineStyle": {"color": PNL_GREEN, "type": "dashed", "width": 1},
                "label": {"formatter": "TP", "color": PNL_GREEN, "position": "insideEndTop"},
            })
        if sl is not None:
            mark_lines.append({
                "yAxis": sl,
                "lineStyle": {"color": PNL_RED, "type": "dashed", "width": 1},
                "label": {"formatter": "SL", "color": PNL_RED, "position": "insideEndBottom"},
            })
        if mark_lines:
            series["markLine"] = {
                "silent": True,
                "symbol": "none",
                "data": mark_lines,
            }

        # Faint band over the window the position was actually held, so the
        # hold period reads at a glance between the two vertical markers.
        if entry_ms is not None and exit_ms is not None:
            series["markArea"] = {
                "silent": True,
                "itemStyle": {"color": "rgba(57, 135, 229, 0.08)"},
                "data": [[{"xAxis": entry_ms}, {"xAxis": exit_ms}]],
            }
        trade_info_chart.update()

    async def show_position_info(
        pos: Dict[str, Any], entry: Optional[Dict[str, Any]]
    ) -> None:
        """Open the per-position info popup: the entry rationale, the numbers,
        and a price chart from entry to now (the still-open hold window).

        Mirrors show_trade_info for closed trades; the key difference is that
        the hold window is open-ended (entry → now) and the entry metadata
        comes from the bot's published open_entries blob, not a persisted
        trade row. Positions adopted from outside the engine (restart, manual
        fill) have no entry metadata — the button is disabled in that case and
        this branch is unreachable, but we still guard for None defensively."""
        render_state["trade_info_token"] += 1
        token = render_state["trade_info_token"]

        symbol = pos["symbol"]
        qty = pos["qty"]
        side = "BUY" if qty > 0 else "SELL"
        pnl = pos["unrealized_pnl"]
        cost_basis = pos["avg_entry_price"] * abs(qty)
        pnl_pct = (
            pnl / cost_basis * 100.0 if pnl is not None and cost_basis else None
        )
        entry = entry or {}

        # --- header: symbol, side chip, unrealized PnL ---
        trade_info_title.clear()
        with trade_info_title:
            ui.label(symbol).classes("text-xl font-bold text-white")
            side_color = "text-green-400" if side == "BUY" else "text-red-400"
            ui.label(("▲ LONG" if side == "BUY" else "▼ SHORT")).classes(
                f"text-xs font-mono font-semibold {side_color} "
                "bg-[#1d2432] px-2 py-0.5 rounded border border-[#2a3140]"
            )
            if pnl is not None:
                ui.label(
                    money(pnl, signed=True)
                    + (f" ({pnl_pct:+.2f}%)" if pnl_pct is not None else "")
                ).classes(f"text-sm font-mono font-semibold {pnl_text_class(pnl)}")

        # --- reset the chart to a clean slate for this position ---
        trade_info_chart.options.clear()
        trade_info_chart.options.update(trade_hold_chart_options())
        trade_info_chart.run_chart_method(
            "setOption", trade_info_chart.options, ":true"
        )
        trade_info_chart.set_visibility(True)
        trade_info_chart_empty.set_text("Loading price history…")

        # --- details body ---
        def info_row(label: str, value: str, value_class: str = "text-white") -> None:
            with ui.row().classes(
                "w-full justify-between py-1 border-b border-[#222938] "
                "flex-nowrap gap-3"
            ):
                ui.label(label).classes(f"text-sm {TEXT_MUTED} shrink-0")
                ui.label(value).classes(
                    f"text-sm font-mono {value_class} text-right"
                )

        entry_time = entry.get("entry_time")
        held = (
            humanize_duration(entry_time, datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ))
            if entry_time else "—"
        )
        trade_info_body.clear()
        with trade_info_body:
            # Why the bot took this position — the headline of the popup.
            with ui.column().classes(
                f"w-full gap-1 {BG_APP} rounded-lg p-3 border border-[#222938]"
            ):
                ui.label("🧠 Why the bot took this position").classes(
                    "text-sm font-semibold text-white"
                )
                ui.label(
                    entry.get("entry_reason")
                    or "No rationale on record — this position was adopted from "
                    "an external fill (e.g. before a restart) and predates "
                    "decision capture, or its entry metadata is unavailable."
                ).classes("text-sm text-gray-300 leading-relaxed")

            # Stop/target status — resting exchange-side bracket legs for
            # regular-session entries, engine-polled soft levels otherwise —
            # with the current price so you can see how close it is.
            tp = entry.get("take_profit")
            sl = entry.get("stop_loss")
            current = pos.get("current_price")
            levels_label = (
                "🎯 Stop / target (resting on the exchange)"
                if entry.get("native_bracket")
                else "🎯 Soft stop / target (polled each cycle)"
            )
            with ui.column().classes(
                f"w-full gap-1 {BG_APP} rounded-lg p-3 border border-[#222938]"
            ):
                ui.label(levels_label).classes(
                    "text-sm font-semibold text-white"
                )
                with ui.row().classes("w-full justify-between py-1 gap-3"):
                    ui.label("Take profit").classes(
                        f"text-sm {TEXT_MUTED} shrink-0"
                    )
                    ui.label(
                        f"${tp:,.2f}" if tp is not None else "—"
                    ).classes(
                        f"text-sm font-mono {PNL_GREEN} text-right"
                    )
                with ui.row().classes("w-full justify-between py-1 gap-3"):
                    ui.label("Stop loss").classes(
                        f"text-sm {TEXT_MUTED} shrink-0"
                    )
                    ui.label(
                        f"${sl:,.2f}" if sl is not None else "—"
                    ).classes(
                        f"text-sm font-mono {PNL_RED} text-right"
                    )
                with ui.row().classes("w-full justify-between py-1 gap-3"):
                    ui.label("Current price").classes(
                        f"text-sm {TEXT_MUTED} shrink-0"
                    )
                    ui.label(
                        "—" if current is None else f"${current:,.2f}"
                    ).classes("text-sm font-mono text-white text-right")

            with ui.element("div").classes(
                "w-full grid grid-cols-1 sm:grid-cols-2 gap-x-6"
            ):
                with ui.column().classes("gap-0"):
                    ui.label("Entry signal").classes(
                        f"text-xs font-semibold uppercase {TEXT_MUTED} mt-1 mb-1"
                    )
                    rsi = entry.get("entry_rsi")
                    info_row("RSI at entry", f"{rsi:.1f}" if rsi is not None else "—")
                    vwap = entry.get("entry_vwap")
                    info_row("VWAP", f"${vwap:,.2f}" if vwap is not None else "—")
                    atr = entry.get("entry_atr")
                    info_row("ATR", f"${atr:,.3f}" if atr is not None else "—")
                    sent = entry.get("entry_sentiment")
                    src = entry.get("sentiment_source")
                    info_row(
                        "News sentiment",
                        f"{sent:.2f} ({src})" if sent is not None else "—",
                    )
                with ui.column().classes("gap-0"):
                    ui.label("Execution").classes(
                        f"text-xs font-semibold uppercase {TEXT_MUTED} mt-1 mb-1"
                    )
                    info_row("Qty", f"{abs(qty):g} sh")
                    info_row("Entry price", f"${pos['avg_entry_price']:,.2f}")
                    info_row(
                        "Now",
                        "—" if current is None else f"${current:,.2f}",
                    )
                    info_row(
                        "Market value",
                        "—" if pos.get("market_value") is None
                        else f"${pos['market_value']:,.2f}",
                    )

            with ui.element("div").classes(
                "w-full grid grid-cols-1 sm:grid-cols-2 gap-x-6"
            ):
                info_row("Opened", fmt_short(entry_time) if entry_time else "—")
                info_row("Held so far", held)

        trade_info_dialog.open()

        # --- price chart from entry to now (best-effort, async) ---
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        bars = await run.io_bound(
            fetch_trade_bars, symbol, entry_time or now_iso, now_iso
        )
        # A newer popup opened while we were fetching — its draw wins, drop ours.
        if token != render_state["trade_info_token"]:
            return
        if not bars:
            trade_info_chart.set_visibility(False)
            trade_info_chart_empty.set_text(
                "Price history unavailable for this window."
            )
            return

        trade_info_chart_empty.set_text("")
        series = trade_info_chart.options["series"][0]
        series["data"] = bars

        entry_ms = epoch_ms(entry_time) if entry_time else None

        # Entry is marked as a vertical line; an open position has no exit
        # marker yet. TP/SL are horizontal level lines.
        mark_lines = []
        if entry_ms is not None:
            mark_lines.append({
                "xAxis": entry_ms,
                "lineStyle": {"color": SERIES_BLUE, "width": 1.5, "opacity": 0.9},
                "label": {
                    "formatter": "IN", "color": SERIES_BLUE, "position": "end",
                    "fontSize": 10, "fontWeight": "bold",
                },
            })
        if tp is not None:
            mark_lines.append({
                "yAxis": tp,
                "lineStyle": {"color": PNL_GREEN, "type": "dashed", "width": 1},
                "label": {"formatter": "TP", "color": PNL_GREEN, "position": "insideEndTop"},
            })
        if sl is not None:
            mark_lines.append({
                "yAxis": sl,
                "lineStyle": {"color": PNL_RED, "type": "dashed", "width": 1},
                "label": {"formatter": "SL", "color": PNL_RED, "position": "insideEndBottom"},
            })
        if mark_lines:
            series["markLine"] = {
                "silent": True,
                "symbol": "none",
                "data": mark_lines,
            }

        # Faint band from entry to the last bar (the position is still open).
        if entry_ms is not None and bars:
            last_ms = bars[-1][0]
            series["markArea"] = {
                "silent": True,
                "itemStyle": {"color": "rgba(57, 135, 229, 0.08)"},
                "data": [[{"xAxis": entry_ms}, {"xAxis": last_ms}]],
            }
        trade_info_chart.update()

    def render_environment(snapshot: Dict[str, Any]) -> None:
        environment = snapshot["environment"]
        for key, label in environment_labels.items():
            value = environment.get(key)
            if value is None:
                label.set_text("—")
            elif key == "engine_started_at":
                label.set_text(fmt_short(str(value)))
            elif isinstance(value, float) and value == int(value):
                label.set_text(f"{value:g}")
            else:
                label.set_text(str(value))

    def _render_analyst_card(
        report: Optional[Dict[str, Any]],
        summary_label: ui.label,
        warnings_label: ui.label,
        warnings_col: ui.column,
        suggestions_label: ui.label,
        suggestions_col: ui.column,
        confidence_label: ui.label,
        timestamp_label: ui.label,
        empty_label: ui.label,
    ) -> None:
        if not report:
            empty_label.set_visibility(True)
            summary_label.set_text("")
            warnings_label.set_visibility(False)
            warnings_col.clear()
            suggestions_label.set_visibility(False)
            suggestions_col.clear()
            confidence_label.set_text("")
            timestamp_label.set_text("")
            return
        empty_label.set_visibility(False)
        summary_label.set_text(report.get("summary", ""))
        warnings = report.get("warnings") or []
        warnings_label.set_visibility(bool(warnings))
        warnings_col.clear()
        with warnings_col:
            for w in warnings:
                ui.label(f"⚠ {w}").classes(
                    "text-xs text-amber-400 bg-[#1d2432] "
                    "border border-[#2a3140] rounded-lg px-2 py-1"
                )
        suggestions = report.get("suggestions") or []
        suggestions_label.set_visibility(bool(suggestions))
        suggestions_col.clear()
        with suggestions_col:
            for i, s in enumerate(suggestions, 1):
                ui.label(f"{i}. {s}").classes(
                    "text-xs text-gray-300"
                )
        conf = report.get("confidence")
        confidence_label.set_text(
            f"Confidence: {conf * 100:.0f}%" if conf is not None else ""
        )
        reviewed = report.get("reviewed_at")
        timestamp_label.set_text(
            f"Reviewed {fmt_short(reviewed)}" if reviewed else ""
        )

    AGENT_BY_KEY = {a["key"]: a for a in ANALYST_AGENTS}

    REVIEW_TYPE_META = {
        "trades": ("📊", "Trade review", "text-sky-400"),
        "optimization": ("🔬", "Optimization", "text-violet-400"),
        "watchlist": ("📋", "Watchlist", "text-teal-400"),
    }

    DECISION_CHIPS = {
        "accept": "text-emerald-400 border-emerald-800",
        "override": "text-amber-400 border-amber-800",
        "reject": "text-rose-400 border-rose-800",
    }

    OPTIMIZER_STATUS_CHIPS = {
        "applied": "text-emerald-400 border-emerald-800",
        "no_change": "text-gray-400 border-gray-700",
        "rejected_validation": "text-amber-400 border-amber-800",
        "rejected_analyst": "text-rose-400 border-rose-800",
        "no_combination": "text-amber-400 border-amber-800",
        "no_data": "text-rose-400 border-rose-800",
        "error": "text-red-500 border-red-800",
    }

    def render_analyst_agents(snapshot: Dict[str, Any]) -> None:
        analyst_cfg = snapshot.get("analyst_config") or {}
        call_log = snapshot.get("analyst_call_log") or []
        fingerprint = (
            len(call_log),
            call_log[-1].get("ts") if call_log else None,
            tuple(
                analyst_cfg.get(f, "")
                for f in ("model", "sentiment_model", "watchlist_model", "risk_model")
            ),
            analyst_cfg.get("trade_review_interval_hours"),
        )
        if render_state.get("agents_fp") == fingerprint:
            return
        render_state["agents_fp"] = fingerprint

        stats = agent_call_stats(call_log)
        main_model = analyst_cfg.get("model", "deepseek-r1")
        agents_grid.clear()
        with agents_grid:
            for agent in ANALYST_AGENTS:
                if is_crypto() and agent["key"] == "optimization":
                    continue
                agent_stats = stats.get(agent["key"])
                model = analyst_cfg.get(agent["model_field"]) or main_model
                when = agent["when"]
                if agent["key"] == "trades":
                    try:
                        hours = float(
                            analyst_cfg.get("trade_review_interval_hours", 4)
                        )
                        when = f"every {hours:g}h during market hours"
                    except (TypeError, ValueError):
                        pass
                with ui.column().classes(
                    "gap-1 rounded-lg border border-[#2a3140] "
                    "bg-[#1d2432] p-3 min-w-0"
                ):
                    with ui.row().classes(
                        "w-full items-center justify-between flex-nowrap gap-2"
                    ):
                        ui.label(f"{agent['icon']} {agent['name']}").classes(
                            "text-sm font-semibold text-white truncate"
                        )
                        if agent_stats is None:
                            ui.label("●").classes("text-xs text-gray-600")
                        elif agent_stats["last_ok"]:
                            ui.label("●").classes("text-xs text-emerald-400")
                        else:
                            dot = ui.label("●").classes("text-xs text-rose-400")
                            with dot:
                                ui.tooltip(
                                    agent_stats.get("last_error")
                                    or "last call failed"
                                ).classes("max-w-96 break-all")
                    ui.label(agent["what"]).classes(f"text-xs {TEXT_MUTED}")
                    with ui.row().classes(
                        "w-full items-center gap-2 flex-nowrap mt-1"
                    ):
                        ui.label(model).classes(
                            "text-[10px] font-mono text-sky-300 "
                            "border border-[#2a3140] rounded px-1.5 py-0.5 "
                            "truncate"
                        )
                        ui.label(when).classes(
                            f"text-[10px] {TEXT_MUTED} truncate"
                        )
                    if agent_stats is None:
                        ui.label("no calls recorded yet").classes(
                            "text-[10px] text-gray-600"
                        )
                    else:
                        errors = agent_stats["errors"]
                        parts = [
                            f"{agent_stats['calls']} calls",
                            f"{errors} errors" if errors else "0 errors",
                            f"⌀ {fmt_latency(agent_stats['avg_latency_ms'])}",
                            humanize_age(age_seconds(agent_stats["last_ts"])),
                        ]
                        ui.label(" · ".join(parts)).classes(
                            "text-[10px] "
                            + ("text-rose-400" if errors else "text-gray-500")
                        )

    VETO_GATE_LABELS = {
        "sentiment": "📰 News sentiment",
        "vwap_recheck": "📉 VWAP re-check",
        "risk_agent": "🛡️ Risk agent",
        "portfolio_manager": "💼 Portfolio manager",
        "regime": "🌐 Market regime",
    }

    def render_vetoes(snapshot: Dict[str, Any]) -> None:
        stats = snapshot.get("veto_stats") or {}
        gates = stats.get("gates") or {}
        fingerprint = (
            stats.get("total"),
            stats.get("resolved"),
            round(stats.get("hypo_pnl", 0.0) or 0.0, 2),
        )
        if render_state.get("vetoes_fp") == fingerprint:
            return
        render_state["vetoes_fp"] = fingerprint

        total = int(stats.get("total") or 0)
        vetoes_empty.set_visibility(total == 0)
        vetoes_headline.set_visibility(total > 0)
        vetoes_box.clear()
        if total == 0:
            return

        resolved = int(stats.get("resolved") or 0)
        pnl = float(stats.get("hypo_pnl") or 0.0)
        verdict = (
            "the gates saved money so far"
            if pnl < 0
            else "the gates cost money so far"
            if pnl > 0
            else "flat so far"
        )
        vetoes_headline.set_text(
            f"Blocked signals would have made ${pnl:+,.2f} "
            f"({resolved} of {total} resolved) — {verdict}."
        )
        with vetoes_box:
            for gate, g in sorted(
                gates.items(), key=lambda kv: kv[1]["total"], reverse=True
            ):
                g_pnl = float(g.get("hypo_pnl") or 0.0)
                g_resolved = int(g.get("resolved") or 0)
                # Negative blocked P&L = the veto avoided a loss = good.
                pnl_color = (
                    PNL_GREEN if g_pnl < 0
                    else "text-rose-400" if g_pnl > 0
                    else TEXT_MUTED
                )
                with ui.row().classes(
                    "w-full items-center justify-between gap-3 flex-nowrap "
                    "border-b border-[#222938] py-1"
                ):
                    ui.label(
                        VETO_GATE_LABELS.get(gate, gate)
                    ).classes("text-sm text-white shrink-0")
                    ui.label(
                        f"{g['total']} blocked · {g_resolved} resolved · "
                        f"{g.get('would_win', 0)}W/{g.get('would_lose', 0)}L"
                    ).classes(f"text-xs {TEXT_MUTED} truncate")
                    ui.label(f"${g_pnl:+,.2f}").classes(
                        f"text-sm font-mono {pnl_color} shrink-0"
                    )

    def render_review_history(snapshot: Dict[str, Any]) -> None:
        history = snapshot.get("analyst_review_history") or []
        fingerprint = (
            len(history),
            history[-1].get("ts") if history else None,
        )
        if render_state.get("history_fp") == fingerprint:
            return
        render_state["history_fp"] = fingerprint

        review_history_empty.set_visibility(not history)
        review_history_col.clear()
        with review_history_col:
            for entry in reversed(history[-20:]):
                icon, type_label, type_color = REVIEW_TYPE_META.get(
                    entry.get("type"),
                    ("🧠", str(entry.get("type", "?")), "text-gray-300"),
                )
                with ui.column().classes(
                    "w-full gap-1 rounded-lg border border-[#2a3140] "
                    "bg-[#1d2432] px-3 py-2"
                ):
                    with ui.row().classes(
                        "w-full items-center gap-2 flex-nowrap"
                    ):
                        ui.label(f"{icon} {type_label}").classes(
                            f"text-xs font-semibold {type_color} shrink-0"
                        )
                        decision = entry.get("decision")
                        if decision:
                            chip = ui.label(str(decision).upper()).classes(
                                "text-[10px] font-bold rounded border "
                                "px-1.5 py-0.5 shrink-0 "
                                + DECISION_CHIPS.get(
                                    decision, "text-gray-300 border-gray-700"
                                )
                            )
                            reason = entry.get("decision_reason")
                            if reason:
                                with chip:
                                    ui.tooltip(reason).classes("max-w-96")
                        conf = entry.get("confidence")
                        if conf is not None:
                            ui.label(f"{float(conf) * 100:.0f}%").classes(
                                f"text-[10px] {TEXT_MUTED} shrink-0"
                            )
                        warn_count = entry.get("warnings") or 0
                        if warn_count:
                            ui.label(f"⚠ {warn_count}").classes(
                                "text-[10px] text-amber-400 shrink-0"
                            )
                        ui.space()
                        ui.label(fmt_short(entry.get("ts"))).classes(
                            f"text-[10px] {TEXT_MUTED} shrink-0"
                        )
                    summary = entry.get("summary") or ""
                    if summary:
                        clamped = ui.label(summary).classes(
                            "text-xs text-gray-300"
                        ).style(
                            "display:-webkit-box;-webkit-line-clamp:2;"
                            "-webkit-box-orient:vertical;overflow:hidden"
                        )
                        with clamped:
                            ui.tooltip(summary).classes("max-w-[32rem]")

    def render_optimizer_status(snapshot: Dict[str, Any]) -> None:
        """Show live optimizer progress while a grid search is running, and
        hide it when idle. Also keeps the 'Run optimizer now' buttons
        disabled/enabled to match the running state."""
        status = snapshot.get("optimizer_status") or {"phase": "idle"}
        phase = status.get("phase", "idle")
        is_running = phase not in ("idle", None)
        started_at = status.get("started_at")

        # Keep both "Run optimizer now" buttons in sync with the live state —
        # disabled while running, re-enabled when the run finishes.
        for btn in (optimize_button_opt, optimize_button):
            if is_running:
                btn.disable()
            else:
                btn.enable()
        for sp in (optimize_spinner_opt, optimize_spinner):
            sp.set_visibility(is_running)

        # Only rebuild the live card contents when the phase or a key counter
        # changes — avoids flickering on every 2s refresh tick. While running
        # we always rebuild so the elapsed timer ticks on every refresh.
        fingerprint = (
            phase,
            status.get("evaluated"),
            status.get("candidates"),
            status.get("validated"),
        )
        if not is_running and render_state.get("opt_status_fp") == fingerprint:
            return
        render_state["opt_status_fp"] = fingerprint

        optimizer_live_card.set_visibility(is_running)
        optimizer_live_card.clear()
        if not is_running:
            return

        phase_labels = {
            "fetching": "Fetching historical bars",
            "grid_search": "Scoring parameter combinations",
            "validation": "Validating out-of-sample",
            "analyst": "LLM analyst review",
            "writing": "Writing parameters",
        }
        with optimizer_live_card:
            with ui.column().classes(
                "w-full gap-2 rounded-lg border border-violet-900/50 "
                "bg-violet-950/20 px-4 py-3"
            ):
                with ui.row().classes("w-full items-center gap-2"):
                    ui.spinner(size="sm", color="primary")
                    ui.label(phase_labels.get(phase, phase)).classes(
                        "text-sm font-medium text-violet-200"
                    )
                    ui.space()
                    trigger = status.get("trigger", "manual")
                    ui.label(trigger.upper()).classes(
                        "text-[10px] font-bold rounded border "
                        "px-1.5 py-0.5 shrink-0 "
                        + (
                            "text-sky-400 border-sky-800"
                            if trigger == "nightly"
                            else "text-amber-400 border-amber-800"
                        )
                    )
                # Progress bar for the grid-search phase (the only phase with
                # a known total); other phases show a spinner without a bar.
                if phase == "grid_search":
                    total = status.get("total_combinations", 0) or 0
                    done = status.get("evaluated", 0) or 0
                    pct = (done / total * 100) if total > 0 else 0.0
                    ui.linear_progress(value=pct / 100.0).classes("w-full")
                    ui.label(
                        f"{done:,} / {total:,} combinations "
                        f"({pct:.0f}%) · {status.get('candidates', 0)} candidates"
                    ).classes(f"text-xs {TEXT_MUTED}")
                elif phase == "validation":
                    ui.label(
                        f"Validated {status.get('validated', 0)} of "
                        f"{status.get('candidates', 0)} ranked candidates"
                    ).classes(f"text-xs {TEXT_MUTED}")
                # Elapsed time since the run started.
                if started_at:
                    start_local = to_local(started_at)
                    if start_local is not None:
                        elapsed_min = max(
                            (datetime.now(start_local.tzinfo) - start_local)
                            .total_seconds() / 60.0,
                            0,
                        )
                        if elapsed_min < 60:
                            elapsed = f"{elapsed_min:.0f}m"
                        else:
                            elapsed = (
                                f"{int(elapsed_min // 60)}h "
                                f"{int(elapsed_min % 60):02d}m"
                            )
                        ui.label(f"Elapsed {elapsed}").classes(
                            f"text-[10px] font-mono {TEXT_MUTED}"
                        )

    def render_optimizer_runs(snapshot: Dict[str, Any]) -> None:
        runs = snapshot.get("optimizer_runs") or []
        fingerprint = (
            len(runs),
            runs[0].get("id") if runs else None,
        )
        if render_state.get("opt_runs_fp") == fingerprint:
            return
        render_state["opt_runs_fp"] = fingerprint

        optimizer_runs_empty.set_visibility(not runs)
        optimizer_runs_col.clear()
        with optimizer_runs_col:
            for run in runs:
                status = run.get("status", "error")
                chip_style = OPTIMIZER_STATUS_CHIPS.get(
                    status, "text-gray-300 border-gray-700"
                )
                with ui.column().classes(
                    "w-full gap-1 rounded-lg border border-[#2a3140] "
                    "bg-[#1d2432] px-3 py-2"
                ):
                    # Header row: timestamp, trigger badge, duration, outcome.
                    # flex-wrap: with a real run the chip train (timestamp +
                    # NIGHTLY + duration + REJECTED_ANALYST + analyst verdict)
                    # is wider than a phone — wrapping beats sideways scroll.
                    with ui.row().classes(
                        "w-full items-center gap-x-2 gap-y-1 flex-wrap"
                    ):
                        ui.label(fmt_short(run.get("started_at"))).classes(
                            "text-xs font-mono text-gray-300 shrink-0"
                        )
                        trigger = run.get("trigger", "manual")
                        ui.label(trigger.upper()).classes(
                            "text-[10px] font-bold rounded border "
                            "px-1.5 py-0.5 shrink-0 "
                            + (
                                "text-sky-400 border-sky-800"
                                if trigger == "nightly"
                                else "text-amber-400 border-amber-800"
                            )
                        )
                        dur = humanize_duration(
                            run.get("started_at"), run.get("finished_at")
                        )
                        ui.label(dur).classes(
                            f"text-[10px] font-mono {TEXT_MUTED} shrink-0"
                        )
                        ui.label(status.upper()).classes(
                            "text-[10px] font-bold rounded border "
                            "px-1.5 py-0.5 shrink-0 " + chip_style
                        )
                        if run.get("analyst_decision"):
                            ad = run["analyst_decision"]
                            ui.label(f"analyst: {ad}").classes(
                                "text-[10px] font-mono "
                                + DECISION_CHIPS.get(ad, "text-gray-300")
                                + " shrink-0"
                            )
                        ui.space()
                        if run.get("detail"):
                            # Own full-width line on phones; inline right on
                            # desktop. min-w-0 lets truncate actually shrink.
                            ui.label(run["detail"]).classes(
                                f"text-[10px] {TEXT_MUTED} truncate min-w-0 "
                                "max-sm:w-full"
                            ).tooltip(run["detail"])

                    # Stats row: symbols, candidates, train/val
                    parts = []
                    n_sym = run.get("n_symbols", 0)
                    cand = run.get("candidates", 0)
                    parts.append(f"{n_sym} symbols")
                    if cand:
                        parts.append(f"{cand} candidates")
                    tr = run.get("train_return")
                    if tr is not None:
                        td = run.get("train_drawdown", 0)
                        tt = run.get("train_trades", 0)
                        ts = run.get("train_score", 0)
                        parts.append(
                            f"train: {tr * 100:+.2f}% / {td * 100:.1f}%dd "
                            f"/ {tt}t / score {ts:.1f}"
                        )
                    vr = run.get("val_return")
                    if vr is not None:
                        vd = run.get("val_drawdown", 0)
                        vt = run.get("val_trades", 0)
                        parts.append(
                            f"val: {vr * 100:+.2f}% / {vd * 100:.1f}%dd / {vt}t"
                        )
                    if parts:
                        ui.label(" · ".join(parts)).classes(
                            f"text-[10px] {TEXT_MUTED}"
                        )

                    # Changed parameters
                    changed = run.get("changed_keys") or []
                    params_before = run.get("params_before") or {}
                    params_after = run.get("params_after") or {}
                    if changed:
                        lines = []
                        for k in changed:
                            b = params_before.get(k, "—")
                            a = params_after.get(k, "—")
                            if isinstance(b, float):
                                b = f"{b:g}"
                            if isinstance(a, float):
                                a = f"{a:g}"
                            lines.append(f"{k}: {b} → {a}")
                        ui.label(" | ".join(lines)).classes(
                            "text-[10px] font-mono text-sky-300"
                        )
                    else:
                        ui.label("No parameter change").classes(
                            f"text-[10px] {TEXT_MUTED}"
                        )

    def render_call_log(snapshot: Dict[str, Any]) -> None:
        call_log = snapshot.get("analyst_call_log") or []
        fingerprint = (
            len(call_log),
            call_log[-1].get("ts") if call_log else None,
        )
        if render_state.get("calllog_fp") == fingerprint:
            return
        render_state["calllog_fp"] = fingerprint

        call_log_empty.set_visibility(not call_log)
        call_log_col.clear()
        with call_log_col:
            for entry in reversed(call_log[-25:]):
                agent = AGENT_BY_KEY.get(entry.get("agent"), {})
                ok = entry.get("ok", False)
                with ui.row().classes(
                    "w-full gap-2 py-0.5 items-baseline flex-nowrap"
                ):
                    ui.label(fmt_clock(entry.get("ts"))).classes(
                        "text-xs text-[#5a6274] shrink-0"
                    )
                    ui.label("✓" if ok else "✗").classes(
                        "text-xs font-bold shrink-0 "
                        + ("text-emerald-400" if ok else "text-rose-400")
                    )
                    ui.label(
                        f"{agent.get('icon', '•')} "
                        f"{agent.get('name', entry.get('agent', '?'))}"
                    ).classes("text-xs text-gray-200 shrink-0")
                    ui.label(entry.get("model", "")).classes(
                        f"text-xs font-mono {TEXT_MUTED} truncate"
                    )
                    ui.space()
                    ui.label(fmt_latency(entry.get("latency_ms"))).classes(
                        f"text-xs {TEXT_MUTED} shrink-0"
                    )
                error = entry.get("error")
                if not ok and error:
                    ui.label(f"↳ {error}").classes(
                        "text-[10px] text-rose-400/80 break-all pl-16 w-full"
                    )

    def render_analyst(snapshot: Dict[str, Any]) -> None:
        config = snapshot["config"]
        enabled = bool(config.get("analyst_enabled", 0.0))
        if render_state["analyst_enabled"] != enabled:
            render_state["analyst_enabled"] = enabled

        analyst_cfg = snapshot.get("analyst_config") or {}
        model = analyst_cfg.get("model", "deepseek-r1")
        base_url = analyst_cfg.get("base_url", "")
        analyst_model_label.set_text(
            f"Model: {model}"
            + (f" @ {base_url}" if base_url else " (not configured)")
        )

        # Populate config fields (only if user hasn't edited them)
        if not analyst_base_url_input.value:
            analyst_base_url_input.value = base_url
        if not analyst_api_key_input.value:
            analyst_api_key_input.value = analyst_cfg.get("api_key", "")
        if not analyst_model_input.value or analyst_model_input.value == "deepseek-r1":
            analyst_model_input.value = model
        if not analyst_sentiment_model_input.value:
            analyst_sentiment_model_input.value = analyst_cfg.get(
                "sentiment_model", ""
            )
        if not analyst_watchlist_model_input.value:
            analyst_watchlist_model_input.value = analyst_cfg.get(
                "watchlist_model", ""
            )
        if not analyst_risk_model_input.value:
            analyst_risk_model_input.value = analyst_cfg.get("risk_model", "")
        if not analyst_interval_input.value or analyst_interval_input.value == 4:
            analyst_interval_input.value = float(
                analyst_cfg.get("trade_review_interval_hours", 4)
            )
        if not analyst_lookback_input.value or analyst_lookback_input.value == 50:
            analyst_lookback_input.value = int(
                analyst_cfg.get("trade_lookback", 50)
            )

        # Connection status
        if not base_url:
            analyst_status_label.set_text(
                "Not configured — set the base URL and apply"
            )
        elif enabled:
            analyst_status_label.set_text("Connected — analyst is active")
        else:
            analyst_status_label.set_text(
                "Configured but disabled — toggle ON to activate"
            )

        # Fail-open badge: signals that passed un-gated because the risk
        # agent / portfolio manager was unreachable (counted per engine
        # session, reset at engine start).
        health = snapshot.get("analyst_health") or {}
        counts = health.get("auto_approvals") or {}
        risk_n = int(counts.get("risk", 0) or 0)
        pm_n = int(counts.get("portfolio", 0) or 0)
        total = risk_n + pm_n
        if total > 0:
            last_at = humanize_age(age_seconds(health.get("last_error_at")))
            analyst_failopen_banner.set_text(
                f"⚠️ Fail-open: {total} auto-approval"
                f"{'s' if total != 1 else ''} this session — signals passed "
                f"UN-GATED because an agent was unreachable "
                f"(risk agent {risk_n}, portfolio manager {pm_n}; "
                f"last error {last_at}: "
                f"{(health.get('last_error') or '?')[:120]})"
            )
            analyst_failopen_banner.set_visibility(True)
        else:
            analyst_failopen_banner.set_visibility(False)

        _render_analyst_card(
            snapshot.get("analyst_optimization"),
            opt_summary,
            opt_warnings_label,
            opt_warnings,
            opt_suggestions_label,
            opt_suggestions,
            opt_confidence,
            opt_timestamp,
            opt_empty,
        )
        _render_analyst_card(
            snapshot.get("analyst_trades"),
            trade_summary,
            trade_warnings_label,
            trade_warnings,
            trade_suggestions_label,
            trade_suggestions,
            trade_confidence,
            trade_timestamp,
            trade_empty,
        )
        render_analyst_agents(snapshot)
        render_vetoes(snapshot)
        render_review_history(snapshot)
        render_call_log(snapshot)

    async def on_analyst_toggle(value: bool) -> None:
        if getattr(analyst_toggle, "_suppress_change", False):
            return
        result = await run.io_bound(
            call_backend, "/analyst/toggle", 10.0, ui_state["market"]
        )
        if result["ok"]:
            state = "enabled" if result["data"].get("analyst_enabled") else "disabled"
            ui.notify(f"Analyst {state}", type="positive")
        else:
            ui.notify(
                f"Toggle failed: {result['error']}",
                type="negative",
                timeout=10000,
            )
            analyst_toggle.set_value(not value)

    def render_log_line(entry: Dict[str, Any]) -> None:
        level = entry["level"]
        color = LEVEL_COLORS.get(level, "text-gray-300")
        with ui.row().classes("w-full gap-2 py-0.5 items-baseline flex-nowrap"):
            ui.label(fmt_clock(entry["ts"])).classes(
                "text-xs text-[#5a6274] shrink-0"
            )
            ui.label(f"[{level}]").classes(f"text-xs font-bold shrink-0 {color}")
            ui.label(entry["message"]).classes("text-xs text-gray-200 break-all")

    def render_logs(snapshot: Dict[str, Any]) -> None:
        levels = set(log_level_select.value or [])
        needle = (log_search.value or "").lower()
        entries = [
            entry
            for entry in snapshot["logs"]
            if entry["level"] in levels
            and (not needle or needle in entry["message"].lower())
        ]
        log_count_label.set_text(
            f"{len(entries)} / {len(snapshot['logs'])} events · newest first"
        )
        log_container.clear()
        with log_container:
            if not entries:
                ui.label("No matching log entries").classes(
                    f"text-sm {TEXT_MUTED}"
                )
            for entry in entries:
                render_log_line(entry)

        activity_container.clear()
        with activity_container:
            recent = snapshot["logs"][:6]
            if not recent:
                ui.label("No log entries yet").classes(f"text-sm {TEXT_MUTED}")
            for entry in recent:
                render_log_line(entry)

    # ------------------------------------------------------------------ #
    # reactive refresh loop
    # ------------------------------------------------------------------ #
    async def refresh() -> None:
        window = EQUITY_RANGES.get(equity_range.value or "1D")
        since = (
            (datetime.now(timezone.utc) - window).isoformat(timespec="seconds")
            if window is not None
            else None
        )
        try:
            snapshot = await run.io_bound(
                fetch_snapshot,
                int(log_rows_select.value or DEFAULT_LOG_ROWS),
                since,
                ui_state["market"],
            )
        except Exception as exc:
            logger.error("Dashboard refresh failed: %s", exc)
            return

        render_header(snapshot)
        render_equity(snapshot)
        render_positions(snapshot)
        render_regime_and_engine(snapshot)
        render_parameters(snapshot)
        render_screener(snapshot)
        render_trades(snapshot)
        render_analyst(snapshot)
        render_environment(snapshot)
        render_logs(snapshot)
        render_optimizer_status(snapshot)
        render_optimizer_runs(snapshot)

    timer = ui.timer(DEFAULT_REFRESH_SECONDS, refresh)
    refresh_select.on_value_change(
        lambda event: setattr(timer, "interval", float(event.value))
    )

    async def on_range_change(_) -> None:
        await refresh()

    equity_range.on_value_change(on_range_change)

    _update_market_visibility()


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        host="0.0.0.0",
        port=8080,
        title="Argus Command Center",
        favicon=ARGUS_FAVICON_SVG,
        dark=True,
        reload=False,
        show=False,
    )
