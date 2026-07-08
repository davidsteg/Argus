"""
Argus — opportunity screener: find CRINX-like setups across a wide universe.

Scans a much larger pool of symbols (e.g. top 200 most active) for the same
RSI-oversold + VWAP-dip + bullish-sentiment pattern the engine trades, then
ranks candidates by dip depth and publishes them. The engine can pull the
top N into its watchlist each cycle, or the dashboard can display them for
manual review.

This is a *candidate generator*, not a trading engine — it never places
orders. It reuses the exact same indicator code (indicators.py) and
sentiment pipeline (sentiment.py) so screener and live signals never drift.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.screener import ScreenerClient
from alpaca.data.requests import MostActivesRequest, StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from indicators import compute_atr, compute_rsi, compute_vwap
from sentiment import get_sentiment_provider
from shared.database import get_db
from universe import filter_untradable

logger = logging.getLogger("argus.screener")

# How many symbols to scan (larger than the trading watchlist).
SCREENER_POOL_SIZE = 200
# How many 1-minute bars to fetch per symbol for evaluation.
SCREENER_BAR_LOOKBACK = 180
# Minimum bars needed for reliable RSI/ATR.
SCREENER_MIN_BARS = 30


# Alpaca's most-actives screener caps at 100 symbols per request.
_HARD_SCREENER_CAP = 100


def _fetch_pool(alpaca_key: str, alpaca_secret: str, top: int) -> List[str]:
    """Fetch the top-N most active symbols as the screening pool,
    with the same leveraged/inverse-ETP filter as the trading universe."""
    client = ScreenerClient(alpaca_key, alpaca_secret)
    response = client.get_most_actives(MostActivesRequest(by="volume", top=min(top, _HARD_SCREENER_CAP)))
    return filter_untradable([item.symbol.upper() for item in response.most_actives])


def _fetch_bars(
    client: StockHistoricalDataClient, symbols: List[str]
) -> Dict[str, pd.DataFrame]:
    """Fetch 1-minute bars for all symbols in one request."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=SCREENER_BAR_LOOKBACK)
    request = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Minute,
        start=start,
    )
    try:
        bars = client.get_stock_bars(request)
    except Exception as exc:
        logger.error("Screener bar fetch failed: %s", exc)
        return {}
    frames: Dict[str, pd.DataFrame] = {}
    df = bars.df
    if df is None or df.empty:
        return frames
    for symbol in symbols:
        try:
            symbol_df = df.xs(symbol, level="symbol")
        except KeyError:
            continue
        if len(symbol_df) >= SCREENER_MIN_BARS:
            frames[symbol] = symbol_df.sort_index()
    return frames


def _score_candidate(
    symbol: str,
    bars: pd.DataFrame,
    config: Dict[str, float],
    sentiment_provider: Any,
) -> Optional[Dict[str, Any]]:
    """Evaluate one symbol for the RSI-oversold + VWAP-dip pattern.

    Returns a candidate dict with signal context, or None if the symbol
    does not meet the entry criteria.
    """
    period = max(int(config["rsi_period"]), 2)
    if len(bars) < period * 2:
        return None

    rsi_series = compute_rsi(bars["close"], period)
    latest_rsi = float(rsi_series.iloc[-1])
    latest_close = float(bars["close"].iloc[-1])
    if np.isnan(latest_rsi):
        return None

    if latest_rsi >= config["rsi_buy_signal"]:
        return None

    vwap = float(compute_vwap(bars).iloc[-1])
    if latest_close > vwap:
        return None

    atr = float(compute_atr(bars).iloc[-1])
    if np.isnan(atr) or atr <= 0:
        return None

    # Sentiment — reuse the same cached pipeline as the engine.
    try:
        sentiment = sentiment_provider.score(symbol)
        score = float(sentiment["score"])
        source = str(sentiment["source"])
    except Exception:
        score = 0.5
        source = "error"

    if score <= config["news_cutoff"]:
        return None

    # Dip depth: how far RSI is below the buy level, normalised.
    rsi_depth = (config["rsi_buy_signal"] - latest_rsi) / config["rsi_buy_signal"]
    # VWAP distance as a fraction of price.
    vwap_distance = (vwap - latest_close) / latest_close if latest_close > 0 else 0.0

    return {
        "symbol": symbol,
        "price": round(latest_close, 4),
        "rsi": round(latest_rsi, 2),
        "vwap": round(vwap, 4),
        "atr": round(atr, 4),
        "sentiment": round(score, 3),
        "sentiment_source": source,
        "rsi_depth": round(rsi_depth, 3),
        "vwap_distance": round(vwap_distance, 3),
        "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def run_screener(
    alpaca_key: str,
    alpaca_secret: str,
    pool_size: int = SCREENER_POOL_SIZE,
) -> List[Dict[str, Any]]:
    """Full screener pass: fetch pool → fetch bars → score → rank.

    Returns candidates sorted by dip depth (deepest first). Call from a
    thread or asyncio.to_thread — it is blocking I/O.
    """
    config = get_db().get_config()
    sentiment_provider = get_sentiment_provider()
    data_client = StockHistoricalDataClient(alpaca_key, alpaca_secret)

    pool = _fetch_pool(alpaca_key, alpaca_secret, pool_size)
    if not pool:
        logger.warning("Screener pool empty — skipping pass")
        return []

    frames = _fetch_bars(data_client, pool)
    if not frames:
        logger.warning("Screener got no bar data — skipping pass")
        return []

    candidates: List[Dict[str, Any]] = []
    for symbol, bars in frames.items():
        candidate = _score_candidate(symbol, bars, config, sentiment_provider)
        if candidate is not None:
            candidates.append(candidate)

    # Sort by dip depth: deepest RSI undershoot first.
    candidates.sort(key=lambda c: c["rsi_depth"], reverse=True)

    logger.info(
        "Screener: %d/%d symbols passed (deepest: %s RSI=%.1f depth=%.2f)",
        len(candidates),
        len(frames),
        candidates[0]["symbol"] if candidates else "—",
        candidates[0]["rsi"] if candidates else 0,
        candidates[0]["rsi_depth"] if candidates else 0,
    )

    return candidates
