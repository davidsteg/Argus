"""
Argus — asynchronous short-term trading engine.

Philosophy: trade small amounts on a large scale for quick flips.
1-minute bars are the execution trigger; a SPY-based market regime filter
(regime.py) blocks entries when the whole tape is falling; a VWAP check
confirms the dip is real; curated news sentiment is the directional
filter. Exits are volatility-adaptive: stop/target distances are ATR
multiples, and position size is chosen so each trade risks a roughly
constant dollar amount. A held long is also closed early when RSI recovers
past the overbought exit level — the mean reversion has played out. Nightly
walk-forward optimization (optimizer.py)
re-tunes the strategy parameters — validated out-of-sample — in the
shared bot_config table, which this engine re-reads on every cycle so
new parameters are absorbed seamlessly without a restart.

Trading window: the full extended-hours session, 4:00 AM – 8:00 PM ET
(pre-market + regular + after-hours). Stop/target enforcement is hybrid:
entries during the REGULAR session carry native exchange-side bracket legs
(OCO take-profit limit + stop-market), so a breach fills immediately;
Alpaca forbids brackets outside the regular session, so pre/after-market
entries are plain extended_hours limit orders whose stop/target are SOFT —
enforced by the engine each poll cycle against the live quote. Bracket
legs are DAY orders that die at the regular close, after which the soft
enforcement covers those positions too. Between polls
(poll_interval_seconds) price can gap through a soft level — the accepted
tradeoff for trading the widest possible window.

Safety:
* Alpaca Paper Trading is forced (paper=True). No hardcoded secrets —
  credentials come exclusively from the environment.
* A hard daily loss limit triggers the emergency kill-sequence: cancel all
  open orders, liquidate all positions (extended-hours limit closes),
  persist KILLED, shut down.
* End-of-day flatten: DAY limit orders expire at the extended close, so
  everything is closed eod_flatten_minutes before 8:00 PM ET — no position
  is ever held overnight.
* Every Alpaca API request is wrapped in try/except; a single API hiccup
  never crashes the engine.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest
from dotenv import load_dotenv

import regime
from analyst import get_analyst
from indicators import (
    COST_PER_TRADE_PCT,
    STOP_SLIPPAGE_PCT,
    bracket_distances,
    compute_atr,
    compute_rsi,
    compute_vwap,
    stop_is_floored,
)
from market import US_EASTERN, make_adapter
from sentiment import get_sentiment_provider
from shared.database import STATUS_KILLED, STATUS_RUNNING, get_db
from shared.version import __version__

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("argus.bot")

ZURICH = ZoneInfo("Europe/Zurich")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
API_PORT = int(os.getenv("API_PORT", "8000"))


class ArgusBot:
    """Asynchronous 1-minute execution engine with bracket-order and
    RSI-signal exits. Supports both long (BUY) and short (SELL) entries."""

    def __init__(self) -> None:
        if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
            raise RuntimeError(
                "ALPACA_API_KEY / ALPACA_SECRET_KEY must be set in the environment"
            )
        # paper=True is deliberately hardcoded: Argus never touches live money
        # unless this line is consciously changed and reviewed.
        self.trading = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
        # The MarketAdapter (equity | crypto, chosen by the MARKET env) owns
        # every asset-class-specific seam: data client, universe, session hours,
        # order construction, regime proxy, position partitioning, equity.
        self.market = make_adapter(self.trading, ALPACA_API_KEY, ALPACA_SECRET_KEY)
        self.db = get_db()
        self.sentiment = get_sentiment_provider()
        self.config: Dict[str, float] = self.db.get_config()
        self.watchlist: list = self.market.get_watchlist()
        self.last_cycle: Dict[str, Any] = {}
        self._open_entries: Dict[str, Dict[str, Any]] = {}
        # Symbols with a signal-exit close submitted but not yet filled —
        # guards against re-submitting the close on the next cycle before
        # the sell reports back. Cleared in sync_portfolio once the position
        # is gone.
        self._closing: set = set()
        # symbol -> time.monotonic() deadline until which entries are benched
        self._cooldowns: Dict[str, float] = {}
        self._current_day: Optional[str] = None
        self._shutdown = asyncio.Event()
        self._cycle_count: int = 0
        self._review_task: Optional[asyncio.Task] = None
        self._screener_task: Optional[asyncio.Task] = None
        # Shadow-veto resolution: background task + rate limiter (monotonic
        # deadline of the earliest next run).
        self._veto_task: Optional[asyncio.Task] = None
        self._last_veto_resolution: float = 0.0
        # symbol -> consecutive failed close submissions. Drives the
        # last-resort market-close escalation and the protection banner;
        # pruned in sync_portfolio when the position is gone.
        self._close_failures: Dict[str, int] = {}
        # symbol -> consecutive cycles the protection watchdog could not
        # attach stop/target levels to an untracked position.
        self._protect_failures: Dict[str, int] = {}
        # Symbols whose residual dust was already swept (or the sweep
        # failed) this session — one liquidation attempt per symbol.
        self._dust_swept: set = set()

    # ------------------------------------------------------------------ #
    # loser cooldown
    # ------------------------------------------------------------------ #

    def in_cooldown(self, symbol: str) -> Optional[float]:
        """Remaining cooldown in minutes, or None when tradable."""
        deadline = self._cooldowns.get(symbol)
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            del self._cooldowns[symbol]
            return None
        return remaining / 60.0

    def start_cooldown(self, symbol: str) -> None:
        cd = self.config.get("cooldown_minutes", 30.0)
        if cd > 0:
            self._cooldowns[symbol] = time.monotonic() + cd * 60.0

    # ------------------------------------------------------------------ #
    # position-protection health (dashboard banner + GET /debug)
    # ------------------------------------------------------------------ #

    def reset_protection_health(self) -> None:
        """Zero the protection counters at engine start, so the dashboard's
        banner means this session — mirrors analyst_health."""
        try:
            self.db.set_state(
                "protection_health",
                {"levels_attached": 0, "forced_market_closes": 0,
                 "protective_closes": 0, "close_failures": {},
                 "last_event": None, "last_event_at": None},
            )
        except Exception as exc:
            logger.error("Failed to reset protection health: %s", exc)

    def _protection_event(self, counter: Optional[str], message: str) -> None:
        """Count a protection incident and surface it (protection_health
        blob → dashboard banner, GET /debug). A position running without a
        working stop, or a stop that cannot execute, must be a visible
        operational alarm, not a log line scrolling out of the buffer —
        the 2026-07-13 AAVE close failed silently every cycle for 3.5 h.
        Never raises: health reporting must not affect trading."""
        try:
            state = self.db.get_state("protection_health") or {}
            if counter:
                state[counter] = int(state.get(counter, 0) or 0) + 1
            state["close_failures"] = {
                s: n for s, n in self._close_failures.items() if n > 0
            }
            state["last_event"] = message[:200]
            state["last_event_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            self.db.set_state("protection_health", state)
        except Exception as exc:
            logger.error("Failed to record protection event: %s", exc)

    # ------------------------------------------------------------------ #
    # shadow tracking of vetoed signals
    # ------------------------------------------------------------------ #

    def _record_veto(
        self,
        signal: Dict[str, Any],
        gate: str,
        reason: str,
        price: Optional[float] = None,
    ) -> None:
        """Shadow-record a signal a gate blocked, with the exact bracket and
        size the engine would have traded, so the gate's hypothetical P&L is
        measurable (see _resolve_vetoes). Without this ledger the sentiment /
        VWAP-recheck / risk-agent / portfolio-manager gates run blind — no
        way to tell whether their vetoes save money or cost it. Never raises:
        losing a shadow record must not affect live trading."""
        try:
            entry_price = float(price if price is not None else signal["price"])
            stop_distance, target_distance = bracket_distances(
                entry_price,
                float(signal["atr"]),
                self.config["atr_stop_mult"],
                self.config["atr_target_mult"],
            )
            qty = self.market.size_qty(
                signal["symbol"],
                self.config.get("position_size_usd", 500.0),
                entry_price,
                stop_distance,
                self.config.get("risk_per_trade_usd", 20.0),
            )
            if qty <= 0:
                return  # unsizeable — this trade could never have happened
            if signal.get("side", "BUY") == "BUY":
                stop_loss = entry_price - stop_distance
                take_profit = entry_price + target_distance
            else:
                stop_loss = entry_price + stop_distance
                take_profit = entry_price - target_distance
            self.db.record_veto(
                symbol=signal["symbol"],
                side=signal.get("side", "BUY"),
                gate=gate,
                reason=str(reason)[:300],
                price=entry_price,
                qty=qty,
                stop_loss=stop_loss,
                take_profit=take_profit,
                dedupe_minutes=self.config.get("cooldown_minutes", 30.0),
            )
        except Exception as exc:
            logger.error(
                "Veto recording failed for %s: %s", signal.get("symbol"), exc
            )

    async def _resolve_vetoes(self) -> None:
        """Resolve shadow-tracked vetoed signals against market data.

        Replays each unresolved veto's bracket over the minute bars after
        its timestamp: pessimistic first-touch (a bar that spans both levels
        counts as a stop), the same friction model as the optimizer backtest
        (COST_PER_TRADE_PCT round-trip, stops slip STOP_SLIPPAGE_PCT), an
        end-of-day close for equities (the live engine never holds
        overnight) and a 24-hour horizon for crypto. Deliberately simpler
        than the live exit stack — no RSI signal exit — so a resolved
        outcome means "what the bracket would have done", which if anything
        FLATTERS the veto: the live engine banks winners early."""
        try:
            vetoes = self.db.get_unresolved_vetoes()
            if not vetoes:
                return
            # Price hypothetical fills with the optimizer's fill-calibrated
            # friction when available (optimizer_friction state, v2.27.0);
            # the env-default constants remain the fallback — notably for
            # the crypto engine, which runs no optimizer.
            friction = self.db.get_state("optimizer_friction") or {}
            stop_slip = float(
                friction.get("stop_slippage_pct") or STOP_SLIPPAGE_PCT
            )
            cost_pct = float(
                friction.get("cost_per_trade_pct") or COST_PER_TRADE_PCT
            )
            symbols = sorted({v["symbol"] for v in vetoes})
            earliest = min(
                datetime.fromisoformat(v["ts"]) for v in vetoes
            ) - timedelta(minutes=1)
            frames = await asyncio.to_thread(
                self.market.fetch_bars, symbols, earliest
            )
            now = datetime.now(timezone.utc)
            for veto in vetoes:
                resolution = self._resolve_one_veto(
                    veto, frames.get(veto["symbol"]), now,
                    stop_slippage_pct=stop_slip, cost_per_trade_pct=cost_pct,
                )
                if resolution is not None:
                    outcome, exit_price, hypo_pnl = resolution
                    self.db.resolve_veto(
                        veto["id"], outcome, exit_price, hypo_pnl
                    )
        except Exception as exc:
            logger.error("Veto resolution failed: %s", exc)

    def _resolve_one_veto(
        self,
        veto: Dict[str, Any],
        bars: Optional[pd.DataFrame],
        now: datetime,
        stop_slippage_pct: float = STOP_SLIPPAGE_PCT,
        cost_per_trade_pct: float = COST_PER_TRADE_PCT,
    ) -> Optional[Tuple[str, Optional[float], Optional[float]]]:
        """(outcome, exit_price, hypo_pnl) for one veto, or None to retry
        later (its trading horizon is still open)."""
        entry_ts = datetime.fromisoformat(veto["ts"])
        is_long = veto["side"] == "BUY"
        entry = float(veto["price"])
        qty = float(veto["qty"])
        stop = float(veto["stop_loss"])
        target = float(veto["take_profit"])

        # Horizon: equities are flattened at the end of the entry's ET day;
        # crypto positions are held at most 24 hours for this simulation.
        if self.market.flatten_before_close:
            entry_et = entry_ts.astimezone(US_EASTERN)
            now_et = now.astimezone(US_EASTERN)
            horizon_over = now_et.date() > entry_et.date() or (
                now_et.date() == entry_et.date() and now_et.hour >= 20
            )
        else:
            horizon_over = now - entry_ts >= timedelta(hours=24)

        if bars is None or bars.empty:
            return ("no_data", None, None) if horizon_over else None
        index = bars.index
        if getattr(index, "tz", None) is None:
            index = index.tz_localize(timezone.utc)
            bars = bars.set_axis(index)
        window = bars[index > entry_ts]
        if self.market.flatten_before_close and not window.empty:
            # The live engine never holds overnight — only the entry's own
            # ET session day counts.
            et_dates = window.index.tz_convert("America/New_York").date
            window = window[et_dates == entry_ts.astimezone(US_EASTERN).date()]
        elif not window.empty:
            window = window[window.index <= entry_ts + timedelta(hours=24)]
        if window.empty:
            return ("no_data", None, None) if horizon_over else None

        exit_price: Optional[float] = None
        outcome: Optional[str] = None
        for high, low in zip(window["high"], window["low"]):
            if is_long:
                if low <= stop:  # pessimistic: stop before target intra-bar
                    outcome, exit_price = "stop", stop
                    break
                if high >= target:
                    outcome, exit_price = "target", target
                    break
            else:
                if high >= stop:
                    outcome, exit_price = "stop", stop
                    break
                if low <= target:
                    outcome, exit_price = "target", target
                    break
        if outcome is None:
            if not horizon_over:
                return None
            outcome = "eod" if self.market.flatten_before_close else "timeout"
            exit_price = float(window["close"].iloc[-1])

        # Same friction model as the optimizer backtest: round-trip cost on
        # notional, and stop fills slip through the level against the trade.
        if outcome == "stop":
            exit_price = (
                exit_price * (1.0 - stop_slippage_pct)
                if is_long
                else exit_price * (1.0 + stop_slippage_pct)
            )
        gross = (
            (exit_price - entry) * qty if is_long else (entry - exit_price) * qty
        )
        hypo_pnl = gross - cost_per_trade_pct * entry * qty
        return (outcome, exit_price, hypo_pnl)

    # ------------------------------------------------------------------ #
    # sentiment filter
    # ------------------------------------------------------------------ #

    async def process_news_sentiment(self, symbol: str) -> Tuple[float, str]:
        """Score curated news sentiment for a symbol, in [0, 1].

        Delegates to the layered pipeline in sentiment.py: Alpaca news
        headlines scored by Claude when ANTHROPIC_API_KEY is set, keyword
        heuristic otherwise, neutral 0.5 when there is no news. Returns
        (score, source). Called only after the technical trigger fired,
        and cached per symbol, to keep LLM cost negligible.
        """
        result = await asyncio.to_thread(self.sentiment.score, symbol)
        return float(result["score"]), str(result["source"])

    # ------------------------------------------------------------------ #
    # market data & signals
    # ------------------------------------------------------------------ #

    async def fetch_minute_bars(
        self, symbols: Optional[List[str]] = None
    ) -> Dict[str, pd.DataFrame]:
        """Fetch recent 1-minute bars for a set of symbols in one request.

        Defaults to the whole watchlist (entry evaluation); the exit phase
        passes the held symbols explicitly, which in whole-market mode may
        no longer be on the current most-actives watchlist.
        """
        targets = self.watchlist if symbols is None else symbols
        if not targets:
            return {}
        start = datetime.now(timezone.utc) - timedelta(minutes=self.config.get("bar_lookback_minutes", 180))
        try:
            return await asyncio.to_thread(self.market.fetch_bars, list(targets), start)
        except Exception as exc:
            logger.error("Failed to fetch 1-minute bars: %s", exc)
            self.db.add_log("ERROR", f"Bar fetch failed: {exc}")
            return {}

    async def evaluate_signal(
        self, symbol: str, bars: pd.DataFrame
    ) -> Optional[Dict[str, Any]]:
        """Layered BUY/SELL decision: RSI trigger → cooldown → VWAP
        confirmation → news sentiment. Cheap technical gates run first so
        the LLM is only consulted for genuine candidates."""
        period = max(int(self.config["rsi_period"]), 2)
        if len(bars) < period * 2:
            return None

        rsi_series = compute_rsi(bars["close"], period)
        latest_rsi = float(rsi_series.iloc[-1])
        latest_close = float(bars["close"].iloc[-1])
        if np.isnan(latest_rsi) or latest_close < self.config.get("min_price_usd", 5.0):
            return None

        vwap = float(compute_vwap(bars).iloc[-1])
        atr = float(compute_atr(bars).iloc[-1])
        if np.isnan(atr) or atr <= 0:
            return None

        # Too-quiet gate: when the percentage floor, not ATR, would set the
        # stop distance, the bracket is inside ordinary bar noise and the
        # trade is a coin flip that loses the spread (this pattern produced
        # most of the 2026-07-08 losers). Skip before any LLM cost is spent.
        if stop_is_floored(latest_close, atr, self.config["atr_stop_mult"]):
            return None

        cooldown_left = self.in_cooldown(symbol)
        if cooldown_left is not None:
            self.db.add_log(
                "INFO",
                f"{symbol}: RSI {latest_rsi:.1f} triggered but symbol is in "
                f"post-loss cooldown for another {cooldown_left:.0f}m — skipped",
            )
            return None

        # --- LONG signal: RSI oversold + price below VWAP (dip) ---
        if latest_rsi < self.config["rsi_buy_signal"]:
            if latest_close > vwap:
                self.db.add_log(
                    "INFO",
                    f"{symbol}: RSI {latest_rsi:.1f} triggered BUY but price "
                    f"${latest_close:.2f} is above VWAP ${vwap:.2f} — not a real "
                    f"dip, skipped",
                )
                return None

            # Falling-knife gate: a genuine dip sits just under VWAP; a price
            # far below it is a collapse in progress, and RSI-oversold entries
            # into that keep falling (see 2026-07-09 VRAX: 24% below VWAP, RSI
            # 26 → −$23 as RSI bled to 13). Reject before any LLM cost.
            dislocation = (vwap - latest_close) / vwap
            max_dislocation = self.config.get("max_vwap_dislocation_pct", 0.15)
            if dislocation > max_dislocation:
                self.db.add_log(
                    "INFO",
                    f"{symbol}: RSI {latest_rsi:.1f} triggered BUY but price "
                    f"${latest_close:.2f} is {dislocation * 100:.0f}% below VWAP "
                    f"${vwap:.2f} (>{max_dislocation * 100:.0f}% cap) — falling "
                    f"knife, skipped",
                )
                return None

            sentiment, source = await self.process_news_sentiment(symbol)
            if sentiment <= self.config["news_cutoff"]:
                self.db.add_log(
                    "INFO",
                    f"{symbol}: RSI {latest_rsi:.1f} triggered BUY but sentiment "
                    f"{sentiment:.2f} ({source}) <= cutoff "
                    f"{self.config['news_cutoff']:.2f} — skipped",
                )
                self._record_veto(
                    {"symbol": symbol, "side": "BUY",
                     "price": latest_close, "atr": atr},
                    "sentiment",
                    f"sentiment {sentiment:.2f} ({source}) <= cutoff "
                    f"{self.config['news_cutoff']:.2f}",
                )
                return None

            return {
                "symbol": symbol,
                "side": "BUY",
                "price": latest_close,
                "rsi": latest_rsi,
                "vwap": vwap,
                "atr": atr,
                "sentiment": sentiment,
                "sentiment_source": source,
                "rsi_buy_signal": self.config["rsi_buy_signal"],
                "rsi_exit_signal": self.config["rsi_exit_signal"],
                "rsi_short_signal": self.config.get("rsi_short_signal", 80.0),
                "rsi_short_exit": self.config.get("rsi_short_exit", 20.0),
            }

        # --- SHORT signal: RSI overbought + price above VWAP (overextended) ---
        short_enabled = bool(self.config.get("short_enabled", 0.0))
        if short_enabled and latest_rsi > self.config["rsi_short_signal"]:
            if latest_close < vwap:
                self.db.add_log(
                    "INFO",
                    f"{symbol}: RSI {latest_rsi:.1f} triggered SELL but price "
                    f"${latest_close:.2f} is below VWAP ${vwap:.2f} — not a real "
                    f"overextension, skipped",
                )
                return None

            # Mirror of the long falling-knife gate: a price far above VWAP is
            # a parabolic squeeze, not an orderly overextension, and shorting
            # into it gets run over the same way a dip-buy catches a knife.
            dislocation = (latest_close - vwap) / vwap
            max_dislocation = self.config.get("max_vwap_dislocation_pct", 0.15)
            if dislocation > max_dislocation:
                self.db.add_log(
                    "INFO",
                    f"{symbol}: RSI {latest_rsi:.1f} triggered SELL but price "
                    f"${latest_close:.2f} is {dislocation * 100:.0f}% above VWAP "
                    f"${vwap:.2f} (>{max_dislocation * 100:.0f}% cap) — parabolic "
                    f"squeeze, skipped",
                )
                return None

            sentiment, source = await self.process_news_sentiment(symbol)
            # Shorts use the mirror of the long gate: longs need
            # sentiment > news_cutoff, shorts need sentiment below
            # 1 - news_cutoff. Both cutoffs sit the same distance from
            # neutral, so a no-news 0.5 passes both sides — only actively
            # bullish headlines block a short, exactly as only actively
            # bearish headlines block a long.
            short_cutoff = 1.0 - self.config["news_cutoff"]
            if sentiment >= short_cutoff:
                self.db.add_log(
                    "INFO",
                    f"{symbol}: RSI {latest_rsi:.1f} triggered SELL but sentiment "
                    f"{sentiment:.2f} ({source}) >= short cutoff "
                    f"{short_cutoff:.2f} — too bullish to short, skipped",
                )
                self._record_veto(
                    {"symbol": symbol, "side": "SELL",
                     "price": latest_close, "atr": atr},
                    "sentiment",
                    f"sentiment {sentiment:.2f} ({source}) >= short cutoff "
                    f"{short_cutoff:.2f}",
                )
                return None

            return {
                "symbol": symbol,
                "side": "SELL",
                "price": latest_close,
                "rsi": latest_rsi,
                "vwap": vwap,
                "atr": atr,
                "sentiment": sentiment,
                "sentiment_source": source,
                "rsi_buy_signal": self.config["rsi_buy_signal"],
                "rsi_exit_signal": self.config["rsi_exit_signal"],
                "rsi_short_signal": self.config.get("rsi_short_signal", 80.0),
                "rsi_short_exit": self.config.get("rsi_short_exit", 20.0),
            }

        return None

    def evaluate_exit(
        self, bars: pd.DataFrame, side: str
    ) -> Optional[Dict[str, Any]]:
        """Early-exit decision for a held position.

        Long: the mean reversion has run its course once RSI recovers to the
        overbought exit level. Short: the overextension has corrected once
        RSI drops to the oversold exit level. Returns exit context, or None
        to keep holding. The bracket's stop/target still guard the position
        independently; this only banks the reversion sooner."""
        period = max(int(self.config["rsi_period"]), 2)
        if len(bars) < period * 2:
            return None
        latest_rsi = float(compute_rsi(bars["close"], period).iloc[-1])
        if np.isnan(latest_rsi):
            return None
        if side == "BUY":
            if latest_rsi < self.config["rsi_exit_signal"]:
                return None
            return {"rsi": latest_rsi, "close": float(bars["close"].iloc[-1])}
        else:
            if latest_rsi > self.config["rsi_short_exit"]:
                return None
            return {"rsi": latest_rsi, "close": float(bars["close"].iloc[-1])}

    # ------------------------------------------------------------------ #
    # order placement
    # ------------------------------------------------------------------ #

    async def _entry_reference_price(
        self, symbol: str, side: OrderSide, fallback: float
    ) -> float:
        """Marketable reference price for an entry/close limit: the opposite
        side of the live quote (ask to buy, bid to sell) so the limit crosses
        the spread and fills. The last *trade* can be minutes stale on a thin
        book — that left GTC crypto entries resting unfilled. Falls back to the
        latest trade, then the caller's bar price, when no quote is available."""
        try:
            quote = await asyncio.to_thread(self.market.latest_quote, symbol)
        except Exception as exc:
            logger.warning("Quote fetch failed for %s: %s", symbol, exc)
            quote = None
        if quote is not None:
            bid, ask = quote
            px = ask if side == OrderSide.BUY else bid
            if px and px > 0:
                return float(px)
        try:
            fresh = await asyncio.to_thread(self.market.latest_price, symbol)
            if fresh is not None:
                return float(fresh)
        except Exception as exc:
            logger.warning(
                "Latest-trade fetch failed for %s, using bar price: %s",
                symbol, exc,
            )
        return fallback

    def _bracket_levels(
        self,
        symbol: str,
        price: float,
        stop_distance: float,
        target_distance: float,
        is_long: bool,
    ) -> Tuple[float, float]:
        """Round the soft take-profit / stop-loss to the market's own price
        tick (cents for equities, the pair's price increment for crypto) and
        guarantee rounding never collapses a level onto the entry price. A flat
        round(2) / 2¢ floor put a sub-dollar crypto stop dollars from the entry.
        Returns (take_profit, stop_loss)."""
        tick = self.market.min_tick(symbol)
        if is_long:
            take_profit = self.market.round_price(symbol, price + target_distance)
            stop_loss = self.market.round_price(symbol, price - stop_distance)
            take_profit = max(
                take_profit, self.market.round_price(symbol, price + 2 * tick)
            )
            stop_loss = min(
                stop_loss, self.market.round_price(symbol, price - 2 * tick)
            )
        else:
            take_profit = self.market.round_price(symbol, price - target_distance)
            stop_loss = self.market.round_price(symbol, price + stop_distance)
            take_profit = min(
                take_profit, self.market.round_price(symbol, price - 2 * tick)
            )
            stop_loss = max(
                stop_loss, self.market.round_price(symbol, price + 2 * tick)
            )
        return take_profit, stop_loss

    async def _entry_filled_qty(
        self, symbol: str, entry: Dict[str, Any]
    ) -> float:
        """Filled quantity of a tracked entry's own order (0.0 when it never
        filled or the status can't be fetched) — the test for whether a vanished
        tracked symbol was ever a real position or just an unfilled order."""
        order_id = entry.get("_entry_order_id")
        if not order_id:
            return 0.0
        try:
            order = await asyncio.to_thread(self.trading.get_order_by_id, order_id)
        except Exception as exc:
            logger.warning(
                "Entry-order status fetch failed for %s: %s", symbol, exc
            )
            return 0.0
        try:
            return float(order.filled_qty or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _vwap_revalidate(
        self, signal: Dict[str, Any], live_price: float
    ) -> bool:
        """Re-check the VWAP direction and dislocation gates against the
        live-repriced entry price. evaluate_signal's VWAP gate ran on the
        last completed 1-minute bar close, but place_limit_buy re-prices
        off the live quote (ask to buy, bid to sell) — on a volatile
        symbol that ask can be several percent above the bar close by the
        time the order is built, so a dip that passed the gate can already
        be above VWAP at the actual entry (see the 2026-07-10 entry that
        passed at $62.50 bar close but filled at $66.41 ask, 6% above
        VWAP, then stopped out instantly). Aborts the entry (returns
        False, with a diagnostic log) when the live price no longer
        satisfies the same direction + falling-knife/parabolic gates the
        bar close passed. Session VWAP drifts slowly (volume-weighted over
        the whole day), so re-checking against the signal's VWAP is
        sufficient — the failure mode is the price moving, not the VWAP."""
        symbol = signal["symbol"]
        side = signal["side"]
        vwap = signal["vwap"]
        bar_close = signal["price"]
        max_dislocation = self.config.get("max_vwap_dislocation_pct", 0.15)

        if side == "BUY":
            if live_price >= vwap:
                self.db.add_log(
                    "INFO",
                    f"{symbol}: VWAP re-check aborted BUY — live ask "
                    f"${live_price:.2f} is at/above VWAP ${vwap:.2f} "
                    f"(gate passed on bar close ${bar_close:.2f}); the dip "
                    f"has reverted, skipping entry",
                )
                return False
            dislocation = (vwap - live_price) / vwap
            if dislocation > max_dislocation:
                self.db.add_log(
                    "INFO",
                    f"{symbol}: VWAP re-check aborted BUY — live ask "
                    f"${live_price:.2f} is {dislocation * 100:.0f}% below "
                    f"VWAP ${vwap:.2f} (>{max_dislocation * 100:.0f}% cap) "
                    f"— falling knife on re-pricing, skipping entry",
                )
                return False
        else:  # SELL
            if live_price <= vwap:
                self.db.add_log(
                    "INFO",
                    f"{symbol}: VWAP re-check aborted SELL — live bid "
                    f"${live_price:.2f} is at/below VWAP ${vwap:.2f} "
                    f"(gate passed on bar close ${bar_close:.2f}); the "
                    f"overextension has reverted, skipping entry",
                )
                return False
            dislocation = (live_price - vwap) / vwap
            if dislocation > max_dislocation:
                self.db.add_log(
                    "INFO",
                    f"{symbol}: VWAP re-check aborted SELL — live bid "
                    f"${live_price:.2f} is {dislocation * 100:.0f}% above "
                    f"VWAP ${vwap:.2f} (>{max_dislocation * 100:.0f}% cap) "
                    f"— parabolic on re-pricing, skipping entry",
                )
                return False
        return True

    async def place_limit_buy(self, signal: Dict[str, Any]) -> None:
        symbol = signal["symbol"]
        price = signal["price"]

        # The signal price comes from the last completed 1-minute bar, which
        # can be up to poll_interval_seconds stale. Re-price against the live
        # quote (the ask a buy would cross) immediately before submission so
        # the marketable limit actually fills — the last *trade* can be minutes
        # old on a thin book, which left GTC crypto entries resting unfilled and
        # stacking duplicates every cycle.
        price = await self._entry_reference_price(symbol, OrderSide.BUY, price)

        # Re-validate the VWAP gate against the live-repriced price before
        # building the order: evaluate_signal's VWAP check ran on the bar
        # close, but the ask can be materially higher by submission time on
        # a volatile symbol, so a "dip below VWAP" that passed on stale bar
        # data can already be above VWAP at the actual entry (see
        # 2026-07-10: passed at $62.50 bar close, filled at $66.41 ask).
        if not self._vwap_revalidate(signal, price):
            self._record_veto(
                signal,
                "vwap_recheck",
                f"live ask ${price:.2f} failed the VWAP gate the bar close "
                f"${signal['price']:.2f} passed (VWAP ${signal['vwap']:.2f})",
                price=price,
            )
            return

        # Stop/target scale with the symbol's own volatility (ATR multiples
        # tuned nightly by the optimizer, floored in indicators.py).
        stop_distance, target_distance = bracket_distances(
            price,
            signal["atr"],
            self.config["atr_stop_mult"],
            self.config["atr_target_mult"],
        )

        # Volatility-scaled sizing: risk a roughly constant dollar amount per
        # trade, capped by the notional position size. Whole shares (equity) or
        # fractional units (crypto) per the market adapter.
        pos_size = self.config.get("position_size_usd", 500.0)
        risk_per = self.config.get("risk_per_trade_usd", 20.0)
        qty = self.market.size_qty(symbol, pos_size, price, stop_distance, risk_per)
        if qty <= 0:
            self.db.add_log(
                "WARNING",
                f"{symbol}: cannot size a position within notional "
                f"${pos_size:.0f} and risk ${risk_per:.0f} "
                f"(price ${price:.2f}, stop distance ${stop_distance:.2f})",
            )
            return

        take_profit, stop_loss = self._bracket_levels(
            symbol, price, stop_distance, target_distance, is_long=True
        )

        # Regular session: the stop/target rest ON the exchange as OCO
        # bracket legs, so a breach fills immediately instead of waiting out
        # the poll cycle (Jul 8–10: polled soft stops slipped 2–4× their
        # designed risk). Outside the regular session (or crypto) Alpaca
        # rejects brackets, so the marketable-limit entry stands alone and
        # the levels are enforced as soft checks each cycle
        # (see evaluate_and_close_stops).
        native_bracket = self.market.bracket_entry_allowed()
        if native_bracket:
            order_request = self.market.build_bracket_entry_order(
                symbol, OrderSide.BUY, qty, price,
                self.config.get("entry_slip_pct", 0.001),
                take_profit, stop_loss,
            )
            # Record the exact resting-leg prices (clamped a tick past the
            # parent limit) so trade records and the soft-stop fallback that
            # takes over after the legs expire match the exchange.
            take_profit = float(order_request.take_profit.limit_price)
            stop_loss = float(order_request.stop_loss.stop_price)
        else:
            order_request = self.market.build_entry_order(
                symbol, OrderSide.BUY, qty, price,
                self.config.get("entry_slip_pct", 0.001),
            )
        try:
            order = await asyncio.to_thread(self.trading.submit_order, order_request)
        except Exception as exc:
            logger.error("Order submission failed for %s: %s", symbol, exc)
            self.db.add_log("ERROR", f"{symbol}: order submission failed: {exc}")
            return

        self._open_entries[symbol] = {
            "qty": float(qty),
            "entry_price": price,
            "side": "BUY",
            "_entry_order_id": getattr(order, "id", None),
            "entry_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "entry_rsi": signal["rsi"],
            "entry_vwap": signal["vwap"],
            "entry_atr": signal["atr"],
            "entry_sentiment": signal["sentiment"],
            "sentiment_source": signal.get("sentiment_source"),
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "native_bracket": native_bracket,
            "entry_reason": (
                f"RSI {signal['rsi']:.1f} was oversold (below the "
                f"{self.config['rsi_buy_signal']:.0f} buy level) while the "
                f"live ask ${price:.2f} sat "
                f"{(signal['vwap'] - price) / signal['vwap'] * 100:.1f}% below "
                f"VWAP ${signal['vwap']:.2f} — a genuine mean-reversion dip, "
                f"not a falling knife (the bar close ${signal['price']:.2f} "
                f"that first triggered the gate was "
                f"{(signal['vwap'] - signal['price']) / signal['vwap'] * 100:.1f}% "
                f"below VWAP). News sentiment {signal['sentiment']:.2f} "
                f"({signal.get('sentiment_source', '?')}) cleared the "
                f"{self.config['news_cutoff']:.2f} cutoff, so a long was "
                f"opened with a {self.config['atr_stop_mult']:.1f}×ATR stop "
                f"and {self.config['atr_target_mult']:.1f}×ATR target"
                + (
                    " resting on the exchange as bracket legs."
                    if native_bracket
                    else " enforced as soft levels each cycle."
                )
            ),
        }
        self.db.add_log(
            "TRADE",
            f"BUY {qty:g} {symbol} @ ~${price:.2f} | RSI {signal['rsi']:.1f} | "
            f"VWAP ${signal['vwap']:.2f} | ATR ${signal['atr']:.3f} | "
            f"sentiment {signal['sentiment']:.2f} "
            f"({signal.get('sentiment_source', '?')}) | TP ${take_profit:.2f} | "
            f"SL ${stop_loss:.2f} "
            f"({'exchange bracket' if native_bracket else 'soft levels'}) | "
            f"risk ~${qty * stop_distance:.0f} | "
            f"order {getattr(order, 'id', '?')}",
        )
        logger.info("Submitted limit BUY for %s x%g @ ~%.2f", symbol, qty, price)

    async def place_limit_short(self, signal: Dict[str, Any]) -> None:
        """Submit an extended-hours limit SELL (short) order. Take-profit sits
        below entry and stop-loss above; both are soft levels enforced each
        cycle rather than resting bracket legs (forbidden in extended hours)."""
        symbol = signal["symbol"]
        price = signal["price"]

        # Re-price against the live bid a short sells into, so the marketable
        # limit crosses and fills (see place_limit_buy).
        price = await self._entry_reference_price(symbol, OrderSide.SELL, price)

        # Re-validate the VWAP gate against the live-repriced price (mirror
        # of the long-side re-check in place_limit_buy).
        if not self._vwap_revalidate(signal, price):
            self._record_veto(
                signal,
                "vwap_recheck",
                f"live bid ${price:.2f} failed the VWAP gate the bar close "
                f"${signal['price']:.2f} passed (VWAP ${signal['vwap']:.2f})",
                price=price,
            )
            return

        stop_distance, target_distance = bracket_distances(
            price,
            signal["atr"],
            self.config["atr_stop_mult"],
            self.config["atr_target_mult"],
        )

        pos_size = self.config.get("position_size_usd", 500.0)
        risk_per = self.config.get("risk_per_trade_usd", 20.0)
        qty = self.market.size_qty(symbol, pos_size, price, stop_distance, risk_per)
        if qty <= 0:
            self.db.add_log(
                "WARNING",
                f"{symbol}: cannot size a position within notional "
                f"${pos_size:.0f} and risk ${risk_per:.0f} "
                f"(price ${price:.2f}, stop distance ${stop_distance:.2f})",
            )
            return

        # Short: take-profit BELOW entry, stop-loss ABOVE, rounded to the
        # market's own tick.
        take_profit, stop_loss = self._bracket_levels(
            symbol, price, stop_distance, target_distance, is_long=False
        )

        # Regular session: exchange-side OCO bracket legs; otherwise a plain
        # marketable limit with soft-level enforcement (see place_limit_buy).
        # Crypto never shorts — short_enabled is off in its config.
        native_bracket = self.market.bracket_entry_allowed()
        if native_bracket:
            order_request = self.market.build_bracket_entry_order(
                symbol, OrderSide.SELL, qty, price,
                self.config.get("entry_slip_pct", 0.001),
                take_profit, stop_loss,
            )
            take_profit = float(order_request.take_profit.limit_price)
            stop_loss = float(order_request.stop_loss.stop_price)
        else:
            order_request = self.market.build_entry_order(
                symbol, OrderSide.SELL, qty, price,
                self.config.get("entry_slip_pct", 0.001),
            )
        try:
            order = await asyncio.to_thread(self.trading.submit_order, order_request)
        except Exception as exc:
            logger.error("Short order submission failed for %s: %s", symbol, exc)
            self.db.add_log("ERROR", f"{symbol}: short order submission failed: {exc}")
            return

        self._open_entries[symbol] = {
            "qty": float(qty),
            "entry_price": price,
            "side": "SELL",
            "_entry_order_id": getattr(order, "id", None),
            "entry_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "entry_rsi": signal["rsi"],
            "entry_vwap": signal["vwap"],
            "entry_atr": signal["atr"],
            "entry_sentiment": signal["sentiment"],
            "sentiment_source": signal.get("sentiment_source"),
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "native_bracket": native_bracket,
            "entry_reason": (
                f"RSI {signal['rsi']:.1f} was overbought (above the "
                f"{self.config['rsi_short_signal']:.0f} short level) while "
                f"the live bid ${price:.2f} sat "
                f"{(price - signal['vwap']) / signal['vwap'] * 100:.1f}% above "
                f"VWAP ${signal['vwap']:.2f} — an orderly overextension, not "
                f"a parabolic squeeze (the bar close ${signal['price']:.2f} "
                f"that first triggered the gate was "
                f"{(signal['price'] - signal['vwap']) / signal['vwap'] * 100:.1f}% "
                f"above VWAP). News sentiment {signal['sentiment']:.2f} "
                f"({signal.get('sentiment_source', '?')}) stayed below the "
                f"{1.0 - self.config['news_cutoff']:.2f} short cutoff (not "
                f"too bullish to fade), so a short was opened with a "
                f"{self.config['atr_stop_mult']:.1f}×ATR stop and "
                f"{self.config['atr_target_mult']:.1f}×ATR target"
                + (
                    " resting on the exchange as bracket legs."
                    if native_bracket
                    else " enforced as soft levels each cycle."
                )
            ),
        }
        self.db.add_log(
            "TRADE",
            f"SELL {qty:g} {symbol} @ ~${price:.2f} | RSI {signal['rsi']:.1f} | "
            f"VWAP ${signal['vwap']:.2f} | ATR ${signal['atr']:.3f} | "
            f"sentiment {signal['sentiment']:.2f} "
            f"({signal.get('sentiment_source', '?')}) | TP ${take_profit:.2f} | "
            f"SL ${stop_loss:.2f} "
            f"({'exchange bracket' if native_bracket else 'soft levels'}) | "
            f"risk ~${qty * stop_distance:.0f} | "
            f"order {getattr(order, 'id', '?')}",
        )
        logger.info("Submitted limit SELL for %s x%g @ ~%.2f", symbol, qty, price)

    # ------------------------------------------------------------------ #
    # signal-driven exits
    # ------------------------------------------------------------------ #

    async def evaluate_and_close_exits(
        self, portfolio: Dict[str, Any], frames: Dict[str, pd.DataFrame]
    ) -> Dict[str, str]:
        """Check each held position for an early-exit signal and close the ones
        that fire. Deliberately runs before the entry gates so a full book
        or a TREND_DOWN tape can still take profit — exits are never blocked
        by the conditions that only govern new entries."""
        exits: Dict[str, str] = {}
        for position in portfolio["positions"]:
            symbol = position["symbol"]
            if symbol in self._closing:
                continue
            qty = float(position.get("qty", 0.0))
            if qty == 0:
                continue
            side = "BUY" if qty > 0 else "SELL"
            bars = frames.get(symbol)
            if bars is None or bars.empty:
                continue
            exit_signal = self.evaluate_exit(bars, side)
            if exit_signal is None:
                continue
            await self.close_position_now(symbol, position, exit_signal)
            exits[symbol] = f"signal-exit-{side}"
        return exits

    async def _latest_price(
        self,
        symbol: str,
        frames: Dict[str, pd.DataFrame],
        position: Dict[str, Any],
    ) -> Optional[float]:
        """Best available current price for stop/target checks, freshest
        source first: the live quote's exit side (the bid a long would sell
        into, the ask a short must buy back). The 1-minute bar close can be
        tens of seconds stale on a thin book — exactly when a breached stop
        most needs catching (Jul 8–10: soft stops detected 2–4× past their
        level off stale bar closes). Falls back to the bar close, then the
        latest trade, then the position's last synced price."""
        is_long = float(position.get("qty", 0.0)) > 0
        try:
            quote = await asyncio.to_thread(self.market.latest_quote, symbol)
        except Exception as exc:
            logger.warning("Quote fetch failed for %s: %s", symbol, exc)
            quote = None
        if quote is not None:
            bid, ask = quote
            price = bid if is_long else ask
            if price and price > 0:
                return float(price)
        bars = frames.get(symbol)
        if bars is not None and not bars.empty:
            return float(bars["close"].iloc[-1])
        try:
            price = await asyncio.to_thread(self.market.latest_price, symbol)
            if price is not None:
                return price
        except Exception as exc:
            logger.warning("Latest-price fetch failed for %s: %s", symbol, exc)
        current = position.get("current_price")
        return None if current is None else float(current)

    async def evaluate_and_close_stops(
        self, portfolio: Dict[str, Any], frames: Dict[str, pd.DataFrame]
    ) -> Dict[str, str]:
        """Protective soft stop/target for held positions — the fallback for
        the exchange-side bracket, which Alpaca forbids outside the regular
        session.

        Each cycle, compare the latest price against the stop_loss/take_profit
        recorded at entry and close the position when a level is breached. This
        is polled every poll_interval_seconds, so price can gap through a level
        between checks — the accepted tradeoff for trading the extended
        session. Positions entered with a native bracket are skipped while
        their legs rest on the exchange (regular session); the DAY legs die at
        the regular close, after which this soft enforcement resumes for them
        automatically. Runs before the entry gates so a stop is always
        honoured."""
        exits: Dict[str, str] = {}
        native_bracket_resting = self.market.bracket_entry_allowed()
        for position in portfolio["positions"]:
            symbol = position["symbol"]
            if symbol in self._closing:
                continue
            qty = float(position.get("qty", 0.0))
            if qty == 0:
                continue
            entry = self._open_entries.get(symbol, {})
            stop_loss = entry.get("stop_loss")
            take_profit = entry.get("take_profit")
            if stop_loss is None and take_profit is None:
                continue  # adopted/legacy position with no recorded levels
            if entry.get("native_bracket") and native_bracket_resting:
                # The exchange enforces this position's stop/target via its
                # resting OCO legs; racing them with a soft close would
                # double-sell. (Once the regular session ends the legs have
                # expired and the skip stops applying.)
                continue
            price = await self._latest_price(symbol, frames, position)
            if price is None:
                continue
            side = "BUY" if qty > 0 else "SELL"
            hit: Optional[str] = None
            if side == "BUY":
                if stop_loss is not None and price <= stop_loss:
                    hit = "stop"
                elif take_profit is not None and price >= take_profit:
                    hit = "target"
            else:
                if stop_loss is not None and price >= stop_loss:
                    hit = "stop"
                elif take_profit is not None and price <= take_profit:
                    hit = "target"
            if hit is None:
                continue
            level = stop_loss if hit == "stop" else take_profit
            if symbol in self._open_entries:
                verb = "stop-loss" if hit == "stop" else "take-profit"
                self._open_entries[symbol]["exit_reason"] = (
                    f"Soft {verb} hit: price ~${price:.2f} reached the "
                    f"${level:.2f} {verb} level."
                )
            order = await self._limit_close(symbol, position)
            if order is None:
                # Close submission failed — _limit_close already logged and
                # counted it; logging the SOFT STOP line here too would spam
                # an identical TRADE entry every cycle until the close lands.
                continue
            exits[symbol] = f"soft-{hit}-{side}"
            self.db.add_log(
                "TRADE",
                f"SOFT {hit.upper()} {symbol} x{abs(qty):g} ({side}) @ "
                f"~${price:.2f} — ${level:.2f} {hit} level breached",
            )
        return exits

    async def _ensure_position_protection(
        self, portfolio: Dict[str, Any], frames: Dict[str, pd.DataFrame]
    ) -> None:
        """Watchdog: every held position must have working protection.

        The soft-stop loop can only enforce levels that exist and are being
        checked; two holes let positions run naked in practice (VRAX -$101.46
        equity, XTZ -$16.96 crypto): an adopted position with no recorded
        stop/target is skipped by the soft loop forever, and a position
        marked native_bracket is skipped while its exchange legs are assumed
        to rest — even if those legs are gone. Each cycle this (a) re-arms
        soft enforcement when a native bracket's legs are no longer resting,
        and (b) attaches ATR-scaled levels (anchored at the CURRENT price —
        protecting from here, not locking in the drawdown a naked position
        already suffered) to any position without levels. If levels cannot
        be computed for 3 consecutive cycles the position is closed: unmanageable
        is worse than closed. Never raises."""
        try:
            checked_brackets: List[str] = []
            for position in portfolio["positions"]:
                symbol = position["symbol"]
                qty = float(position.get("qty", 0.0))
                if symbol in self._closing or qty == 0:
                    continue
                entry = self._open_entries.get(symbol)
                if entry is None:
                    continue  # adopted next sync; protected the cycle after
                has_levels = (
                    entry.get("stop_loss") is not None
                    or entry.get("take_profit") is not None
                )
                if has_levels:
                    if entry.get("native_bracket") and self.market.bracket_entry_allowed():
                        checked_brackets.append(symbol)
                    continue
                await self._attach_protective_levels(symbol, position, frames)

            # Trust-but-verify the exchange-side brackets in one batched
            # order lookup: a position whose OCO legs are gone (cancelled,
            # rejected, expired early) would otherwise sit in the soft-stop
            # loop's skip branch with nothing enforcing its levels.
            if checked_brackets:
                open_orders = await asyncio.to_thread(
                    self.trading.get_orders,
                    GetOrdersRequest(
                        status=QueryOrderStatus.OPEN, symbols=checked_brackets
                    ),
                )
                covered = {
                    self.market.normalize_symbol(o.symbol) for o in open_orders
                }
                for symbol in checked_brackets:
                    if symbol not in covered:
                        self._open_entries[symbol]["native_bracket"] = False
                        self.db.add_log(
                            "INFO",
                            f"{symbol}: bracket legs no longer resting on the "
                            f"exchange — soft stop/target enforcement re-armed",
                        )
        except Exception as exc:
            logger.error("Position-protection watchdog failed: %s", exc)

    async def _attach_protective_levels(
        self,
        symbol: str,
        position: Dict[str, Any],
        frames: Dict[str, pd.DataFrame],
    ) -> None:
        """Give an untracked position ATR-scaled stop/target levels, or close
        it after 3 cycles if no usable price/ATR is available."""
        qty = float(position.get("qty", 0.0))
        side = "BUY" if qty > 0 else "SELL"
        bars = frames.get(symbol)
        atr: Optional[float] = None
        price: Optional[float] = None
        if bars is not None and not bars.empty:
            candidate = float(compute_atr(bars).iloc[-1])
            if not np.isnan(candidate) and candidate > 0:
                atr = candidate
            price = float(bars["close"].iloc[-1])
        if price is None and position.get("current_price") is not None:
            price = float(position["current_price"])

        if atr is None or price is None or price <= 0:
            failures = self._protect_failures.get(symbol, 0) + 1
            self._protect_failures[symbol] = failures
            if failures < 3:
                return
            entry = self._open_entries.get(symbol)
            if entry is not None:
                entry["exit_reason"] = (
                    "Protective close: the position had no stop/target on "
                    "record and no usable price/ATR data to attach one — "
                    "unmanageable is worse than closed."
                )
            self.db.add_log(
                "TRADE",
                f"PROTECTIVE CLOSE {symbol} x{abs(qty):g} ({side}) — no "
                f"stop/target on record and none could be attached for "
                f"{failures} cycles",
            )
            self._protection_event(
                "protective_closes",
                f"{symbol}: closed — untracked and no data to attach levels",
            )
            await self._limit_close(symbol, position)
            return

        self._protect_failures.pop(symbol, None)
        stop_distance, target_distance = bracket_distances(
            price,
            atr,
            self.config["atr_stop_mult"],
            self.config["atr_target_mult"],
        )
        if side == "BUY":
            stop_loss = price - stop_distance
            take_profit = price + target_distance
        else:
            stop_loss = price + stop_distance
            take_profit = price - target_distance
        entry = self._open_entries[symbol]
        entry["stop_loss"] = stop_loss
        entry["take_profit"] = take_profit
        entry["native_bracket"] = False
        self.db.add_log(
            "TRADE",
            f"PROTECTION ATTACHED {symbol} x{abs(qty):g} ({side}) — position "
            f"had no stop/target on record; soft SL ${stop_loss:.2f} / TP "
            f"${take_profit:.2f} set from the current price ~${price:.2f} "
            f"({self.config['atr_stop_mult']:.1f}×/"
            f"{self.config['atr_target_mult']:.1f}×ATR ${atr:.3f})",
        )
        self._protection_event(
            "levels_attached",
            f"{symbol}: protective levels attached (SL ${stop_loss:.2f})",
        )

    async def _cancel_symbol_orders(self, symbol: str) -> None:
        """Cancel a symbol's resting orders (e.g. an unfilled entry limit)
        before submitting a close, so the close cannot collide with a resting
        order or leave one dangling to trade shares the position no longer
        holds."""
        try:
            open_orders = await asyncio.to_thread(
                self.trading.get_orders,
                GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol]),
            )
        except Exception as exc:
            logger.error("Open-order fetch failed for %s: %s", symbol, exc)
            self.db.add_log("ERROR", f"{symbol}: open-order fetch failed: {exc}")
            return
        for order in open_orders:
            try:
                await asyncio.to_thread(self.trading.cancel_order_by_id, order.id)
            except Exception as exc:
                logger.error(
                    "Order cancel failed for %s (%s): %s", symbol, order.id, exc
                )
                self.db.add_log("ERROR", f"{symbol}: order cancel failed: {exc}")

    def _position_dict(self, p: Any) -> Dict[str, Any]:
        """Minimal position dict (the fields _limit_close needs) from a live
        Alpaca position object, with the symbol canonicalized — Alpaca returns
        crypto positions slashless ("PAXGUSD") but orders need the slashed
        form, and tracking keys must match the entry."""
        return {
            "symbol": self.market.normalize_symbol(p.symbol),
            "qty": float(p.qty),
            "current_price": (
                None if p.current_price is None else float(p.current_price)
            ),
            "avg_entry_price": float(p.avg_entry_price),
        }

    async def _limit_close(
        self, symbol: str, position: Dict[str, Any]
    ) -> Optional[Any]:
        """Close a held position with a marketable limit order — market and
        bracket closes are rejected outside the regular session (and for crypto
        entirely). Cancels any resting orders first, then submits the market
        adapter's close order (equity: extended-hours DAY; crypto: GTC),
        exit_slip_pct through the last trade on the opposite side for the full
        quantity, so the close actually fills in a thin book. Returns the
        submitted order, or None on failure. The fill is reconciled into a trade
        record on the next cycle by sync_portfolio — the single path."""
        self._closing.add(symbol)
        await self._cancel_symbol_orders(symbol)
        raw_qty = float(position.get("qty", 0.0))
        qty = abs(raw_qty)
        if qty <= 0:
            self._closing.discard(symbol)
            return None
        is_long = raw_qty > 0

        # Price the close off the live quote we'd hit (the bid to sell a long,
        # the ask to buy back a short) so the marketable limit fills in a thin
        # book; fall back to the last synced price, then the average entry.
        fallback = position.get("current_price")
        if fallback is None:
            fallback = float(position.get("avg_entry_price", 0.0))
        close_side = OrderSide.SELL if is_long else OrderSide.BUY
        price = await self._entry_reference_price(symbol, close_side, float(fallback))

        order_request = self.market.build_close_order(
            symbol, is_long, qty, float(price),
            self.config.get("exit_slip_pct", 0.002),
        )
        try:
            order = await asyncio.to_thread(self.trading.submit_order, order_request)
            self._close_failures.pop(symbol, None)
            if symbol in self._open_entries:
                self._open_entries[symbol]["_close_order_id"] = order.id
            return order
        except Exception as exc:
            self._closing.discard(symbol)
            failures = self._close_failures.get(symbol, 0) + 1
            self._close_failures[symbol] = failures
            logger.error(
                "Limit close failed for %s (attempt %d): %s", symbol, failures, exc
            )
            # Deduplicate the dashboard log: the 2026-07-13 AAVE close
            # rejection repeated identically every 60s cycle for 3.5 h and
            # flooded 200 of the 500 visible log lines.
            if failures == 1 or failures % 10 == 0:
                self.db.add_log(
                    "ERROR",
                    f"{symbol}: limit close failed "
                    f"({failures} consecutive attempt"
                    f"{'s' if failures != 1 else ''}): {exc}",
                )
            self._protection_event(
                None, f"{symbol}: limit close failed ({failures}x): {exc}"
            )
            if failures >= 3:
                return await self._market_close_fallback(symbol, failures)
            return None

    async def _market_close_fallback(
        self, symbol: str, failures: int
    ) -> Optional[Any]:
        """Last-resort liquidation after repeated limit-close failures.

        A close that cannot execute leaves a breached stop unenforced
        indefinitely — strictly worse than paying a market order's spread.
        Only fires when the adapter says Alpaca accepts a market close right
        now (crypto: always; equities: regular session), otherwise the
        limit-close retry loop continues and the protection banner carries
        the alarm. Uses close_position (full position, no qty), which
        sidesteps qty-precision rejections entirely."""
        if not self.market.market_close_allowed():
            return None
        self._closing.add(symbol)
        try:
            # The positions API wants the slashless form ("AAVEUSD").
            order = await asyncio.to_thread(
                self.trading.close_position, symbol.replace("/", "")
            )
            self._close_failures.pop(symbol, None)
            if symbol in self._open_entries:
                self._open_entries[symbol]["_close_order_id"] = getattr(
                    order, "id", None
                )
            self.db.add_log(
                "TRADE",
                f"FORCED MARKET CLOSE {symbol} after {failures} failed limit "
                f"closes — position was running without an enforceable stop",
            )
            self._protection_event(
                "forced_market_closes",
                f"{symbol}: market-close fallback after {failures} failed "
                f"limit closes",
            )
            return order
        except Exception as exc:
            self._closing.discard(symbol)
            logger.error("Market-close fallback failed for %s: %s", symbol, exc)
            self.db.add_log(
                "ERROR", f"{symbol}: market-close fallback failed: {exc}"
            )
            self._protection_event(
                None, f"{symbol}: market-close fallback failed: {exc}"
            )
            return None

    async def close_position_now(
        self,
        symbol: str,
        position: Dict[str, Any],
        exit_signal: Dict[str, Any],
    ) -> None:
        """Close a held position early via an extended-hours limit order. The
        resulting fill is turned into a trade record on the next cycle by
        sync_portfolio/reconcile_closed_trade — exactly like a soft stop/target
        exit — so there is a single trade-recording path."""
        order = await self._limit_close(symbol, position)
        if order is None:
            return

        entry = self._open_entries.get(symbol, {})
        entry_price = float(
            entry.get("entry_price", position.get("avg_entry_price", 0.0))
        )
        qty = abs(float(position.get("qty", 0.0)))
        side = entry.get("side", "BUY")
        if symbol in self._open_entries:
            verb = "RSI dropped back to" if side == "SELL" else "RSI recovered to"
            self._open_entries[symbol]["exit_reason"] = (
                f"Signal exit: {verb} {exit_signal['rsi']:.1f}, so the "
                f"reversion was banked early at ~${exit_signal['close']:.2f} "
                "rather than waiting for the stop/target."
            )
        exit_label = "COVER" if side == "SELL" else "SIGNAL EXIT"
        self.db.add_log(
            "TRADE",
            f"{exit_label} {symbol} x{qty:g} @ ~${exit_signal['close']:.2f} | "
            f"RSI {exit_signal['rsi']:.1f} | entry ~${entry_price:.2f} | "
            f"order {getattr(order, 'id', '?')}",
        )
        logger.info("Signal-exit close for %s x%g (%s)", symbol, qty, side)

    # ------------------------------------------------------------------ #
    # portfolio sync & trade reconciliation
    # ------------------------------------------------------------------ #

    async def sync_portfolio(self) -> Optional[Dict[str, Any]]:
        """Mirror live Alpaca account/positions into SQLite for the frontend."""
        try:
            account = await asyncio.to_thread(self.trading.get_account)
            live_positions = await asyncio.to_thread(self.trading.get_all_positions)
        except Exception as exc:
            logger.error("Portfolio sync failed: %s", exc)
            self.db.add_log("ERROR", f"Portfolio sync failed: {exc}")
            return None

        # One Alpaca account serves both the equity and crypto engines, so
        # get_all_positions returns the blended book — keep only this market's
        # own positions or the two engines would try to manage each other's.
        # Save the full list before filtering so compute_equity can subtract
        # the other market's market value from the blended account equity.
        all_positions = list(live_positions)
        live_positions = [
            p for p in live_positions if self.market.owns_symbol(p.symbol)
        ]

        snapshot = [
            {
                "symbol": self.market.normalize_symbol(p.symbol),
                "qty": float(p.qty),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": (
                    None if p.current_price is None else float(p.current_price)
                ),
                "unrealized_pnl": (
                    None if p.unrealized_pl is None else float(p.unrealized_pl)
                ),
                "market_value": (
                    None if p.market_value is None else float(p.market_value)
                ),
            }
            for p in live_positions
        ]

        # Residual fee/floor dust is NOT a position (see market.is_dust):
        # counting it held max_positions slots hostage, spun the protection
        # watchdog on unmanageable 1e-9 balances, and — worst — kept the
        # symbol in live_symbols after a real close, so the reconciler
        # never recorded the real trade (its P&L vanished from the ledger,
        # replaced after a restart by a -$0.00 dust row). Dust is dropped
        # here, before anything downstream sees it, and swept from the
        # account once per session without ever becoming a trade record.
        dust = [
            p for p in snapshot
            if self.market.is_dust(p["symbol"], p["qty"], p.get("current_price"))
        ]
        if dust:
            dust_symbols = {p["symbol"] for p in dust}
            snapshot = [p for p in snapshot if p["symbol"] not in dust_symbols]
            await self._sweep_dust(dust)

        self.db.replace_positions(snapshot)

        live_symbols = {p["symbol"] for p in snapshot}
        # Drop close-in-flight guards for positions that have now exited
        # (their signal-exit close filled); keep guards for any still held so
        # the next cycle does not double-submit a close. Failure counters
        # follow the same rule — a closed position's streak is over.
        self._closing.intersection_update(live_symbols)
        for tracker in (self._close_failures, self._protect_failures):
            for symbol in list(tracker):
                if symbol not in live_symbols:
                    del tracker[symbol]
        for symbol, entry in list(self._open_entries.items()):
            if symbol in live_symbols:
                entry["opened"] = True  # confirmed a live position at least once
                continue
            # A tracked symbol with no live position is EITHER a position that
            # opened and has since closed, OR an entry order that never filled.
            # Only the former is a real trade — recording the latter fabricated
            # phantom trades and left the unfilled GTC entry resting, stacking a
            # fresh duplicate every cycle.
            if entry.get("opened") or entry.get("_close_order_id"):
                await self.reconcile_closed_trade(symbol, entry)
                del self._open_entries[symbol]
                continue
            if await self._entry_filled_qty(symbol, entry) > 0:
                # Filled then vanished between two syncs — a genuine fast trade.
                entry["opened"] = True
                await self.reconcile_closed_trade(symbol, entry)
            else:
                # Never opened a position: cancel the resting entry order and
                # drop it WITHOUT recording a trade. Bench the symbol briefly so
                # an unfillable book is not hammered every cycle.
                await self._cancel_symbol_orders(symbol)
                self.db.add_log(
                    "INFO",
                    f"{symbol}: entry order did not fill within a cycle — "
                    f"cancelled, no position opened",
                )
                self.start_cooldown(symbol)
            del self._open_entries[symbol]

        # Adopt positions opened outside this process (e.g. before a restart)
        # so their eventual close still produces a trade record. These are
        # confirmed-live positions, so mark them opened immediately — otherwise
        # a fast close would be mistaken for an unfilled entry and dropped.
        # The tracked metadata (stop/target/entry context) is republished to
        # runtime_state every cycle, so a restart can restore it instead of
        # adopting the position naked: an adopted position used to carry NO
        # stop_loss/take_profit, which the soft-stop loop skips — VRAX rode
        # untracked from a restart to the EOD flatten on 2026-07-10 and lost
        # $101.46, a third of that week's damage. Positions with no persisted
        # record still adopt bare; _ensure_position_protection covers them.
        adopting = [p for p in snapshot if p["symbol"] not in self._open_entries]
        persisted: Dict[str, Any] = {}
        if adopting:
            try:
                persisted = self.db.get_state("open_entries", {}) or {}
            except Exception as exc:
                logger.error("Persisted open-entry fetch failed: %s", exc)
        for pos in adopting:
            symbol = pos["symbol"]
            qty = pos["qty"]
            entry: Dict[str, Any] = {
                "qty": abs(qty),
                "entry_price": pos["avg_entry_price"],
                "side": "BUY" if qty > 0 else "SELL",
                "opened": True,
                "entry_time": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            }
            saved = persisted.get(symbol)
            if isinstance(saved, dict) and saved.get("side") == entry["side"]:
                # Same symbol, same direction → same position; restore its
                # levels and entry context (entry_time/price included — the
                # live avg_entry_price is authoritative for P&L, but the
                # saved record carries the original context fields).
                entry.update(
                    {k: v for k, v in saved.items() if not k.startswith("_")}
                )
                entry["qty"] = abs(qty)
                entry["entry_price"] = pos["avg_entry_price"]
                entry["opened"] = True
                if saved.get("stop_loss") is not None:
                    self.db.add_log(
                        "INFO",
                        f"{symbol}: restored tracked stop/target from the "
                        f"persisted entry record after restart (SL "
                        f"${saved['stop_loss']:.2f} / TP "
                        f"${saved.get('take_profit', 0) or 0:.2f})",
                    )
            self._open_entries[symbol] = entry

        # Per-market equity: crypto uses a notional base + its own PnL; equity
        # subtracts the other market's market value from the blended account
        # equity. Both engines' daily-stop and equity curve stay independent
        # on one shared account.
        equity = self.market.compute_equity(account, snapshot, self.db, all_positions)
        self.db.set_status(equity=equity)
        self.db.record_equity(equity)
        return {"equity": equity, "positions": snapshot}

    async def _sweep_dust(self, dust_positions: List[Dict[str, Any]]) -> None:
        """Liquidate residual dust balances, once per symbol per session.

        Uses close_position (full position, no qty), which demonstrably
        works below the minimum order size where regular orders are
        rejected with "qty must be > 0". Deliberately NOT routed through
        _limit_close and never recorded as a trade — dust is a fee/rounding
        remainder worth ~$0.000001, not a trade. A failed sweep is logged
        at debug level and not retried: invisible dust on the account is
        harmless once it no longer counts as a position."""
        for pos in dust_positions:
            symbol = pos["symbol"]
            if symbol in self._dust_swept:
                continue
            self._dust_swept.add(symbol)
            try:
                await asyncio.to_thread(
                    self.trading.close_position, symbol.replace("/", "")
                )
                self.db.add_log(
                    "INFO",
                    f"{symbol}: swept {abs(pos['qty']):g} residual dust "
                    f"(fee/rounding remainder — not a position, not a trade)",
                )
            except Exception as exc:
                logger.debug("Dust sweep failed for %s: %s", symbol, exc)

    @staticmethod
    def _infer_exit_reason(
        side: str,
        exit_price: Optional[float],
        take_profit: Optional[float],
        stop_loss: Optional[float],
    ) -> str:
        """Label a bracket exit (no signal-exit reason was recorded) by seeing
        whether the fill landed nearer the take-profit or the stop-loss leg."""
        if exit_price is None:
            return (
                "Position closed but the exit fill could not be reconciled — "
                "treated conservatively as a loss for cooldown purposes."
            )
        if take_profit is None or stop_loss is None:
            return (
                "Position closed via its bracket, an end-of-day flatten, or "
                "outside this engine (bracket levels were not on record)."
            )
        nearer_target = abs(exit_price - take_profit) <= abs(exit_price - stop_loss)
        if nearer_target:
            return (
                f"Take-profit leg filled: exit ~${exit_price:.2f} reached the "
                f"${take_profit:.2f} target."
            )
        return (
            f"Stop-loss leg filled: exit ~${exit_price:.2f} hit the "
            f"${stop_loss:.2f} stop."
        )

    async def reconcile_closed_trade(self, symbol: str, entry: Dict[str, Any]) -> None:
        """A tracked position vanished — find its exit fill and log the trade."""
        side = entry.get("side", "BUY")
        exit_price: Optional[float] = None
        close_order_id = entry.get("_close_order_id")
        try:
            if close_order_id:
                order = await asyncio.to_thread(
                    self.trading.get_order_by_id, close_order_id
                )
                if order.filled_avg_price:
                    exit_price = float(order.filled_avg_price)
            else:
                exit_side = OrderSide.BUY if side == "SELL" else OrderSide.SELL
                closed_orders = await asyncio.to_thread(
                    self.trading.get_orders,
                    GetOrdersRequest(
                        status=QueryOrderStatus.CLOSED, symbols=[symbol], limit=10
                    ),
                )
                for order in closed_orders:
                    if order.side == exit_side and order.filled_avg_price:
                        exit_price = float(order.filled_avg_price)
                        break
        except Exception as exc:
            logger.error("Exit reconciliation failed for %s: %s", symbol, exc)
            self.db.add_log("ERROR", f"{symbol}: exit reconciliation failed: {exc}")

        qty = float(entry["qty"])
        entry_price = float(entry["entry_price"])
        if side == "SELL":
            realized = None if exit_price is None else (entry_price - exit_price) * qty
        else:
            realized = None if exit_price is None else (exit_price - entry_price) * qty
        exit_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Preserve the entry rationale + bracket levels captured at open time,
        # and — if the position left via its bracket rather than a signal exit
        # (no exit_reason recorded) — infer whether the take-profit or stop-loss
        # leg filled from where the exit landed relative to those levels.
        context = {
            key: entry.get(key)
            for key in (
                "entry_rsi", "entry_vwap", "entry_atr", "entry_sentiment",
                "sentiment_source", "stop_loss", "take_profit", "entry_reason",
            )
        }
        context["exit_reason"] = entry.get("exit_reason") or self._infer_exit_reason(
            side, exit_price, entry.get("take_profit"), entry.get("stop_loss")
        )
        self.db.record_trade(
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=entry_price,
            exit_price=exit_price,
            entry_time=entry["entry_time"],
            exit_time=exit_time,
            realized_pnl=realized,
            context=context,
        )
        pnl_text = "unknown PnL" if realized is None else f"PnL ${realized:+.2f}"
        self.db.add_log("TRADE", f"CLOSED {symbol} x{qty:g} ({side}) — {pnl_text}")

        # Feed the outcome back into the analyst's decision memory so
        # lesson extraction sees decision → result pairs.
        try:
            get_analyst().update_decision_memory(
                symbol,
                "close",
                {
                    "realized_pnl": realized,
                    "exit_price": exit_price,
                    "exit_time": exit_time,
                },
                self.db,
            )
        except Exception as exc:
            logger.error("Decision memory update failed for %s: %s", symbol, exc)

        # Cooldown after every close, not just losses. Without this a winning
        # trade on a low-volatility symbol (e.g. PAXG/USD) churns every minute:
        # buy → RSI recovers → signal-exit for a tiny profit → no cooldown →
        # re-buy next cycle. The RSI oscillates around the buy/exit thresholds
        # on quiet assets, so the bot must sit out a cooldown regardless of
        # the PnL sign. An unknown exit price is still treated conservatively.
        self.start_cooldown(symbol)

    # ------------------------------------------------------------------ #
    # risk management
    # ------------------------------------------------------------------ #

    async def check_daily_risk(self, equity: float) -> bool:
        """Return True when trading may continue, False after a kill."""
        status = self.db.get_status()
        daily_pnl = equity - status["daily_start_balance"]
        stop_loss = self.config.get("daily_stop_loss", 100.0)
        if status["daily_start_balance"] > 0 and daily_pnl <= -stop_loss:
            logger.critical(
                "Daily loss limit breached: %.2f <= -%.2f", daily_pnl, stop_loss
            )
            await self.kill_sequence(
                f"Daily loss ${-daily_pnl:.2f} breached limit ${stop_loss:.2f}"
            )
            return False
        return True

    async def flatten_all(self, reason: str) -> None:
        """Close every open position and cancel all resting orders.

        Used ahead of the extended close, where the DAY limit orders would
        expire and leave positions orphaned overnight. Unlike kill_sequence
        this is routine housekeeping: the bot stays RUNNING and the fills
        are reconciled into trade records by the next cycle's
        sync_portfolio — the same single trade-recording path as any exit.

        Extended hours forbids market and close_all_positions liquidation, so
        every position is closed with its own extended-hours limit order.
        """
        self.db.add_log("TRADE", f"EOD FLATTEN — closing all positions ({reason})")
        try:
            positions = await asyncio.to_thread(self.trading.get_all_positions)
        except Exception as exc:
            logger.error("EOD flatten position fetch failed: %s", exc)
            self.db.add_log("ERROR", f"EOD flatten position fetch failed: {exc}")
            return
        for p in positions:
            if self.market.owns_symbol(p.symbol):
                pos = self._position_dict(p)
                await self._limit_close(pos["symbol"], pos)

    async def kill_sequence(self, reason: str) -> None:
        """Emergency shutdown: flatten everything, persist KILLED, stop.

        Liquidation uses extended-hours limit closes (marketable, priced
        exit_slip_pct through the last trade) because market and
        close_all_positions liquidation are rejected outside regular hours."""
        self.db.add_log("CRITICAL", f"KILL SEQUENCE INITIATED: {reason}")
        # Cancel only THIS market's open orders — cancel_orders() is
        # account-wide and would kill the other engine's resting orders.
        try:
            open_orders = await asyncio.to_thread(
                self.trading.get_orders,
                GetOrdersRequest(status=QueryOrderStatus.OPEN),
            )
            for order in open_orders:
                if self.market.owns_symbol(order.symbol):
                    await asyncio.to_thread(
                        self.trading.cancel_order_by_id, order.id
                    )
            self.db.add_log("CRITICAL", "Open orders cancelled")
        except Exception as exc:
            logger.error("Cancel-all failed during kill sequence: %s", exc)
            self.db.add_log("ERROR", f"Cancel-all failed: {exc}")
        try:
            positions = await asyncio.to_thread(self.trading.get_all_positions)
            for p in positions:
                if self.market.owns_symbol(p.symbol):
                    pos = self._position_dict(p)
                    await self._limit_close(pos["symbol"], pos)
            self.db.add_log("CRITICAL", "All positions liquidated")
        except Exception as exc:
            logger.error("Liquidation failed during kill sequence: %s", exc)
            self.db.add_log("ERROR", f"Liquidation failed: {exc}")
        self.db.set_status(status=STATUS_KILLED)
        self.db.add_log("CRITICAL", "Bot state set to KILLED — engine shutting down")
        self._shutdown.set()

    # ------------------------------------------------------------------ #
    # daily anchor (Europe/Zurich)
    # ------------------------------------------------------------------ #

    def roll_daily_anchor(self, equity: float) -> None:
        """Reset the daily PnL baseline at Swiss midnight.

        The anchor date is persisted: a mid-day engine restart keeps the
        existing baseline instead of re-arming a fresh daily loss budget."""
        today = datetime.now(ZURICH).strftime("%Y-%m-%d")
        if self._current_day == today:
            return
        self._current_day = today
        if self.db.get_state("daily_anchor_date") == today:
            return
        self.db.set_state("daily_anchor_date", today)
        self.db.set_status(daily_start_balance=equity)
        self.db.add_log(
            "INFO",
            f"New trading day {today} (Europe/Zurich) — "
            f"daily baseline set to ${equity:,.2f}",
        )

    # ------------------------------------------------------------------ #
    # main loop
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        if self.db.is_killed():
            logger.critical(
                "bot_status is KILLED — refusing to start. "
                "Reset the status to RUNNING to re-enable trading."
            )
            self.db.add_log("CRITICAL", "Startup aborted: bot_status is KILLED")
            return

        self.db.set_status(status=STATUS_RUNNING)
        # Zero the analyst fail-open counters so the dashboard's
        # "auto-approvals this session" badge means this session.
        get_analyst().reset_health(self.db)
        # Same for the position-protection counters and banner.
        self.reset_protection_health()
        cfg = self.config
        self.db.add_log(
            "INFO",
            f"Argus engine started (paper trading) — {self.market.name} | universe "
            f"{self.market.describe_mode()} | position size ${cfg.get('position_size_usd', 500):.0f} "
            f"| risk/trade ${cfg.get('risk_per_trade_usd', 20):.0f} | max positions "
            f"{cfg.get('max_positions', 5):.0f} | daily stop ${cfg.get('daily_stop_loss', 100):.0f} | loser "
            f"cooldown {cfg.get('cooldown_minutes', 30):.0f}m | regime filter on "
            f"{self.market.regime_symbol}",
        )
        logger.info(
            "Argus engine started — %s universe %s",
            self.market.name,
            self.market.describe_mode(),
        )

        # Publish operational environment (no secrets) so the Settings tab
        # can display and edit them.
        self.db.set_state(
            "environment",
            {
                "market": self.market.name,
                "universe_mode": self.market.describe_mode(),
                "watchlist_size": len(self.watchlist),
                "position_size_usd": cfg.get("position_size_usd", 500),
                "risk_per_trade_usd": cfg.get("risk_per_trade_usd", 20),
                "max_positions": cfg.get("max_positions", 5),
                "daily_stop_loss": cfg.get("daily_stop_loss", 100),
                "min_price_usd": cfg.get("min_price_usd", 5),
                "cooldown_minutes": cfg.get("cooldown_minutes", 30),
                "poll_interval_seconds": cfg.get("poll_interval_seconds", 60),
                "bar_lookback_minutes": cfg.get("bar_lookback_minutes", 180),
                "entry_slip_pct": cfg.get("entry_slip_pct", 0.001),
                "exit_slip_pct": cfg.get("exit_slip_pct", 0.002),
                "regime_symbol": self.market.regime_symbol,
                "paper_trading": True,
                "engine_version": __version__,
                "engine_started_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            },
        )

        while not self._shutdown.is_set():
            try:
                await self.run_cycle()
            except Exception as exc:
                # Belt-and-braces: run_cycle guards each API call, but the
                # engine must survive anything unexpected as well.
                logger.exception("Unhandled error in trading cycle: %s", exc)
                self.db.add_log("ERROR", f"Unhandled cycle error: {exc}")
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=self.config.get("poll_interval_seconds", 60)
                )
            except asyncio.TimeoutError:
                pass

        logger.info("Argus engine stopped")

    async def run_cycle(self) -> None:
        self._cycle_count += 1
        cycle: Dict[str, Any] = {
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stage": "start",
        }
        try:
            await self._run_cycle_inner(cycle)
        finally:
            cycle["finished_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            cycle["cooldowns"] = {
                symbol: round(remaining, 1)
                for symbol in list(self._cooldowns)
                if (remaining := self.in_cooldown(symbol)) is not None
            }
            self.last_cycle = cycle
            # Publish the trace so the dashboard can render engine internals
            # (stage, regime, cooldowns) straight from the shared database.
            try:
                self.db.set_state("last_cycle", cycle)
            except Exception as exc:
                logger.error("Failed to publish cycle state: %s", exc)
            # Publish the in-memory open-entry metadata (RSI/VWAP/ATR/
            # sentiment/stop/target/entry_reason) so the dashboard's
            # per-position info popup can render it for live positions.
            # Internal guard fields (prefixed with _) are stripped.
            try:
                self.db.set_state(
                    "open_entries",
                    {
                        symbol: {k: v for k, v in entry.items() if not k.startswith("_")}
                        for symbol, entry in self._open_entries.items()
                    },
                )
            except Exception as exc:
                logger.error("Failed to publish open entries: %s", exc)

    async def _run_cycle_inner(self, cycle: Dict[str, Any]) -> None:
        # Absorb parameter changes written by the optimizer overnight.
        self.config = self.db.get_config()
        cycle["config"] = dict(self.config)

        # Honour an external kill (e.g. the dashboard's EMERGENCY HARD STOP).
        if self.db.is_killed():
            self.db.add_log("CRITICAL", "External KILL detected — engine stopping")
            self._shutdown.set()
            cycle["stage"] = "external-kill"
            return

        # Refresh the trading universe (most-actives for equities, the crypto
        # asset list for crypto; cached inside the adapter).
        self.watchlist = await asyncio.to_thread(self.market.get_watchlist)
        cycle["watchlist_size"] = len(self.watchlist)

        portfolio = await self.sync_portfolio()
        if portfolio is None:
            cycle["stage"] = "portfolio-sync-failed"
            return
        cycle["equity"] = portfolio["equity"]

        self.roll_daily_anchor(portfolio["equity"])
        if not await self.check_daily_risk(portfolio["equity"]):
            cycle["stage"] = "risk-kill"
            return

        # Shadow-veto resolution, in the background every ~10 minutes.
        # Scheduled BEFORE the session gate on purpose: equity vetoes only
        # resolve (as end-of-day closes) once the market has closed, which
        # is exactly when the later stages of this cycle never run.
        if (
            self._veto_task is None or self._veto_task.done()
        ) and time.monotonic() - self._last_veto_resolution >= 600:
            self._last_veto_resolution = time.monotonic()
            self._veto_task = asyncio.create_task(self._resolve_vetoes())

        # Session gate — the adapter decides: equities trade the 4 AM–8 PM ET
        # extended session (derived from the trading calendar); crypto is
        # always open (24/7).
        state = await asyncio.to_thread(self.market.session_state)
        now_utc = datetime.now(timezone.utc)
        cycle["market_open"] = state.open
        if not state.open:
            if state.next_open is None:
                logger.info("Market closed / not a trading day — sitting out")
            else:
                logger.info("Outside trading session — next open %s", state.next_open)
                cycle["next_open"] = str(state.next_open)
            cycle["stage"] = "market-closed"
            return

        # End-of-day flatten (equities only): entries are DAY limit orders that
        # expire at the extended close, which would leave a surviving position
        # orphaned overnight. Flatten with a margin before the close. Crypto is
        # 24/7 (flatten_before_close=False), so this is skipped and positions
        # are held continuously under the soft stop/target.
        flatten_minutes = self.config.get("eod_flatten_minutes", 10.0)
        if (
            self.market.flatten_before_close
            and flatten_minutes > 0
            and state.close_utc is not None
        ):
            until_close = state.close_utc - now_utc
            if until_close <= timedelta(minutes=flatten_minutes):
                if portfolio["positions"]:
                    await self.flatten_all(
                        f"{until_close.total_seconds() / 60:.0f}m to the close"
                    )
                cycle["stage"] = "eod-flatten"
                return

        held_symbols = {p["symbol"] for p in portfolio["positions"]}
        cycle["held_symbols"] = sorted(held_symbols)

        # Phase 0: protective soft stop/target, then signal-driven early
        # exits, on held positions. Runs ahead of the entry gates on purpose —
        # a full book or a TREND_DOWN tape must never stop us honouring a stop
        # or banking a bounce that is exhausted. Held symbols may have fallen
        # off the current watchlist, so fetch their bars directly.
        if held_symbols:
            exit_frames = await self.fetch_minute_bars(sorted(held_symbols))
            stop_exits = await self.evaluate_and_close_stops(portfolio, exit_frames)
            if stop_exits:
                cycle["stop_exits"] = stop_exits
            signal_exits = await self.evaluate_and_close_exits(portfolio, exit_frames)
            if signal_exits:
                cycle["signal_exits"] = signal_exits
            # Watchdog: any position still held after the exit passes must
            # have working protection (levels + something enforcing them).
            await self._ensure_position_protection(portfolio, exit_frames)

        # Market regime shapes how much book we run this cycle: ANY
        # down-trend (index below its EMA, stressed or calm) blocks new
        # longs — every dip is a knife; 2026-07-13's calm-vol CAUTION drift
        # stopped out 23 of 28 dip-buys — while shorts stay allowed since a
        # falling market favours them. CAUTION (trend down OR vol elevated)
        # additionally halves the position cap. Blocked BUY signals are
        # still evaluated and shadow-recorded (gate "regime") so this gate's
        # P&L is measured like every other gate. Existing positions keep
        # their soft stop/target and the daily stop still guards them.
        regime_info = await asyncio.to_thread(self.market.regime)
        cycle["regime"] = regime_info
        regime_blocks_longs = regime.blocks_long_entries(regime_info)

        max_positions = int(self.config.get("max_positions", 5))
        if regime_info.get("regime") == regime.CAUTION:
            max_positions = max(1, max_positions // 2)
            cycle["caution_position_cap"] = max_positions

        # Open slots count still-held positions: a close submitted this cycle
        # only frees its slot once the sell fills (reconciled next cycle), so
        # we never over-allocate into a slot that is only theoretically free.
        open_slots = max_positions - len(held_symbols)
        cycle["open_slots"] = open_slots
        if open_slots <= 0:
            cycle["stage"] = "max-positions"
            return
        # NOTE: a long-blocking regime no longer short-circuits the cycle —
        # signals must still be evaluated so the blocked ones land in the
        # shadow-veto ledger. The sentiment calls this spends on a red tape
        # are the price of knowing what the gate is worth.
        frames = await self.fetch_minute_bars()
        cycle["symbols_with_bars"] = len(frames)

        # Phase 1: technical signal evaluation, concurrently across symbols.
        # The RSI/VWAP gates are cheap in-process math; only symbols that
        # pass them reach the sentiment scorer, whose news/LLM calls run in
        # threads — the semaphore bounds how many run at once.
        semaphore = asyncio.Semaphore(8)

        async def evaluate_bounded(sym: str, sym_bars: pd.DataFrame):
            async with semaphore:
                return await self.evaluate_signal(sym, sym_bars)

        results = await asyncio.gather(
            *(
                evaluate_bounded(symbol, bars)
                for symbol, bars in frames.items()
                if symbol not in held_symbols
            ),
            return_exceptions=True,
        )
        pending_signals: List[Dict[str, Any]] = [
            s for s in results if isinstance(s, dict)
        ]
        # Deepest dips first for BUY, highest overextension first for SELL
        pending_signals.sort(key=lambda s: s["rsi"])

        # Regime gate: veto BUY signals while the index trends down, shadow-
        # recording each one exactly like the sentiment/risk/PM gates so
        # GET /vetoes answers whether the blocked dip-buys would have won.
        if regime_blocks_longs:
            blocked_buys = [s for s in pending_signals if s["side"] == "BUY"]
            for s in blocked_buys:
                self._record_veto(
                    s,
                    "regime",
                    f"regime {regime_info.get('regime', '?')}: "
                    f"{regime_info.get('symbol', '?')} "
                    f"${regime_info.get('close', 0):.2f} < EMA "
                    f"${regime_info.get('ema', 0):.2f} — no dip-buying into "
                    f"a falling tape",
                )
            if blocked_buys:
                cycle["regime_blocked_buys"] = len(blocked_buys)
                self.db.add_log(
                    "INFO",
                    f"Regime {regime_info.get('regime', '?')} "
                    f"({regime_info.get('symbol', '?')} "
                    f"${regime_info.get('close', 0):.2f} < EMA "
                    f"${regime_info.get('ema', 0):.2f}) — "
                    f"{len(blocked_buys)} BUY signal"
                    f"{'s' if len(blocked_buys) != 1 else ''} blocked and "
                    f"shadow-tracked",
                )
            pending_signals = [s for s in pending_signals if s["side"] != "BUY"]

        # Phase 2: LLM risk agent evaluates each signal (in parallel).
        analyst = get_analyst()
        if analyst.enabled(self.db) and analyst.available and pending_signals:
            # Only the best candidates for the available slots are worth
            # an LLM review; the rest could never be executed this cycle.
            pending_signals = pending_signals[: max(open_slots * 2, 1)]
            risk_results = await asyncio.gather(
                *(
                    asyncio.to_thread(
                        analyst.evaluate_signal_risk,
                        s,
                        portfolio,
                        regime_info,
                        self.db,
                    )
                    for s in pending_signals
                )
            )
            filtered_signals = []
            for s, risk in zip(pending_signals, risk_results):
                s["_risk_score"] = risk.get("risk_score", 0.5)
                if risk.get("approved", True):
                    filtered_signals.append(s)
                else:
                    self.db.add_log(
                        "ANALYST",
                        f"Risk agent rejected {s['symbol']}: {risk.get('reason', 'no reason')}",
                    )
                    self._record_veto(
                        s, "risk_agent", risk.get("reason", "no reason")
                    )
            pending_signals = filtered_signals

            # Phase 3: portfolio manager decides execution order. Silence is
            # not consent: a signal the manager neither approved nor rejected
            # is skipped, not traded. (On manager failure the analyst layer
            # itself fails open by approving everything.)
            pm_result = await asyncio.to_thread(
                analyst.portfolio_manager,
                pending_signals, portfolio, regime_info, self.db,
            )
            approved = pm_result.get("approved_symbols", [])
            rejected = pm_result.get("rejected_symbols", [])
            kept: List[Dict[str, Any]] = []
            for s in pending_signals:
                if s["symbol"] in rejected:
                    self.db.add_log(
                        "ANALYST",
                        f"Portfolio manager rejected {s['symbol']}: {pm_result.get('reason', 'no reason')}",
                    )
                    self._record_veto(
                        s, "portfolio_manager",
                        pm_result.get("reason", "no reason"),
                    )
                elif s["symbol"] not in approved:
                    self.db.add_log(
                        "ANALYST",
                        f"Portfolio manager did not approve {s['symbol']} — skipped",
                    )
                    self._record_veto(
                        s, "portfolio_manager",
                        "not approved (silence is not consent)",
                    )
                else:
                    kept.append(s)
            # Reorder by portfolio manager's preference
            kept.sort(key=lambda s: approved.index(s["symbol"]))
            pending_signals = kept

        # Phase 4: execute approved signals
        evaluated: Dict[str, str] = {}
        for signal_info in pending_signals:
            if open_slots <= 0:
                break
            if signal_info["side"] == "BUY":
                await self.place_limit_buy(signal_info)
            else:
                await self.place_limit_short(signal_info)
            evaluated[signal_info["symbol"]] = signal_info["side"]
            open_slots -= 1
            # Record decision for memory
            try:
                analyst.update_decision_memory(
                    signal_info["symbol"], signal_info["side"], None, self.db
                )
            except Exception:
                pass

        # Mark symbols that had no signal
        for symbol in frames:
            if symbol not in evaluated and symbol not in held_symbols:
                evaluated[symbol] = "no-signal"
        cycle["evaluated"] = evaluated
        cycle["stage"] = "complete"

        # Periodic LLM reviews run in the background so a slow review can
        # never delay the next order-placing cycle. At most one review batch
        # runs at a time.
        if self._review_task is None or self._review_task.done():
            self._review_task = asyncio.create_task(
                self._run_periodic_reviews(regime_info, self._cycle_count)
            )

        # Periodic opportunity screener runs in the background — equities only:
        # it scans the equity most-actives pool, which has no crypto analogue.
        if self.market.dynamic_watchlist and (
            self._screener_task is None or self._screener_task.done()
        ):
            self._screener_task = asyncio.create_task(
                self._run_screener()
            )

    async def _run_periodic_reviews(
        self, regime_info: Dict[str, Any], cycle_count: int
    ) -> None:
        """Trade review, watchlist curation and lesson extraction — advisory
        work that must never block order placement."""
        analyst = get_analyst()

        # Periodic LLM trade review (if analyst is enabled and enough time
        # has passed since the last review).
        try:
            if analyst.should_review_trades():
                trades = self.db.get_trades(200)
                stats = self.db.get_trade_stats()
                await asyncio.to_thread(
                    analyst.review_trades,
                    trades,
                    stats,
                    self.config,
                    regime_info,
                    self.db,
                )
        except Exception as exc:
            logger.error("Trade review failed: %s", exc)

        # Periodic LLM watchlist curation (equities only — the crypto universe
        # is a fixed USD-pair list the adapter never overrides). The override
        # carries a timestamp so universe.py can expire it.
        try:
            if self.market.dynamic_watchlist and analyst.should_review_watchlist():
                current_symbols = list(self.watchlist)
                new_symbols = await asyncio.to_thread(
                    analyst.review_watchlist,
                    current_symbols,
                    regime_info,
                    self.db,
                )
                if new_symbols and new_symbols != current_symbols:
                    self.db.set_state(
                        "watchlist_override",
                        {
                            "symbols": new_symbols,
                            "written_at": datetime.now(timezone.utc).isoformat(
                                timespec="seconds"
                            ),
                        },
                    )
                    self.db.add_log(
                        "ANALYST",
                        f"Watchlist updated: {len(current_symbols)} → "
                        f"{len(new_symbols)} symbols",
                    )
        except Exception as exc:
            logger.error("Watchlist review failed: %s", exc)

        # Periodic decision memory lesson extraction (every 50 cycles).
        try:
            if analyst.enabled(self.db) and cycle_count % 50 == 0:
                await asyncio.to_thread(analyst.extract_lessons, self.db)
        except Exception as exc:
            logger.error("Lesson extraction failed: %s", exc)

    async def _run_screener(self) -> None:
        """Periodic opportunity screener: scan a wide pool for RSI-oversold
        + VWAP-dip setups and publish the top candidates for the dashboard
        and the engine to consume. Runs at most once per 5 minutes."""
        from screener import run_screener

        while not self._shutdown.is_set():
            try:
                enabled = bool(self.config.get("screener_enabled", 0.0))
                if not enabled:
                    await asyncio.sleep(60)
                    continue
                pool_size = int(self.config.get("screener_pool_size", 200.0))
                candidates = await asyncio.to_thread(
                    run_screener, ALPACA_API_KEY, ALPACA_SECRET_KEY, pool_size
                )
                max_candidates = int(
                    self.config.get("screener_max_candidates", 5.0)
                )
                top = candidates[:max_candidates] if candidates else []
                self.db.set_state("screener_candidates", top)
                self.db.add_log(
                    "INFO",
                    f"Screener: {len(candidates)} candidates found "
                    f"(top: {', '.join(c['symbol'] for c in top[:5])})"
                    if top
                    else "Screener: no candidates this pass",
                )
            except Exception as exc:
                logger.error("Screener pass failed: %s", exc)
            # Run at most once per 5 minutes.
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), timeout=300
                )
            except asyncio.TimeoutError:
                pass


class EngineController:
    """Owns the engine task's lifecycle so the debug API can inspect it,
    kill it, and — unlike the old design — restart it after a KILLED state
    without a container bounce."""

    def __init__(self) -> None:
        self.bot: Optional[ArgusBot] = None
        self.task: Optional[asyncio.Task] = None
        self.started_at: Optional[str] = None

    @property
    def engine_running(self) -> bool:
        return self.task is not None and not self.task.done()

    async def start_engine(self) -> Tuple[bool, str]:
        if self.engine_running:
            return False, "engine already running"
        if get_db().is_killed():
            return False, "bot_status is KILLED — POST /reset to recover"
        self.bot = ArgusBot()
        self.task = asyncio.create_task(self.bot.run())
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return True, "engine started"

    async def kill(self, reason: str) -> None:
        if self.engine_running and self.bot is not None:
            await self.bot.kill_sequence(reason)
            return
        # Engine not running (crashed, or already killed): still flatten
        # everything and persist KILLED via a fresh bot instance.
        bot = self.bot or ArgusBot()
        await bot.kill_sequence(reason)

    async def reset(self) -> Tuple[bool, str]:
        db = get_db()
        if not db.is_killed():
            return False, "bot_status is not KILLED — nothing to reset"
        if self.engine_running:
            return False, "engine still running — wait for it to stop"
        db.set_status(status=STATUS_RUNNING)
        db.add_log("INFO", "Status reset RUNNING via debug API — restarting engine")
        return await self.start_engine()


async def main() -> None:
    import uvicorn

    from api import create_app
    from optimizer import schedule_daily_optimization

    controller = EngineController()
    started, message = await controller.start_engine()
    if not started:
        logger.critical("Engine not started: %s (API stays up for /reset)", message)

    # The nightly optimizer's backtest is equity-bar based; it must not tune the
    # crypto engine's params. Crypto runs on static/default params in v1.
    optimizer_task: Optional[asyncio.Task] = None
    if os.getenv("MARKET", "equity").strip().lower() != "crypto":
        from optimizer import clear_stale_status

        # A previous process's run died with that process; without this the
        # dashboard keeps showing its frozen progress bar (2026-07-14: a
        # manual run killed by a deploy restart "ran" for 3 h on screen).
        clear_stale_status()
        optimizer_task = asyncio.create_task(schedule_daily_optimization())

    # The debug API runs in the same event loop as the engine so it can
    # introspect live state. uvicorn owns SIGINT/SIGTERM: when the server
    # stops, the engine and optimizer are shut down in `finally`.
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(controller),
            host="0.0.0.0",
            port=API_PORT,
            log_level="warning",
        )
    )
    logger.info("Debug API listening on :%d (docs at /docs)", API_PORT)
    try:
        await server.serve()
    finally:
        if optimizer_task is not None:
            optimizer_task.cancel()
        if controller.bot is not None:
            controller.bot._shutdown.set()
        if controller.task is not None:
            try:
                await asyncio.wait_for(controller.task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass


if __name__ == "__main__":
    asyncio.run(main())
