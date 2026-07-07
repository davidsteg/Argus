"""
Argus — news sentiment pipeline.

Replaces the original hash stub with a real, layered scorer. For a symbol
it fetches the last 24h of curated headlines from the Alpaca News API and
scores them in [0, 1] (0 = very bearish, 1 = very bullish):

1. LLM scorer     — when ANTHROPIC_API_KEY is set, headlines are scored
                    by Claude (model via SENTIMENT_MODEL, default
                    claude-opus-4-8; set claude-haiku-4-5 for the
                    cheapest/fastest option) using structured JSON output.
2. Keyword scorer — no API key: a transparent bull/bear keyword heuristic
                    over the same headlines. Free and deterministic.
3. Neutral floor  — no news for the symbol: 0.5 (never trades on silence
                    with the default news_cutoff of 0.55).

Efficiency: scores are cached per symbol for SENTIMENT_CACHE_MINUTES
(default 15), and the engine only requests a score after the technical
trigger has already fired — so the LLM is consulted for a handful of
symbols per hour, not the whole watchlist every minute.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from alpaca.data.historical.news import NewsClient
from alpaca.data.requests import NewsRequest
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("argus.sentiment")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SENTIMENT_MODEL = os.getenv("SENTIMENT_MODEL", "claude-opus-4-8")
SENTIMENT_CACHE_MINUTES = int(os.getenv("SENTIMENT_CACHE_MINUTES", "15"))
HEADLINE_LIMIT = 10

BULLISH_WORDS = frozenset(
    "beat beats surge surges soar soars rally rallies record upgrade upgrades "
    "outperform growth profit profits gain gains jump jumps strong strength "
    "raise raises boost boosts win wins approval breakthrough partnership "
    "expand expands bullish buyback dividend exceed exceeds".split()
)
BEARISH_WORDS = frozenset(
    "miss misses fall falls drop drops plunge plunges sink sinks slump slumps "
    "downgrade downgrades underperform loss losses weak weakness cut cuts "
    "lawsuit probe investigation recall layoff layoffs warning warns fraud "
    "bankruptcy default crash bearish decline declines tumble tumbles".split()
)

LLM_SYSTEM_PROMPT = (
    "You are a financial news analyst for an intraday long-only trading "
    "bot. Score the aggregate sentiment of the provided headlines for the "
    "given stock on a scale from 0.0 (very bearish) to 1.0 (very bullish), "
    "where 0.5 is neutral. Weigh concrete, market-moving facts (earnings, "
    "guidance, regulatory actions, analyst moves) over vague commentary."
)

LLM_OUTPUT_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "score": {"type": "number"},
            "rationale": {"type": "string"},
        },
        "required": ["score", "rationale"],
        "additionalProperties": False,
    },
}


class SentimentProvider:
    """Layered news sentiment scorer with per-symbol caching."""

    def __init__(self) -> None:
        self._news = NewsClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        self._lock = threading.Lock()
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._anthropic = None
        if ANTHROPIC_API_KEY:
            try:
                from anthropic import Anthropic

                self._anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)
                logger.info("LLM sentiment enabled (model %s)", SENTIMENT_MODEL)
            except Exception as exc:
                logger.error("Anthropic client unavailable: %s", exc)
        else:
            logger.info(
                "ANTHROPIC_API_KEY not set — using keyword sentiment heuristic"
            )

    # ------------------------------------------------------------------ #
    # public API (blocking — call via asyncio.to_thread from the engine)
    # ------------------------------------------------------------------ #

    def score(self, symbol: str) -> Dict[str, Any]:
        """Return {'score', 'source', 'headlines', 'rationale', 'scored_at'}."""
        symbol = symbol.upper()
        with self._lock:
            cached = self._cache.get(symbol)
            if cached and time.monotonic() - cached["_ts"] < (
                SENTIMENT_CACHE_MINUTES * 60
            ):
                return cached

        result = self._score_uncached(symbol)
        result["_ts"] = time.monotonic()
        with self._lock:
            self._cache[symbol] = result
        return result

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _score_uncached(self, symbol: str) -> Dict[str, Any]:
        scored_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            headlines = self._fetch_headlines(symbol)
        except Exception as exc:
            logger.error("News fetch failed for %s: %s", symbol, exc)
            return {
                "score": 0.5,
                "source": "neutral-fallback",
                "rationale": f"news fetch failed: {exc}",
                "headlines": [],
                "scored_at": scored_at,
            }

        if not headlines:
            return {
                "score": 0.5,
                "source": "no-news",
                "rationale": "no headlines in the last 24h",
                "headlines": [],
                "scored_at": scored_at,
            }

        if self._anthropic is not None:
            try:
                score, rationale = self._score_with_llm(symbol, headlines)
                return {
                    "score": score,
                    "source": f"llm:{SENTIMENT_MODEL}",
                    "rationale": rationale,
                    "headlines": headlines,
                    "scored_at": scored_at,
                }
            except Exception as exc:
                logger.error(
                    "LLM scoring failed for %s (%s) — falling back to "
                    "keyword heuristic",
                    symbol,
                    exc,
                )

        score, rationale = self._score_with_keywords(headlines)
        return {
            "score": score,
            "source": "keyword-heuristic",
            "rationale": rationale,
            "headlines": headlines,
            "scored_at": scored_at,
        }

    def _fetch_headlines(self, symbol: str) -> List[str]:
        request = NewsRequest(
            symbols=symbol,
            start=datetime.now(timezone.utc) - timedelta(hours=24),
            limit=HEADLINE_LIMIT,
        )
        response = self._news.get_news(request)
        items = getattr(response, "news", None)
        if items is None and hasattr(response, "data"):
            items = response.data.get("news", [])
        headlines: List[str] = []
        for item in items or []:
            headline = getattr(item, "headline", None)
            if headline:
                headlines.append(str(headline).strip())
        return headlines[:HEADLINE_LIMIT]

    def _score_with_llm(self, symbol: str, headlines: List[str]) -> tuple:
        numbered = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(headlines))
        response = self._anthropic.messages.create(
            model=SENTIMENT_MODEL,
            max_tokens=512,
            system=LLM_SYSTEM_PROMPT,
            output_config={"format": LLM_OUTPUT_SCHEMA},
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Stock: {symbol}\nHeadlines from the last 24 hours:\n"
                        f"{numbered}\n\nScore the aggregate sentiment."
                    ),
                }
            ],
        )
        if response.stop_reason == "refusal":
            raise RuntimeError("model refused the scoring request")
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        score = max(0.0, min(1.0, float(data["score"])))
        return round(score, 4), str(data.get("rationale", ""))[:300]

    @staticmethod
    def _score_with_keywords(headlines: List[str]) -> tuple:
        bullish = 0
        bearish = 0
        for headline in headlines:
            words = {w.strip(".,:;!?'\"()").lower() for w in headline.split()}
            bullish += len(words & BULLISH_WORDS)
            bearish += len(words & BEARISH_WORDS)
        total = bullish + bearish
        if total == 0:
            return 0.5, "no sentiment-bearing keywords in headlines"
        score = 0.5 + 0.5 * (bullish - bearish) / total
        score = max(0.0, min(1.0, score))
        return (
            round(score, 4),
            f"{bullish} bullish vs {bearish} bearish keyword hits "
            f"across {len(headlines)} headlines",
        )


_provider: SentimentProvider = None
_provider_lock = threading.Lock()


def get_sentiment_provider() -> SentimentProvider:
    global _provider
    with _provider_lock:
        if _provider is None:
            _provider = SentimentProvider()
        return _provider
