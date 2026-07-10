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
GET /optimizer/status  live progress of a running grid search

Action endpoints
----------------
POST /optimize  start the walk-forward grid search in the background
POST /kill      emergency kill-sequence (cancel, liquidate, KILLED)
POST /reset     recover from KILLED: set RUNNING and restart the engine
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict

import numpy as np
from fastapi import Body, FastAPI, HTTPException, Query

import regime as regime_module
import universe
from indicators import compute_atr, compute_rsi, compute_vwap, stop_is_floored
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
                "regime_symbol": regime_module.REGIME_SYMBOL,
                "alpaca_api_key": _mask(engine.ALPACA_API_KEY),
                "paper_trading": True,
                "version": __version__,
            },
        }

    @app.get("/regime")
    async def market_regime() -> Dict[str, Any]:
        """Current market regime — TREND_DOWN means no new BUY entries. Uses the
        engine's own proxy (SPY for equities, BTC/USD for crypto)."""
        probe = bot_or_probe()
        info = await asyncio.to_thread(probe.market.regime)
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
        regime_info = await asyncio.to_thread(probe.market.regime)
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
                "rsi_exit_signal": probe_config["rsi_exit_signal"],
                "vwap": round(vwap, 4),
                "atr": None if np.isnan(atr) else round(atr, 4),
                "news_cutoff": probe_config["news_cutoff"],
            }
            if np.isnan(rsi):
                entry.update({"decision": "SKIP", "reason": "RSI not defined yet"})
                evaluation[symbol] = entry
                continue
            if close < probe_config.get("min_price_usd", 5.0):
                entry.update(
                    {
                        "decision": "SKIP",
                        "reason": f"price ${close:.2f} below minimum "
                        f"${probe_config.get('min_price_usd', 5.0):.2f}",
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
            if not np.isnan(atr) and stop_is_floored(
                close, atr, probe_config["atr_stop_mult"]
            ):
                entry.update(
                    {
                        "decision": "SKIP",
                        "reason": "ATR-scaled stop sits inside the % floor — "
                        "too quiet for a meaningful bracket",
                    }
                )
                evaluation[symbol] = entry
                continue
            dislocation = (vwap - close) / vwap if vwap > 0 else 0.0
            max_disloc = probe_config.get("max_vwap_dislocation_pct", 0.15)
            if dislocation > max_disloc:
                entry.update(
                    {
                        "decision": "BLOCKED",
                        "reason": f"price {dislocation * 100:.0f}% below VWAP "
                        f"(> {max_disloc * 100:.0f}% cap) — falling knife",
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
                        "market regime is TREND_DOWN — no new BUY entries",
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

    @app.get("/screener")
    async def screener() -> Dict[str, Any]:
        """Latest opportunity screener results — RSI-oversold + VWAP-dip
        candidates found by the periodic background scan."""
        candidates = db.get_state("screener_candidates", [])
        config = db.get_config()
        return {
            "enabled": bool(config.get("screener_enabled", 0.0)),
            "candidates": candidates,
            "pool_size": int(config.get("screener_pool_size", 200.0)),
            "max_candidates": int(config.get("screener_max_candidates", 5.0)),
        }

    @app.get("/optimizer/status")
    async def optimizer_status() -> Dict[str, Any]:
        """Live progress of the optimizer grid search — the dashboard polls
        this to show a running progress bar instead of a frozen spinner. The
        optimizer publishes phase/evaluated/candidates to runtime_state at
        each boundary; `phase: 'idle'` means no run is active."""
        status = db.get_state("optimizer_status", {"phase": "idle"})
        return {"status": status}

    # ------------------------------------------------------------------ #
    # action endpoints
    # ------------------------------------------------------------------ #

    @app.post("/optimize")
    async def optimize() -> Dict[str, Any]:
        from optimizer import run_optimization

        # The optimizer's backtester replays equity bars and writes equity-tuned
        # parameters; running it against the crypto engine's config is invalid.
        if os.getenv("MARKET", "equity").strip().lower() == "crypto":
            raise HTTPException(
                status_code=400,
                detail="Optimizer is equity-only; the crypto engine runs on "
                "static parameters.",
            )
        # Prevent overlapping runs — the grid search is CPU-heavy and a second
        # concurrent run would corrupt the live status blob and double the load.
        current = db.get_state("optimizer_status", {"phase": "idle"})
        if current.get("phase") not in ("idle", None):
            raise HTTPException(
                status_code=409,
                detail="Optimizer already running — watch /optimizer/status",
            )
        db.add_log("OPTIMIZER", "Manual optimization triggered via API")

        async def _run() -> None:
            try:
                await asyncio.to_thread(run_optimization, "manual")
            except Exception as exc:
                logger.exception("Background optimization failed: %s", exc)

        asyncio.create_task(_run())
        return {"optimized": False, "status": "started", "message": "Optimizer started — poll /optimizer/status"}

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

    # ------------------------------------------------------------------ #
    # analyst endpoints
    # ------------------------------------------------------------------ #

    @app.get("/analyst/optimization")
    async def analyst_optimization() -> Dict[str, Any]:
        return {
            "report": db.get_state("analyst_optimization"),
            "enabled": bool(db.get_config().get("analyst_enabled", 0.0)),
        }

    @app.get("/analyst/trades")
    async def analyst_trades() -> Dict[str, Any]:
        return {
            "report": db.get_state("analyst_trades"),
            "enabled": bool(db.get_config().get("analyst_enabled", 0.0)),
        }

    @app.post("/analyst/review")
    async def analyst_review() -> Dict[str, Any]:
        from analyst import get_analyst

        analyst = get_analyst()
        if not analyst.available:
            raise HTTPException(
                status_code=503,
                detail="Analyst unavailable — set the base URL in the Analyst tab",
            )
        if not analyst.enabled(db):
            raise HTTPException(
                status_code=400,
                detail="Analyst is disabled — enable it from the dashboard first",
            )
        db.add_log("INFO", "Manual trade review triggered via API")
        trades = db.get_trades(200)
        stats = db.get_trade_stats()
        config = db.get_config()
        regime_info = await asyncio.to_thread(bot_or_probe().market.regime)
        report = await asyncio.to_thread(
            analyst.review_trades, trades, stats, config, regime_info, db
        )
        if report is None:
            raise HTTPException(
                status_code=500,
                detail="Trade review failed — check backend logs",
            )
        return {"reviewed": True, "report": report}

    @app.post("/analyst/toggle")
    async def analyst_toggle() -> Dict[str, Any]:
        config = db.get_config()
        current = bool(config.get("analyst_enabled", 0.0))
        new_value = 0.0 if current else 1.0
        db.set_config({"analyst_enabled": new_value})
        state = "enabled" if new_value else "disabled"
        db.add_log("INFO", f"Analyst {state} from dashboard")
        return {"analyst_enabled": bool(new_value)}

    @app.get("/analyst/config")
    async def analyst_get_config() -> Dict[str, Any]:
        from analyst import get_analyst

        return {
            "config": get_analyst().get_config(),
            "available": get_analyst().available,
            "enabled": bool(db.get_config().get("analyst_enabled", 0.0)),
        }

    @app.post("/analyst/config")
    async def analyst_set_config(
        updates: Dict[str, Any] = Body(...)
    ) -> Dict[str, Any]:
        from analyst import get_analyst

        allowed = {"base_url", "api_key", "model", "sentiment_model",
                     "watchlist_model", "risk_model",
                     "trade_review_interval_hours", "trade_lookback"}
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            raise HTTPException(
                status_code=400,
                detail="No valid config keys provided",
            )
        analyst = get_analyst()
        new_config = analyst.configure(filtered, db)
        # The sentiment provider caches its own LLM client — refresh it so a
        # base-URL / model change from the dashboard takes effect immediately.
        try:
            get_sentiment_provider().reload_config()
        except Exception as exc:
            logger.warning("Sentiment client reload failed: %s", exc)
        db.add_log(
            "INFO",
            "Analyst config updated: "
            + ", ".join(f"{k}={v}" for k, v in filtered.items()),
        )
        return {
            "config": new_config,
            "available": analyst.available,
        }

    @app.get("/analyst/activity")
    async def analyst_activity(limit: int = 100) -> Dict[str, Any]:
        """Rolling log of every LLM call plus per-agent 24h aggregates —
        the fastest way to answer 'is the analyst actually working?'."""
        import llm_log

        return {
            "stats": llm_log.get_agent_stats(db),
            "calls": llm_log.get_call_log(db, limit=max(1, min(limit, 400))),
        }

    @app.get("/analyst/reviews")
    async def analyst_reviews() -> Dict[str, Any]:
        """History of past trade/optimization/watchlist reviews, newest first."""
        from analyst import StrategyAnalyst

        history = db.get_state(StrategyAnalyst.REVIEW_HISTORY_KEY) or []
        return {"reviews": list(reversed(history))}

    @app.get("/analyst/memory")
    async def analyst_memory() -> Dict[str, Any]:
        from analyst import get_analyst
        memory = db.get_state("decision_memory") or {}
        return {
            "decisions": memory.get("decisions", [])[-20:],
            "lessons": memory.get("lessons", []),
        }

    @app.post("/analyst/extract-lessons")
    async def analyst_extract_lessons() -> Dict[str, Any]:
        from analyst import get_analyst
        analyst = get_analyst()
        await asyncio.to_thread(analyst.extract_lessons, db)
        return {"extracted": True}

    return app
