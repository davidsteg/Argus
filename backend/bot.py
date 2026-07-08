"""
Argus — asynchronous short-term trading engine.

Philosophy: trade small amounts on a large scale for quick flips.
1-minute bars are the execution trigger; a SPY-based market regime filter
(regime.py) blocks entries when the whole tape is falling; a VWAP check
confirms the dip is real; curated news sentiment is the directional
filter. Exits are volatility-adaptive: bracket stop/target distances are
ATR multiples, and position size is chosen so each trade risks a roughly
constant dollar amount. Nightly walk-forward optimization (optimizer.py)
re-tunes the strategy parameters — validated out-of-sample — in the
shared bot_config table, which this engine re-reads on every cycle so
new parameters are absorbed seamlessly without a restart.

Safety:
* Alpaca Paper Trading is forced (paper=True). No hardcoded secrets —
  credentials come exclusively from the environment.
* A hard daily loss limit triggers the emergency kill-sequence: cancel all
  open orders, liquidate all positions, persist KILLED, shut down.
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
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)
from dotenv import load_dotenv

import regime
import universe
from analyst import get_analyst
from indicators import bracket_distances, compute_atr, compute_rsi, compute_vwap
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
POSITION_SIZE_USD = float(os.getenv("POSITION_SIZE_USD", "500"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS", "5"))
DAILY_STOP_LOSS = float(os.getenv("DAILY_STOP_LOSS", "100"))
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "60"))
BAR_LOOKBACK_MINUTES = int(os.getenv("BAR_LOOKBACK_MINUTES", "180"))
# Quick flips need liquid, penny-increment names; sub-$MIN_PRICE symbols
# from the most-actives screener are skipped.
MIN_PRICE_USD = float(os.getenv("MIN_PRICE_USD", "5"))
API_PORT = int(os.getenv("API_PORT", "8000"))
# Volatility-scaled sizing: shares are chosen so that hitting the stop
# loses about this many dollars, capped by POSITION_SIZE_USD notional.
# Set to 0 to disable and size purely by notional.
RISK_PER_TRADE_USD = float(os.getenv("RISK_PER_TRADE_USD", "20"))
# After a losing exit a symbol is benched for this long — a stopped-out
# dip that keeps falling keeps triggering RSI, and re-buying the same
# knife each minute is how mean reversion bleeds out.
COOLDOWN_MINUTES = float(os.getenv("COOLDOWN_MINUTES", "30"))


class ArgusBot:
    """Asynchronous 1-minute execution engine with bracket-order exits."""

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
        # symbol -> time.monotonic() deadline until which entries are benched
        self._cooldowns: Dict[str, float] = {}
        self._current_day: Optional[str] = None
        self._shutdown = asyncio.Event()

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
        if COOLDOWN_MINUTES > 0:
            self._cooldowns[symbol] = time.monotonic() + COOLDOWN_MINUTES * 60.0

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

    async def fetch_minute_bars(self) -> Dict[str, pd.DataFrame]:
        """Fetch recent 1-minute bars for the whole watchlist in one request."""
        if not self.watchlist:
            return {}
        start = datetime.now(timezone.utc) - timedelta(minutes=BAR_LOOKBACK_MINUTES)
        request = StockBarsRequest(
            symbol_or_symbols=self.watchlist,
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
        for symbol in self.watchlist:
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
        """Layered BUY decision: RSI trigger → cooldown → VWAP dip
        confirmation → news sentiment. Cheap technical gates run first so
        the LLM is only consulted for genuine candidates."""
        period = max(int(self.config["rsi_period"]), 2)
        if len(bars) < period * 2:
            return None

        rsi_series = compute_rsi(bars["close"], period)
        latest_rsi = float(rsi_series.iloc[-1])
        latest_close = float(bars["close"].iloc[-1])
        if np.isnan(latest_rsi) or latest_close < MIN_PRICE_USD:
            return None

        if latest_rsi >= self.config["rsi_buy_signal"]:
            return None

        cooldown_left = self.in_cooldown(symbol)
        if cooldown_left is not None:
            self.db.add_log(
                "INFO",
                f"{symbol}: RSI {latest_rsi:.1f} triggered but symbol is in "
                f"post-loss cooldown for another {cooldown_left:.0f}m — skipped",
            )
            return None

        # Mean reversion needs a genuine dip: price stretched below the
        # session's volume-weighted fair value, not an RSI artifact.
        vwap = float(compute_vwap(bars).iloc[-1])
        if latest_close > vwap:
            self.db.add_log(
                "INFO",
                f"{symbol}: RSI {latest_rsi:.1f} triggered but price "
                f"${latest_close:.2f} is above VWAP ${vwap:.2f} — not a real "
                f"dip, skipped",
            )
            return None

        atr = float(compute_atr(bars).iloc[-1])
        if np.isnan(atr) or atr <= 0:
            return None

        sentiment, source = await self.process_news_sentiment(symbol)
        if sentiment <= self.config["news_cutoff"]:
            self.db.add_log(
                "INFO",
                f"{symbol}: RSI {latest_rsi:.1f} triggered but sentiment "
                f"{sentiment:.2f} ({source}) <= cutoff "
                f"{self.config['news_cutoff']:.2f} — skipped",
            )
            return None

        return {
            "symbol": symbol,
            "price": latest_close,
            "rsi": latest_rsi,
            "vwap": vwap,
            "atr": atr,
            "sentiment": sentiment,
            "sentiment_source": source,
        }

    # ------------------------------------------------------------------ #
    # order placement
    # ------------------------------------------------------------------ #

    async def place_bracket_buy(self, signal: Dict[str, Any]) -> None:
        symbol = signal["symbol"]
        price = signal["price"]

        # The signal price comes from the last completed 1-minute bar, which
        # can be up to POLL_INTERVAL_SECONDS stale. On cheap, low-volatility
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
        qty = int(POSITION_SIZE_USD // price)
        if RISK_PER_TRADE_USD > 0:
            qty = min(qty, int(RISK_PER_TRADE_USD // stop_distance))
        if qty < 1:
            self.db.add_log(
                "WARNING",
                f"{symbol}: cannot size a whole share within notional "
                f"${POSITION_SIZE_USD:.0f} and risk ${RISK_PER_TRADE_USD:.0f} "
                f"(price ${price:.2f}, stop distance ${stop_distance:.2f})",
            )
            return

        take_profit = round(price + target_distance, 2)
        stop_loss = round(price - stop_distance, 2)
        # Penny rounding must never collapse a bracket onto the entry price.
        # The 2-cent floor (not Alpaca's bare 1-cent minimum) leaves a little
        # slack for the price to keep moving between this quote and the fill.
        take_profit = max(take_profit, round(price + 0.02, 2))
        stop_loss = min(stop_loss, round(price - 0.02, 2))

        order_request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=take_profit),
            stop_loss=StopLossRequest(stop_price=stop_loss),
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
            "entry_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
        logger.info("Submitted bracket BUY for %s x%d @ ~%.2f", symbol, qty, price)

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
        for symbol, entry in list(self._open_entries.items()):
            if symbol in live_symbols:
                continue
            await self.reconcile_closed_trade(symbol, entry)
            del self._open_entries[symbol]

        # Adopt positions opened outside this process (e.g. before a restart)
        # so their eventual close still produces a trade record.
        for pos in snapshot:
            self._open_entries.setdefault(
                pos["symbol"],
                {
                    "qty": pos["qty"],
                    "entry_price": pos["avg_entry_price"],
                    "entry_time": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                },
            )

        equity = float(account.equity)
        self.db.set_status(equity=equity)
        self.db.record_equity(equity)
        return {"equity": equity, "positions": snapshot}

    async def reconcile_closed_trade(self, symbol: str, entry: Dict[str, Any]) -> None:
        """A tracked position vanished — find its exit fill and log the trade."""
        exit_price: Optional[float] = None
        try:
            closed_orders = await asyncio.to_thread(
                self.trading.get_orders,
                GetOrdersRequest(
                    status=QueryOrderStatus.CLOSED, symbols=[symbol], limit=10
                ),
            )
            for order in closed_orders:
                if order.side == OrderSide.SELL and order.filled_avg_price:
                    exit_price = float(order.filled_avg_price)
                    break
        except Exception as exc:
            logger.error("Exit reconciliation failed for %s: %s", symbol, exc)
            self.db.add_log("ERROR", f"{symbol}: exit reconciliation failed: {exc}")

        qty = float(entry["qty"])
        entry_price = float(entry["entry_price"])
        realized = None if exit_price is None else (exit_price - entry_price) * qty
        exit_time = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.db.record_trade(
            symbol=symbol,
            side="LONG",
            qty=qty,
            entry_price=entry_price,
            exit_price=exit_price,
            entry_time=entry["entry_time"],
            exit_time=exit_time,
            realized_pnl=realized,
        )
        pnl_text = "unknown PnL" if realized is None else f"PnL ${realized:+.2f}"
        self.db.add_log("TRADE", f"CLOSED {symbol} x{qty:g} — {pnl_text}")

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
        if status["daily_start_balance"] > 0 and daily_pnl <= -DAILY_STOP_LOSS:
            logger.critical(
                "Daily loss limit breached: %.2f <= -%.2f", daily_pnl, DAILY_STOP_LOSS
            )
            await self.kill_sequence(
                f"Daily loss ${-daily_pnl:.2f} breached limit ${DAILY_STOP_LOSS:.2f}"
            )
            return False
        return True

    async def kill_sequence(self, reason: str) -> None:
        """Emergency shutdown: flatten everything, persist KILLED, stop."""
        self.db.add_log("CRITICAL", f"KILL SEQUENCE INITIATED: {reason}")
        try:
            await asyncio.to_thread(self.trading.cancel_orders)
            self.db.add_log("CRITICAL", "All open orders cancelled")
        except Exception as exc:
            logger.error("Cancel-all failed during kill sequence: %s", exc)
            self.db.add_log("ERROR", f"Cancel-all failed: {exc}")
        try:
            await asyncio.to_thread(
                self.trading.close_all_positions, cancel_orders=True
            )
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
        """Reset the daily PnL baseline at Swiss midnight."""
        today = datetime.now(ZURICH).strftime("%Y-%m-%d")
        if self._current_day == today:
            return
        self._current_day = today
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
        self.db.add_log(
            "INFO",
            f"Argus engine started (paper trading) — universe "
            f"{universe.describe_mode()} | position size ${POSITION_SIZE_USD:.0f} "
            f"| risk/trade ${RISK_PER_TRADE_USD:.0f} | max positions "
            f"{MAX_POSITIONS} | daily stop ${DAILY_STOP_LOSS:.0f} | loser "
            f"cooldown {COOLDOWN_MINUTES:.0f}m | regime filter on "
            f"{regime.REGIME_SYMBOL}",
        )
        logger.info("Argus engine started — universe %s", universe.describe_mode())

        # Operational knobs are env vars the dashboard cannot see otherwise;
        # publish them (no secrets) so the Settings tab can display them.
        self.db.set_state(
            "environment",
            {
                "universe_mode": universe.describe_mode(),
                "watchlist_size": len(self.watchlist),
                "position_size_usd": POSITION_SIZE_USD,
                "risk_per_trade_usd": RISK_PER_TRADE_USD,
                "max_positions": MAX_POSITIONS,
                "daily_stop_loss": DAILY_STOP_LOSS,
                "min_price_usd": MIN_PRICE_USD,
                "cooldown_minutes": COOLDOWN_MINUTES,
                "poll_interval_seconds": POLL_INTERVAL_SECONDS,
                "bar_lookback_minutes": BAR_LOOKBACK_MINUTES,
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
                    self._shutdown.wait(), timeout=POLL_INTERVAL_SECONDS
                )
            except asyncio.TimeoutError:
                pass

        logger.info("Argus engine stopped")

    async def run_cycle(self) -> None:
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

        try:
            clock = await asyncio.to_thread(self.trading.get_clock)
        except Exception as exc:
            logger.error("Clock fetch failed: %s", exc)
            self.db.add_log("ERROR", f"Clock fetch failed: {exc}")
            cycle["stage"] = "clock-fetch-failed"
            return
        cycle["market_open"] = clock.is_open
        if not clock.is_open:
            logger.info("Market closed — next open %s", clock.next_open)
            cycle["stage"] = "market-closed"
            cycle["next_open"] = str(clock.next_open)
            return

        held_symbols = {p["symbol"] for p in portfolio["positions"]}
        open_slots = MAX_POSITIONS - len(held_symbols)
        cycle["held_symbols"] = sorted(held_symbols)
        cycle["open_slots"] = open_slots
        if open_slots <= 0:
            cycle["stage"] = "max-positions"
            return

        # Market regime gate: in RISK_OFF (index falling on stressed vol)
        # every dip is a knife — stop opening new positions. Existing
        # positions keep their brackets; the daily stop still guards them.
        regime_info = await asyncio.to_thread(regime.get_regime)
        cycle["regime"] = regime_info
        if regime.blocks_new_entries(regime_info):
            self.db.add_log(
                "INFO",
                f"Regime RISK_OFF ({regime_info['symbol']} "
                f"${regime_info.get('close', 0):.2f} < EMA "
                f"${regime_info.get('ema', 0):.2f}, realized vol "
                f"{regime_info.get('realized_vol_pct', 0):.0f}%) — "
                f"no new entries this cycle",
            )
            cycle["stage"] = "risk-off"
            return

        frames = await self.fetch_minute_bars()
        cycle["symbols_with_bars"] = len(frames)
        evaluated: Dict[str, str] = {}
        for symbol, bars in frames.items():
            if open_slots <= 0:
                break
            if symbol in held_symbols:
                evaluated[symbol] = "held"
                continue
            signal_info = await self.evaluate_signal(symbol, bars)
            if signal_info is not None:
                await self.place_bracket_buy(signal_info)
                evaluated[symbol] = "buy"
                open_slots -= 1
            else:
                evaluated[symbol] = "no-signal"
        cycle["evaluated"] = evaluated
        cycle["stage"] = "complete"

        # Periodic LLM trade review (if analyst is enabled and enough time
        # has passed since the last review).
        try:
            analyst = get_analyst()
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

        # Periodic LLM watchlist curation (if analyst is enabled).
        try:
            analyst = get_analyst()
            if analyst.should_review_watchlist():
                current_symbols = list(self.watchlist)
                new_symbols = await asyncio.to_thread(
                    analyst.review_watchlist,
                    current_symbols,
                    regime_info,
                    self.db,
                )
                if new_symbols and new_symbols != current_symbols:
                    self.db.set_state("watchlist_override", new_symbols)
                    self.db.add_log(
                        "ANALYST",
                        f"Watchlist updated: {len(current_symbols)} → "
                        f"{len(new_symbols)} symbols",
                    )
        except Exception as exc:
            logger.error("Watchlist review failed: %s", exc)


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
