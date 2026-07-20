"""
Argus — candidate strategy definitions for the shadow-trading harness.

Each Strategy is a pure function over historical bars: given RSI/VWAP/ATR
computed with the SAME indicators.py math the live engine and the optimizer
use, decide whether to open a signal. No I/O, no LLM calls, no side effects
— a strategy must be cheap enough to evaluate for the whole watchlist every
cycle. Sizing, exit management, persistence and friction live in shadow.py,
not here, so a strategy author only ever writes the entry rule.

These two candidates are seeded directly from the 2026-07-14..19 shadow-veto
ledger's findings (see CHANGELOG v2.28.0 / the argus-profitability-backlog
memory): signals the live gates blocked for being "too scary" (bearish
sentiment, a calm downtrend) or "too extended" (deep VWAP dislocation)
resolved profitably MORE often than the signals the gates let through. That
is either noise from a small sample, or a real inversion in this signal
family. FearConfirmationStrategy tests the inversion directly.
RandomBaselineStrategy tests a cheaper, more important question first: does
the live RSI/VWAP entry carry any directional information at all, or would
a coin flip on the same trigger bars do just as well? If the live strategy
cannot beat this control, no amount of parameter tuning will fix it — the
entry itself is the defect.

Adding a third candidate: subclass ShadowStrategy, implement evaluate(), add
an instance to STRATEGIES. It starts accumulating a paper track record on
the very next cycle; nothing else needs to change.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from indicators import (
    bracket_distances,
    compute_atr,
    compute_rsi,
    compute_vwap,
    stop_is_floored,
)


@dataclass
class Signal:
    """A candidate entry, sized and recorded by shadow.py — never submitted
    as a real order. `rsi`/`atr` are carried through for the shadow_positions
    record (parity with the live engine's open_entries fields)."""

    symbol: str
    side: str  # "BUY" | "SELL"
    price: float
    atr: float
    rsi: Optional[float]
    stop_loss: float
    take_profit: float
    rationale: str


class ShadowStrategy(ABC):
    """A candidate entry strategy evaluated on paper alongside the live
    engine. Subclasses implement evaluate(); shadow.py owns sizing, exit
    management, persistence and the friction model — a strategy author
    never touches money, orders, or the database directly."""

    name: str
    max_positions: int = 5

    @abstractmethod
    def evaluate(
        self, symbol: str, bars: pd.DataFrame, config: Dict[str, float]
    ) -> Optional[Signal]:
        """A candidate entry for this symbol's current bar, or None.

        Must not raise on short/bad data — return None instead. The shadow
        runner treats an uncaught exception as a bug in the candidate (and
        logs it), not as "no signal today"."""
        ...


class FearConfirmationStrategy(ShadowStrategy):
    """Requires a DEEPER VWAP dislocation than the live falling-knife cap
    allows (not less — the live cap rejects exactly these as "too scary"),
    and requires the bar to have already turned — closed above the prior
    bar's close — before entering, instead of catching the first oversold
    tick. Mirror rule for shorts. If the ledger's inversion is real and not
    sampling noise, this should out-earn the live strategy's shallower,
    unconfirmed dips."""

    name = "fear_confirmation"
    max_positions = 5

    MIN_DISLOCATION_PCT = 0.15  # deeper than the live cap, not shallower
    RSI_PERIOD_FLOOR = 2

    def evaluate(self, symbol, bars, config):
        period = max(int(config.get("rsi_period", 14)), self.RSI_PERIOD_FLOOR)
        if len(bars) < period * 2 + 2:
            return None
        rsi = compute_rsi(bars["close"], period)
        atr = compute_atr(bars)
        vwap = compute_vwap(bars)
        latest_rsi = float(rsi.iloc[-1])
        latest_atr = float(atr.iloc[-1])
        latest_vwap = float(vwap.iloc[-1])
        closes = bars["close"]
        latest_close = float(closes.iloc[-1])
        prev_close = float(closes.iloc[-2])
        if np.isnan(latest_rsi) or np.isnan(latest_atr) or latest_atr <= 0:
            return None
        stop_mult = config.get("atr_stop_mult", 2.0)
        target_mult = config.get("atr_target_mult", 4.0)
        if stop_is_floored(latest_close, latest_atr, stop_mult):
            return None

        buy_signal = config.get("rsi_buy_signal", 30.0)
        if latest_rsi < buy_signal and latest_close < latest_vwap:
            dislocation = (latest_vwap - latest_close) / latest_vwap
            if dislocation >= self.MIN_DISLOCATION_PCT and latest_close > prev_close:
                stop_d, target_d = bracket_distances(
                    latest_close, latest_atr, stop_mult, target_mult
                )
                return Signal(
                    symbol=symbol, side="BUY", price=latest_close,
                    atr=latest_atr, rsi=latest_rsi,
                    stop_loss=latest_close - stop_d,
                    take_profit=latest_close + target_d,
                    rationale=(
                        f"RSI {latest_rsi:.1f} oversold, {dislocation * 100:.0f}% "
                        f"below VWAP (deep dislocation the live cap would "
                        f"reject) and the bar has turned up (${latest_close:.2f} "
                        f"> prior ${prev_close:.2f}) — a confirmed fear dip."
                    ),
                )

        if not bool(config.get("short_enabled", 0.0)):
            return None
        short_signal = config.get("rsi_short_signal", 70.0)
        if latest_rsi > short_signal and latest_close > latest_vwap:
            dislocation = (latest_close - latest_vwap) / latest_vwap
            if dislocation >= self.MIN_DISLOCATION_PCT and latest_close < prev_close:
                stop_d, target_d = bracket_distances(
                    latest_close, latest_atr, stop_mult, target_mult
                )
                return Signal(
                    symbol=symbol, side="SELL", price=latest_close,
                    atr=latest_atr, rsi=latest_rsi,
                    stop_loss=latest_close + stop_d,
                    take_profit=latest_close - target_d,
                    rationale=(
                        f"RSI {latest_rsi:.1f} overbought, {dislocation * 100:.0f}% "
                        f"above VWAP (deep extension the live cap would "
                        f"reject) and the bar has turned down — a confirmed "
                        f"fade."
                    ),
                )
        return None


class RandomBaselineStrategy(ShadowStrategy):
    """Honesty check: does the live entry logic carry any directional
    information at all? Fires on the SAME trigger bars as the live RSI
    signal (same extremes, same too-quiet/floored-stop filter — so it isn't
    simply trading noisier symbols or a different opportunity set) at a
    matched rate, but the SIDE is a coin flip instead of the RSI/VWAP call.
    Deterministically seeded from (symbol, bar timestamp) so a restart
    replays the same decisions rather than introducing fresh randomness —
    a paper track record must be reproducible to be trustworthy. If this
    performs comparably to the live strategy, the live entry has no edge
    over noise; if it clearly underperforms, the live entry's direction
    call is real information worth keeping."""

    name = "random_baseline"
    max_positions = 5
    FIRE_RATE = 0.5  # fraction of live-strategy trigger bars this also takes

    def evaluate(self, symbol, bars, config):
        period = max(int(config.get("rsi_period", 14)), 2)
        if len(bars) < period * 2:
            return None
        rsi = compute_rsi(bars["close"], period)
        atr = compute_atr(bars)
        latest_rsi = float(rsi.iloc[-1])
        latest_atr = float(atr.iloc[-1])
        latest_close = float(bars["close"].iloc[-1])
        if np.isnan(latest_rsi) or np.isnan(latest_atr) or latest_atr <= 0:
            return None
        stop_mult = config.get("atr_stop_mult", 2.0)
        if stop_is_floored(latest_close, latest_atr, stop_mult):
            return None

        buy_signal = config.get("rsi_buy_signal", 30.0)
        short_signal = config.get("rsi_short_signal", 70.0)
        short_enabled = bool(config.get("short_enabled", 0.0))
        triggered = latest_rsi < buy_signal or (
            short_enabled and latest_rsi > short_signal
        )
        if not triggered:
            return None

        bar_ts = bars.index[-1]
        digest = hashlib.sha256(f"{symbol}|{bar_ts}".encode()).digest()
        roll = int.from_bytes(digest[:4], "big") / 2 ** 32
        if roll >= self.FIRE_RATE:
            return None
        side = "BUY" if digest[4] % 2 == 0 or not short_enabled else "SELL"

        target_mult = config.get("atr_target_mult", 4.0)
        stop_d, target_d = bracket_distances(
            latest_close, latest_atr, stop_mult, target_mult
        )
        if side == "BUY":
            stop_loss, take_profit = latest_close - stop_d, latest_close + target_d
        else:
            stop_loss, take_profit = latest_close + stop_d, latest_close - target_d
        return Signal(
            symbol=symbol, side=side, price=latest_close, atr=latest_atr,
            rsi=latest_rsi, stop_loss=stop_loss, take_profit=take_profit,
            rationale=(
                f"RSI {latest_rsi:.1f} was extreme (same trigger bar the "
                f"live strategy would act on) but side {side} was a coin "
                f"flip, not the RSI/VWAP call — control for whether the "
                f"live entry's direction carries information."
            ),
        )


# Registered candidates, evaluated every cycle the market is open. Order is
# cosmetic (dashboard listing order); each strategy's own position book is
# independent.
STRATEGIES: List[ShadowStrategy] = [
    FearConfirmationStrategy(),
    RandomBaselineStrategy(),
]
