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
"""

from __future__ import annotations

import logging
import os
import threading
import time
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

# Alpaca's most-actives screener caps at 100 symbols per request.
WATCHLIST_SIZE = min(int(os.getenv("WATCHLIST_SIZE", "50")), 100)
WATCHLIST_REFRESH_MINUTES = int(os.getenv("WATCHLIST_REFRESH_MINUTES", "15"))

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")

_lock = threading.Lock()
_cached_symbols: List[str] = []
_cached_at: float = 0.0


def _fetch_most_actives(top: int) -> List[str]:
    client = ScreenerClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    response = client.get_most_actives(MostActivesRequest(by="volume", top=top))
    return [item.symbol.upper() for item in response.most_actives]


def get_watchlist(limit: Optional[int] = None) -> List[str]:
    """Return the current trading universe (thread-safe, cached).

    Static mode returns the configured list. Dynamic mode returns the
    most active symbols, refreshed at most every WATCHLIST_REFRESH_MINUTES;
    on screener failure the last good list is kept so the engine never
    loses its universe mid-session.

    If the analyst has written a watchlist_override to runtime_state,
    that takes precedence over the screener.
    """
    if not DYNAMIC_MODE:
        return STATIC_SYMBOLS[:limit] if limit else list(STATIC_SYMBOLS)

    global _cached_symbols, _cached_at
    with _lock:
        # Check for analyst override first
        try:
            from shared.database import get_db
            override = get_db().get_state("watchlist_override")
            if override and isinstance(override, list) and len(override) >= 3:
                _cached_symbols = override
                _cached_at = time.monotonic()
                return _cached_symbols[:limit] if limit else list(_cached_symbols)
        except Exception:
            pass

        age = time.monotonic() - _cached_at
        if _cached_symbols and age < WATCHLIST_REFRESH_MINUTES * 60:
            return _cached_symbols[:limit] if limit else list(_cached_symbols)
        try:
            symbols = _fetch_most_actives(WATCHLIST_SIZE)
        except Exception as exc:
            logger.error("Most-actives screener failed: %s", exc)
            if _cached_symbols:
                return _cached_symbols[:limit] if limit else list(_cached_symbols)
            return ["AAPL", "MSFT", "GOOGL", "NVDA", "TSLA"][:limit or 5]
        if symbols:
            _cached_symbols = symbols
            _cached_at = time.monotonic()
            logger.info(
                "Watchlist refreshed: %d most active symbols (top: %s)",
                len(symbols),
                ", ".join(symbols[:10]),
            )
        return _cached_symbols[:limit] if limit else list(_cached_symbols)


def describe_mode() -> str:
    if DYNAMIC_MODE:
        return (
            f"whole-market (top {WATCHLIST_SIZE} most active by volume, "
            f"refresh {WATCHLIST_REFRESH_MINUTES}m)"
        )
    return f"static ({', '.join(STATIC_SYMBOLS)})"
