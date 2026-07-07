"""
Argus — debug & operations API.

A FastAPI application served by uvicorn inside the backend container, next
to the trading engine (see bot.py main()). It exposes deep introspection
into the live engine for debugging plus a small set of operational actions.
Interactive docs: http://localhost:8000/docs

Read endpoints
--------------
GET /health     liveness: process, database, Alpaca connectivity
GET /status     bot_status row + daily PnL + engine task state
GET /config     live strategy parameters and when they last changed
GET /debug      engine internals: env, last cycle trace, market clock
GET /regime     current market regime (SPY trend + realized volatility)
GET /signals    evaluate the strategy RIGHT NOW for every watchlist symbol
GET /positions  positions snapshot from the shared database
GET /trades     recent trade history
GET /logs       recent system log entries

Action endpoints
----------------
POST /optimize  run the walk-forward grid search immediately
POST /kill      emergency kill-sequence (cancel, liquidate, KILLED)
POST /reset     recover from KILLED: set RUNNING and restart the engine
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict

import numpy as np
from fastapi import FastAPI, HTTPException, Query

import regime as regime_module
import universe
from indicators import compute_atr, compute_rsi, compute_vwap
from sentiment import get_sentiment_provider
from shared.database import get_db
from shared.version import RELEASES, __version__

logger = logging.getLogger("argus.api")


def _mask(secret: str) -> str:
    if len(secret) < 8:
        return "***"
    return f"{secret[:4]}…{secret[-4:]}"


def create_app(controller: "EngineController") -> FastAPI:  # noqa: F821
    # Imported here (not at module top) to avoid a circular import:
    # bot.py imports create_app from this module.
    import bot as engine

    app = FastAPI(
        title="Argus Debug API",
        version="1.0.0",
        description="Introspection and operations for the Argus trading engine",
    )
    db = get_db()

    def bot_or_probe() -> "engine.ArgusBot":
        """Return the live engine, or a stateless probe instance for
        data-only endpoints when the engine task is not running."""
        if controller.bot is not None:
            return controller.bot
        return engine.ArgusBot()

    # ------------------------------------------------------------------ #
    # read endpoints
    # ------------------------------------------------------------------ #

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "api": "ok",
            "engine_running": controller.engine_running,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        try:
            db.get_status()
            result["database"] = "ok"
        except Exception as exc:
            result["database"] = f"error: {exc}"
        try:
            probe = bot_or_probe()
            clock = await asyncio.to_thread(probe.trading.get_clock)
            result["alpaca"] = "ok"
            result["market_open"] = clock.is_open
        except Exception as exc:
            result["alpaca"] = f"error: {exc}"
        return result

    @app.get("/status")
    async def status() -> Dict[str, Any]:
        row = db.get_status()
        return {
            **row,
            "daily_pnl": row["equity"] - row["daily_start_balance"],
            "engine_running": controller.engine_running,
            "engine_started_at": controller.started_at,
        }

    @app.get("/version")
    async def version() -> Dict[str, Any]:
        return {"version": __version__, "releases": RELEASES}

    @app.get("/config")
    async def config() -> Dict[str, Any]:
        return {
            "config": db.get_config(),
            "updated_at": db.get_config_updated_at(),
        }

    @app.get("/debug")
    async def debug() -> Dict[str, Any]:
        clock_info: Dict[str, Any]
        try:
            probe = bot_or_probe()
            clock = await asyncio.to_thread(probe.trading.get_clock)
            clock_info = {
                "is_open": clock.is_open,
                "next_open": str(clock.next_open),
                "next_close": str(clock.next_close),
            }
        except Exception as exc:
            clock_info = {"error": str(exc)}
        return {
            "engine_running": controller.engine_running,
            "engine_started_at": controller.started_at,
            "last_cycle": controller.bot.last_cycle if controller.bot else {},
            "open_entries": controller.bot._open_entries if controller.bot else {},
            "market_clock": clock_info,
            "cooldowns_active": (
                sorted(controller.bot._cooldowns) if controller.bot else []
            ),
            "environment": {
                "universe_mode": universe.describe_mode(),
                "watchlist": universe.get_watchlist(),
                "position_size_usd": engine.POSITION_SIZE_USD,
                "risk_per_trade_usd": engine.RISK_PER_TRADE_USD,
                "cooldown_minutes": engine.COOLDOWN_MINUTES,
                "max_positions": engine.MAX_POSITIONS,
                "daily_stop_loss": engine.DAILY_STOP_LOSS,
                "min_price_usd": engine.MIN_PRICE_USD,
                "poll_interval_seconds": engine.POLL_INTERVAL_SECONDS,
                "bar_lookback_minutes": engine.BAR_LOOKBACK_MINUTES,
                "regime_symbol": regime_module.REGIME_SYMBOL,
                "alpaca_api_key": _mask(engine.ALPACA_API_KEY),
                "paper_trading": True,
                "version": __version__,
            },
        }

    @app.get("/regime")
    async def market_regime() -> Dict[str, Any]:
        """Current market regime — RISK_OFF means no new entries."""
        info = await asyncio.to_thread(regime_module.get_regime)
        return {
            **info,
            "blocks_new_entries": regime_module.blocks_new_entries(info),
        }

    @app.get("/signals")
    async def signals() -> Dict[str, Any]:
        """Full dry-run of the decision logic for every watchlist symbol —
        the single most useful endpoint when asking 'why is it (not)
        trading right now?'. Never places orders."""
        probe = bot_or_probe()
        probe_config = db.get_config()
        provider = get_sentiment_provider()
        watchlist = probe.watchlist or universe.get_watchlist()
        frames = await probe.fetch_minute_bars()
        regime_info = await asyncio.to_thread(regime_module.get_regime)
        regime_blocks = regime_module.blocks_new_entries(regime_info)
        evaluation: Dict[str, Any] = {}
        for symbol in watchlist:
            bars = frames.get(symbol)
            if bars is None or bars.empty:
                evaluation[symbol] = {"decision": "SKIP", "reason": "no bar data"}
                continue
            period = max(int(probe_config["rsi_period"]), 2)
            if len(bars) < period * 2:
                evaluation[symbol] = {
                    "decision": "SKIP",
                    "reason": f"only {len(bars)} bars, need {period * 2}",
                }
                continue
            rsi = float(compute_rsi(bars["close"], period).iloc[-1])
            close = float(bars["close"].iloc[-1])
            vwap = float(compute_vwap(bars).iloc[-1])
            atr = float(compute_atr(bars).iloc[-1])
            entry: Dict[str, Any] = {
                "last_close": round(close, 4),
                "last_bar_utc": str(bars.index[-1]),
                "bars": len(bars),
                "rsi": None if np.isnan(rsi) else round(rsi, 2),
                "rsi_buy_signal": probe_config["rsi_buy_signal"],
                "vwap": round(vwap, 4),
                "atr": None if np.isnan(atr) else round(atr, 4),
                "news_cutoff": probe_config["news_cutoff"],
            }
            if np.isnan(rsi):
                entry.update({"decision": "SKIP", "reason": "RSI not defined yet"})
                evaluation[symbol] = entry
                continue
            if close < engine.MIN_PRICE_USD:
                entry.update(
                    {
                        "decision": "SKIP",
                        "reason": f"price ${close:.2f} below minimum "
                        f"${engine.MIN_PRICE_USD:.2f}",
                    }
                )
                evaluation[symbol] = entry
                continue
            if rsi >= probe_config["rsi_buy_signal"]:
                entry.update(
                    {
                        "decision": "HOLD",
                        "reason": f"RSI {rsi:.1f} above buy level "
                        f"{probe_config['rsi_buy_signal']:.0f}",
                    }
                )
                evaluation[symbol] = entry
                continue
            cooldown_left = probe.in_cooldown(symbol)
            if cooldown_left is not None:
                entry.update(
                    {
                        "decision": "BLOCKED",
                        "reason": f"RSI triggered but symbol is in post-loss "
                        f"cooldown for another {cooldown_left:.0f}m",
                    }
                )
                evaluation[symbol] = entry
                continue
            if close > vwap:
                entry.update(
                    {
                        "decision": "BLOCKED",
                        "reason": f"RSI triggered but price ${close:.2f} is "
                        f"above VWAP ${vwap:.2f} — not a confirmed dip",
                    }
                )
                evaluation[symbol] = entry
                continue
            # Sentiment is fetched only for RSI-triggered symbols — mirrors
            # the live engine and keeps LLM cost bounded.
            sentiment = await asyncio.to_thread(provider.score, symbol)
            entry.update(
                {
                    "sentiment": sentiment["score"],
                    "sentiment_source": sentiment["source"],
                    "sentiment_rationale": sentiment["rationale"],
                    "headlines": sentiment["headlines"],
                }
            )
            if sentiment["score"] <= probe_config["news_cutoff"]:
                entry.update(
                    {
                        "decision": "BLOCKED",
                        "reason": f"RSI triggered but sentiment "
                        f"{sentiment['score']:.2f} ({sentiment['source']}) "
                        f"below cutoff {probe_config['news_cutoff']:.2f}",
                    }
                )
            elif regime_blocks:
                entry.update(
                    {
                        "decision": "BLOCKED",
                        "reason": "all technical + sentiment gates passed but "
                        "market regime is RISK_OFF — no new entries",
                    }
                )
            else:
                entry.update(
                    {
                        "decision": "BUY",
                        "reason": "RSI trigger + VWAP dip + sentiment + "
                        "regime pass",
                    }
                )
            evaluation[symbol] = entry
        return {
            "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "config": probe_config,
            "regime": regime_info,
            "watchlist_size": len(watchlist),
            "signals": evaluation,
        }

    @app.get("/positions")
    async def positions() -> Dict[str, Any]:
        return {"positions": db.get_positions()}

    @app.get("/trades")
    async def trades(limit: int = Query(50, ge=1, le=1000)) -> Dict[str, Any]:
        return {"trades": db.get_trades(limit)}

    @app.get("/logs")
    async def logs(limit: int = Query(20, ge=1, le=500)) -> Dict[str, Any]:
        return {"logs": db.get_logs(limit)}

    # ------------------------------------------------------------------ #
    # action endpoints
    # ------------------------------------------------------------------ #

    @app.post("/optimize")
    async def optimize() -> Dict[str, Any]:
        from optimizer import run_optimization

        db.add_log("OPTIMIZER", "Manual optimization triggered via API")
        best = await asyncio.to_thread(run_optimization)
        if best is None:
            raise HTTPException(
                status_code=500,
                detail="Optimization produced no result — see /logs",
            )
        return {"optimized": True, "parameters": best}

    @app.post("/kill")
    async def kill() -> Dict[str, Any]:
        await controller.kill("Kill requested via debug API")
        return {"killed": True, "status": db.get_status()}

    @app.post("/reset")
    async def reset() -> Dict[str, Any]:
        ok, message = await controller.reset()
        if not ok:
            raise HTTPException(status_code=409, detail=message)
        return {"reset": True, "message": message, "status": db.get_status()}

    return app
