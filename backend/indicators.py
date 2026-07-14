"""
Argus — shared technical indicators.

Single implementation used by both the live engine (bot.py) and the
nightly optimizer (optimizer.py) so a backtest can never drift from the
live signal math. All functions are pure pandas/numpy — no I/O.

Strategy constants that must stay identical between live trading and
backtesting (bracket floors, ATR period) live here as well.
"""

from __future__ import annotations

import os

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

# Trading friction applied to every SIMULATED trade — the optimizer's
# backtest fills and the shadow ledger's vetoed-signal resolution. Shared
# here so "what a vetoed trade would have made" and "what the optimizer
# thinks a trade makes" can never drift apart. COST is a round-trip
# fraction of notional (half-spread in and out); stops additionally slip
# against the trade because a breached stop fills through the level.
# Since v2.27.0 STOP_SLIPPAGE_PCT is only the FLOOR/fallback: each optimizer
# run calibrates the working value from the ledger's realized stop fills
# (optimizer.calibrate_stop_slippage) and publishes it in the
# optimizer_friction state blob, which the shadow-veto resolver also reads.
COST_PER_TRADE_PCT = float(os.getenv("OPTIMIZER_COST_PCT", "0.10")) / 100.0
STOP_SLIPPAGE_PCT = float(os.getenv("OPTIMIZER_STOP_SLIP_PCT", "0.05")) / 100.0


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
    """Session-anchored volume-weighted average price.

    Cumulative price×volume over volume, reset at each US-Eastern trading
    date, so the fair-value anchor never spans the overnight gap and does
    not shift with the length of the fetched window. Frames without a
    datetime index fall back to window-cumulative. Zero-volume stretches
    fall back to the typical price itself.
    """
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3.0
    volume = bars["volume"].astype(float)
    pv = typical * volume
    index = bars.index
    if isinstance(index, pd.DatetimeIndex):
        session = (
            index.tz_convert("America/New_York").date
            if index.tz is not None
            else index.date
        )
        cum_volume = volume.groupby(session).cumsum()
        cum_pv = pv.groupby(session).cumsum()
    else:
        cum_volume = volume.cumsum()
        cum_pv = pv.cumsum()
    vwap = cum_pv / cum_volume.replace(0.0, np.nan)
    return vwap.fillna(typical)


def stop_is_floored(price: float, atr: float, stop_mult: float) -> bool:
    """True when the percentage floor, not ATR, would set the stop distance.

    A symbol whose ATR-scaled stop falls below MIN_STOP_PCT is too quiet
    for a volatility-scaled bracket to mean anything: the floor stop sits
    inside ordinary bar-to-bar noise and the trade is a coin flip that
    loses the spread. Both the live engine and the backtest skip these
    entries so the optimizer cannot tune parameters on them.
    """
    return atr * stop_mult < price * MIN_STOP_PCT


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
