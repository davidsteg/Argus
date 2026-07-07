"""
Argus — NiceGUI command center.

A fully custom, asynchronous dark-themed dashboard. It reads the shared
SQLite database directly (no HTTP hop to the backend) and refreshes every
two seconds via ui.timer without page reloads. The EMERGENCY HARD STOP
button talks straight to Alpaca: cancel all open orders, liquidate all
positions, persist KILLED — independent of whether the backend engine is
alive.

Design follows the original Argus dashboard: near-black canvas, elevated
slate cards, a status dot, and green/red PnL accents.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict

from dotenv import load_dotenv
from nicegui import run, ui

from shared.database import STATUS_KILLED, STATUS_RUNNING, get_db
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

REFRESH_SECONDS = 2.0
LOG_ROWS = 20

# Palette (Tailwind arbitrary values) — matches the dark look of the
# original dashboard: near-black app background, elevated slate cards.
BG_APP = "bg-[#0e1117]"
BG_CARD = "bg-[#161b26]"
BORDER_CARD = "border border-[#2a3140]"
TEXT_MUTED = "text-[#8b93a7]"

LEVEL_COLORS: Dict[str, str] = {
    "CRITICAL": "text-red-500",
    "ERROR": "text-red-400",
    "WARNING": "text-amber-400",
    "TRADE": "text-emerald-400",
    "OPTIMIZER": "text-violet-400",
    "INFO": "text-sky-300",
}

PARAM_LABELS: Dict[str, str] = {
    "rsi_period": "RSI Period",
    "rsi_buy_signal": "RSI Buy Signal",
    "news_cutoff": "News Cutoff",
    "atr_stop_mult": "Stop Loss (× ATR)",
    "atr_target_mult": "Take Profit (× ATR)",
}


def fetch_snapshot() -> Dict[str, Any]:
    """Blocking read of everything the dashboard shows (run via io_bound)."""
    db = get_db()
    status = db.get_status()
    return {
        "status": status,
        "daily_pnl": status["equity"] - status["daily_start_balance"],
        "positions": db.get_positions(),
        "config": db.get_config(),
        "config_updated_at": db.get_config_updated_at(),
        "logs": db.get_logs(LOG_ROWS),
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
        "</style>"
    )

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
        with ui.column().classes(
            "w-full gap-0 max-h-[32rem] overflow-y-auto"
        ):
            for release in RELEASES:
                with ui.column().classes(
                    "w-full gap-1 py-3 border-b border-[#222938]"
                ):
                    with ui.row().classes("items-baseline gap-3"):
                        ui.label(f"v{release['version']}").classes(
                            "font-mono font-bold text-emerald-400"
                        )
                        ui.label(str(release["date"])).classes(
                            f"text-xs {TEXT_MUTED}"
                        )
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
            ui.label("🛡️").classes("text-3xl")
            with ui.column().classes("gap-0"):
                with ui.row().classes("items-center gap-2"):
                    ui.label("ARGUS").classes(
                        "text-2xl font-bold tracking-widest text-white"
                    )
                    ui.button(
                        f"v{__version__}", on_click=release_dialog.open
                    ).props("flat dense no-caps").classes(
                        "text-xs font-mono text-emerald-400 bg-[#1d2432] "
                        "px-2 py-0.5 rounded border border-[#2a3140]"
                    ).tooltip("Release notes")
                ui.label("Short-Term Algorithmic Trading — Paper").classes(
                    f"text-xs {TEXT_MUTED}"
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
            with ui.column().classes("gap-0 items-end"):
                ui.label("STATUS").classes(f"text-xs {TEXT_MUTED}")
                status_label = ui.label("● …").classes(
                    "text-xl font-semibold text-green-400"
                )

            async def on_emergency_click() -> None:
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

            emergency_button = ui.button(
                "EMERGENCY HARD STOP", on_click=on_emergency_click
            ).classes(
                "bg-red-700 hover:bg-red-600 text-white font-bold "
                "px-5 py-3 rounded-lg shadow-lg shadow-red-900/40"
            ).props("color=red-10 push")

    # ------------------------------------------------------------------ #
    # main layout: left (positions + parameters) | right (log terminal)
    # ------------------------------------------------------------------ #
    with ui.row().classes("w-full gap-4 p-6 items-stretch flex-nowrap"):

        with ui.column().classes("gap-4 grow basis-0 min-w-0"):
            with ui.card().classes(
                f"w-full {BG_CARD} {BORDER_CARD} rounded-xl shadow-lg"
            ):
                ui.label("📊 Active Positions").classes(
                    "text-lg font-semibold text-white"
                )
                positions_grid = ui.aggrid(
                    {
                        "defaultColDef": {"resizable": True, "sortable": True},
                        "columnDefs": [
                            {"headerName": "Symbol", "field": "symbol", "flex": 1},
                            {"headerName": "Qty", "field": "qty", "flex": 1},
                            {
                                "headerName": "Avg Entry",
                                "field": "avg_entry_price",
                                "flex": 1,
                                "valueFormatter": "'$' + value.toFixed(2)",
                            },
                            {
                                "headerName": "Updated (UTC)",
                                "field": "updated_at",
                                "flex": 2,
                            },
                        ],
                        "rowData": [],
                        "domLayout": "autoHeight",
                    },
                    theme="balham-dark",
                ).classes("w-full")
                positions_empty = ui.label("No open positions").classes(
                    f"text-sm {TEXT_MUTED}"
                )

            with ui.card().classes(
                f"w-full {BG_CARD} {BORDER_CARD} rounded-xl shadow-lg"
            ):
                ui.label("🧠 Strategy Parameters (walk-forward optimized)").classes(
                    "text-lg font-semibold text-white"
                )
                param_labels: Dict[str, ui.label] = {}
                with ui.column().classes("w-full gap-1"):
                    for key, title in PARAM_LABELS.items():
                        with ui.row().classes(
                            "w-full justify-between py-1 "
                            "border-b border-[#222938]"
                        ):
                            ui.label(title).classes(f"text-sm {TEXT_MUTED}")
                            param_labels[key] = ui.label("—").classes(
                                "text-sm font-mono text-white"
                            )
                config_updated_label = ui.label("Last optimization: —").classes(
                    f"text-xs {TEXT_MUTED} mt-2"
                )

        with ui.column().classes("gap-4 grow basis-0 min-w-0"):
            with ui.card().classes(
                f"w-full h-full {BG_CARD} {BORDER_CARD} rounded-xl shadow-lg"
            ):
                with ui.row().classes("w-full items-center justify-between"):
                    ui.label("🖥️ Live System Log").classes(
                        "text-lg font-semibold text-white"
                    )
                    ui.label(f"last {LOG_ROWS} events · newest first").classes(
                        f"text-xs {TEXT_MUTED}"
                    )
                log_container = ui.column().classes(
                    "w-full gap-0 bg-[#0a0d13] rounded-lg p-3 "
                    "font-mono overflow-x-auto min-h-[28rem]"
                )

    # ------------------------------------------------------------------ #
    # reactive refresh loop
    # ------------------------------------------------------------------ #
    async def refresh() -> None:
        try:
            snapshot = await run.io_bound(fetch_snapshot)
        except Exception as exc:
            logger.error("Dashboard refresh failed: %s", exc)
            return

        status = snapshot["status"]
        balance_label.set_text(f"${status['equity']:,.2f}")

        daily_pnl = snapshot["daily_pnl"]
        pnl_label.set_text(f"${daily_pnl:+,.2f}")
        pnl_label.classes(
            replace=(
                "text-xl font-semibold "
                + ("text-green-400" if daily_pnl >= 0 else "text-red-400")
            )
        )

        running = status["status"] == STATUS_RUNNING
        status_label.set_text(f"● {status['status']}")
        status_label.classes(
            replace=(
                "text-xl font-semibold "
                + ("text-green-400" if running else "text-red-500")
            )
        )
        if status["status"] == STATUS_KILLED:
            emergency_button.disable()

        positions = snapshot["positions"]
        positions_grid.options["rowData"] = positions
        positions_grid.update()
        positions_empty.set_visibility(len(positions) == 0)

        config = snapshot["config"]
        for key, label in param_labels.items():
            value = config.get(key)
            if value is None:
                label.set_text("—")
            elif key == "rsi_period":
                label.set_text(f"{int(value)}")
            else:
                label.set_text(f"{value:.2f}")
        updated_at = snapshot["config_updated_at"] or "—"
        config_updated_label.set_text(f"Last optimization: {updated_at}")

        log_container.clear()
        with log_container:
            if not snapshot["logs"]:
                ui.label("No log entries yet").classes(f"text-sm {TEXT_MUTED}")
            for entry in snapshot["logs"]:
                level = entry["level"]
                color = LEVEL_COLORS.get(level, "text-gray-300")
                with ui.row().classes(
                    "w-full gap-2 py-0.5 items-baseline flex-nowrap"
                ):
                    ui.label(entry["ts"][:19].replace("T", " ")).classes(
                        "text-xs text-[#5a6274] shrink-0"
                    )
                    ui.label(f"[{level}]").classes(
                        f"text-xs font-bold shrink-0 {color}"
                    )
                    ui.label(entry["message"]).classes(
                        "text-xs text-gray-200 break-all"
                    )

    ui.timer(REFRESH_SECONDS, refresh)


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        host="0.0.0.0",
        port=8080,
        title="Argus Command Center",
        favicon="🛡️",
        dark=True,
        reload=False,
        show=False,
    )
