"""
Argus — nightly walk-forward parameter optimizer with out-of-sample
validation.

Runs as an asyncio task inside the backend container (started by bot.py)
and fires at midnight Europe/Zurich. It fetches OPTIMIZER_LOOKBACK_DAYS of
1-minute historical bars from Alpaca and replays the exact live strategy
(RSI dip entry + ATR-multiple bracket exits + RSI-overbought early exit,
same indicator code via indicators.py) across a parameter grid.

Overfitting guard: the bars are split chronologically into a train window
(first 75 %) and OPTIMIZER_VALIDATION_FOLDS sequential validation folds
covering the remainder. Combinations are ranked by yield-to-drawdown on
the train window, but a candidate is only allowed to go live if it made
money in a MAJORITY of the unseen validation folds AND in aggregate — one
lucky holdout window is no longer enough (v2.27.0; before that a single
25 % window was the whole gate). The best validated combination is
written to the shared bot_config table; the engine re-reads bot_config
every cycle, so new parameters take effect at the next trading session
without a restart.

Friction honesty: stop slippage is calibrated from the ledger's own
realized stop fills (calibrate_stop_slippage) instead of trusting the
OPTIMIZER_STOP_SLIP_PCT guess — the Jul 8–10 sessions proved the guess
could be 10–200× under reality, which let the grid promote bracket-tight
parameters that only won on paper.
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
    COST_PER_TRADE_PCT,
    STOP_SLIPPAGE_PCT,
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
LOOKBACK_DAYS = int(os.getenv("OPTIMIZER_LOOKBACK_DAYS", "60"))
# In whole-market mode the watchlist can be 100 symbols; the grid search
# replays LOOKBACK_DAYS of minute bars per symbol per combination, so it
# optimizes over the most active subset. Runtime budget: the measured
# nightly run was ~108 min at 30 days × 10 symbols and scales linearly in
# (days × symbols), so 60 × 15 lands near 5.5 h — inside the 22:00 UTC →
# 08:00 UTC equity-pre-open window, with margin. Bump these only with that
# arithmetic in hand.
OPTIMIZER_MAX_SYMBOLS = int(os.getenv("OPTIMIZER_MAX_SYMBOLS", "15"))
# Chronological share of each symbol's bars used for parameter selection;
# the remainder is cut into the sequential validation folds.
TRAIN_FRACTION = float(os.getenv("OPTIMIZER_TRAIN_FRACTION", "0.75"))
# Number of sequential out-of-sample folds the holdout is cut into. A
# winner must be profitable in a majority of folds and in aggregate.
VALIDATION_FOLDS = max(1, int(os.getenv("OPTIMIZER_VALIDATION_FOLDS", "3")))
# Guardrails for fill-calibrated stop slippage: never calibrate below the
# configured default (optimism is what caused the paper-only winners) and
# never above 2% (one catastrophic fill must not poison every backtest).
CALIBRATION_MIN_SAMPLES = int(os.getenv("OPTIMIZER_CALIBRATION_MIN_SAMPLES", "8"))
CALIBRATION_MAX_SLIP = 0.02
# Post-close re-entry bench, mirrored from the live engine (1 bar ≈ 1 min).
# Read from bot_config at runtime so it's tunable from the dashboard.

# Trading friction (COST_PER_TRADE_PCT / STOP_SLIPPAGE_PCT) is imported from
# indicators.py — shared with the shadow-veto resolver so "what a vetoed
# trade would have made" uses the same fill model as the backtest. Without
# friction the optimizer reliably promotes floor-tight brackets that
# backtest beautifully and bleed the spread live (see 2026-07-08: +103%
# "train return" selecting parameters that lost money the same afternoon).


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


# Grid searched nightly: 4*4*3*4*3*3*3*3 = 15552 combinations in full —
# the long-standing "5184" comment under-counted by the dislocation
# dimension. In practice the run searches effective_grid(), which pins the
# two short dimensions to "disabled" while short_enabled is off live
# (15552 → 1728 combos, 9×), both for speed and for fidelity: shorts are
# simulated whenever rsi_short_signal < 999, so searching them while the
# live engine can't take them ranked candidates on phantom short P&L.
# NOT cheap at scale: the full grid measured ~108 min at 30 days × 10
# symbols (2026-07-13 run) and grows linearly with days × symbols — see
# the runtime note on OPTIMIZER_MAX_SYMBOLS before widening anything.
# Bracket distances are ATR multiples (see indicators.bracket_distances),
# so the same parameters adapt to each symbol's own volatility.
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

# Total number of parameter combinations in the FULL grid (informational);
# each run's actual count comes from effective_grid(), which may pin
# dimensions, and is what the live status reports.
GRID_TOTAL_COMBINATIONS = 1
for _values in PARAMETER_GRID.values():
    GRID_TOTAL_COMBINATIONS *= len(_values)


def effective_grid(live_config: Dict[str, float]) -> Dict[str, List[float]]:
    """The grid a run actually searches, derived from the live config.

    While short_enabled is off, the short dimensions are pinned to their
    disabled sentinels (rsi_short_signal 999 never crosses, so backtest()
    takes no shorts): the live engine cannot take a short, so exploring
    3×3 short combinations both multiplied the grid 9× (15552 vs 1728)
    and — worse — let candidates win on simulated short P&L the bot can
    never realize."""
    grid: Dict[str, List[float]] = dict(PARAMETER_GRID)
    if not bool(live_config.get("short_enabled", 0.0)):
        grid["rsi_short_signal"] = [999.0]
        grid["rsi_short_exit"] = [0.0]
    return grid


def clear_stale_status() -> None:
    """Reset the optimizer status blob at process start.

    A run only lives inside its process: a deploy restart kills the grid-
    search thread without running its `finally`, leaving the last progress
    blob behind — the 2026-07-14 manual run died at the v2.27.2 restart
    and the dashboard showed its frozen progress bar for hours ("optimizer
    is very slow"). At process start nothing can be running yet, so any
    non-idle phase found here is stale by definition. Never raises."""
    try:
        db = get_db()
        status = db.get_state("optimizer_status") or {}
        phase = status.get("phase")
        if phase and phase != "idle":
            db.add_log(
                "OPTIMIZER",
                f"Startup: cleared stale optimizer status (phase '{phase}' "
                f"from a previous process — that run died with the restart)",
            )
        _clear_status(db)
    except Exception as exc:
        logger.error("Stale optimizer-status clear failed: %s", exc)


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


def calibrate_stop_slippage(db) -> Tuple[float, int]:
    """(stop_slippage_pct, sample_count) measured from the ledger's own
    realized stop fills — the "more data" the optimizer actually needed.

    For every recent closed trade whose exit was a stop, the realized
    slippage is how far the fill landed beyond the recorded stop level,
    as a fraction of that level (long: fill below stop; short: fill
    above). The median over the sample is robust to the odd disaster
    fill; the result is clamped to [default, CALIBRATION_MAX_SLIP] so
    calibration can tighten the backtest's honesty but never make it more
    optimistic than the configured default. Falls back to the default
    when there are fewer than CALIBRATION_MIN_SAMPLES stop exits (fresh
    DB, or an engine that has been closing everything via targets)."""
    try:
        trades = db.get_trades(200)
    except Exception as exc:
        logger.error("Slippage calibration: trade fetch failed: %s", exc)
        return STOP_SLIPPAGE_PCT, 0

    slips: List[float] = []
    for t in trades:
        reason = (t.get("exit_reason") or "").lower()
        if "stop" not in reason:
            continue
        stop = t.get("stop_loss")
        exit_price = t.get("exit_price")
        if not stop or not exit_price or stop <= 0:
            continue
        if t.get("side") == "SELL":
            slip = (float(exit_price) - float(stop)) / float(stop)
        else:
            slip = (float(stop) - float(exit_price)) / float(stop)
        # A limit close can fill better than the level; that is zero
        # slippage for this model, not negative.
        slips.append(max(slip, 0.0))

    if len(slips) < CALIBRATION_MIN_SAMPLES:
        return STOP_SLIPPAGE_PCT, len(slips)
    measured = float(np.median(slips))
    return min(max(measured, STOP_SLIPPAGE_PCT), CALIBRATION_MAX_SLIP), len(slips)


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
    cost_per_trade_pct: float = COST_PER_TRADE_PCT,
    stop_slippage_pct: float = STOP_SLIPPAGE_PCT,
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
        cost = cost_per_trade_pct * qty * entry_price
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
                    exit_price = stop_price * (1.0 - stop_slippage_pct)
                elif highs[i] >= target_price:
                    exit_price = target_price
                elif not np.isnan(rsi[i]) and rsi[i] >= rsi_exit_signal:
                    exit_price = closes[i]
            else:
                if highs[i] >= stop_price:
                    exit_price = stop_price * (1.0 + stop_slippage_pct)
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
        cost_per_trade_pct: float = COST_PER_TRADE_PCT,
        stop_slippage_pct: float = STOP_SLIPPAGE_PCT,
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
                cost_per_trade_pct=cost_per_trade_pct,
                stop_slippage_pct=stop_slippage_pct,
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
    history: Dict[str, pd.DataFrame],
    n_folds: int = VALIDATION_FOLDS,
) -> Tuple[Dict[str, pd.DataFrame], List[Dict[str, pd.DataFrame]]]:
    """Chronological per-symbol split into a train window and n_folds
    sequential validation folds covering the holdout.

    One 25 % holdout window rewards candidates that got lucky in that
    single stretch of tape; splitting it into sequential folds and (in
    run_optimization) demanding profitability in a majority of them keeps
    the same data budget while filtering one-window luck. A symbol whose
    slice of a fold is under 50 bars is left out of that fold; symbols too
    short to validate at all stay train-only, exactly as before."""
    train: Dict[str, pd.DataFrame] = {}
    folds: List[Dict[str, pd.DataFrame]] = [dict() for _ in range(n_folds)]
    for symbol, bars in history.items():
        cut = int(len(bars) * TRAIN_FRACTION)
        if cut < 50 or len(bars) - cut < 50:
            # Too little data to validate meaningfully — train-only symbol.
            train[symbol] = bars
            continue
        train[symbol] = bars.iloc[:cut]
        holdout = bars.iloc[cut:]
        fold_len = len(holdout) // n_folds
        for i in range(n_folds):
            start = i * fold_len
            end = (i + 1) * fold_len if i < n_folds - 1 else len(holdout)
            fold_bars = holdout.iloc[start:end]
            if len(fold_bars) >= 50:
                folds[i][symbol] = fold_bars
    return train, [f for f in folds if f]


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
            f"{TRAIN_FRACTION:.0%} train / {VALIDATION_FOLDS} validation "
            f"folds, {len(symbols)} symbols: {', '.join(symbols)})",
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

        train_frames, validation_folds = split_history(history)
        train = _WindowData(train_frames)
        validations = [_WindowData(f) for f in validation_folds]

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

        # Friction honesty: replace the stop-slippage guess with the median
        # of the ledger's own realized stop fills (clamped, never below the
        # configured default). Published to runtime_state so the shadow-veto
        # resolver prices hypothetical fills with the same numbers.
        stop_slip, slip_samples = calibrate_stop_slippage(db)
        sizing["cost_per_trade_pct"] = COST_PER_TRADE_PCT
        sizing["stop_slippage_pct"] = stop_slip
        db.set_state("optimizer_friction", {
            "stop_slippage_pct": stop_slip,
            "cost_per_trade_pct": COST_PER_TRADE_PCT,
            "samples": slip_samples,
            "calibrated_at": started_at,
        })
        if slip_samples >= CALIBRATION_MIN_SAMPLES:
            db.add_log(
                "OPTIMIZER",
                f"Stop slippage calibrated from {slip_samples} realized stop "
                f"fills: {stop_slip * 100:.3f}% "
                f"(env default {STOP_SLIPPAGE_PCT * 100:.3f}%)",
            )
        else:
            db.add_log(
                "OPTIMIZER",
                f"Stop slippage NOT calibrated — only {slip_samples} stop "
                f"fills on record (need {CALIBRATION_MIN_SAMPLES}); using the "
                f"{STOP_SLIPPAGE_PCT * 100:.3f}% default",
            )

        # The grid a run actually searches (short dims pinned while shorts
        # are disabled live — 9× fewer combos AND no phantom short P&L in
        # the ranking; see effective_grid).
        grid = effective_grid(live_config)
        short_dims_pinned = len(grid["rsi_short_signal"]) == 1
        total_combos = 1
        for values in grid.values():
            total_combos *= len(values)
        run["total_combinations"] = total_combos
        db.add_log(
            "OPTIMIZER",
            f"Grid: {total_combos} combinations"
            + (
                " (short dimensions pinned — shorts are disabled live, so "
                "simulating them would rank candidates on P&L the engine "
                "cannot realize)"
                if short_dims_pinned else " (full two-sided grid)"
            ),
        )

        # Score every combination on the train window, rank best-first.
        _publish_status(db, {
            "phase": "grid_search",
            "trigger": trigger,
            "started_at": started_at,
            "total_bars": total_bars,
            "n_symbols": len(history),
            "total_combinations": total_combos,
            "evaluated": 0,
            "candidates": 0,
        })
        ranked: List[Tuple[float, Dict[str, float], Tuple[float, float, int]]] = []
        evaluated = 0
        for rsi_period, rsi_buy, stop_mult, target_mult, exit_signal, short_signal, short_exit, max_disloc in itertools.product(
            grid["rsi_period"],
            grid["rsi_buy_signal"],
            grid["atr_stop_mult"],
            grid["atr_target_mult"],
            grid["rsi_exit_signal"],
            grid["rsi_short_signal"],
            grid["rsi_short_exit"],
            grid["max_vwap_dislocation_pct"],
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
                    "total_combinations": total_combos,
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

        # Walk down the train ranking and take the first combination that
        # made money in a MAJORITY of the unseen validation folds and in
        # aggregate — surviving one lucky window is not survival.
        _publish_status(db, {
            "phase": "validation",
            "trigger": trigger,
            "started_at": started_at,
            "total_bars": total_bars,
            "n_symbols": len(history),
            "total_combinations": total_combos,
            "evaluated": total_combos,
            "candidates": len(ranked),
            "validated": 0,
        })
        best_params: Optional[Dict[str, float]] = None
        best_train_score = 0.0
        train_stats: Tuple[float, float, int] = (0.0, 0.0, 0)
        val_stats: Optional[Tuple[float, float, int]] = None
        fold_note: Optional[str] = None
        folds_needed = len(validations) // 2 + 1
        validated = 0
        for score, params, stats in ranked:
            if not validations:
                best_params, best_train_score, train_stats = params, score, stats
                break
            fold_results: List[Tuple[float, float, int]] = []
            for window in validations:
                _, val_return, val_drawdown, val_trades = window.score(
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
                fold_results.append((val_return, val_drawdown, val_trades))
            validated += 1
            if validated % 50 == 0:
                _publish_status(db, {
                    "phase": "validation",
                    "trigger": trigger,
                    "started_at": started_at,
                    "total_bars": total_bars,
                    "n_symbols": len(history),
                    "total_combinations": total_combos,
                    "evaluated": total_combos,
                    "candidates": len(ranked),
                    "validated": validated,
                })
            profitable_folds = sum(1 for r in fold_results if r[0] > 0.0)
            aggregate_return = sum(r[0] for r in fold_results)
            if profitable_folds >= folds_needed and aggregate_return > 0.0:
                best_params, best_train_score, train_stats = params, score, stats
                val_stats = (
                    aggregate_return,
                    max(r[1] for r in fold_results),
                    sum(r[2] for r in fold_results),
                )
                fold_note = (
                    f"{profitable_folds}/{len(fold_results)} folds profitable: "
                    + ", ".join(f"{r[0] * 100:+.2f}%" for r in fold_results)
                )
                break

        if best_params is None:
            db.add_log(
                "OPTIMIZER",
                f"No combination survived out-of-sample validation — "
                f"{len(ranked)} candidates were profitable in-sample but none "
                f"made money in {folds_needed} of {len(validations)} holdout "
                f"folds — keeping current parameters unchanged",
            )
            run["status"] = "rejected_validation"
            run["detail"] = (
                f"All {len(ranked)} candidates failed the "
                f"{folds_needed}-of-{len(validations)}-fold validation gate"
            )
            return None

        # Post-optimization LLM review (if analyst is enabled): a binary
        # sanity check that can only accept the validated winner or reject
        # it and keep the current parameters. It deliberately cannot pick a
        # different rank — an earlier "override" option let the LLM select
        # from the train-window ranking (mostly combinations that failed or
        # never saw the out-of-sample gate), i.e. exactly the in-sample
        # cherry-picking the walk-forward validation exists to prevent.
        # best_params is still grid-keys-only here ON PURPOSE: the reviewer
        # compares the winner against the candidates, and merging the
        # carried-forward config keys (news_cutoff etc.) before this point
        # made it reject every run as a "structural mismatch between grid
        # search and validation" (2026-07-13 nightly).
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
                "total_combinations": total_combos,
                "evaluated": total_combos,
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

        # news_cutoff, analyst_enabled and short_enabled are not part of the
        # technical grid; carry the live values forward so a config read
        # after this write stays complete. Merged only NOW, after the
        # analyst review — see the note above the review block.
        current = db.get_config()
        best_params["news_cutoff"] = current["news_cutoff"]
        best_params["analyst_enabled"] = current.get("analyst_enabled", 0.0)
        best_params["short_enabled"] = current.get("short_enabled", 0.0)
        if short_dims_pinned:
            # The 999/0 short sentinels were search-time placeholders, not
            # tuned values; keep the stored short thresholds so flipping
            # short_enabled on later starts from sane numbers instead of a
            # short signal that can never fire.
            best_params["rsi_short_signal"] = current.get("rsi_short_signal", 80.0)
            best_params["rsi_short_exit"] = current.get("rsi_short_exit", 20.0)

        _publish_status(db, {
            "phase": "writing",
            "trigger": trigger,
            "started_at": started_at,
            "total_bars": total_bars,
            "n_symbols": len(history),
            "total_combinations": total_combos,
            "evaluated": total_combos,
            "candidates": len(ranked),
            "validated": validated,
        })
        db.set_config(best_params)

        total_return, drawdown, trades = train_stats
        validation_note = "no validation window (too little data)"
        if val_stats is not None:
            validation_note = (
                f"validation: {val_stats[0] * 100:+.2f}% return, "
                f"{val_stats[1] * 100:.2f}% worst fold drawdown, "
                f"{val_stats[2]} trades ({fold_note})"
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
        slip_note = (
            f"stop slip {stop_slip * 100:.3f}% from {slip_samples} fills"
            if slip_samples >= CALIBRATION_MIN_SAMPLES
            else f"stop slip default ({slip_samples} fills on record)"
        )
        run["detail"] = (
            f"Parameters unchanged (winner matches current); {slip_note}"
            if not changed_keys
            else f"{fold_note or 'no validation folds'}; {slip_note}"
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
