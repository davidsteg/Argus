"""
Argus — trading universe resolution.

Decides which symbols the engine watches. Two modes, controlled by the
TRADING_SYMBOLS environment variable:

* Static list  — "AAPL,MSFT,GOOGL" trades exactly those tickers.
* Whole market — unset, empty, "ALL" or "*" switches to a dynamic
  watchlist: the top-N most active US equities by volume, fetched from
  Alpaca's market screener and refreshed periodically. This is how Argus
  "trades on the whole market" without polling thousands of tickers —
  liquidity concentrates in the most active names, which is exactly what
  a quick-flip strategy needs.

The refresh cadence and override TTL (below) are runtime-tunable strategy
parameters in bot_config — edit them from the dashboard's Settings tab,
same as RSI/ATR — not environment variables, so they take effect on the
next call without a restart.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import MostActivesRequest
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("argus.universe")

_RAW_SYMBOLS = os.getenv("TRADING_SYMBOLS", "ALL").strip()
DYNAMIC_MODE = _RAW_SYMBOLS.upper() in ("", "ALL", "*")
STATIC_SYMBOLS: List[str] = (
    []
    if DYNAMIC_MODE
    else [s.strip().upper() for s in _RAW_SYMBOLS.split(",") if s.strip()]
)

# Alpaca's most-actives screener caps at 100 symbols per request. This one
# stays an environment setting: it defines the deployment's universe size,
# not a strategy dial to retune live.
WATCHLIST_SIZE = min(int(os.getenv("WATCHLIST_SIZE", "50")), 100)

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")


def _refresh_minutes() -> float:
    try:
        from shared.database import get_db
        return float(get_db().get_config().get("watchlist_refresh_minutes", 15.0))
    except Exception:
        return 15.0


def _override_ttl_minutes() -> float:
    try:
        from shared.database import get_db
        return float(
            get_db().get_config().get("watchlist_override_ttl_minutes", 30.0)
        )
    except Exception:
        return 30.0

_lock = threading.Lock()
_cached_symbols: List[str] = []
_cached_at: float = 0.0


def _fetch_most_actives(top: int) -> List[str]:
    client = ScreenerClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    response = client.get_most_actives(MostActivesRequest(by="volume", top=top))
    return [item.symbol.upper() for item in response.most_actives]


def _read_override() -> Optional[List[str]]:
    """Analyst override symbols, or None when absent, expired or malformed.

    Only the {"symbols": [...], "written_at": iso} format carries a
    timestamp; legacy plain-list overrides are ignored so a value written
    before the TTL existed cannot pin the universe forever.
    """
    try:
        from shared.database import get_db
        override = get_db().get_state("watchlist_override")
    except Exception:
        return None
    if not isinstance(override, dict):
        return None
    symbols = override.get("symbols")
    written_at = override.get("written_at")
    if not isinstance(symbols, list) or len(symbols) < 3 or not written_at:
        return None
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(str(written_at))
    except (ValueError, TypeError):
        return None
    if age > timedelta(minutes=_override_ttl_minutes()):
        return None
    return [str(s).upper() for s in symbols]


def get_watchlist(limit: Optional[int] = None) -> List[str]:
    """Return the current trading universe (thread-safe, cached).

    Static mode returns the configured list. Dynamic mode returns the
    most active symbols, refreshed at most every watchlist_refresh_minutes
    (bot_config); on screener failure the last good list is kept so the
    engine never loses its universe mid-session.

    A fresh analyst watchlist_override takes precedence, but it expires
    after watchlist_override_ttl_minutes and never displaces the screener
    cache.
    """
    if not DYNAMIC_MODE:
        return STATIC_SYMBOLS[:limit] if limit else list(STATIC_SYMBOLS)

    override = _read_override()
    if override is not None:
        return override[:limit] if limit else override

    symbols = get_screener_watchlist()
    return symbols[:limit] if limit else symbols


def get_screener_watchlist() -> List[str]:
    """Most-actives screener list, bypassing any analyst override
    (thread-safe, cached for watchlist_refresh_minutes)."""
    if not DYNAMIC_MODE:
        return list(STATIC_SYMBOLS)

    global _cached_symbols, _cached_at
    with _lock:
        age = time.monotonic() - _cached_at
        if _cached_symbols and age < _refresh_minutes() * 60:
            return list(_cached_symbols)
        try:
            symbols = _fetch_most_actives(WATCHLIST_SIZE)
        except Exception as exc:
            logger.error("Most-actives screener failed: %s", exc)
            if _cached_symbols:
                return list(_cached_symbols)
            return ["AAPL", "MSFT", "GOOGL", "NVDA", "TSLA"]
        if symbols:
            _cached_symbols = symbols
            _cached_at = time.monotonic()
            logger.info(
                "Watchlist refreshed: %d most active symbols (top: %s)",
                len(symbols),
                ", ".join(symbols[:10]),
            )
        return list(_cached_symbols)


def describe_mode() -> str:
    if DYNAMIC_MODE:
        return (
            f"whole-market (top {WATCHLIST_SIZE} most active by volume, "
            f"refresh {_refresh_minutes():g}m)"
        )
    return f"static ({', '.join(STATIC_SYMBOLS)})"
