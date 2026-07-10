"""
Argus — market adapters.

The engine (bot.py) is market-agnostic; everything that differs between US
equities and crypto lives behind a MarketAdapter injected at construction.
MARKET=equity|crypto (env) selects which adapter EngineController builds.

Why an adapter rather than a fork: the strategy and orchestration (RSI/VWAP
signal, soft stop/target, sizing, cooldown, analyst, decision memory, trade
reconciliation, DB layer) are single-sourced, so equity and crypto can never
drift. Only the market-specific seams are swapped:

* data client + bar/latest-price fetch (Stock vs Crypto),
* trading universe (most-actives vs the crypto asset list),
* session window (4 AM–8 PM ET extended session vs 24/7 always-open),
* order construction (DAY + extended_hours limit vs GTC limit; whole vs
  fractional qty; price/qty rounded to the crypto asset increments),
* market regime proxy (SPY vs BTC/USD, or fail-open),
* position partitioning + equity, because both engines share ONE Alpaca
  account: get_all_positions()/get_account() return the whole blended book,
  so each adapter keeps only its own asset class and computes its own equity.

Confirmed against alpaca-py 0.43.5: crypto .df has the same
['symbol','timestamp'] MultiIndex and OHLCV columns as stock bars; crypto
orders use TimeInForce.GTC (DAY is rejected), no extended_hours, and accept
fractional qty; the Asset model exposes min_order_size / min_trade_increment
/ price_increment for rounding.
"""

from __future__ import annotations

import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
from alpaca.data.historical import (
    CryptoHistoricalDataClient,
    StockHistoricalDataClient,
)
from alpaca.data.requests import (
    CryptoBarsRequest,
    CryptoLatestQuoteRequest,
    CryptoLatestTradeRequest,
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    AssetClass,
    AssetStatus,
    OrderClass,
    OrderSide,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetAssetsRequest,
    LimitOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

import regime
import universe

logger = logging.getLogger("argus.market")

US_EASTERN = ZoneInfo("America/New_York")

EQUITY = "equity"
CRYPTO = "crypto"

# Fiat/stable quote currencies of the crypto pairs this account trades. Used to
# recognise a crypto symbol regardless of which Alpaca API surfaced it: the
# positions API returns the compact form ("PAXGUSD") while the orders API, the
# universe and our own signals use the slashed form ("PAXG/USD"). Longest first
# so "USDT"/"USDC" match before the "USD" substring.
_CRYPTO_QUOTES = ("USDT", "USDC", "USDG", "USD")


def _is_crypto_symbol(symbol: str) -> bool:
    """True for a crypto symbol in either form (slashed "BTC/USD" or the
    slashless "BTCUSD" Alpaca returns for positions). Equities never carry a
    slash and are not USD-quoted pairs, so the two engines partition one shared
    account cleanly. The length guard keeps a real equity ticker like "USD"
    (ProShares Ultra Semiconductors) on the equity side."""
    s = symbol.upper()
    if "/" in s:
        return True
    return any(s.endswith(q) and len(s) > len(q) for q in _CRYPTO_QUOTES)


@dataclass
class SessionState:
    """Whether the market is currently tradable, plus the boundaries the engine
    needs. close_utc is None for a 24/7 market (no end-of-day flatten)."""

    open: bool
    next_open: Optional[datetime] = None
    close_utc: Optional[datetime] = None


class MarketAdapter(ABC):
    """Everything the engine needs that differs by asset class."""

    name: str
    #: True when this market's positions must be flattened before close_utc.
    flatten_before_close: bool
    #: True for a screener-driven universe (equities); False for a fixed asset
    #: list (crypto). Gates LLM watchlist curation and the equity opportunity
    #: screener, neither of which applies to the static crypto USD-pair set.
    dynamic_watchlist: bool = True

    def __init__(self, trading: TradingClient, api_key: str, secret_key: str) -> None:
        self.trading = trading
        self._api_key = api_key
        self._secret_key = secret_key

    # -- data ------------------------------------------------------------- #
    @abstractmethod
    def fetch_bars(
        self, symbols: List[str], start: datetime
    ) -> Dict[str, pd.DataFrame]:
        ...

    @abstractmethod
    def latest_price(self, symbol: str) -> Optional[float]:
        ...

    @abstractmethod
    def latest_quote(self, symbol: str) -> Optional[Tuple[float, float]]:
        """Live (bid, ask), or None when unavailable. Fresher than the last
        trade on a thin book, so entries/exits can price a marketable limit
        that actually crosses the spread."""
        ...

    # -- universe / ownership -------------------------------------------- #
    @abstractmethod
    def get_watchlist(self) -> List[str]:
        ...

    @abstractmethod
    def owns_symbol(self, symbol: str) -> bool:
        """One Alpaca account trades both markets; each engine keeps only the
        positions belonging to its own asset class."""
        ...

    # -- session --------------------------------------------------------- #
    @abstractmethod
    def session_state(self) -> SessionState:
        ...

    # -- orders / sizing ------------------------------------------------- #
    @abstractmethod
    def size_qty(
        self,
        symbol: str,
        pos_size: float,
        price: float,
        stop_distance: float,
        risk_per: float,
    ) -> float:
        """Position size: whole shares (equity) or fractional units (crypto),
        risk-capped, rounded to the market's increment. 0 = cannot size."""
        ...

    @abstractmethod
    def build_entry_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: float,
        ref_price: float,
        slip_pct: float,
    ) -> LimitOrderRequest:
        """Marketable-limit entry: priced slip_pct through ref_price so it fills
        in a thin book, with the market's TIF / extended-hours / qty rules."""
        ...

    @abstractmethod
    def build_close_order(
        self,
        symbol: str,
        is_long: bool,
        qty: float,
        ref_price: float,
        slip_pct: float,
    ) -> LimitOrderRequest:
        """Marketable-limit close of the full held qty on the opposite side."""
        ...

    def bracket_entry_allowed(self) -> bool:
        """True when an entry submitted right now may carry exchange-side
        bracket legs. Alpaca rejects bracket orders outside the regular
        equity session and for crypto entirely, so the default is False —
        the engine then records soft stop/target levels and enforces them
        itself each cycle."""
        return False

    def build_bracket_entry_order(
        self,
        symbol: str,
        side: OrderSide,
        qty: float,
        ref_price: float,
        slip_pct: float,
        take_profit: float,
        stop_loss: float,
    ) -> LimitOrderRequest:
        """Marketable-limit entry with resting OCO take-profit/stop legs.
        Only callable when bracket_entry_allowed() is True."""
        raise NotImplementedError(
            f"{self.name} market does not support native bracket orders"
        )

    # -- regime ---------------------------------------------------------- #
    @abstractmethod
    def regime(self) -> Dict[str, Any]:
        ...

    # -- equity ---------------------------------------------------------- #
    @abstractmethod
    def compute_equity(
        self, account: Any, own_positions: List[Dict[str, Any]], db: Any,
        all_positions: Optional[List[Any]] = None,
    ) -> float:
        ...

    # -- symbol / price conventions -------------------------------------- #
    def normalize_symbol(self, symbol: str) -> str:
        """Canonical form of a symbol as this engine tracks it. Equities pass
        through; the crypto adapter restores the slash Alpaca strips from
        position symbols so a held position matches the order that opened it."""
        return symbol.upper()

    def round_price(self, symbol: str, price: float) -> float:
        """Snap a price to the market's tick. Equity default: whole cents."""
        return round(price, 2)

    def min_tick(self, symbol: str) -> float:
        """Smallest meaningful price increment for the symbol (equity: 1¢)."""
        return 0.01


# ---------------------------------------------------------------------- #
# Equities — reproduces the pre-adapter behavior exactly.
# ---------------------------------------------------------------------- #
class EquityAdapter(MarketAdapter):
    name = EQUITY
    flatten_before_close = True

    def __init__(self, trading: TradingClient, api_key: str, secret_key: str) -> None:
        super().__init__(trading, api_key, secret_key)
        self.data = StockHistoricalDataClient(api_key, secret_key)
        # (ET-date -> {"regular": (open_utc, close_utc), "extended": (...)}
        # | None) — Alpaca's calendar hit once a day, not per poll.
        self._session_cache: Optional[
            Tuple[str, Optional[Dict[str, Tuple[datetime, datetime]]]]
        ] = None

    def fetch_bars(
        self, symbols: List[str], start: datetime
    ) -> Dict[str, pd.DataFrame]:
        request = StockBarsRequest(
            symbol_or_symbols=symbols, timeframe=TimeFrame.Minute, start=start
        )
        bars = self.data.get_stock_bars(request)
        return _frames_from_df(bars.df, symbols)

    def latest_price(self, symbol: str) -> Optional[float]:
        latest = self.data.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol)
        )
        return float(latest[symbol].price)

    def latest_quote(self, symbol: str) -> Optional[Tuple[float, float]]:
        try:
            quote = self.data.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol)
            )[symbol]
            bid = float(quote.bid_price or 0.0)
            ask = float(quote.ask_price or 0.0)
        except Exception as exc:
            logger.warning("Stock quote fetch failed for %s: %s", symbol, exc)
            return None
        return (bid, ask) if bid > 0 and ask > 0 else None

    def get_watchlist(self) -> List[str]:
        return universe.get_watchlist()

    def owns_symbol(self, symbol: str) -> bool:
        # One Alpaca account trades both markets; the equity engine keeps
        # everything that is NOT a crypto symbol (crypto positions come back
        # slashless as "PAXGUSD", which a bare '/' test used to misclassify).
        return not _is_crypto_symbol(symbol)

    def session_state(self) -> SessionState:
        bounds = self._extended_session_bounds()
        now_utc = datetime.now(timezone.utc)
        if bounds is None:
            return SessionState(open=False)
        open_utc, close_utc = bounds
        return SessionState(
            open=open_utc <= now_utc < close_utc,
            next_open=open_utc,
            close_utc=close_utc,
        )

    def _extended_session_bounds(self) -> Optional[Tuple[datetime, datetime]]:
        """(open_utc, close_utc) for today's extended session (4 AM–8 PM ET,
        earlier on half-days), or None when today is not a trading day."""
        bounds = self._session_bounds()
        return None if bounds is None else bounds["extended"]

    def _session_bounds(
        self,
    ) -> Optional[Dict[str, Tuple[datetime, datetime]]]:
        """Today's session boundaries from Alpaca's calendar, cached per ET
        date: "regular" is the exchange's own open/close (the only window in
        which Alpaca accepts bracket orders), "extended" is 4 AM ET through
        4 hours past the regular close (the engine's trading window, earlier
        on half-days). None when today is not a trading day; a failed
        calendar fetch is not cached, so the next cycle retries."""
        from alpaca.trading.requests import GetCalendarRequest

        today_et = datetime.now(US_EASTERN).date()
        cache_key = today_et.isoformat()
        if self._session_cache is not None and self._session_cache[0] == cache_key:
            return self._session_cache[1]

        try:
            calendars = self.trading.get_calendar(
                GetCalendarRequest(start=today_et, end=today_et)
            )
        except Exception as exc:
            logger.error("Calendar fetch failed: %s", exc)
            return None

        bounds: Optional[Dict[str, Tuple[datetime, datetime]]] = None
        for cal in calendars:
            if cal.date != today_et:
                continue
            open_tod = _et_time_of_day(cal.open)
            close_tod = _et_time_of_day(cal.close)
            regular_open = datetime.combine(cal.date, open_tod, tzinfo=US_EASTERN)
            regular_close = datetime.combine(cal.date, close_tod, tzinfo=US_EASTERN)
            ext_open = datetime.combine(cal.date, dtime(4, 0), tzinfo=US_EASTERN)
            ext_close = regular_close + timedelta(hours=4)
            bounds = {
                "regular": (
                    regular_open.astimezone(timezone.utc),
                    regular_close.astimezone(timezone.utc),
                ),
                "extended": (
                    ext_open.astimezone(timezone.utc),
                    ext_close.astimezone(timezone.utc),
                ),
            }
            break

        self._session_cache = (cache_key, bounds)
        return bounds

    def bracket_entry_allowed(self) -> bool:
        # Bracket legs are only accepted (and only rest) during the regular
        # session; outside it the engine falls back to soft enforcement.
        bounds = self._session_bounds()
        if bounds is None:
            return False
        open_utc, close_utc = bounds["regular"]
        return open_utc <= datetime.now(timezone.utc) < close_utc

    def size_qty(
        self, symbol, pos_size, price, stop_distance, risk_per
    ) -> float:
        qty = int(pos_size // price)
        if risk_per > 0:
            qty = min(qty, int(risk_per // stop_distance))
        return float(qty)

    def build_entry_order(
        self, symbol, side, qty, ref_price, slip_pct
    ) -> LimitOrderRequest:
        # Equities: extended-hours DAY limit, whole shares, ≥2¢ from ref so
        # penny rounding never collapses the limit onto the entry price.
        if side == OrderSide.BUY:
            limit = max(round(ref_price * (1 + slip_pct), 2), round(ref_price + 0.02, 2))
        else:
            limit = min(round(ref_price * (1 - slip_pct), 2), round(ref_price - 0.02, 2))
        return LimitOrderRequest(
            symbol=symbol,
            qty=int(qty),
            side=side,
            time_in_force=TimeInForce.DAY,
            extended_hours=True,
            limit_price=limit,
        )

    def build_close_order(
        self, symbol, is_long, qty, ref_price, slip_pct
    ) -> LimitOrderRequest:
        side = OrderSide.SELL if is_long else OrderSide.BUY
        if is_long:
            limit = max(round(ref_price * (1 - slip_pct), 2), 0.01)
        else:
            limit = round(ref_price * (1 + slip_pct), 2)
        return LimitOrderRequest(
            symbol=symbol,
            qty=int(qty),
            side=side,
            time_in_force=TimeInForce.DAY,
            extended_hours=True,
            limit_price=limit,
        )

    def build_bracket_entry_order(
        self, symbol, side, qty, ref_price, slip_pct, take_profit, stop_loss
    ) -> LimitOrderRequest:
        """Regular-session entry whose stop/target rest ON the exchange.

        The parent is the same marketable limit as build_entry_order (minus
        extended_hours, which Alpaca rejects on brackets); the take-profit
        rests as a limit and the stop-loss as a stop-market, held OCO by
        Alpaca — a breach fills immediately instead of waiting out the
        engine's poll cycle, which is where the Jul 8–10 soft stops lost
        2–4× their designed risk. Alpaca requires each leg strictly beyond
        the parent limit, so both are clamped at least a cent past it —
        callers must record the clamped leg prices, not their inputs."""
        if side == OrderSide.BUY:
            limit = max(
                round(ref_price * (1 + slip_pct), 2), round(ref_price + 0.02, 2)
            )
            take_profit = max(round(take_profit, 2), round(limit + 0.01, 2))
            stop_loss = min(round(stop_loss, 2), round(limit - 0.01, 2))
        else:
            limit = min(
                round(ref_price * (1 - slip_pct), 2), round(ref_price - 0.02, 2)
            )
            take_profit = min(round(take_profit, 2), round(limit - 0.01, 2))
            stop_loss = max(round(stop_loss, 2), round(limit + 0.01, 2))
        return LimitOrderRequest(
            symbol=symbol,
            qty=int(qty),
            side=side,
            time_in_force=TimeInForce.DAY,
            limit_price=limit,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=take_profit),
            stop_loss=StopLossRequest(stop_price=stop_loss),
        )

    def regime(self) -> Dict[str, Any]:
        return regime.get_regime()

    def compute_equity(
        self, account: Any, own_positions: List[Dict[str, Any]], db: Any,
        all_positions: Optional[List[Any]] = None,
    ) -> float:
        # Isolate the equity engine's equity from the crypto engine on the
        # shared Alpaca account. Subtract only the crypto positions' UNREALIZED
        # P&L, not their full market value: the cash that bought a crypto
        # position already left the shared pool, so account.equity only reflects
        # that position through its unrealized P&L. Subtracting the whole market
        # value double-counted the cost basis — a $200 crypto position looked
        # like a $200 equity-engine loss and tripped the daily stop (v2.20.0
        # exposed this once crypto could actually hold positions). Residual: a
        # crypto trade's realized P&L briefly touches this figure until the next
        # daily anchor; that is dollars, not the hundreds the old bug injected.
        crypto_upnl = 0.0
        if all_positions:
            crypto_upnl = sum(
                float(getattr(p, "unrealized_pl", 0.0) or 0.0)
                for p in all_positions
                if not self.owns_symbol(p.symbol)
            )
        return float(account.equity) - crypto_upnl

    def describe_mode(self) -> str:
        return universe.describe_mode()

    @property
    def regime_symbol(self) -> str:
        return regime.REGIME_SYMBOL


# ---------------------------------------------------------------------- #
# Crypto — spot, long-only, 24/7.
# ---------------------------------------------------------------------- #
class CryptoAdapter(MarketAdapter):
    name = CRYPTO
    flatten_before_close = False  # 24/7: never a scheduled flatten
    dynamic_watchlist = False     # fixed USD-pair universe, no screener/curation

    def __init__(self, trading: TradingClient, api_key: str, secret_key: str) -> None:
        super().__init__(trading, api_key, secret_key)
        # Crypto market data is public, but pass keys for rate limits.
        self.data = CryptoHistoricalDataClient(api_key, secret_key)
        # symbol -> {min_order_size, min_trade_increment, price_increment},
        # refreshed with the universe.
        self._assets: Dict[str, Dict[str, float]] = {}
        self._universe: List[str] = []
        self._universe_at: float = 0.0
        self._base_equity = float(os.getenv("CRYPTO_BASE_EQUITY_USD", "100000"))

    def fetch_bars(
        self, symbols: List[str], start: datetime
    ) -> Dict[str, pd.DataFrame]:
        request = CryptoBarsRequest(
            symbol_or_symbols=symbols, timeframe=TimeFrame.Minute, start=start
        )
        bars = self.data.get_crypto_bars(request)
        return _frames_from_df(bars.df, symbols)

    def latest_price(self, symbol: str) -> Optional[float]:
        latest = self.data.get_crypto_latest_trade(
            CryptoLatestTradeRequest(symbol_or_symbols=symbol)
        )
        return float(latest[symbol].price)

    def latest_quote(self, symbol: str) -> Optional[Tuple[float, float]]:
        try:
            quote = self.data.get_crypto_latest_quote(
                CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
            )[symbol]
            bid = float(quote.bid_price or 0.0)
            ask = float(quote.ask_price or 0.0)
        except Exception as exc:
            logger.warning("Crypto quote fetch failed for %s: %s", symbol, exc)
            return None
        return (bid, ask) if bid > 0 and ask > 0 else None

    def get_watchlist(self) -> List[str]:
        # All tradable USD crypto pairs, cached ~15 min. Also builds the
        # increment map used to round order qty/price.
        if self._universe and time.monotonic() - self._universe_at < 900:
            return list(self._universe)
        try:
            assets = self.trading.get_all_assets(
                GetAssetsRequest(
                    status=AssetStatus.ACTIVE, asset_class=AssetClass.CRYPTO
                )
            )
        except Exception as exc:
            logger.error("Crypto asset fetch failed: %s", exc)
            return list(self._universe)

        symbols: List[str] = []
        meta: Dict[str, Dict[str, float]] = {}
        for a in assets:
            if not a.tradable or not a.symbol.endswith("/USD"):
                continue
            symbols.append(a.symbol)
            meta[a.symbol] = {
                "min_order_size": float(a.min_order_size or 0.0),
                "min_trade_increment": float(a.min_trade_increment or 0.0),
                "price_increment": float(a.price_increment or 0.0),
            }
        if symbols:
            self._universe = symbols
            self._assets = meta
            self._universe_at = time.monotonic()
        return list(self._universe)

    def owns_symbol(self, symbol: str) -> bool:
        # Matches both the slashed form ("BTC/USD") and the slashless form
        # Alpaca returns for positions ("BTCUSD").
        return _is_crypto_symbol(symbol)

    def normalize_symbol(self, symbol: str) -> str:
        """Restore the slash Alpaca strips from crypto position symbols
        ("PAXGUSD" → "PAXG/USD") so a held position matches the slashed symbol
        the entry order was placed with. Prefer an exact universe match; fall
        back to splitting on the known quote currency."""
        s = symbol.upper()
        if "/" in s:
            return s
        for candidate in self._universe:
            if candidate.replace("/", "") == s:
                return candidate
        for quote in _CRYPTO_QUOTES:
            if s.endswith(quote) and len(s) > len(quote):
                return f"{s[:-len(quote)]}/{quote}"
        return s

    def min_tick(self, symbol: str) -> float:
        inc = self._assets.get(symbol, {}).get("price_increment", 0.0)
        return inc if inc > 0 else 0.01

    def session_state(self) -> SessionState:
        # Always open; no close boundary → no scheduled flatten.
        return SessionState(open=True)

    def size_qty(
        self, symbol, pos_size, price, stop_distance, risk_per
    ) -> float:
        qty = pos_size / price
        if risk_per > 0 and stop_distance > 0:
            qty = min(qty, risk_per / stop_distance)
        return self._round_qty(qty, symbol)

    def _round_qty(self, qty: float, symbol: str) -> float:
        meta = self._assets.get(symbol, {})
        inc = meta.get("min_trade_increment", 0.0)
        if inc > 0:
            qty = (qty // inc) * inc
        min_size = meta.get("min_order_size", 0.0)
        if min_size > 0 and qty < min_size:
            return 0.0
        # Guard against float dust; 8 dp covers every Alpaca crypto increment.
        return round(qty, 8)

    def round_price(self, symbol: str, price: float) -> float:
        meta = self._assets.get(symbol, {})
        inc = meta.get("price_increment", 0.0)
        if inc > 0:
            steps = round(price / inc)
            return round(steps * inc, 8)
        return round(price, 2)

    def build_entry_order(
        self, symbol, side, qty, ref_price, slip_pct
    ) -> LimitOrderRequest:
        # Crypto: GTC limit (DAY is rejected), no extended_hours, fractional qty
        # rounded to the pair's price/qty increments.
        if side == OrderSide.BUY:
            limit = self.round_price(symbol, ref_price * (1 + slip_pct))
        else:
            limit = self.round_price(symbol, ref_price * (1 - slip_pct))
        return LimitOrderRequest(
            symbol=symbol,
            qty=self._round_qty(qty, symbol),
            side=side,
            time_in_force=TimeInForce.GTC,
            limit_price=limit,
        )

    def build_close_order(
        self, symbol, is_long, qty, ref_price, slip_pct
    ) -> LimitOrderRequest:
        # Close the full held qty (already a valid increment from the fill).
        side = OrderSide.SELL if is_long else OrderSide.BUY
        if is_long:
            limit = self.round_price(symbol, ref_price * (1 - slip_pct))
        else:
            limit = self.round_price(symbol, ref_price * (1 + slip_pct))
        return LimitOrderRequest(
            symbol=symbol,
            qty=round(qty, 8),
            side=side,
            time_in_force=TimeInForce.GTC,
            limit_price=limit,
        )

    def regime(self) -> Dict[str, Any]:
        # BTC/USD as the "market" proxy, 24/7 annualization. Fails open to
        # UNKNOWN like the equity regime, so a data hiccup never halts trading.
        return regime.get_regime(
            symbol=os.getenv("CRYPTO_REGIME_SYMBOL", "BTC/USD"),
            data_client=self.data,
            crypto=True,
        )

    def compute_equity(
        self, account: Any, own_positions: List[Dict[str, Any]], db: Any,
        all_positions: Optional[List[Any]] = None,
    ) -> float:
        # Per-market equity, independent of the shared account cash: a notional
        # base + realized PnL of this DB's own (crypto) trades + unrealized PnL
        # of the currently-held crypto positions.
        realized = float(db.get_trade_stats().get("total_pnl", 0.0))
        unrealized = sum(
            float(p.get("unrealized_pnl") or 0.0) for p in own_positions
        )
        return self._base_equity + realized + unrealized

    def describe_mode(self) -> str:
        n = len(self._universe)
        return f"crypto — {n} USD pairs" if n else "crypto (USD pairs)"

    @property
    def regime_symbol(self) -> str:
        return os.getenv("CRYPTO_REGIME_SYMBOL", "BTC/USD")


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #
def _frames_from_df(
    df: Optional[pd.DataFrame], symbols: List[str]
) -> Dict[str, pd.DataFrame]:
    """Split a multi-symbol bars .df into per-symbol frames. Shared by both
    markets — crypto and stock bars have the same ['symbol','timestamp']
    MultiIndex and OHLCV columns (confirmed, alpaca-py 0.43.5)."""
    frames: Dict[str, pd.DataFrame] = {}
    if df is None or df.empty:
        return frames
    for symbol in symbols:
        try:
            symbol_df = df.xs(symbol, level="symbol")
        except KeyError:
            continue
        if not symbol_df.empty:
            frames[symbol] = symbol_df.sort_index()
    return frames


def _et_time_of_day(value: Any) -> dtime:
    """ET wall-clock time-of-day of an Alpaca calendar field, accepting a
    datetime.time, a naive datetime (ET wall time), or a tz-aware datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(US_EASTERN)
        return value.time()
    return value


def make_adapter(
    trading: TradingClient, api_key: str, secret_key: str
) -> MarketAdapter:
    """Build the adapter selected by the MARKET env (default: equity)."""
    market = os.getenv("MARKET", EQUITY).strip().lower()
    if market == CRYPTO:
        logger.info("Market adapter: crypto (spot, long-only, 24/7)")
        return CryptoAdapter(trading, api_key, secret_key)
    logger.info("Market adapter: equity (extended-hours session)")
    return EquityAdapter(trading, api_key, secret_key)
