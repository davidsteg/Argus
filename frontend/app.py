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

DEFAULT_REFRESH_SECONDS = 2.0
DEFAULT_LOG_ROWS = 50
TRADES_LIMIT = 200

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
    "RISK_ON": {"dot": "bg-emerald-400", "text": "text-emerald-400"},
    "CAUTION": {"dot": "bg-amber-400", "text": "text-amber-400"},
    "RISK_OFF": {"dot": "bg-red-500", "text": "text-red-400"},
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
    "news_cutoff": {
        "label": "News Cutoff",
        "hint": "minimum sentiment score to trade (0.50 = neutral/no news)",
        "min": 0.0, "max": 1.0, "step": 0.01, "int": False,
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

ENVIRONMENT_LABELS: Dict[str, str] = {
    "universe_mode": "Universe",
    "watchlist_size": "Watchlist size",
    "position_size_usd": "Position size (USD)",
    "risk_per_trade_usd": "Risk per trade (USD)",
    "max_positions": "Max positions",
    "daily_stop_loss": "Daily stop loss (USD)",
    "min_price_usd": "Min price (USD)",
    "cooldown_minutes": "Loser cooldown (min)",
    "poll_interval_seconds": "Poll interval (s)",
    "bar_lookback_minutes": "Bar lookback (min)",
    "regime_symbol": "Regime symbol",
    "engine_version": "Engine version",
    "engine_started_at": "Engine started",
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


def fetch_snapshot(log_limit: int, equity_since: Optional[str]) -> Dict[str, Any]:
    """Blocking read of everything the dashboard shows (run via io_bound)."""
    db = get_db()
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
        "environment": db.get_state("environment", {}) or {},
        "analyst_optimization": db.get_state("analyst_optimization"),
        "analyst_trades": db.get_state("analyst_trades"),
        "analyst_config": db.get_state("analyst_config") or {},
    }


def execute_hard_stop() -> Dict[str, Any]:
    """Blocking emergency intervention (run via io_bound).

    Talks directly to Alpaca — cancel everything, flatten everything — and
    persists KILLED so the backend engine stops itself on its next cycle
    and refuses to restart.
    """
    from alpaca.trading.client import TradingClient

    db = get_db()
    db.add_log("CRITICAL", "EMERGENCY HARD STOP triggered from dashboard")
    errors = []
    try:
        trading = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
        try:
            trading.cancel_orders()
            db.add_log("CRITICAL", "All open orders cancelled (dashboard)")
        except Exception as exc:
            errors.append(f"cancel orders: {exc}")
            db.add_log("ERROR", f"Dashboard cancel-all failed: {exc}")
        try:
            trading.close_all_positions(cancel_orders=True)
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


def close_single_position(symbol: str) -> Optional[str]:
    """Blocking market-close of one position via the dashboard's own
    Alpaca client (paper trading, like the hard stop). Returns an error
    message or None on success."""
    from alpaca.trading.client import TradingClient
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    db = get_db()
    db.add_log("WARNING", f"{symbol}: manual close requested from dashboard")
    try:
        trading = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
        # The bracket's exit legs hold the shares — cancel them first or
        # the market close would be rejected for insufficient quantity.
        open_orders = trading.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        )
        for order in open_orders:
            trading.cancel_order_by_id(order.id)
        trading.close_position(symbol)
        db.add_log("TRADE", f"{symbol}: manual close submitted (dashboard)")
        return None
    except Exception as exc:
        db.add_log("ERROR", f"{symbol}: dashboard close failed: {exc}")
        return str(exc)


def call_backend(path: str, timeout: float) -> Dict[str, Any]:
    """Blocking POST to the backend debug API (run via io_bound)."""
    url = f"{BACKEND_API_URL.rstrip('/')}{path}"
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
    path: str, body: Dict[str, Any], timeout: float
) -> Dict[str, Any]:
    """Blocking POST with JSON body to the backend debug API."""
    url = f"{BACKEND_API_URL.rstrip('/')}{path}"
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
    ui.add_head_html(
        "<style>"
        ".nicegui-content { padding: 0 !important; }"
        ".ag-theme-balham-dark { --ag-background-color: #161b26;"
        " --ag-header-background-color: #1d2432;"
        " --ag-odd-row-background-color: #19202c;"
        " --ag-border-color: #2a3140; }"
        ".pnl-pos { color: #34d399 !important; }"
        ".pnl-neg { color: #f87171 !important; }"
        ".pos-grid { display: grid;"
        " grid-template-columns: 1.1fr 0.7fr 1fr 1fr 1.1fr 1.5fr 2.75rem;"
        " column-gap: 0.5rem; align-items: center; width: 100%; }"
        "</style>"
    )

    db = get_db()
    initial_config = db.get_config()

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
    # header banner
    # ------------------------------------------------------------------ #
    with ui.row().classes(
        "w-full items-center justify-between px-6 py-4 "
        "bg-gradient-to-r from-[#131722] to-[#1a2030] "
        "border-b border-[#2a3140] sticky top-0 z-50"
    ):
        with ui.row().classes("items-center gap-3"):
            ui.html(ARGUS_LOGO_SVG).classes("shrink-0")
            with ui.column().classes("gap-0"):
                with ui.row().classes("items-center gap-2"):
                    ui.label("ARGUS").classes(
                        "text-2xl font-bold tracking-widest text-white"
                    )
                    ui.button(f"v{__version__}", on_click=release_dialog.open).props(
                        "flat dense no-caps"
                    ).classes(
                        "text-xs font-mono text-emerald-400 bg-[#1d2432] "
                        "px-2 py-0.5 rounded border border-[#2a3140]"
                    ).tooltip("Release notes")
                ui.label("Short-Term Algorithmic Trading — Paper").classes(
                    f"text-xs {TEXT_MUTED}"
                )

        # live condition chips: regime, market session, engine heartbeat
        with ui.row().classes("items-center gap-2"):
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
                "Market regime gates new entries only (RISK_OFF = no new "
                "buys). Evaluated while the market is open."
            )
            market_chip["holder"].tooltip("US equity market session")
            engine_chip["holder"].tooltip(
                "Heartbeat of the backend trading engine — LIVE while cycle "
                "traces keep arriving within 3× the poll interval"
            )

        with ui.row().classes("items-center gap-8"):
            with ui.column().classes("gap-0 items-end"):
                ui.label("TOTAL BALANCE").classes(f"text-xs {TEXT_MUTED}")
                balance_label = ui.label("$0.00").classes(
                    "text-xl font-semibold text-white"
                )
            with ui.column().classes("gap-0 items-end"):
                ui.label("DAILY PNL").classes(f"text-xs {TEXT_MUTED}")
                pnl_label = ui.label("$0.00").classes(
                    "text-xl font-semibold text-green-400"
                )
                pnl_pct_label = ui.label("").classes(f"text-xs {TEXT_MUTED}")
            with ui.column().classes("gap-0 items-end"):
                ui.label("STATUS").classes(f"text-xs {TEXT_MUTED}")
                status_label = ui.label("● …").classes(
                    "text-xl font-semibold text-green-400"
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
                result = await run.io_bound(execute_hard_stop)
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
                result = await run.io_bound(call_backend, "/reset", 15.0)
                if result["ok"]:
                    ui.notify("Engine resumed — state RUNNING", type="positive")
                else:
                    ui.notify(
                        f"Resume failed: {result['error']}",
                        type="negative",
                        timeout=10000,
                    )
                    resume_button.enable()

            resume_button = ui.button("RESUME", on_click=on_resume_click).classes(
                "bg-emerald-700 hover:bg-emerald-600 text-white font-bold "
                "px-5 py-3 rounded-lg shadow-lg shadow-emerald-900/40"
            ).props("color=green-10 push")
            resume_button.set_visibility(False)

            emergency_button = ui.button(
                "EMERGENCY HARD STOP", on_click=on_emergency_click
            ).classes(
                "bg-red-700 hover:bg-red-600 text-white font-bold "
                "px-5 py-3 rounded-lg shadow-lg shadow-red-900/40"
            ).props("color=red-10 push")

    # ------------------------------------------------------------------ #
    # tabs
    # ------------------------------------------------------------------ #
    with ui.tabs().classes(
        "w-full px-6 bg-[#131722] border-b border-[#2a3140] text-gray-300"
    ) as tabs:
        overview_tab = ui.tab("Overview", icon="dashboard")
        trades_tab = ui.tab("Trades", icon="receipt_long")
        analyst_tab = ui.tab("Analyst", icon="psychology")
        settings_tab = ui.tab("Settings", icon="tune")
        logs_tab = ui.tab("Logs", icon="terminal")

    with ui.tab_panels(tabs, value=overview_tab).classes(
        "w-full bg-transparent"
    ):
        # ============================ OVERVIEW ========================= #
        with ui.tab_panel(overview_tab).classes("p-6"):
            with ui.row().classes("w-full gap-4 items-stretch flex-nowrap"):
                with ui.column().classes("gap-4 grow-[3] basis-0 min-w-0"):
                    with card():
                        with ui.row().classes(
                            "w-full items-center justify-between"
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
                        card_title("📊 Active Positions", "live from Alpaca sync")
                        with ui.element("div").classes(
                            "pos-grid text-xs font-semibold uppercase "
                            f"{TEXT_MUTED} border-b border-[#222938] pb-1"
                        ):
                            for header in (
                                "Symbol", "Qty", "Entry", "Now",
                                "Value", "Unrealized PnL", "",
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
                        }
                        config_updated_label = ui.label(
                            "Last optimization: —"
                        ).classes(f"text-xs {TEXT_MUTED} mt-2")

                    with card():
                        card_title("🕑 Recent Activity", "full log in Logs tab")
                        activity_container = ui.column().classes(
                            "w-full gap-0 font-mono"
                        )

        # ============================= TRADES ========================== #
        with ui.tab_panel(trades_tab).classes("p-6"):
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
                        "📜 Trade History", f"latest {TRADES_LIMIT} trades"
                    )
                    trades_grid = ui.aggrid(
                        {
                            "defaultColDef": {"resizable": True, "sortable": True},
                            "columnDefs": [
                                {"headerName": "Closed", "field": "closed", "flex": 1.2},
                                {"headerName": "Symbol", "field": "symbol", "flex": 0.9},
                                {"headerName": "Qty", "field": "qty", "flex": 0.6},
                                {
                                    "headerName": "Entry",
                                    "field": "entry_price",
                                    "flex": 0.8,
                                    "valueFormatter": "'$' + value.toFixed(2)",
                                },
                                {
                                    "headerName": "Exit",
                                    "field": "exit_price",
                                    "flex": 0.8,
                                    "valueFormatter":
                                        "value == null ? '—' : '$' + value.toFixed(2)",
                                },
                                {
                                    "headerName": "PnL",
                                    "field": "realized_pnl",
                                    "flex": 0.9,
                                    "valueFormatter":
                                        "value == null ? '—' : (value >= 0 ? '+$' : "
                                        "'-$') + Math.abs(value).toFixed(2)",
                                    "cellClassRules": {
                                        "pnl-pos": "x > 0",
                                        "pnl-neg": "x < 0",
                                    },
                                },
                                {
                                    "headerName": "PnL %",
                                    "field": "pnl_pct",
                                    "flex": 0.8,
                                    "valueFormatter":
                                        "value == null ? '—' : (value >= 0 ? '+' : "
                                        "'') + value.toFixed(2) + '%'",
                                    "cellClassRules": {
                                        "pnl-pos": "x > 0",
                                        "pnl-neg": "x < 0",
                                    },
                                },
                                {"headerName": "Held", "field": "duration", "flex": 0.7},
                            ],
                            "rowData": [],
                            "domLayout": "autoHeight",
                            "pagination": True,
                            "paginationPageSize": 15,
                        },
                        theme="balham-dark",
                    ).classes("w-full")
                    trades_empty = ui.label("No trades recorded yet").classes(
                        f"text-sm {TEXT_MUTED}"
                    )

        # ============================ ANALYST ========================== #
        with ui.tab_panel(analyst_tab).classes("p-6"):
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

                # Connection & schedule config
                with card():
                    card_title("🔌 Connection & Schedule")
                    ui.label(
                        "Changes take effect immediately — no restart needed. "
                        "The base URL must point to an OpenAI-compatible API "
                        "(e.g. cloud Ollama endpoint)."
                    ).classes(f"text-xs {TEXT_MUTED}")
                    with ui.row().classes(
                        "w-full items-center justify-between gap-4 flex-nowrap"
                    ):
                        with ui.column().classes("gap-0 min-w-0"):
                            ui.label("Base URL").classes("text-sm text-white")
                            ui.label(
                                "OpenAI-compatible API endpoint"
                            ).classes(f"text-xs {TEXT_MUTED}")
                        analyst_base_url_input = ui.input(
                            value="", placeholder="https://..."
                        ).props("dense outlined").classes("w-80 shrink-0")
                    with ui.row().classes(
                        "w-full items-center justify-between gap-4 flex-nowrap"
                    ):
                        with ui.column().classes("gap-0 min-w-0"):
                            ui.label("Model").classes("text-sm text-white")
                            ui.label(
                                "Model name as known by the API"
                            ).classes(f"text-xs {TEXT_MUTED}")
                        analyst_model_input = ui.input(
                            value="deepseek-r1", placeholder="deepseek-r1"
                        ).props("dense outlined").classes("w-56 shrink-0")
                    with ui.row().classes(
                        "w-full items-center justify-between gap-4 flex-nowrap"
                    ):
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
                        ).props("dense outlined").classes("w-56 shrink-0")
                    with ui.row().classes(
                        "w-full items-center justify-between gap-4 flex-nowrap"
                    ):
                        with ui.column().classes("gap-0 min-w-0"):
                            ui.label("Risk Model").classes("text-sm text-white")
                            ui.label(
                                "Cheaper/faster model for pre-trade risk "
                                "checks — leave empty to use Model"
                            ).classes(f"text-xs {TEXT_MUTED}")
                        analyst_risk_model_input = ui.input(
                            value="", placeholder="(same as Model)"
                        ).props("dense outlined").classes("w-56 shrink-0")
                    with ui.row().classes(
                        "w-full items-center justify-between gap-4 flex-nowrap"
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
                            "w-64 shrink-0"
                        )
                    with ui.row().classes(
                        "w-full items-center justify-between gap-4 flex-nowrap"
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
                        ).props("dense outlined").classes("w-24 shrink-0")
                    with ui.row().classes(
                        "w-full items-center justify-between gap-4 flex-nowrap"
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
                        ).props("dense outlined").classes("w-24 shrink-0")

                    async def apply_analyst_config() -> None:
                        updates = {
                            "base_url": analyst_base_url_input.value or "",
                            "api_key": analyst_api_key_input.value or "",
                            "model": analyst_model_input.value or "deepseek-r1",
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
                            call_backend_json, "/analyst/config", updates, 10.0
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

                with ui.row().classes("w-full gap-4 items-start flex-nowrap"):
                    # Optimization review card
                    with ui.column().classes("gap-4 grow basis-0 min-w-0"):
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
                                        call_backend, "/analyst/review", 120.0
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

        # ============================ SETTINGS ========================= #
        with ui.tab_panel(settings_tab).classes("p-6"):
            with ui.row().classes("w-full gap-4 items-start flex-nowrap"):
                with ui.column().classes("gap-4 grow basis-0 min-w-0"):
                    with card():
                        card_title("🧠 Strategy Parameters")
                        ui.label(
                            "Written to the shared bot_config table and picked "
                            "up by the engine on its next cycle. The nightly "
                            "walk-forward optimizer (midnight Europe/Zurich) "
                            "re-tunes and overwrites these values."
                        ).classes(f"text-xs {TEXT_MUTED}")
                        param_inputs: Dict[str, ui.number] = {}
                        for key, meta in PARAM_META.items():
                            with ui.row().classes(
                                "w-full items-center justify-between gap-4 "
                                "flex-nowrap"
                            ):
                                with ui.column().classes("gap-0 min-w-0"):
                                    ui.label(meta["label"]).classes(
                                        "text-sm text-white"
                                    )
                                    ui.label(meta["hint"]).classes(
                                        f"text-xs {TEXT_MUTED}"
                                    )
                                param_inputs[key] = ui.number(
                                    value=(
                                        int(initial_config[key])
                                        if meta["int"]
                                        else round(float(initial_config[key]), 2)
                                    ),
                                    min=meta["min"],
                                    max=meta["max"],
                                    step=meta["step"],
                                ).props("dense outlined").classes("w-32 shrink-0")

                        async def apply_parameters() -> None:
                            updates: Dict[str, float] = {}
                            for key, meta in PARAM_META.items():
                                raw = param_inputs[key].value
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
                            if updates["atr_target_mult"] <= updates["atr_stop_mult"]:
                                ui.notify(
                                    "Take profit ≤ stop loss (× ATR): negative "
                                    "expectancy bracket — applied anyway, "
                                    "double-check this is intended",
                                    type="warning",
                                    timeout=8000,
                                )
                            await run.io_bound(db.set_config, updates)
                            await run.io_bound(
                                db.add_log,
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
                                param_inputs[key].value = (
                                    int(DEFAULT_CONFIG[key])
                                    if meta["int"]
                                    else DEFAULT_CONFIG[key]
                                )
                            await apply_parameters()

                        def reload_from_db() -> None:
                            live = db.get_config()
                            for key, meta in PARAM_META.items():
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

                    with card():
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
                                "flex-nowrap"
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
                                ).props("dense outlined").classes("w-32 shrink-0")

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
                            await run.io_bound(db.set_config, updates)
                            ui.notify(
                                "Watchlist settings applied",
                                type="positive",
                            )

                        def reload_watchlist_from_db() -> None:
                            live = db.get_config()
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
                            ).props("dense outlined").classes("w-24")
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
                            ).props("dense outlined").classes("w-24")

                with ui.column().classes("gap-4 grow basis-0 min-w-0"):
                    with card():
                        card_title("🔧 Engine & Optimizer")
                        ui.label(
                            f"Actions call the backend debug API at "
                            f"{BACKEND_API_URL}. Everything else on this "
                            "dashboard reads the shared database directly."
                        ).classes(f"text-xs {TEXT_MUTED}")

                        async def run_optimizer() -> None:
                            if not await confirm(
                                "Run optimizer now",
                                "Trigger the walk-forward grid search "
                                "immediately instead of waiting for midnight. "
                                "This can take several minutes; validated "
                                "parameters go live automatically.",
                                "OPTIMIZE",
                            ):
                                return
                            optimize_button.disable()
                            optimize_spinner.set_visibility(True)
                            ui.notify(
                                "Optimizer running — this can take a few "
                                "minutes…",
                                type="info",
                            )
                            result = await run.io_bound(
                                call_backend, "/optimize", 900.0
                            )
                            optimize_spinner.set_visibility(False)
                            optimize_button.enable()
                            if result["ok"]:
                                params = result["data"].get("parameters", {})
                                pretty = ", ".join(
                                    f"{k}={v:g}" for k, v in params.items()
                                ) or "unchanged"
                                ui.notify(
                                    f"Optimization complete: {pretty}",
                                    type="positive",
                                    timeout=10000,
                                )
                            else:
                                ui.notify(
                                    f"Optimization failed: {result['error']}",
                                    type="negative",
                                    timeout=10000,
                                )

                        with ui.row().classes("items-center gap-2 mt-1"):
                            optimize_button = ui.button(
                                "Run optimizer now", on_click=run_optimizer
                            ).props("no-caps unelevated color=deep-purple")
                            optimize_spinner = ui.spinner(size="sm")
                            optimize_spinner.set_visibility(False)
                        ui.label(
                            "Resume-from-KILLED lives in the header — it "
                            "appears when the bot is stopped."
                        ).classes(f"text-xs {TEXT_MUTED} mt-1")

                    with card():
                        card_title("🌍 Operational Environment", "read-only")
                        ui.label(
                            "Set via .env / docker-compose and applied when "
                            "the engine (re)starts — not tunable at runtime "
                            "by design."
                        ).classes(f"text-xs {TEXT_MUTED}")
                        environment_labels: Dict[str, ui.label] = {
                            key: kv_row(label)
                            for key, label in ENVIRONMENT_LABELS.items()
                        }

        # ============================== LOGS =========================== #
        with ui.tab_panel(logs_tab).classes("p-6"):
            with card():
                with ui.row().classes("w-full items-center justify-between gap-4"):
                    ui.label("🖥️ Live System Log").classes(
                        "text-lg font-semibold text-white"
                    )
                    with ui.row().classes("items-center gap-3"):
                        log_level_select = ui.select(
                            list(LEVEL_COLORS),
                            multiple=True,
                            value=list(LEVEL_COLORS),
                            label="levels",
                        ).props(
                            "dense outlined use-chips options-dense"
                        ).classes("min-w-[16rem]")
                        log_search = ui.input(placeholder="filter text…").props(
                            "dense outlined clearable"
                        ).classes("w-56")
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
    }

    def render_header(snapshot: Dict[str, Any]) -> None:
        status = snapshot["status"]
        balance_label.set_text(f"${status['equity']:,.2f}")

        daily_pnl = snapshot["daily_pnl"]
        pnl_label.set_text(money(daily_pnl, signed=True))
        pnl_label.classes(
            replace="text-xl font-semibold "
            + ("text-green-400" if daily_pnl >= 0 else "text-red-400")
        )
        baseline = status["daily_start_balance"]
        pnl_pct_label.set_text(
            f"{daily_pnl / baseline * 100.0:+.2f}% today" if baseline > 0 else ""
        )

        running = status["status"] == STATUS_RUNNING
        status_label.set_text(f"● {status['status']}")
        status_label.classes(
            replace="text-xl font-semibold "
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
            f"Cancel {symbol}'s bracket exit orders and market-close the "
            "position now.",
            f"CLOSE {symbol}",
        ):
            return
        render_state["closing"].add(symbol)
        ui.notify(f"Closing {symbol}…", type="warning")
        error = await run.io_bound(close_single_position, symbol)
        render_state["closing"].discard(symbol)
        if error is None:
            ui.notify(f"{symbol} close submitted", type="positive")
        else:
            ui.notify(f"{symbol} close failed: {error}", type="negative",
                      timeout=10000)

    def render_positions(snapshot: Dict[str, Any]) -> None:
        positions = snapshot["positions"]
        positions_empty.set_visibility(not positions)
        positions_container.clear()
        with positions_container:
            for pos in positions:
                pnl = pos["unrealized_pnl"]
                cost_basis = pos["avg_entry_price"] * pos["qty"]
                pnl_pct = (
                    pnl / cost_basis * 100.0
                    if pnl is not None and cost_basis
                    else None
                )
                with ui.element("div").classes(
                    "pos-grid py-1.5 border-b border-[#222938] text-sm"
                ):
                    ui.label(pos["symbol"]).classes("font-bold text-white")
                    ui.label(f"{pos['qty']:g}").classes("font-mono text-gray-300")
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
                f"Checked {fmt_clock(regime_info.get('checked_at'))} — regime "
                "gates new entries only, never forces exits"
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

    def render_trades(snapshot: Dict[str, Any]) -> None:
        trades = snapshot["trades"]
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

        rows = []
        for trade in trades:
            pnl = trade["realized_pnl"]
            cost_basis = trade["entry_price"] * trade["qty"]
            rows.append(
                {
                    "closed": fmt_short(trade["exit_time"]),
                    "symbol": trade["symbol"],
                    "qty": trade["qty"],
                    "entry_price": trade["entry_price"],
                    "exit_price": trade["exit_price"],
                    "realized_pnl": pnl,
                    "pnl_pct": (
                        pnl / cost_basis * 100.0
                        if pnl is not None and cost_basis
                        else None
                    ),
                    "duration": humanize_duration(
                        trade["entry_time"], trade["exit_time"]
                    ),
                }
            )
        trades_grid.options["rowData"] = rows
        trades_grid.update()
        trades_empty.set_visibility(not rows)

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

    async def on_analyst_toggle(value: bool) -> None:
        if getattr(analyst_toggle, "_suppress_change", False):
            return
        result = await run.io_bound(call_backend, "/analyst/toggle", 10.0)
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
                fetch_snapshot, int(log_rows_select.value or DEFAULT_LOG_ROWS), since
            )
        except Exception as exc:
            logger.error("Dashboard refresh failed: %s", exc)
            return

        render_header(snapshot)
        render_equity(snapshot)
        render_positions(snapshot)
        render_regime_and_engine(snapshot)
        render_parameters(snapshot)
        render_trades(snapshot)
        render_analyst(snapshot)
        render_environment(snapshot)
        render_logs(snapshot)

    timer = ui.timer(DEFAULT_REFRESH_SECONDS, refresh)
    refresh_select.on_value_change(
        lambda event: setattr(timer, "interval", float(event.value))
    )

    async def on_range_change(_) -> None:
        await refresh()

    equity_range.on_value_change(on_range_change)


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
