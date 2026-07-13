"""
Argus — market regime filter.

A dip-buying 1-minute mean-reversion strategy has one systematic blind
spot: during a broad market sell-off *everything* is a dip. This module classifies the market's current regime from SPY minute
bars so the engine can stop opening new positions while the tape is
falling apart, instead of catching knives all the way down.

Classification (deliberately simple and explainable, no ML):

* trend  — last SPY close vs. an EMA of the recent window. Below the
           EMA means the index is trading under its short-term fair value.
* stress — annualized realized volatility of the most recent returns vs.
           a threshold. Elevated vol means moves are violent, spreads are
           wide and stops get run.

    TREND_UP   trend up, volatility normal       → trade normally
    CAUTION    trend down OR volatility elevated → position cap halved;
                                                    if the trend is the
                                                    trigger, longs blocked
    TREND_DOWN trend down AND volatility elevated → no new BUY entries
                                                    (shorts still allowed)
    UNKNOWN    SPY data unavailable              → fail-open, trade normally

The consequences are enforced by the engine (bot.py): CAUTION halves
max_positions so a stressed tape is traded at half throttle instead of
full speed, and any down-trend — calm or stressed — blocks new longs
(blocks_long_entries). 2026-07-13 showed why trend alone must gate longs:
SPY drifted below its EMA on quiet vol (CAUTION) all session and 23 of 28
dip-buys stopped out; an orderly fall knifes a dip-buyer just as surely
as a violent one. Blocked BUY signals are shadow-recorded (gate "regime")
so the cost/saving of this gate is measured, not assumed.

The regime never forces an exit — brackets and the daily kill-switch own
that. It only gates new entries. Results are cached for a few minutes so
the check adds one data request per cache window, not per cycle.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import numpy as np
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("argus.regime")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")

REGIME_SYMBOL = os.getenv("REGIME_SYMBOL", "SPY")
REGIME_LOOKBACK_MINUTES = int(os.getenv("REGIME_LOOKBACK_MINUTES", "180"))
REGIME_EMA_BARS = int(os.getenv("REGIME_EMA_BARS", "60"))
REGIME_VOL_BARS = int(os.getenv("REGIME_VOL_BARS", "30"))
# Annualized realized volatility (percent) above which the tape counts as
# stressed. SPY's normal intraday realized vol sits well under this.
REGIME_MAX_ANN_VOL = float(os.getenv("REGIME_MAX_ANN_VOL", "35"))
REGIME_CACHE_MINUTES = float(os.getenv("REGIME_CACHE_MINUTES", "5"))

# Minutes in a US equity trading year, for annualizing 1-minute returns.
_MINUTES_PER_YEAR = 252 * 390
# Crypto trades 24/7, so a year is every minute of it.
_CRYPTO_MINUTES_PER_YEAR = 365 * 24 * 60

TREND_UP = "TREND_UP"
CAUTION = "CAUTION"
TREND_DOWN = "TREND_DOWN"
UNKNOWN = "UNKNOWN"

_lock = threading.Lock()
_cached: Dict[str, Any] = {}
_cached_at: float = 0.0
_client: Optional[StockHistoricalDataClient] = None


def _get_client() -> StockHistoricalDataClient:
    # Lazily built once and reused; _classify only runs under _lock.
    global _client
    if _client is None:
        _client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    return _client


def _classify(symbol: str, data_client: Any, crypto: bool) -> Dict[str, Any]:
    start = datetime.now(timezone.utc) - timedelta(minutes=REGIME_LOOKBACK_MINUTES)
    if crypto:
        request = CryptoBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start
        )
        df = data_client.get_crypto_bars(request).df
        minutes_per_year = _CRYPTO_MINUTES_PER_YEAR
    else:
        request = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, start=start
        )
        df = data_client.get_stock_bars(request).df
        minutes_per_year = _MINUTES_PER_YEAR
    if df is None or df.empty:
        raise RuntimeError(f"no {symbol} bars returned")
    bars = df.xs(symbol, level="symbol").sort_index()
    if len(bars) < max(REGIME_EMA_BARS, REGIME_VOL_BARS) + 2:
        raise RuntimeError(f"only {len(bars)} {symbol} bars in window")

    closes = bars["close"]
    last_close = float(closes.iloc[-1])
    ema = float(closes.ewm(span=REGIME_EMA_BARS, adjust=False).mean().iloc[-1])
    trend_down = last_close < ema

    returns = closes.pct_change().dropna().tail(REGIME_VOL_BARS)
    ann_vol_pct = float(returns.std() * np.sqrt(minutes_per_year) * 100.0)
    stressed = ann_vol_pct > REGIME_MAX_ANN_VOL

    if trend_down and stressed:
        regime = TREND_DOWN
    elif trend_down or stressed:
        regime = CAUTION
    else:
        regime = TREND_UP

    return {
        "regime": regime,
        "symbol": symbol,
        "close": round(last_close, 2),
        "ema": round(ema, 2),
        "trend_down": trend_down,
        "realized_vol_pct": round(ann_vol_pct, 1),
        "vol_threshold_pct": REGIME_MAX_ANN_VOL,
        "stressed": stressed,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def get_regime(
    symbol: str = REGIME_SYMBOL,
    data_client: Any = None,
    crypto: bool = False,
) -> Dict[str, Any]:
    """Current market regime, cached for REGIME_CACHE_MINUTES (thread-safe).

    Defaults to the equity SPY proxy on the module's own stock client. The
    crypto engine passes its BTC/USD proxy + CryptoHistoricalDataClient
    (crypto=True) so 1-minute returns annualize on the 24/7 calendar.

    Fails open: when the proxy's data is unavailable the engine keeps trading
    (regime UNKNOWN) rather than halting on a data hiccup — the soft stops and
    the daily loss limit remain the hard safety nets.
    """
    global _cached, _cached_at
    with _lock:
        if (
            _cached
            and _cached.get("symbol") == symbol
            and time.monotonic() - _cached_at < REGIME_CACHE_MINUTES * 60
        ):
            return dict(_cached)
        try:
            result = _classify(symbol, data_client or _get_client(), crypto)
        except Exception as exc:
            logger.error("Regime classification failed: %s", exc)
            result = {
                "regime": UNKNOWN,
                "symbol": symbol,
                "error": str(exc),
                "checked_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ),
            }
        _cached = result
        _cached_at = time.monotonic()
        return dict(result)


def blocks_new_entries(regime_info: Dict[str, Any]) -> bool:
    return regime_info.get("regime") == TREND_DOWN


def blocks_long_entries(regime_info: Dict[str, Any]) -> bool:
    """True when new BUY (dip-buy) entries should be blocked: whenever the
    index is below its EMA, stressed or not. A superset of
    blocks_new_entries — TREND_DOWN implies trend_down. Fails open on
    UNKNOWN (no trend_down key → False), matching get_regime's contract
    that a data hiccup never halts trading."""
    return bool(regime_info.get("trend_down")) or blocks_new_entries(regime_info)
