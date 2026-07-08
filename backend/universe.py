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
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

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

# Alpaca's most-actives screener caps at 100 symbols per request. This
# is the hard cap; the actual size is read from bot_config.watchlist_size
# at runtime so it can be tuned from the dashboard.
HARD_WATCHLIST_CAP = 100

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


def _watchlist_size() -> int:
    try:
        from shared.database import get_db
        return min(int(get_db().get_config().get("watchlist_size", 50.0)), HARD_WATCHLIST_CAP)
    except Exception:
        return 50

_lock = threading.Lock()
_cached_symbols: List[str] = []
_cached_at: float = 0.0

# ---------------------------------------------------------------------- #
# leveraged/inverse ETP filter
# ---------------------------------------------------------------------- #
# The most-actives-by-volume screener is dominated by geared ETPs (SOXS,
# SQQQ, TSLL, single-stock 2x funds, …). Their daily reset and volatility
# decay make an RSI "dip" a leveraged bet against the prevailing trend —
# structurally the worst instruments for a mean-reversion strategy — so
# they are excluded from the dynamic universe by asset name.
_LEVERAGED_NAME_RE = re.compile(
    r"(\b\d(?:\.\d)?x\b"                # "2X", "3X", "1.5X" leverage factors
    r"|\binverse\b|\bleveraged\b"
    r"|\bbull\b|\bbear\b"               # Direxion-style bull/bear pairs
    r"|\bdirexion\b"                    # every Direxion Daily fund is geared
    r"|proshares\s+(?:ultra|short)"     # ProShares Ultra*/Short* families
    r")",
    re.IGNORECASE,
)

_ASSET_CACHE_SECONDS = 24 * 3600.0
_asset_names: Dict[str, str] = {}
_asset_names_at: float = 0.0


def _get_asset_names() -> Dict[str, str]:
    """symbol → asset name for all active US equities, cached for a day.

    One bulk request instead of per-symbol lookups; on failure the stale
    cache (or an empty map) is returned and the filter degrades to a
    no-op — the universe must never vanish because a metadata call failed.
    """
    global _asset_names, _asset_names_at
    if _asset_names and time.monotonic() - _asset_names_at < _ASSET_CACHE_SECONDS:
        return _asset_names
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
        assets = client.get_all_assets(
            GetAssetsRequest(
                status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY
            )
        )
        _asset_names = {a.symbol.upper(): (a.name or "") for a in assets}
        _asset_names_at = time.monotonic()
        logger.info("Asset metadata refreshed: %d names", len(_asset_names))
    except Exception as exc:
        logger.error(
            "Asset metadata fetch failed — leveraged-ETP filter degraded: %s",
            exc,
        )
    return _asset_names


def filter_untradable(symbols: List[str]) -> List[str]:
    """Drop leveraged/inverse ETPs from a symbol list by asset name.

    Symbols without metadata are kept (fail open) — a filter outage must
    not empty the watchlist.
    """
    names = _get_asset_names()
    if not names:
        return list(symbols)
    kept: List[str] = []
    dropped: List[str] = []
    for symbol in symbols:
        name = names.get(symbol)
        if name and _LEVERAGED_NAME_RE.search(name):
            dropped.append(symbol)
        else:
            kept.append(symbol)
    if dropped:
        logger.info(
            "Universe filter dropped %d leveraged/inverse ETPs: %s",
            len(dropped),
            ", ".join(dropped[:15]),
        )
    return kept


def _fetch_most_actives(top: int) -> List[str]:
    client = ScreenerClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    response = client.get_most_actives(MostActivesRequest(by="volume", top=top))
    return filter_untradable(
        [item.symbol.upper() for item in response.most_actives]
    )


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
            symbols = _fetch_most_actives(_watchlist_size())
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
            f"whole-market (top {_watchlist_size()} most active by volume, "
            f"refresh {_refresh_minutes():g}m)"
        )
    return f"static ({', '.join(STATIC_SYMBOLS)})"
