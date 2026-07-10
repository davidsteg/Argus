"""
Argus — nightly walk-forward parameter optimizer with out-of-sample
validation.

Runs as an asyncio task inside the backend container (started by bot.py)
and fires at midnight Europe/Zurich. It fetches the last 30 days of
1-minute historical bars from Alpaca and replays the exact live strategy
(RSI dip entry + ATR-multiple bracket exits + RSI-overbought early exit,
same indicator code via indicators.py) across a parameter grid.

Overfitting guard: the bars are split chronologically into a train window
(first 75 %) and a validation window (last 25 %). Combinations are ranked
by yield-to-drawdown on the train window, but a candidate is only allowed
to go live if it also made money on the validation window it has never
seen — parameters that merely memorized the past are rejected. The best
validated combination is written to the shared bot_config table; the
engine re-reads bot_config every cycle, so new parameters take effect at
the next trading session without a restart.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from dotenv import load_dotenv

import universe
from indicators import (
    bracket_distances,
    compute_atr,
    compute_rsi,
    compute_vwap,
    stop_is_floored,
)
from shared.database import get_db

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("argus.optimizer")

ZURICH = ZoneInfo("Europe/Zurich")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
LOOKBACK_DAYS = int(os.getenv("OPTIMIZER_LOOKBACK_DAYS", "30"))
# In whole-market mode the watchlist can be 100 symbols; the grid search
# replays 30 days of minute bars per symbol per combination, so it
# optimizes over the most active subset to stay fast.
OPTIMIZER_MAX_SYMBOLS = int(os.getenv("OPTIMIZER_MAX_SYMBOLS", "10"))
# Chronological share of each symbol's bars used for parameter selection;
# the remainder is the untouched validation window.
TRAIN_FRACTION = float(os.getenv("OPTIMIZER_TRAIN_FRACTION", "0.75"))
# Post-close re-entry bench, mirrored from the live engine (1 bar ≈ 1 min).
# Read from bot_config at runtime so it's tunable from the dashboard.

# Trading friction applied to every simulated trade. Without it the
# optimizer reliably promotes floor-tight brackets that backtest
# beautifully and bleed the spread live (see 2026-07-08: +103% "train
# return" selecting parameters that lost money the same afternoon).
# COST is a round-trip fraction of notional (half-spread in and out);
# stops additionally slip against the trade because they trigger market
# orders.
COST_PER_TRADE_PCT = float(os.getenv("OPTIMIZER_COST_PCT", "0.10")) / 100.0
STOP_SLIPPAGE_PCT = float(os.getenv("OPTIMIZER_STOP_SLIP_PCT", "0.05")) / 100.0


def _publish_status(db, status: Dict[str, Any]) -> None:
    """Publish the optimizer's live progress to runtime_state so the
    dashboard and GET /optimizer/status can report a running grid search
    instead of a frozen spinner. Called at each phase boundary."""
    try:
        db.set_state("optimizer_status", status)
    except Exception:
        pass


def _clear_status(db) -> None:
    """Mark the optimizer idle by clearing the live status blob."""
    try:
        db.set_state("optimizer_status", {"phase": "idle"})
    except Exception:
        pass


# Grid searched nightly. Kept deliberately compact: 4*4*3*4*3*3*3 = 5184
# combinations replayed over train+validation finish in a few minutes of
# CPU inside the container. Bracket distances are ATR multiples
# (see indicators.bracket_distances), so the same parameters adapt to each
# symbol's own volatility.
PARAMETER_GRID: Dict[str, List[float]] = {
    "rsi_period": [7.0, 10.0, 14.0, 21.0],
    "rsi_buy_signal": [20.0, 25.0, 30.0, 35.0],
    "atr_stop_mult": [1.0, 1.5, 2.0],
    "atr_target_mult": [1.5, 2.0, 3.0, 4.0],
    "rsi_exit_signal": [60.0, 70.0, 80.0],
    "rsi_short_signal": [60.0, 70.0, 80.0],
    "rsi_short_exit": [20.0, 30.0, 40.0],
    # Falling-knife cap: max fraction past VWAP an entry may sit before it is
    # rejected as a collapse rather than a dip. 999.0 = off, so the optimizer
    # can drop the gate entirely if it fails out-of-sample validation.
    "max_vwap_dislocation_pct": [0.08, 0.15, 999.0],
}

# Total number of parameter combinations the grid search evaluates, computed
# once so the live status can report a concrete progress fraction (the grid
# itself is a module-level constant so this never drifts from the actual loop).
GRID_TOTAL_COMBINATIONS = 1
for _values in PARAMETER_GRID.values():
    GRID_TOTAL_COMBINATIONS *= len(_values)


def fetch_history(symbols: List[str], days: int) -> Dict[str, pd.DataFrame]:
    """Fetch `days` of 1-minute bars per symbol from Alpaca market data."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        raise RuntimeError(
            "ALPACA_API_KEY / ALPACA_SECRET_KEY must be set in the environment"
        )
    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    # Free-tier market data excludes the most recent 15 minutes of SIP data;
    # end the window slightly in the past so the request never 403s.
    end = datetime.now(timezone.utc) - timedelta(minutes=16)
    start = end - timedelta(days=days)
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
    )
    bars = client.get_stock_bars(request)
    frames: Dict[str, pd.DataFrame] = {}
    df = bars.df
    if df is None or df.empty:
        return frames
    for symbol in symbols:
        try:
            symbol_df = df.xs(symbol, level="symbol")
        except KeyError:
            logger.warning("No historical bars returned for %s", symbol)
            continue
        if len(symbol_df) > 0:
            frames[symbol] = symbol_df.sort_index()
    return frames


def position_qty(
    price: float,
    stop_distance: float,
    position_size_usd: float,
    risk_per_trade_usd: float,
) -> int:
    """Whole-share quantity the live engine would trade for this entry.

    Mirrors bot.py's `place_bracket` sizing exactly: risk a roughly constant
    dollar amount (risk_per_trade_usd / stop_distance), capped by the notional
    position size (position_size_usd / price), floored to whole shares.
    Returns 0 when not even one share fits within both caps — the live engine
    skips such a signal, and so must the backtest.
    """
    if price <= 0:
        return 0
    qty = int(position_size_usd // price)
    if risk_per_trade_usd > 0 and stop_distance > 0:
        qty = min(qty, int(risk_per_trade_usd // stop_distance))
    return max(qty, 0)


def backtest(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    rsi: np.ndarray,
    atr: np.ndarray,
    vwap: np.ndarray,
    rsi_buy_signal: float,
    atr_stop_mult: float,
    atr_target_mult: float,
    rsi_exit_signal: float,
    rsi_short_signal: float = 999.0,
    rsi_short_exit: float = 0.0,
    max_vwap_dislocation_pct: float = 999.0,
    cooldown_bars: int = 0,
    position_size_usd: float = 500.0,
    risk_per_trade_usd: float = 20.0,
    account_equity: float = 100000.0,
) -> Tuple[float, float, int]:
    """Replay the live strategy over one symbol's bars.

    Entry: RSI crosses below the buy level (previous bar at/above, current
    below) — the crossing condition avoids re-entering on every bar of a
    sustained oversold stretch, matching the live engine which is blocked
    from re-entry while it already holds the symbol. Price must also sit
    at or below the session VWAP (the live dip-confirmation gate), and the
    symbol must be past its post-close cooldown. The live sentiment and
    regime gates cannot be replayed from bars alone and stay out.

    Short entry: RSI crosses above the short signal level (previous bar
    at/below, current above) with price above VWAP (overextended).

    Exit: intra-bar bracket simulation with the same ATR-multiple
    distances and floors the live engine uses (indicators.bracket_distances).
    The stop is checked before the target on each bar (pessimistic fill
    assumption). If neither bracket leg fills intra-bar, the RSI-overbought
    early exit is evaluated at the bar close (RSI >= rsi_exit_signal),
    mirroring the live engine, which only reads RSI on the completed bar.

    Sizing: each trade is sized in whole shares exactly as the live engine
    would (see position_qty) — a roughly constant dollar risk per trade,
    capped at position_size_usd notional. P&L accrues in dollars against a
    fixed account_equity base; position size does NOT scale with accumulated
    equity, because the live engine sizes off the constant risk_per_trade_usd,
    not off the current balance. This is the whole point of the fix: full-
    notional compounding (`equity *= exit/entry`) turned a ~0.2% average edge
    over ~1500 trades into a +2459% fantasy that no trading cost could offset;
    fixed-dollar sizing keeps the return additive and realistic. A signal that
    cannot fund even one share (position_qty == 0) is skipped, as it is live.

    Friction: every trade pays COST_PER_TRADE_PCT of the entry notional
    round-trip, and stop fills slip a further STOP_SLIPPAGE_PCT against the
    trade — stops trigger market orders and never fill at the exact stop
    price. Entries whose stop would be set by the percentage floor rather
    than ATR are skipped, matching the live engine's stop_is_floored gate.

    Returns (total_return, max_drawdown, n_trades) — return and drawdown as
    fractions of account_equity.
    """
    account_equity = max(account_equity, 1.0)
    cash_pnl = 0.0
    peak_pnl = 0.0
    max_drawdown = 0.0
    n_trades = 0

    in_position = False
    position_side = "BUY"
    stop_price = 0.0
    target_price = 0.0
    entry_price = 0.0
    qty = 0
    cooldown_until = -1

    def book(exit_price: float, bar: int) -> None:
        """Realize the open position at exit_price: dollar P&L on the sized
        share count, less round-trip cost, folded into the equity curve."""
        nonlocal cash_pnl, peak_pnl, max_drawdown, n_trades, in_position
        nonlocal cooldown_until
        if position_side == "BUY":
            gross = qty * (exit_price - entry_price)
        else:
            gross = qty * (entry_price - exit_price)
        cost = COST_PER_TRADE_PCT * qty * entry_price
        cash_pnl += gross - cost
        n_trades += 1
        peak_pnl = max(peak_pnl, cash_pnl)
        max_drawdown = max(max_drawdown, (peak_pnl - cash_pnl) / account_equity)
        in_position = False
        # Post-close bench, mirrored from the live engine (v2.18.3+): a symbol
        # is benched after EVERY close, not just losses, so a quiet asset whose
        # RSI oscillates around the thresholds cannot churn every cycle.
        if cooldown_bars > 0:
            cooldown_until = bar + cooldown_bars

    for i in range(1, len(closes)):
        if in_position:
            exit_price: Optional[float] = None
            if position_side == "BUY":
                if lows[i] <= stop_price:
                    exit_price = stop_price * (1.0 - STOP_SLIPPAGE_PCT)
                elif highs[i] >= target_price:
                    exit_price = target_price
                elif not np.isnan(rsi[i]) and rsi[i] >= rsi_exit_signal:
                    exit_price = closes[i]
            else:
                if highs[i] >= stop_price:
                    exit_price = stop_price * (1.0 + STOP_SLIPPAGE_PCT)
                elif lows[i] <= target_price:
                    exit_price = target_price
                elif not np.isnan(rsi[i]) and rsi[i] <= rsi_short_exit:
                    exit_price = closes[i]
            if exit_price is not None:
                book(exit_price, i)
            continue

        if np.isnan(rsi[i]) or np.isnan(rsi[i - 1]) or np.isnan(atr[i]):
            continue
        if i <= cooldown_until:
            continue

        # Too-quiet gate: skip symbols where the percentage floor, not
        # ATR, would set the stop — mirrors the live engine.
        if stop_is_floored(closes[i], atr[i], atr_stop_mult):
            continue

        # LONG entry: RSI crosses below buy_signal, price below VWAP but not so
        # far below it that the dip is a falling knife (live falling-knife gate).
        if (
            rsi[i - 1] >= rsi_buy_signal > rsi[i]
            and closes[i] <= vwap[i]
            and (vwap[i] - closes[i]) / vwap[i] <= max_vwap_dislocation_pct
        ):
            entry_price = closes[i]
            stop_distance, target_distance = bracket_distances(
                entry_price, atr[i], atr_stop_mult, atr_target_mult
            )
            qty = position_qty(
                entry_price, stop_distance, position_size_usd, risk_per_trade_usd
            )
            if qty < 1:
                continue
            in_position = True
            position_side = "BUY"
            stop_price = entry_price - stop_distance
            target_price = entry_price + target_distance
            continue

        # SHORT entry: RSI crosses above short_signal, price above VWAP but not
        # so far above it that the move is a parabolic squeeze (mirror gate).
        if (
            rsi_short_signal < 999.0
            and rsi[i - 1] <= rsi_short_signal < rsi[i]
            and closes[i] >= vwap[i]
            and (closes[i] - vwap[i]) / vwap[i] <= max_vwap_dislocation_pct
        ):
            entry_price = closes[i]
            stop_distance, target_distance = bracket_distances(
                entry_price, atr[i], atr_stop_mult, atr_target_mult
            )
            qty = position_qty(
                entry_price, stop_distance, position_size_usd, risk_per_trade_usd
            )
            if qty < 1:
                continue
            in_position = True
            position_side = "SELL"
            stop_price = entry_price + stop_distance
            target_price = entry_price - target_distance
            continue

    # Force-close a dangling position at the final bar so the last trade
    # is reflected in the score.
    if in_position:
        book(closes[-1], len(closes) - 1)

    return cash_pnl / account_equity, max_drawdown, n_trades


class _WindowData:
    """Pre-extracted arrays plus per-window indicator caches."""

    def __init__(self, frames: Dict[str, pd.DataFrame]) -> None:
        self.closes: Dict[str, np.ndarray] = {}
        self.highs: Dict[str, np.ndarray] = {}
        self.lows: Dict[str, np.ndarray] = {}
        self.atr: Dict[str, np.ndarray] = {}
        self.vwap: Dict[str, np.ndarray] = {}
        self._close_series: Dict[str, pd.Series] = {}
        self._rsi_cache: Dict[Tuple[str, int], np.ndarray] = {}
        for symbol, bars in frames.items():
            self.closes[symbol] = bars["close"].to_numpy()
            self.highs[symbol] = bars["high"].to_numpy()
            self.lows[symbol] = bars["low"].to_numpy()
            self.atr[symbol] = compute_atr(bars).to_numpy()
            self.vwap[symbol] = compute_vwap(bars).to_numpy()
            self._close_series[symbol] = bars["close"]

    def rsi(self, symbol: str, period: int) -> np.ndarray:
        key = (symbol, period)
        if key not in self._rsi_cache:
            self._rsi_cache[key] = compute_rsi(
                self._close_series[symbol], period
            ).to_numpy()
        return self._rsi_cache[key]

    def score(
        self,
        rsi_period: int,
        rsi_buy_signal: float,
        atr_stop_mult: float,
        atr_target_mult: float,
        rsi_exit_signal: float,
        rsi_short_signal: float = 999.0,
        rsi_short_exit: float = 0.0,
        max_vwap_dislocation_pct: float = 999.0,
        cooldown_bars: int = 30,
        position_size_usd: float = 500.0,
        risk_per_trade_usd: float = 20.0,
        account_equity: float = 100000.0,
    ) -> Tuple[float, float, float, int]:
        """Aggregate (score, return, worst drawdown, trades) across symbols.

        Each symbol contributes its dollar P&L against the shared
        account_equity, so summing the per-symbol returns is the portfolio
        return of running the fixed-dollar-sized strategy on all of them.
        """
        total_return = 0.0
        worst_drawdown = 0.0
        total_trades = 0
        for symbol in self.closes:
            ret, drawdown, trades = backtest(
                closes=self.closes[symbol],
                highs=self.highs[symbol],
                lows=self.lows[symbol],
                rsi=self.rsi(symbol, rsi_period),
                atr=self.atr[symbol],
                vwap=self.vwap[symbol],
                rsi_buy_signal=rsi_buy_signal,
                atr_stop_mult=atr_stop_mult,
                atr_target_mult=atr_target_mult,
                rsi_exit_signal=rsi_exit_signal,
                rsi_short_signal=rsi_short_signal,
                rsi_short_exit=rsi_short_exit,
                max_vwap_dislocation_pct=max_vwap_dislocation_pct,
                cooldown_bars=cooldown_bars,
                position_size_usd=position_size_usd,
                risk_per_trade_usd=risk_per_trade_usd,
                account_equity=account_equity,
            )
            total_return += ret
            worst_drawdown = max(worst_drawdown, drawdown)
            total_trades += trades

        # Yield-to-drawdown with a floor so an (unrealistic) zero-drawdown
        # run does not divide by zero; combinations that never trade score 0.
        if total_trades == 0:
            return 0.0, total_return, worst_drawdown, total_trades
        score = total_return / max(worst_drawdown, 0.005)
        return score, total_return, worst_drawdown, total_trades


def split_history(
    history: Dict[str, pd.DataFrame]
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, pd.DataFrame]]:
    """Chronological per-symbol split into train and validation windows."""
    train: Dict[str, pd.DataFrame] = {}
    validation: Dict[str, pd.DataFrame] = {}
    for symbol, bars in history.items():
        cut = int(len(bars) * TRAIN_FRACTION)
        if cut < 50 or len(bars) - cut < 50:
            # Too little data to validate meaningfully — train-only symbol.
            train[symbol] = bars
            continue
        train[symbol] = bars.iloc[:cut]
        validation[symbol] = bars.iloc[cut:]
    return train, validation


def run_optimization(trigger: str = "manual") -> Optional[Dict[str, float]]:
    """Grid search on train data, out-of-sample gate on validation data;
    writes the winner to bot_config and returns it. Records a structured
    row in optimizer_runs on every exit path."""
    db = get_db()
    started_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    params_before = db.get_config()
    symbols = universe.get_watchlist(limit=OPTIMIZER_MAX_SYMBOLS)

    run: Dict[str, Any] = {
        "started_at": started_at,
        "trigger": trigger,
        "status": "error",
        "symbols": ",".join(symbols),
        "n_symbols": len(symbols),
        "params_before": params_before,
    }

    try:
        _publish_status(db, {
            "phase": "fetching",
            "trigger": trigger,
            "started_at": started_at,
            "symbols": symbols,
            "n_symbols": len(symbols),
        })
        db.add_log(
            "OPTIMIZER",
            f"{'Nightly' if trigger == 'nightly' else 'Manual'} walk-forward "
            f"optimization started "
            f"({LOOKBACK_DAYS}d of 1-minute bars, "
            f"{TRAIN_FRACTION:.0%} train / {1 - TRAIN_FRACTION:.0%} validation, "
            f"{len(symbols)} symbols: {', '.join(symbols)})",
        )
        try:
            history = fetch_history(symbols, LOOKBACK_DAYS)
        except Exception as exc:
            logger.error("Historical data fetch failed: %s", exc)
            db.add_log("ERROR", f"Optimizer data fetch failed: {exc}")
            run["status"] = "error"
            run["detail"] = f"Data fetch failed: {exc}"
            return None

        if not history:
            db.add_log("ERROR", "Optimizer aborted: no historical data returned")
            run["status"] = "no_data"
            run["detail"] = "No historical data returned from Alpaca"
            return None

        total_bars = sum(len(bars) for bars in history.values())
        run["total_bars"] = total_bars
        logger.info(
            "Optimizing over %d bars across %d symbols", total_bars, len(history)
        )

        train_frames, validation_frames = split_history(history)
        train = _WindowData(train_frames)
        validation = _WindowData(validation_frames) if validation_frames else None

        # Read cooldown and position sizing from live config/status so the
        # backtest sizes and benches exactly like the engine. account_equity is
        # the base the dollar P&L is expressed against; a fresh/empty status
        # falls back to a nominal $100k so returns stay well-scaled.
        live_config = db.get_config()
        cooldown_bars = int(live_config.get("cooldown_minutes", 30.0))
        position_size_usd = float(live_config.get("position_size_usd", 500.0))
        risk_per_trade_usd = float(live_config.get("risk_per_trade_usd", 20.0))
        account_equity = float(db.get_status().get("equity", 0.0)) or 100000.0
        sizing = {
            "position_size_usd": position_size_usd,
            "risk_per_trade_usd": risk_per_trade_usd,
            "account_equity": account_equity,
        }

        # Score every combination on the train window, rank best-first.
        _publish_status(db, {
            "phase": "grid_search",
            "trigger": trigger,
            "started_at": started_at,
            "total_bars": total_bars,
            "n_symbols": len(history),
            "total_combinations": GRID_TOTAL_COMBINATIONS,
            "evaluated": 0,
            "candidates": 0,
        })
        ranked: List[Tuple[float, Dict[str, float], Tuple[float, float, int]]] = []
        evaluated = 0
        for rsi_period, rsi_buy, stop_mult, target_mult, exit_signal, short_signal, short_exit, max_disloc in itertools.product(
            PARAMETER_GRID["rsi_period"],
            PARAMETER_GRID["rsi_buy_signal"],
            PARAMETER_GRID["atr_stop_mult"],
            PARAMETER_GRID["atr_target_mult"],
            PARAMETER_GRID["rsi_exit_signal"],
            PARAMETER_GRID["rsi_short_signal"],
            PARAMETER_GRID["rsi_short_exit"],
            PARAMETER_GRID["max_vwap_dislocation_pct"],
        ):
            score, total_return, drawdown, trades = train.score(
                rsi_period=int(rsi_period),
                rsi_buy_signal=rsi_buy,
                atr_stop_mult=stop_mult,
                atr_target_mult=target_mult,
                rsi_exit_signal=exit_signal,
                rsi_short_signal=short_signal,
                rsi_short_exit=short_exit,
                max_vwap_dislocation_pct=max_disloc,
                cooldown_bars=cooldown_bars,
                **sizing,
            )
            evaluated += 1
            if evaluated % 500 == 0:
                _publish_status(db, {
                    "phase": "grid_search",
                    "trigger": trigger,
                    "started_at": started_at,
                    "total_bars": total_bars,
                    "n_symbols": len(history),
                    "total_combinations": GRID_TOTAL_COMBINATIONS,
                    "evaluated": evaluated,
                    "candidates": len(ranked),
                })
            if trades == 0:
                continue
            params = {
                "rsi_period": float(int(rsi_period)),
                "rsi_buy_signal": rsi_buy,
                "atr_stop_mult": stop_mult,
                "atr_target_mult": target_mult,
                "rsi_exit_signal": exit_signal,
                "rsi_short_signal": short_signal,
                "rsi_short_exit": short_exit,
                "max_vwap_dislocation_pct": max_disloc,
            }
            ranked.append((score, params, (total_return, drawdown, trades)))
        ranked.sort(key=lambda item: item[0], reverse=True)
        run["candidates"] = len(ranked)

        if not ranked:
            db.add_log(
                "OPTIMIZER",
                "Grid search produced no trading combination — keeping current "
                "parameters unchanged",
            )
            run["status"] = "no_combination"
            run["detail"] = "No combination produced any trades on train data"
            return None

        # Walk down the train ranking and take the first combination that also
        # made money on the unseen validation window.
        _publish_status(db, {
            "phase": "validation",
            "trigger": trigger,
            "started_at": started_at,
            "total_bars": total_bars,
            "n_symbols": len(history),
            "total_combinations": GRID_TOTAL_COMBINATIONS,
            "evaluated": GRID_TOTAL_COMBINATIONS,
            "candidates": len(ranked),
            "validated": 0,
        })
        best_params: Optional[Dict[str, float]] = None
        best_train_score = 0.0
        train_stats: Tuple[float, float, int] = (0.0, 0.0, 0)
        val_stats: Optional[Tuple[float, float, int]] = None
        validated = 0
        for score, params, stats in ranked:
            if validation is None:
                best_params, best_train_score, train_stats = params, score, stats
                break
            _, val_return, val_drawdown, val_trades = validation.score(
                rsi_period=int(params["rsi_period"]),
                rsi_buy_signal=params["rsi_buy_signal"],
                atr_stop_mult=params["atr_stop_mult"],
                atr_target_mult=params["atr_target_mult"],
                rsi_exit_signal=params["rsi_exit_signal"],
                rsi_short_signal=params.get("rsi_short_signal", 999.0),
                rsi_short_exit=params.get("rsi_short_exit", 0.0),
                max_vwap_dislocation_pct=params.get("max_vwap_dislocation_pct", 999.0),
                cooldown_bars=cooldown_bars,
                **sizing,
            )
            validated += 1
            if validated % 50 == 0:
                _publish_status(db, {
                    "phase": "validation",
                    "trigger": trigger,
                    "started_at": started_at,
                    "total_bars": total_bars,
                    "n_symbols": len(history),
                    "total_combinations": GRID_TOTAL_COMBINATIONS,
                    "evaluated": GRID_TOTAL_COMBINATIONS,
                    "candidates": len(ranked),
                    "validated": validated,
                })
            if val_return > 0.0:
                best_params, best_train_score, train_stats = params, score, stats
                val_stats = (val_return, val_drawdown, val_trades)
                break

        if best_params is None:
            db.add_log(
                "OPTIMIZER",
                f"No combination survived out-of-sample validation "
                f"({len(ranked)} candidates were profitable in-sample only) — "
                f"keeping current parameters unchanged",
            )
            run["status"] = "rejected_validation"
            run["detail"] = (
                f"All {len(ranked)} candidates failed out-of-sample validation"
            )
            return None

        # news_cutoff, analyst_enabled and short_enabled are not part of the
        # technical grid; carry the live values forward so a config read after
        # this write stays complete.
        current = db.get_config()
        best_params["news_cutoff"] = current["news_cutoff"]
        best_params["analyst_enabled"] = current.get("analyst_enabled", 0.0)
        best_params["short_enabled"] = current.get("short_enabled", 0.0)

        # Post-optimization LLM review (if analyst is enabled): a binary
        # sanity check that can only accept the validated winner or reject
        # it and keep the current parameters. It deliberately cannot pick a
        # different rank — an earlier "override" option let the LLM select
        # from the train-window ranking (mostly combinations that failed or
        # never saw the out-of-sample gate), i.e. exactly the in-sample
        # cherry-picking the walk-forward validation exists to prevent.
        analyst_decision = None
        try:
            from analyst import get_analyst
            import regime as regime_module

            _publish_status(db, {
                "phase": "analyst",
                "trigger": trigger,
                "started_at": started_at,
                "total_bars": total_bars,
                "n_symbols": len(history),
                "total_combinations": GRID_TOTAL_COMBINATIONS,
                "evaluated": GRID_TOTAL_COMBINATIONS,
                "candidates": len(ranked),
                "validated": validated,
            })
            analyst = get_analyst()
            regime_info = regime_module.get_regime()
            analyst_result = analyst.review_optimization(
                ranked, best_params, regime_info, db, validation=val_stats
            )
            if analyst_result is None:
                db.add_log(
                    "ANALYST",
                    "LLM rejected optimizer winner — keeping current parameters",
                )
                run["status"] = "rejected_analyst"
                run["detail"] = "LLM analyst rejected the optimizer winner"
                run["analyst_decision"] = "reject"
                return None
            analyst_decision = "accept"
        except Exception as exc:
            logger.error("Post-optimization analyst review failed: %s", exc)

        _publish_status(db, {
            "phase": "writing",
            "trigger": trigger,
            "started_at": started_at,
            "total_bars": total_bars,
            "n_symbols": len(history),
            "total_combinations": GRID_TOTAL_COMBINATIONS,
            "evaluated": GRID_TOTAL_COMBINATIONS,
            "candidates": len(ranked),
            "validated": validated,
        })
        db.set_config(best_params)

        total_return, drawdown, trades = train_stats
        validation_note = "no validation window (too little data)"
        if val_stats is not None:
            validation_note = (
                f"validation: {val_stats[0] * 100:+.2f}% return, "
                f"{val_stats[1] * 100:.2f}% max drawdown, {val_stats[2]} trades"
            )
        db.add_log(
            "OPTIMIZER",
            f"New parameters live: RSI({int(best_params['rsi_period'])}) "
            f"buy<{best_params['rsi_buy_signal']:.0f}, "
            f"exit>{best_params['rsi_exit_signal']:.0f}, "
            f"short>{best_params.get('rsi_short_signal', 70):.0f}, "
            f"cover<{best_params.get('rsi_short_exit', 30):.0f}, "
            f"stop {best_params['atr_stop_mult']:.1f}×ATR, "
            f"target {best_params['atr_target_mult']:.1f}×ATR | "
            f"train: {total_return * 100:+.2f}% return, "
            f"{drawdown * 100:.2f}% max drawdown, {trades} trades, "
            f"score {best_train_score:.2f} | {validation_note}",
        )
        logger.info(
            "Optimization complete: %s (train score %.2f)",
            best_params,
            best_train_score,
        )

        # Determine if parameters actually changed.
        grid_keys = set(PARAMETER_GRID.keys())
        changed_keys = [
            k for k in grid_keys
            if params_before.get(k) != best_params.get(k)
        ]
        run["status"] = "no_change" if not changed_keys else "applied"
        run["detail"] = (
            "Parameters unchanged (winner matches current)"
            if not changed_keys
            else None
        )
        run["params_after"] = best_params
        run["changed_keys"] = changed_keys
        run["train_return"] = total_return
        run["train_drawdown"] = drawdown
        run["train_score"] = best_train_score
        run["train_trades"] = trades
        if val_stats is not None:
            run["val_return"] = val_stats[0]
            run["val_drawdown"] = val_stats[1]
            run["val_trades"] = val_stats[2]
        run["analyst_decision"] = analyst_decision

        return best_params

    finally:
        run["finished_at"] = datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        )
        db.record_optimizer_run(run)
        _clear_status(db)


def seconds_until_midnight_zurich() -> float:
    now = datetime.now(ZURICH)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max((tomorrow - now).total_seconds(), 1.0)


async def schedule_daily_optimization() -> None:
    """Sleep until each Swiss midnight, then run the grid search off-loop."""
    while True:
        wait_seconds = seconds_until_midnight_zurich()
        logger.info(
            "Optimizer scheduled in %.0f minutes (midnight Europe/Zurich)",
            wait_seconds / 60.0,
        )
        try:
            await asyncio.sleep(wait_seconds)
        except asyncio.CancelledError:
            logger.info("Optimizer schedule cancelled")
            raise
        try:
            await asyncio.to_thread(run_optimization, "nightly")
        except Exception as exc:
            logger.exception("Nightly optimization failed: %s", exc)
            get_db().add_log("ERROR", f"Nightly optimization failed: {exc}")
        # Guard against clock skew re-triggering within the same minute.
        await asyncio.sleep(61)


if __name__ == "__main__":
    # Manual invocation for testing: python optimizer.py
    run_optimization()
