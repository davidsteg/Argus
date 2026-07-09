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
(pre-market + regular + after-hours). Alpaca forbids bracket and market
orders outside the regular session, so every entry and exit is a plain
extended_hours limit order and the stop/target are SOFT: enforced by the
engine each poll cycle rather than resting on the exchange. Between polls
(poll_interval_seconds) price can gap through a level — the accepted
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
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetCalendarRequest,
    GetOrdersRequest,
    LimitOrderRequest,
)
from dotenv import load_dotenv

import regime
import universe
from analyst import get_analyst
from indicators import (
    bracket_distances,
    compute_atr,
    compute_rsi,
    compute_vwap,
    stop_is_floored,
)
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
US_EASTERN = ZoneInfo("America/New_York")

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
        self.data = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        self.db = get_db()
        self.sentiment = get_sentiment_provider()
        self.config: Dict[str, float] = self.db.get_config()
        self.watchlist: list = universe.get_watchlist()
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
        # (ET-date -> (open_utc, close_utc) | None) cache for the extended
        # trading session, so Alpaca's calendar is hit once a day, not per poll.
        self._session_cache: Optional[
            Tuple[str, Optional[Tuple[datetime, datetime]]]
        ] = None
        self._shutdown = asyncio.Event()
        self._cycle_count: int = 0
        self._review_task: Optional[asyncio.Task] = None
        self._screener_task: Optional[asyncio.Task] = None

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
        request = StockBarsRequest(
            symbol_or_symbols=targets,
            timeframe=TimeFrame.Minute,
            start=start,
        )
        try:
            bars = await asyncio.to_thread(self.data.get_stock_bars, request)
        except Exception as exc:
            logger.error("Failed to fetch 1-minute bars: %s", exc)
            self.db.add_log("ERROR", f"Bar fetch failed: {exc}")
            return {}

        frames: Dict[str, pd.DataFrame] = {}
        df = bars.df
        if df is None or df.empty:
            return frames
        for symbol in targets:
            try:
                symbol_df = df.xs(symbol, level="symbol")
            except KeyError:
                continue
            if not symbol_df.empty:
                frames[symbol] = symbol_df.sort_index()
        return frames

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

    async def place_limit_buy(self, signal: Dict[str, Any]) -> None:
        symbol = signal["symbol"]
        price = signal["price"]

        # The signal price comes from the last completed 1-minute bar, which
        # can be up to poll_interval_seconds stale. On cheap, low-volatility
        # tickers the ATR-scaled stop distance is only a few cents, so a
        # bracket priced off a stale bar routinely landed on the wrong side
        # of Alpaca's live quote and was rejected outright. Re-price against
        # the latest trade immediately before submission; fall back to the
        # bar price if the live quote is unavailable.
        try:
            latest_trade = await asyncio.to_thread(
                self.data.get_stock_latest_trade,
                StockLatestTradeRequest(symbol_or_symbols=symbol),
            )
            price = float(latest_trade[symbol].price)
        except Exception as exc:
            logger.warning(
                "Latest-trade fetch failed for %s, using bar price: %s",
                symbol,
                exc,
            )

        # Brackets scale with the symbol's own volatility (ATR multiples
        # tuned nightly by the optimizer, floored in indicators.py).
        stop_distance, target_distance = bracket_distances(
            price,
            signal["atr"],
            self.config["atr_stop_mult"],
            self.config["atr_target_mult"],
        )

        # Volatility-scaled sizing: risk a roughly constant dollar amount
        # per trade, capped by the notional position size. Wide stop on a
        # volatile name → fewer shares; tight stop on a quiet name → more.
        pos_size = self.config.get("position_size_usd", 500.0)
        risk_per = self.config.get("risk_per_trade_usd", 20.0)
        qty = int(pos_size // price)
        if risk_per > 0:
            qty = min(qty, int(risk_per // stop_distance))
        if qty < 1:
            self.db.add_log(
                "WARNING",
                f"{symbol}: cannot size a whole share within notional "
                f"${pos_size:.0f} and risk ${risk_per:.0f} "
                f"(price ${price:.2f}, stop distance ${stop_distance:.2f})",
            )
            return

        take_profit = round(price + target_distance, 2)
        stop_loss = round(price - stop_distance, 2)
        # Penny rounding must never collapse a level onto the entry price.
        # The 2-cent floor (not Alpaca's bare 1-cent minimum) leaves a little
        # slack for the price to keep moving between this quote and the fill.
        take_profit = max(take_profit, round(price + 0.02, 2))
        stop_loss = min(stop_loss, round(price - 0.02, 2))

        # Extended hours forbids market and bracket orders, so enter with a
        # marketable limit — priced entry_slip_pct through the last trade so it
        # fills in a thin book — and enforce the stop/target as soft levels
        # each cycle (see evaluate_and_close_stops).
        slip = self.config.get("entry_slip_pct", 0.001)
        limit_price = max(round(price * (1 + slip), 2), round(price + 0.02, 2))
        order_request = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            extended_hours=True,
            limit_price=limit_price,
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
            "entry_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "entry_rsi": signal["rsi"],
            "entry_vwap": signal["vwap"],
            "entry_atr": signal["atr"],
            "entry_sentiment": signal["sentiment"],
            "sentiment_source": signal.get("sentiment_source"),
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "entry_reason": (
                f"RSI {signal['rsi']:.1f} was oversold (below the "
                f"{self.config['rsi_buy_signal']:.0f} buy level) while price "
                f"${price:.2f} sat below VWAP ${signal['vwap']:.2f} — a genuine "
                f"mean-reversion dip, not a falling knife. News sentiment "
                f"{signal['sentiment']:.2f} ({signal.get('sentiment_source', '?')}) "
                f"cleared the {self.config['news_cutoff']:.2f} cutoff, so a long "
                f"was opened with a {self.config['atr_stop_mult']:.1f}×ATR stop "
                f"and {self.config['atr_target_mult']:.1f}×ATR target."
            ),
        }
        self.db.add_log(
            "TRADE",
            f"BUY {qty} {symbol} @ ~${price:.2f} | RSI {signal['rsi']:.1f} | "
            f"VWAP ${signal['vwap']:.2f} | ATR ${signal['atr']:.3f} | "
            f"sentiment {signal['sentiment']:.2f} "
            f"({signal.get('sentiment_source', '?')}) | TP ${take_profit:.2f} | "
            f"SL ${stop_loss:.2f} | risk ~${qty * stop_distance:.0f} | "
            f"order {getattr(order, 'id', '?')}",
        )
        logger.info("Submitted limit BUY for %s x%d @ ~%.2f", symbol, qty, price)

    async def place_limit_short(self, signal: Dict[str, Any]) -> None:
        """Submit an extended-hours limit SELL (short) order. Take-profit sits
        below entry and stop-loss above; both are soft levels enforced each
        cycle rather than resting bracket legs (forbidden in extended hours)."""
        symbol = signal["symbol"]
        price = signal["price"]

        try:
            latest_trade = await asyncio.to_thread(
                self.data.get_stock_latest_trade,
                StockLatestTradeRequest(symbol_or_symbols=symbol),
            )
            price = float(latest_trade[symbol].price)
        except Exception as exc:
            logger.warning(
                "Latest-trade fetch failed for %s, using bar price: %s",
                symbol,
                exc,
            )

        stop_distance, target_distance = bracket_distances(
            price,
            signal["atr"],
            self.config["atr_stop_mult"],
            self.config["atr_target_mult"],
        )

        pos_size = self.config.get("position_size_usd", 500.0)
        risk_per = self.config.get("risk_per_trade_usd", 20.0)
        qty = int(pos_size // price)
        if risk_per > 0:
            qty = min(qty, int(risk_per // stop_distance))
        if qty < 1:
            self.db.add_log(
                "WARNING",
                f"{symbol}: cannot size a whole share within notional "
                f"${pos_size:.0f} and risk ${risk_per:.0f} "
                f"(price ${price:.2f}, stop distance ${stop_distance:.2f})",
            )
            return

        # Short: take-profit is BELOW entry, stop-loss is ABOVE entry (soft
        # levels enforced by evaluate_and_close_stops).
        take_profit = round(price - target_distance, 2)
        stop_loss = round(price + stop_distance, 2)
        take_profit = min(take_profit, round(price - 0.02, 2))
        stop_loss = max(stop_loss, round(price + 0.02, 2))

        # Marketable limit priced entry_slip_pct BELOW the last trade so the
        # short fills in a thin extended-hours book.
        slip = self.config.get("entry_slip_pct", 0.001)
        limit_price = min(round(price * (1 - slip), 2), round(price - 0.02, 2))
        order_request = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            extended_hours=True,
            limit_price=limit_price,
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
            "entry_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "entry_rsi": signal["rsi"],
            "entry_vwap": signal["vwap"],
            "entry_atr": signal["atr"],
            "entry_sentiment": signal["sentiment"],
            "sentiment_source": signal.get("sentiment_source"),
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "entry_reason": (
                f"RSI {signal['rsi']:.1f} was overbought (above the "
                f"{self.config['rsi_short_signal']:.0f} short level) while price "
                f"${price:.2f} sat above VWAP ${signal['vwap']:.2f} — an orderly "
                f"overextension, not a parabolic squeeze. News sentiment "
                f"{signal['sentiment']:.2f} ({signal.get('sentiment_source', '?')}) "
                f"stayed below the {1.0 - self.config['news_cutoff']:.2f} short "
                f"cutoff (not too bullish to fade), so a short was opened with a "
                f"{self.config['atr_stop_mult']:.1f}×ATR stop and "
                f"{self.config['atr_target_mult']:.1f}×ATR target."
            ),
        }
        self.db.add_log(
            "TRADE",
            f"SELL {qty} {symbol} @ ~${price:.2f} | RSI {signal['rsi']:.1f} | "
            f"VWAP ${signal['vwap']:.2f} | ATR ${signal['atr']:.3f} | "
            f"sentiment {signal['sentiment']:.2f} "
            f"({signal.get('sentiment_source', '?')}) | TP ${take_profit:.2f} | "
            f"SL ${stop_loss:.2f} | risk ~${qty * stop_distance:.0f} | "
            f"order {getattr(order, 'id', '?')}",
        )
        logger.info("Submitted limit SELL for %s x%d @ ~%.2f", symbol, qty, price)

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
        """Best available current price: freshest 1-minute bar close, else the
        latest trade, else the position's last synced price."""
        bars = frames.get(symbol)
        if bars is not None and not bars.empty:
            return float(bars["close"].iloc[-1])
        try:
            latest = await asyncio.to_thread(
                self.data.get_stock_latest_trade,
                StockLatestTradeRequest(symbol_or_symbols=symbol),
            )
            return float(latest[symbol].price)
        except Exception as exc:
            logger.warning("Latest-price fetch failed for %s: %s", symbol, exc)
        current = position.get("current_price")
        return None if current is None else float(current)

    async def evaluate_and_close_stops(
        self, portfolio: Dict[str, Any], frames: Dict[str, pd.DataFrame]
    ) -> Dict[str, str]:
        """Protective soft stop/target for held positions — the replacement for
        the exchange-side bracket, which Alpaca forbids in extended hours.

        Each cycle, compare the latest price against the stop_loss/take_profit
        recorded at entry and close the position when a level is breached. This
        is polled every poll_interval_seconds, so price can gap through a level
        between checks — the accepted tradeoff for trading the extended
        session. Runs before the entry gates so a stop is always honoured."""
        exits: Dict[str, str] = {}
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
            await self._limit_close(symbol, position)
            exits[symbol] = f"soft-{hit}-{side}"
            self.db.add_log(
                "TRADE",
                f"SOFT {hit.upper()} {symbol} x{abs(qty):g} ({side}) @ "
                f"~${price:.2f} — ${level:.2f} {hit} level breached",
            )
        return exits

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

    @staticmethod
    def _position_dict(p: Any) -> Dict[str, Any]:
        """Minimal position dict (the fields _limit_close needs) from a live
        Alpaca position object."""
        return {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "current_price": (
                None if p.current_price is None else float(p.current_price)
            ),
            "avg_entry_price": float(p.avg_entry_price),
        }

    async def _limit_close(
        self, symbol: str, position: Dict[str, Any]
    ) -> Optional[Any]:
        """Close a held position with an extended-hours limit order — market
        and bracket closes are rejected outside the regular session. Cancels
        any resting orders first, then submits a marketable limit (exit_slip_pct
        through the last trade) on the opposite side for the full quantity, so
        the close actually fills in a thin book. Returns the submitted order, or
        None on failure. The fill is reconciled into a trade record on the next
        cycle by sync_portfolio — the single trade-recording path."""
        self._closing.add(symbol)
        await self._cancel_symbol_orders(symbol)
        raw_qty = float(position.get("qty", 0.0))
        qty = abs(raw_qty)
        if qty <= 0:
            self._closing.discard(symbol)
            return None
        is_long = raw_qty > 0
        side = OrderSide.SELL if is_long else OrderSide.BUY

        price = position.get("current_price")
        try:
            latest = await asyncio.to_thread(
                self.data.get_stock_latest_trade,
                StockLatestTradeRequest(symbol_or_symbols=symbol),
            )
            price = float(latest[symbol].price)
        except Exception as exc:
            logger.warning("Latest-trade fetch failed for %s close: %s", symbol, exc)
        if price is None:
            price = float(position.get("avg_entry_price", 0.0))

        # Sell through the bid / buy through the ask so the close fills.
        slip = self.config.get("exit_slip_pct", 0.002)
        if is_long:
            limit_price = max(round(price * (1 - slip), 2), 0.01)
        else:
            limit_price = round(price * (1 + slip), 2)
        order_request = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            extended_hours=True,
            limit_price=limit_price,
        )
        try:
            return await asyncio.to_thread(self.trading.submit_order, order_request)
        except Exception as exc:
            self._closing.discard(symbol)
            logger.error("Limit close failed for %s: %s", symbol, exc)
            self.db.add_log("ERROR", f"{symbol}: limit close failed: {exc}")
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

        snapshot = [
            {
                "symbol": p.symbol,
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
        self.db.replace_positions(snapshot)

        live_symbols = {p["symbol"] for p in snapshot}
        # Drop close-in-flight guards for positions that have now exited
        # (their signal-exit close filled); keep guards for any still held so
        # the next cycle does not double-submit a close.
        self._closing.intersection_update(live_symbols)
        for symbol, entry in list(self._open_entries.items()):
            if symbol in live_symbols:
                continue
            await self.reconcile_closed_trade(symbol, entry)
            del self._open_entries[symbol]

        # Adopt positions opened outside this process (e.g. before a restart)
        # so their eventual close still produces a trade record.
        for pos in snapshot:
            qty = pos["qty"]
            self._open_entries.setdefault(
                pos["symbol"],
                {
                    "qty": abs(qty),
                    "entry_price": pos["avg_entry_price"],
                    "side": "BUY" if qty > 0 else "SELL",
                    "entry_time": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                },
            )

        equity = float(account.equity)
        self.db.set_status(equity=equity)
        self.db.record_equity(equity)
        return {"equity": equity, "positions": snapshot}

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
        # The exit order's side is the opposite of the entry: a long exits
        # with a SELL, a short covers with a BUY. Matching on SELL for a
        # short would find the short's own entry order and record ~zero PnL.
        exit_side = OrderSide.BUY if side == "SELL" else OrderSide.SELL
        exit_price: Optional[float] = None
        try:
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

        # Bench losers: an unknown exit price is treated as a loss too —
        # the conservative assumption when reconciliation could not find
        # the fill.
        if realized is None or realized < 0:
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
            await self._limit_close(p.symbol, self._position_dict(p))

    async def kill_sequence(self, reason: str) -> None:
        """Emergency shutdown: flatten everything, persist KILLED, stop.

        Liquidation uses extended-hours limit closes (marketable, priced
        exit_slip_pct through the last trade) because market and
        close_all_positions liquidation are rejected outside regular hours."""
        self.db.add_log("CRITICAL", f"KILL SEQUENCE INITIATED: {reason}")
        try:
            await asyncio.to_thread(self.trading.cancel_orders)
            self.db.add_log("CRITICAL", "All open orders cancelled")
        except Exception as exc:
            logger.error("Cancel-all failed during kill sequence: %s", exc)
            self.db.add_log("ERROR", f"Cancel-all failed: {exc}")
        try:
            positions = await asyncio.to_thread(self.trading.get_all_positions)
            for p in positions:
                await self._limit_close(p.symbol, self._position_dict(p))
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
    # extended trading session (US-Eastern)
    # ------------------------------------------------------------------ #

    async def _extended_session_bounds(
        self,
    ) -> Optional[Tuple[datetime, datetime]]:
        """(open_utc, close_utc) for today's extended session, or None when
        today is not a trading day.

        Pre-market always opens at 4:00 AM ET; after-hours always runs four
        hours past the regular close (→ 8:00 PM on a normal day, 5:00 PM on a
        half-day). Deriving the extended close from the regular close — which
        alpaca-py's Calendar reliably exposes — keeps half-days correct without
        depending on the optional session_open/session_close calendar fields.
        Cached per US-Eastern calendar date; a fetch failure returns None
        uncached so the next cycle retries."""
        today_et = datetime.now(US_EASTERN).date()
        cache_key = today_et.isoformat()
        if self._session_cache is not None and self._session_cache[0] == cache_key:
            return self._session_cache[1]

        try:
            calendars = await asyncio.to_thread(
                self.trading.get_calendar,
                GetCalendarRequest(start=today_et, end=today_et),
            )
        except Exception as exc:
            logger.error("Calendar fetch failed: %s", exc)
            self.db.add_log("ERROR", f"Calendar fetch failed: {exc}")
            return None

        bounds: Optional[Tuple[datetime, datetime]] = None
        for cal in calendars:
            if cal.date != today_et:
                continue
            open_et = datetime.combine(cal.date, dtime(4, 0), tzinfo=US_EASTERN)
            close_et = (
                datetime.combine(cal.date, cal.close, tzinfo=US_EASTERN)
                + timedelta(hours=4)
            )
            bounds = (
                open_et.astimezone(timezone.utc),
                close_et.astimezone(timezone.utc),
            )
            break

        self._session_cache = (cache_key, bounds)
        return bounds

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
        cfg = self.config
        self.db.add_log(
            "INFO",
            f"Argus engine started (paper trading) — universe "
            f"{universe.describe_mode()} | position size ${cfg.get('position_size_usd', 500):.0f} "
            f"| risk/trade ${cfg.get('risk_per_trade_usd', 20):.0f} | max positions "
            f"{cfg.get('max_positions', 5):.0f} | daily stop ${cfg.get('daily_stop_loss', 100):.0f} | loser "
            f"cooldown {cfg.get('cooldown_minutes', 30):.0f}m | regime filter on "
            f"{regime.REGIME_SYMBOL}",
        )
        logger.info("Argus engine started — universe %s", universe.describe_mode())

        # Publish operational environment (no secrets) so the Settings tab
        # can display and edit them.
        self.db.set_state(
            "environment",
            {
                "universe_mode": universe.describe_mode(),
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
                "regime_symbol": regime.REGIME_SYMBOL,
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

        # Refresh the trading universe (no-op in static mode, cached in
        # dynamic whole-market mode).
        self.watchlist = await asyncio.to_thread(universe.get_watchlist)
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

        # Trade the full extended session (4:00 AM – 8:00 PM ET). Alpaca's
        # regular-hours clock (is_open) would gate us to 9:30–4:00 only, so we
        # derive the extended window from the trading calendar instead.
        session = await self._extended_session_bounds()
        now_utc = datetime.now(timezone.utc)
        in_session = session is not None and session[0] <= now_utc < session[1]
        cycle["market_open"] = in_session
        if not in_session:
            if session is None:
                logger.info("Not a trading day — sitting out")
                cycle["stage"] = "market-closed"
            else:
                logger.info("Outside extended session — next open %s", session[0])
                cycle["stage"] = "market-closed"
                cycle["next_open"] = str(session[0])
            return

        # End-of-day flatten: entries are DAY limit orders that expire at the
        # extended close, which would leave any surviving position orphaned
        # overnight. Flatten with a margin before 8:00 PM ET; no new entries
        # are evaluated inside the window.
        flatten_minutes = self.config.get("eod_flatten_minutes", 10.0)
        if flatten_minutes > 0:
            until_close = session[1] - now_utc
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

        # Market regime shapes how much book we run this cycle: TREND_DOWN
        # (index falling on stressed vol) blocks new longs outright — every
        # dip is a knife — while shorts stay allowed since a falling market
        # favours them. CAUTION (trend down OR vol elevated, like the losing
        # 2026-07-08 session) halves the position cap instead of trading at
        # full throttle. Existing positions keep their soft stop/target and
        # the daily stop still guards them.
        regime_info = await asyncio.to_thread(regime.get_regime)
        cycle["regime"] = regime_info
        regime_blocks = regime.blocks_new_entries(regime_info)

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
        short_enabled = bool(self.config.get("short_enabled", 0.0))
        if regime_blocks:
            if not short_enabled:
                self.db.add_log(
                    "INFO",
                    f"Regime TREND_DOWN ({regime_info['symbol']} "
                    f"${regime_info.get('close', 0):.2f} < EMA "
                    f"${regime_info.get('ema', 0):.2f}, realized vol "
                    f"{regime_info.get('realized_vol_pct', 0):.0f}%) — "
                    f"no new entries this cycle (shorts disabled)",
                )
                cycle["stage"] = "risk-off"
                return
            self.db.add_log(
                "INFO",
                f"Regime TREND_DOWN — BUY entries blocked, SELL entries allowed",
            )

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

        # Filter BUY signals in TREND_DOWN regime
        if regime_blocks:
            pending_signals = [s for s in pending_signals if s["side"] != "BUY"]
            if not pending_signals:
                self.db.add_log(
                    "INFO",
                    "Regime TREND_DOWN — no SELL signals this cycle",
                )
                cycle["stage"] = "risk-off"
                return

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
                elif s["symbol"] not in approved:
                    self.db.add_log(
                        "ANALYST",
                        f"Portfolio manager did not approve {s['symbol']} — skipped",
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

        # Periodic opportunity screener runs in the background.
        if self._screener_task is None or self._screener_task.done():
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

        # Periodic LLM watchlist curation (if analyst is enabled). The
        # override carries a timestamp so universe.py can expire it.
        try:
            if analyst.should_review_watchlist():
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
