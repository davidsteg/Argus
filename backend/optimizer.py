"""
Argus — nightly walk-forward parameter optimizer with out-of-sample
validation.

Runs as an asyncio task inside the backend container (started by bot.py)
and fires at midnight Europe/Zurich. It fetches the last 30 days of
1-minute historical bars from Alpaca and replays the exact live strategy
(RSI dip entry + ATR-multiple bracket exits, same indicator code via
indicators.py) across a parameter grid.

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
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from dotenv import load_dotenv

import universe
from indicators import bracket_distances, compute_atr, compute_rsi
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

# Grid searched nightly. Kept deliberately compact: 4*4*3*4 = 192
# combinations replayed over train+validation finish in well under a
# minute of CPU inside the container. Bracket distances are ATR multiples
# (see indicators.bracket_distances), so the same parameters adapt to each
# symbol's own volatility.
PARAMETER_GRID: Dict[str, List[float]] = {
    "rsi_period": [7.0, 10.0, 14.0, 21.0],
    "rsi_buy_signal": [20.0, 25.0, 30.0, 35.0],
    "atr_stop_mult": [1.0, 1.5, 2.0],
    "atr_target_mult": [1.5, 2.0, 3.0, 4.0],
}


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


def backtest(
    closes: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    rsi: np.ndarray,
    atr: np.ndarray,
    rsi_buy_signal: float,
    atr_stop_mult: float,
    atr_target_mult: float,
) -> Tuple[float, float, int]:
    """Replay the live strategy over one symbol's bars.

    Entry: RSI crosses below the buy level (previous bar at/above, current
    below) — the crossing condition avoids re-entering on every bar of a
    sustained oversold stretch, matching the live engine which is blocked
    from re-entry while it already holds the symbol.

    Exit: intra-bar bracket simulation with the same ATR-multiple
    distances and floors the live engine uses (indicators.bracket_distances).
    The stop is checked before the target on each bar (pessimistic fill
    assumption).

    Returns (total_return, max_drawdown, n_trades), both as fractions.
    """
    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    n_trades = 0

    in_position = False
    stop_price = 0.0
    target_price = 0.0
    entry_price = 0.0

    for i in range(1, len(closes)):
        if in_position:
            exit_price: Optional[float] = None
            if lows[i] <= stop_price:
                exit_price = stop_price
            elif highs[i] >= target_price:
                exit_price = target_price
            if exit_price is not None:
                equity *= exit_price / entry_price
                in_position = False
                n_trades += 1
                peak = max(peak, equity)
                drawdown = (peak - equity) / peak
                max_drawdown = max(max_drawdown, drawdown)
            continue

        if np.isnan(rsi[i]) or np.isnan(rsi[i - 1]) or np.isnan(atr[i]):
            continue
        if rsi[i - 1] >= rsi_buy_signal > rsi[i]:
            in_position = True
            entry_price = closes[i]
            stop_distance, target_distance = bracket_distances(
                entry_price, atr[i], atr_stop_mult, atr_target_mult
            )
            stop_price = entry_price - stop_distance
            target_price = entry_price + target_distance

    # Force-close a dangling position at the final bar so the last trade
    # is reflected in the score.
    if in_position:
        equity *= closes[-1] / entry_price
        n_trades += 1
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, (peak - equity) / peak)

    return equity - 1.0, max_drawdown, n_trades


class _WindowData:
    """Pre-extracted arrays plus per-window indicator caches."""

    def __init__(self, frames: Dict[str, pd.DataFrame]) -> None:
        self.closes: Dict[str, np.ndarray] = {}
        self.highs: Dict[str, np.ndarray] = {}
        self.lows: Dict[str, np.ndarray] = {}
        self.atr: Dict[str, np.ndarray] = {}
        self._close_series: Dict[str, pd.Series] = {}
        self._rsi_cache: Dict[Tuple[str, int], np.ndarray] = {}
        for symbol, bars in frames.items():
            self.closes[symbol] = bars["close"].to_numpy()
            self.highs[symbol] = bars["high"].to_numpy()
            self.lows[symbol] = bars["low"].to_numpy()
            self.atr[symbol] = compute_atr(bars).to_numpy()
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
    ) -> Tuple[float, float, float, int]:
        """Aggregate (score, return, worst drawdown, trades) across symbols."""
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
                rsi_buy_signal=rsi_buy_signal,
                atr_stop_mult=atr_stop_mult,
                atr_target_mult=atr_target_mult,
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


def run_optimization() -> Optional[Dict[str, float]]:
    """Grid search on train data, out-of-sample gate on validation data;
    writes the winner to bot_config and returns it."""
    db = get_db()
    symbols = universe.get_watchlist(limit=OPTIMIZER_MAX_SYMBOLS)
    db.add_log(
        "OPTIMIZER",
        f"Nightly walk-forward optimization started "
        f"({LOOKBACK_DAYS}d of 1-minute bars, "
        f"{TRAIN_FRACTION:.0%} train / {1 - TRAIN_FRACTION:.0%} validation, "
        f"{len(symbols)} symbols: {', '.join(symbols)})",
    )
    try:
        history = fetch_history(symbols, LOOKBACK_DAYS)
    except Exception as exc:
        logger.error("Historical data fetch failed: %s", exc)
        db.add_log("ERROR", f"Optimizer data fetch failed: {exc}")
        return None

    if not history:
        db.add_log("ERROR", "Optimizer aborted: no historical data returned")
        return None

    total_bars = sum(len(bars) for bars in history.values())
    logger.info(
        "Optimizing over %d bars across %d symbols", total_bars, len(history)
    )

    train_frames, validation_frames = split_history(history)
    train = _WindowData(train_frames)
    validation = _WindowData(validation_frames) if validation_frames else None

    # Score every combination on the train window, rank best-first.
    ranked: List[Tuple[float, Dict[str, float], Tuple[float, float, int]]] = []
    for rsi_period, rsi_buy, stop_mult, target_mult in itertools.product(
        PARAMETER_GRID["rsi_period"],
        PARAMETER_GRID["rsi_buy_signal"],
        PARAMETER_GRID["atr_stop_mult"],
        PARAMETER_GRID["atr_target_mult"],
    ):
        score, total_return, drawdown, trades = train.score(
            rsi_period=int(rsi_period),
            rsi_buy_signal=rsi_buy,
            atr_stop_mult=stop_mult,
            atr_target_mult=target_mult,
        )
        if trades == 0:
            continue
        params = {
            "rsi_period": float(int(rsi_period)),
            "rsi_buy_signal": rsi_buy,
            "atr_stop_mult": stop_mult,
            "atr_target_mult": target_mult,
        }
        ranked.append((score, params, (total_return, drawdown, trades)))
    ranked.sort(key=lambda item: item[0], reverse=True)

    if not ranked:
        db.add_log(
            "OPTIMIZER",
            "Grid search produced no trading combination — keeping current "
            "parameters unchanged",
        )
        return None

    # Walk down the train ranking and take the first combination that also
    # made money on the unseen validation window.
    best_params: Optional[Dict[str, float]] = None
    best_train_score = 0.0
    train_stats: Tuple[float, float, int] = (0.0, 0.0, 0)
    val_stats: Optional[Tuple[float, float, int]] = None
    for score, params, stats in ranked:
        if validation is None:
            best_params, best_train_score, train_stats = params, score, stats
            break
        _, val_return, val_drawdown, val_trades = validation.score(
            rsi_period=int(params["rsi_period"]),
            rsi_buy_signal=params["rsi_buy_signal"],
            atr_stop_mult=params["atr_stop_mult"],
            atr_target_mult=params["atr_target_mult"],
        )
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
        return None

    # news_cutoff and analyst_enabled are not part of the technical grid;
    # carry the live values forward so a config read after this write stays
    # complete.
    current = db.get_config()
    best_params["news_cutoff"] = current["news_cutoff"]
    best_params["analyst_enabled"] = current.get("analyst_enabled", 0.0)

    # Post-optimization LLM review (if analyst is enabled).
    # The analyst can accept, override (pick a different rank), or reject
    # the winner. If it rejects, keep current params unchanged.
    try:
        from analyst import get_analyst
        import regime as regime_module

        analyst = get_analyst()
        regime_info = regime_module.get_regime()
        analyst_result = analyst.review_optimization(
            ranked, best_params, regime_info, db
        )
        if analyst_result is None:
            db.add_log(
                "ANALYST",
                "LLM rejected optimizer winner — keeping current parameters",
            )
            return None
        if analyst_result != best_params:
            best_params = analyst_result
            db.add_log(
                "ANALYST",
                f"LLM overrode optimizer winner — new params: "
                f"RSI({int(best_params['rsi_period'])}) "
                f"buy<{best_params['rsi_buy_signal']:.0f}, "
                f"stop {best_params['atr_stop_mult']:.1f}×ATR, "
                f"target {best_params['atr_target_mult']:.1f}×ATR",
            )
    except Exception as exc:
        logger.error("Post-optimization analyst review failed: %s", exc)

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

    return best_params


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
            await asyncio.to_thread(run_optimization)
        except Exception as exc:
            logger.exception("Nightly optimization failed: %s", exc)
            get_db().add_log("ERROR", f"Nightly optimization failed: {exc}")
        # Guard against clock skew re-triggering within the same minute.
        await asyncio.sleep(61)


if __name__ == "__main__":
    # Manual invocation for testing: python optimizer.py
    run_optimization()
