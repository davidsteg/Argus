"""
Argus — news sentiment pipeline.

For a symbol it fetches the last 24h of curated headlines from the Alpaca
News API and scores them in [0, 1] (0 = very bearish, 1 = very bullish):

1. LLM scorer     — when SENTIMENT_OLLAMA_BASE_URL (or ANALYST_OLLAMA_BASE_URL)
                    is set, headlines are scored by the configured model via
                    an OpenAI-compatible endpoint (Ollama, etc.) using JSON
                    output. Falls back to the analyst's LLM config.
2. Keyword scorer — no LLM endpoint: a transparent bull/bear keyword heuristic
                    over the same headlines. Free and deterministic.
3. Neutral floor  — no news for the symbol: 0.5. With the default
                    news_cutoff of 0.45 a no-news symbol may still trade
                    (long gate: score > news_cutoff; short gate:
                    score < 1 - news_cutoff) — only actively contrary
                    headlines block a trade.

Efficiency: scores are cached per symbol for SENTIMENT_CACHE_MINUTES
(default 15), and the engine only requests a score after the technical
trigger has already fired — so the LLM is consulted for a handful of
symbols per hour, not the whole watchlist every minute.
"""

from __future__ import annotations

import json
import logging
import os
import re
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
    "You are a financial news analyst for an intraday trading bot that "
    "trades mean reversion both long and short — bullish scores gate long "
    "entries, bearish scores gate short entries. Score the aggregate "
    "sentiment of the provided headlines for the given stock on a scale "
    "from 0.0 (very bearish) to 1.0 (very bullish), where 0.5 is neutral. "
    "Weigh concrete, market-moving facts (earnings, guidance, regulatory "
    "actions, analyst moves) over vague commentary."
)

class SentimentProvider:
    """Layered news sentiment scorer with per-symbol caching.

    Reads the LLM endpoint from the DB-stored analyst config (same config
    the Analyst tab in the UI manages), so changing the base URL or model
    in the dashboard takes effect for sentiment too. Falls back to env
    vars ANALYST_OLLAMA_BASE_URL / ANALYST_OLLAMA_MODEL on first init.
    """

    def __init__(self) -> None:
        self._news = NewsClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        self._lock = threading.Lock()
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._client = None
        self._model = ""
        self._rebuild_client()

    def _rebuild_client(self) -> None:
        """Read analyst config from DB and (re)build the OpenAI client."""
        self._client = None
        self._model = os.getenv("ANALYST_OLLAMA_MODEL", "deepseek-r1")
        base_url = os.getenv("ANALYST_OLLAMA_BASE_URL", "")
        api_key = os.getenv("ANALYST_OLLAMA_API_KEY", "")
        try:
            from shared.database import get_db
            stored = get_db().get_state("analyst_config")
            if stored and isinstance(stored, dict):
                base_url = str(stored.get("base_url", base_url))
                api_key = str(stored.get("api_key", api_key))
                self._model = str(stored.get("sentiment_model", stored.get("model", self._model)))
        except Exception:
            pass
        if not base_url:
            logger.info(
                "No LLM endpoint configured — using keyword sentiment heuristic"
            )
            return
        try:
            from openai import OpenAI
            self._client = OpenAI(
                base_url=base_url,
                api_key=api_key if api_key else "ollama",
            )
            logger.info(
                "LLM sentiment enabled (model %s @ %s)", self._model, base_url
            )
        except Exception as exc:
            logger.error("OpenAI client unavailable for sentiment: %s", exc)

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

        if self._client is not None:
            try:
                score, rationale = self._score_with_llm(symbol, headlines)
                return {
                    "score": score,
                    "source": f"llm:{self._model}",
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
        from llm_log import record_llm_call

        numbered = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(headlines))
        started = time.monotonic()
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": LLM_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Stock: {symbol}\nHeadlines from the last 24 hours:\n"
                            f"{numbered}\n\nScore the aggregate sentiment. "
                            "Respond with valid JSON only, no markdown, no preamble: "
                            '{"score": 0.0-1.0, "rationale": "..."}'
                        ),
                    },
                ],
                temperature=0.3,
                max_tokens=512,
            )
            text = response.choices[0].message.content
            if not text:
                raise RuntimeError("LLM returned blank content")
        except Exception as exc:
            record_llm_call(
                "sentiment", self._model, (time.monotonic() - started) * 1000,
                ok=False, error=f"{symbol}: {exc}", request_chars=len(numbered),
            )
            raise
        record_llm_call(
            "sentiment", self._model, (time.monotonic() - started) * 1000,
            ok=True, request_chars=len(numbered), response_chars=len(text),
        )
        text = text.strip()
        text = re.sub(r"```(?:json)?\s*\n?(.*?)\n?```", r"\1", text, flags=re.DOTALL).strip()
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
