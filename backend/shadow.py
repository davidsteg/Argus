"""
Argus — shadow-strategy runner.

Candidate strategies (backend/strategies.py) trade a fully separate paper
book alongside the live engine: same bars, same ATR-bracket math
(indicators.py), same friction model as the optimizer backtest and the
vetoed-signal resolver, zero real capital and zero interaction with the
live order-submission path. A candidate earns a real track record — trade
count, win rate, expectancy — before anyone considers giving it money.

Deliberately simpler than the live exit stack, matching the precedent set
by bot.py's _resolve_one_veto: a shadow position closes ONLY via its stop
or target (pessimistic stop-before-target on the same bar), no RSI early
exit and no end-of-day flatten. That keeps "what would this strategy's
bracket have done" an honest, comparable number across candidates and
across the optimizer's own backtest — not a moving target every time a
strategy's exit logic changes. A closed (strategy, symbol) is benched for
the live engine's own cooldown_minutes before the same strategy may
re-enter it — otherwise a candidate whose technical conditions still
qualify could reopen the identical symbol in the identical cycle it just
stopped out of, the moment the position frees its slot.

Decoupled from the live pipeline on purpose: run_cycle() takes the
watchlist and does its own bar fetch, and is called from bot.py right
after the session-open gate — before the live book's slot count, regime
gate, or EOD-flatten state can short-circuit it. A full or paused live
book must never silence candidate measurement. Every entry point below is
called from bot.py wrapped in try/except; nothing here may ever raise
into the live trading cycle.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from indicators import COST_PER_TRADE_PCT, STOP_SLIPPAGE_PCT
from strategies import STRATEGIES, ShadowStrategy

logger = logging.getLogger("argus.shadow")


class ShadowRunner:
    def __init__(self, strategies: Optional[List[ShadowStrategy]] = None) -> None:
        self.strategies = strategies if strategies is not None else STRATEGIES
        # (strategy_name, symbol) -> monotonic deadline before re-entry is
        # allowed. In-memory only, mirroring bot.py's live _cooldowns (also
        # not persisted) — without this, a strategy whose technical
        # conditions still qualify right after a stop-out could re-enter
        # the SAME symbol in the SAME cycle the instant it stops being
        # "held" (closed positions free their slot immediately). Real
        # candidates re-evaluate technicals every cycle, unlike a fixed
        # test signal, so this is a genuine gap, not just a test artifact.
        self._cooldowns: Dict[Tuple[str, str], float] = {}

    @staticmethod
    def _friction(db: Any) -> Tuple[float, float]:
        """(stop_slippage_pct, cost_per_trade_pct), calibrated when the
        optimizer has run (same optimizer_friction blob the live veto
        resolver reads — see optimizer.calibrate_stop_slippage), falling
        back to the indicators.py env defaults otherwise (e.g. crypto,
        which runs no optimizer)."""
        try:
            friction = db.get_state("optimizer_friction") or {}
        except Exception:
            friction = {}
        stop_slip = float(friction.get("stop_slippage_pct") or STOP_SLIPPAGE_PCT)
        cost_pct = float(friction.get("cost_per_trade_pct") or COST_PER_TRADE_PCT)
        return stop_slip, cost_pct

    def _in_cooldown(self, strategy_name: str, symbol: str) -> bool:
        deadline = self._cooldowns.get((strategy_name, symbol))
        if deadline is None:
            return False
        if time.monotonic() >= deadline:
            del self._cooldowns[(strategy_name, symbol)]
            return False
        return True

    def _start_cooldown(
        self, strategy_name: str, symbol: str, minutes: float
    ) -> None:
        if minutes > 0:
            self._cooldowns[(strategy_name, symbol)] = (
                time.monotonic() + minutes * 60.0
            )

    def _check_exits(
        self,
        db: Any,
        strategy_name: str,
        held: Dict[str, Dict[str, Any]],
        frames: Dict[str, pd.DataFrame],
        stop_slip: float,
        cost_pct: float,
        cooldown_minutes: float,
    ) -> set:
        """Close any held shadow position whose stop or target was touched
        by the latest bar (stop checked first — pessimistic same-bar
        assumption, matching backtest()/veto resolution). Returns the set
        of symbols closed this cycle."""
        closed: set = set()
        for symbol, pos in held.items():
            bars = frames.get(symbol)
            if bars is None or bars.empty:
                continue
            bar = bars.iloc[-1]
            try:
                high, low = float(bar["high"]), float(bar["low"])
            except (KeyError, TypeError, ValueError):
                continue
            is_long = pos["side"] == "BUY"
            stop, target = float(pos["stop_loss"]), float(pos["take_profit"])
            outcome: Optional[str] = None
            raw_exit = 0.0
            if is_long:
                if low <= stop:
                    outcome, raw_exit = "stop", stop
                elif high >= target:
                    outcome, raw_exit = "target", target
            else:
                if high >= stop:
                    outcome, raw_exit = "stop", stop
                elif low <= target:
                    outcome, raw_exit = "target", target
            if outcome is None:
                continue

            if outcome == "stop":
                exit_price = (
                    raw_exit * (1.0 - stop_slip)
                    if is_long
                    else raw_exit * (1.0 + stop_slip)
                )
            else:
                exit_price = raw_exit
            qty = float(pos["qty"])
            entry_price = float(pos["entry_price"])
            gross = (
                (exit_price - entry_price) * qty
                if is_long
                else (entry_price - exit_price) * qty
            )
            realized_pnl = gross - cost_pct * entry_price * qty
            try:
                db.close_shadow_position(
                    strategy_name, symbol, exit_price, realized_pnl,
                    f"{outcome.capitalize()} hit @ ~{exit_price:.4f}",
                )
                db.add_log(
                    "INFO",
                    f"[shadow:{strategy_name}] {symbol} {outcome} @ "
                    f"~${exit_price:.4f} — {'+' if realized_pnl >= 0 else ''}"
                    f"${realized_pnl:.2f}",
                )
            except Exception as exc:
                logger.error(
                    "Shadow close failed for %s/%s: %s",
                    strategy_name, symbol, exc,
                )
                continue
            self._start_cooldown(strategy_name, symbol, cooldown_minutes)
            closed.add(symbol)
        return closed

    async def run_cycle(
        self,
        market: Any,
        config: Dict[str, float],
        db: Any,
        watchlist: List[str],
    ) -> None:
        if not watchlist or not self.strategies:
            return
        start = datetime.now(timezone.utc) - timedelta(
            minutes=config.get("bar_lookback_minutes", 180)
        )
        try:
            frames = await asyncio.to_thread(
                market.fetch_bars, list(watchlist), start
            )
        except Exception as exc:
            logger.error("Shadow bar fetch failed: %s", exc)
            return
        if not frames:
            return

        stop_slip, cost_pct = self._friction(db)

        open_by_strategy: Dict[str, Dict[str, Dict[str, Any]]] = {}
        try:
            for pos in db.get_shadow_positions():
                open_by_strategy.setdefault(pos["strategy"], {})[pos["symbol"]] = pos
        except Exception as exc:
            logger.error("Shadow position load failed: %s", exc)
            return

        cooldown_minutes = float(config.get("cooldown_minutes", 30.0))
        for strategy in self.strategies:
            held = open_by_strategy.get(strategy.name, {})
            closed = self._check_exits(
                db, strategy.name, held, frames, stop_slip, cost_pct,
                cooldown_minutes,
            )
            held_symbols = set(held) - closed
            room = strategy.max_positions - len(held_symbols)
            if room <= 0:
                continue

            for symbol, bars in frames.items():
                if room <= 0:
                    break
                if (
                    symbol in held_symbols
                    or bars is None
                    or bars.empty
                    or self._in_cooldown(strategy.name, symbol)
                ):
                    continue
                try:
                    signal = strategy.evaluate(symbol, bars, config)
                except Exception as exc:
                    logger.error(
                        "Shadow strategy %s raised on %s: %s",
                        strategy.name, symbol, exc,
                    )
                    continue
                if signal is None:
                    continue
                stop_distance = abs(signal.price - signal.stop_loss)
                try:
                    qty = market.size_qty(
                        symbol,
                        config.get("position_size_usd", 500.0),
                        signal.price,
                        stop_distance,
                        config.get("risk_per_trade_usd", 20.0),
                    )
                except Exception as exc:
                    logger.error(
                        "Shadow sizing failed for %s/%s: %s",
                        strategy.name, symbol, exc,
                    )
                    continue
                if qty <= 0:
                    continue
                try:
                    db.open_shadow_position(
                        strategy.name, symbol, signal.side, qty, signal.price,
                        signal.stop_loss, signal.take_profit, signal.rsi,
                        signal.atr, signal.rationale,
                    )
                    db.add_log(
                        "INFO",
                        f"[shadow:{strategy.name}] {signal.side} {symbol} "
                        f"x{qty:g} @ ~${signal.price:.4f} | "
                        f"{signal.rationale[:150]}",
                    )
                except Exception as exc:
                    logger.error(
                        "Shadow open failed for %s/%s: %s",
                        strategy.name, symbol, exc,
                    )
                    continue
                held_symbols.add(symbol)
                room -= 1
