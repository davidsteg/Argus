"""
Argus — shared technical indicators.

Single implementation used by both the live engine (bot.py) and the
nightly optimizer (optimizer.py) so a backtest can never drift from the
live signal math. All functions are pure pandas/numpy — no I/O.

Strategy constants that must stay identical between live trading and
backtesting (bracket floors, ATR period) live here as well.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# ATR lookback in 1-minute bars. Fixed (not optimizer-tuned) so ATR arrays
# can be cached once per symbol during the nightly grid search.
ATR_PERIOD = 14

# Bracket sanity floors, as a fraction of entry price. ATR on a quiet
# megacap can shrink below the bid/ask spread; a stop that tight would be
# pure noise. Both live orders and backtest fills apply the same floors.
MIN_STOP_PCT = 0.0035    # stop never tighter than 0.35 % of price
MIN_TARGET_PCT = 0.0050  # target never tighter than 0.50 % of price


def compute_rsi(closes: pd.Series, period: int) -> pd.Series:
    """Wilder-smoothed Relative Strength Index computed with pandas."""
    delta = closes.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)
    avg_gain = gains.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    # avg_loss == 0 with avg_gain > 0 means pure upside: RSI is 100 by definition.
    rsi = rsi.where(~((avg_loss == 0.0) & (avg_gain > 0.0)), 100.0)
    return rsi


def compute_atr(bars: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Wilder-smoothed Average True Range from high/low/close columns."""
    high = bars["high"]
    low = bars["low"]
    prev_close = bars["close"].shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(
        alpha=1.0 / period, min_periods=period, adjust=False
    ).mean()


def compute_vwap(bars: pd.DataFrame) -> pd.Series:
    """Volume-weighted average price, cumulative over the fetched window.

    With the engine's default 3-hour bar lookback this behaves as a rolling
    intraday VWAP: the fair-value anchor mean-reversion entries should sit
    below. Zero-volume stretches fall back to the typical price itself.
    """
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    volume = bars["volume"].astype(float)
    cum_volume = volume.cumsum()
    cum_pv = (typical * volume).cumsum()
    vwap = cum_pv / cum_volume.replace(0.0, np.nan)
    return vwap.fillna(typical)


def bracket_distances(
    price: float, atr: float, stop_mult: float, target_mult: float
) -> tuple:
    """Volatility-adaptive bracket distances with the shared sanity floors.

    Returns (stop_distance, target_distance) in dollars. A fixed percent
    bracket treats a sleepy megacap and a high-beta mover identically —
    scaling by ATR keeps the stop outside one bar of ordinary noise on
    every symbol.
    """
    stop_distance = max(atr * stop_mult, price * MIN_STOP_PCT, 0.01)
    target_distance = max(atr * target_mult, price * MIN_TARGET_PCT, 0.01)
    return stop_distance, target_distance
