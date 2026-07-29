"""
Argus — candidate strategy definitions for the shadow-trading harness.

Each Strategy is a pure function over historical bars: given RSI/VWAP/ATR
computed with the SAME indicators.py math the live engine and the optimizer
use, decide whether to open a signal. No I/O, no LLM calls, no side effects
— a strategy must be cheap enough to evaluate for the whole watchlist every
cycle. Sizing, exit management, persistence and friction live in shadow.py,
not here, so a strategy author only ever writes the entry rule.

The first two candidates are seeded from the 2026-07-14..19 shadow-veto
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

That control has now returned its verdict (Jul 8–22 ledger, v2.30.0): the
live entry's expectancy (-$3.65/trade) is statistically indistinguishable
from the coin flip's (-$3.77) — the mean-reversion dip entry carries no
directional edge on this universe. MomentumBreakoutStrategy is the response:
the opposite hypothesis, buying breakouts (strength) instead of dips
(weakness), on the premise that these high-beta most-actives trend rather
than revert. It earns its own paper track record before anyone considers it
for real capital — the whole point of the harness.

v2.31.0 turns the page: after a week, momentum_breakout (-$2.45/trade) ≈
random_baseline (-$2.22) ≈ the live rule — every direction rule loses at
about the same rate on this universe, while the crypto engine (same code,
liquid majors) sits near breakeven. The working hypothesis is now TERRAIN,
not trigger: thin, gappy, sub-$20 most-actives are unwinnable after
friction. fear_confirmation is retired (falsified at -$9.93/trade), and the
{dip, momentum} x {all, liquid} grid is completed with LiquidDipStrategy
and LiquidMomentumStrategy — each one variable (a $20 price floor) away
from a running baseline. Live entries are paused (entries_paused config
key) while the grid decides which cell, if any, deserves capital.

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
    # RETIRED v2.31.0 — hypothesis falsified: 43 shadow trades, expectancy
    # -$9.93/trade (worst of all candidates); deeper dips resolved WORSE,
    # not better, so the Jul 14–19 ledger inversion was sampling noise.
    # max_positions = 0 stops new entries while the runner keeps managing
    # (draining) any still-open paper positions; the class and its DB track
    # record stay for the historical row on the scoreboard.
    max_positions = 0

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


class MomentumBreakoutStrategy(ShadowStrategy):
    """The opposite hypothesis to the live entry. The live strategy buys
    oversold dips below VWAP (mean reversion); the random_baseline control
    showed that entry carries no directional edge on this most-actives
    universe (Jul 8–22 ledger: live expectancy -$3.65 ≈ coin-flip -$3.77 —
    see argus-profitability-backlog). If dip-buying is edgeless because these
    high-beta names trend rather than revert, the natural thing to test next
    is the inverse: buy *strength*, not weakness. This fires on a fresh
    breakout above the recent high while price leads VWAP and RSI is strong
    but not blow-off exhausted — trend continuation. Mirror rule shorts a
    breakdown below the recent low when shorts are enabled. Same bracket and
    friction math as every other candidate, so its track record is directly
    comparable to the live strategy's."""

    name = "momentum_breakout"
    max_positions = 5

    LOOKBACK = 20  # bars whose high/low define the breakout level
    RSI_FLOOR = 55.0  # momentum must be present...
    RSI_CEILING = 78.0  # ...but not a parabolic blow-off

    def evaluate(self, symbol, bars, config):
        period = max(int(config.get("rsi_period", 14)), 2)
        if len(bars) < max(period * 2 + 2, self.LOOKBACK + 2):
            return None
        rsi = compute_rsi(bars["close"], period)
        atr = compute_atr(bars)
        vwap = compute_vwap(bars)
        closes = bars["close"]
        latest_rsi = float(rsi.iloc[-1])
        latest_atr = float(atr.iloc[-1])
        latest_vwap = float(vwap.iloc[-1])
        latest_close = float(closes.iloc[-1])
        if np.isnan(latest_rsi) or np.isnan(latest_atr) or latest_atr <= 0:
            return None
        stop_mult = config.get("atr_stop_mult", 2.0)
        target_mult = config.get("atr_target_mult", 4.0)
        if stop_is_floored(latest_close, latest_atr, stop_mult):
            return None

        # Breakout level excludes the current bar, so "cleared the high" means
        # the latest close exceeds the prior LOOKBACK bars, not itself.
        prior = closes.iloc[-(self.LOOKBACK + 1):-1]
        prior_high = float(prior.max())
        prior_low = float(prior.min())

        if (
            latest_close > prior_high
            and latest_close > latest_vwap
            and self.RSI_FLOOR <= latest_rsi <= self.RSI_CEILING
        ):
            stop_d, target_d = bracket_distances(
                latest_close, latest_atr, stop_mult, target_mult
            )
            return Signal(
                symbol=symbol, side="BUY", price=latest_close,
                atr=latest_atr, rsi=latest_rsi,
                stop_loss=latest_close - stop_d,
                take_profit=latest_close + target_d,
                rationale=(
                    f"Breakout: close ${latest_close:.2f} cleared the "
                    f"{self.LOOKBACK}-bar high ${prior_high:.2f} while leading "
                    f"VWAP ${latest_vwap:.2f}, RSI {latest_rsi:.1f} (momentum, "
                    f"not exhaustion) — trend continuation long."
                ),
            )

        if not bool(config.get("short_enabled", 0.0)):
            return None
        if (
            latest_close < prior_low
            and latest_close < latest_vwap
            and (100.0 - self.RSI_CEILING) <= latest_rsi <= (100.0 - self.RSI_FLOOR)
        ):
            stop_d, target_d = bracket_distances(
                latest_close, latest_atr, stop_mult, target_mult
            )
            return Signal(
                symbol=symbol, side="SELL", price=latest_close,
                atr=latest_atr, rsi=latest_rsi,
                stop_loss=latest_close + stop_d,
                take_profit=latest_close - target_d,
                rationale=(
                    f"Breakdown: close ${latest_close:.2f} broke the "
                    f"{self.LOOKBACK}-bar low ${prior_low:.2f} below VWAP "
                    f"${latest_vwap:.2f}, RSI {latest_rsi:.1f} — trend "
                    f"continuation short."
                ),
            )
        return None


class LiquidDipStrategy(ShadowStrategy):
    """The live entry rule on better terrain — the terrain experiment's
    missing cell. Jul 8–28 evidence: every direction rule on the full
    most-actives universe (dip-buy, deep-dip, breakout, coin flip) lost at
    roughly the same rate, while the crypto engine — same code, liquid
    majors — sat near breakeven. That pattern says the universe, not the
    trigger, is the defect: thin sub-$20 movers where the spread eats the
    bracket and stops gap through. This candidate replicates the live
    technical rule EXACTLY (RSI oversold + close below VWAP + falling-knife
    cap, same config keys) with one added condition: price >= $20. One
    variable versus the live rule's own veto-ledger record — if this earns
    while the full-universe rule keeps losing, the fix for the live engine
    is a universe change, not a new signal."""

    name = "liquid_dip"
    max_positions = 5

    MIN_PRICE = 20.0  # the single experimental variable vs the live rule

    def evaluate(self, symbol, bars, config):
        period = max(int(config.get("rsi_period", 14)), 2)
        if len(bars) < period * 2:
            return None
        closes = bars["close"]
        latest_close = float(closes.iloc[-1])
        if latest_close < self.MIN_PRICE:
            return None
        rsi = compute_rsi(closes, period)
        atr = compute_atr(bars)
        vwap = compute_vwap(bars)
        latest_rsi = float(rsi.iloc[-1])
        latest_atr = float(atr.iloc[-1])
        latest_vwap = float(vwap.iloc[-1])
        if np.isnan(latest_rsi) or np.isnan(latest_atr) or latest_atr <= 0:
            return None
        stop_mult = config.get("atr_stop_mult", 1.5)
        target_mult = config.get("atr_target_mult", 2.5)
        if stop_is_floored(latest_close, latest_atr, stop_mult):
            return None

        if latest_rsi >= config.get("rsi_buy_signal", 30.0):
            return None
        if latest_close > latest_vwap:
            return None  # not a real dip — mirrors the live gate
        dislocation = (latest_vwap - latest_close) / latest_vwap
        if dislocation > config.get("max_vwap_dislocation_pct", 0.15):
            return None  # falling knife — mirrors the live cap

        stop_d, target_d = bracket_distances(
            latest_close, latest_atr, stop_mult, target_mult
        )
        return Signal(
            symbol=symbol, side="BUY", price=latest_close,
            atr=latest_atr, rsi=latest_rsi,
            stop_loss=latest_close - stop_d,
            take_profit=latest_close + target_d,
            rationale=(
                f"Live dip rule on liquid terrain: RSI {latest_rsi:.1f} "
                f"oversold, {dislocation * 100:.1f}% below VWAP "
                f"${latest_vwap:.2f}, price ${latest_close:.2f} >= "
                f"${self.MIN_PRICE:.0f} floor — same trigger, better tape."
            ),
        )


class LiquidMomentumStrategy(MomentumBreakoutStrategy):
    """momentum_breakout restricted to liquid terrain (price >= $20) — the
    fourth cell of the {dip, momentum} x {all, liquid} experiment grid.
    Everything else inherits from MomentumBreakoutStrategy, so the pair
    differs by exactly one variable and their track records read directly
    against each other."""

    name = "liquid_momentum"
    max_positions = 5

    MIN_PRICE = 20.0

    def evaluate(self, symbol, bars, config):
        if len(bars) and float(bars["close"].iloc[-1]) < self.MIN_PRICE:
            return None
        return super().evaluate(symbol, bars, config)


# Registered candidates, evaluated every cycle the market is open. Order is
# cosmetic (dashboard listing order); each strategy's own position book is
# independent. fear_confirmation stays registered with max_positions=0
# (retired: entries off, exits keep draining, scoreboard row preserved).
STRATEGIES: List[ShadowStrategy] = [
    FearConfirmationStrategy(),
    RandomBaselineStrategy(),
    MomentumBreakoutStrategy(),
    LiquidDipStrategy(),
    LiquidMomentumStrategy(),
]
