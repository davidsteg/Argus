"""
Argus — LLM strategy analyst.

An advisory module that uses a cloud-hosted Ollama model (OpenAI-compatible
API) to review the bot's own performance and suggest improvements. It never
changes parameters directly — reports are stored in runtime_state for the
dashboard to display.

Two review modes:
1. Post-optimization review — after the nightly grid search, the LLM
   analyzes the ranked results and the winning combination.
2. Periodic trade review — every few hours during market hours, the LLM
   analyzes recent closed trades for failure patterns.

The analyst is toggleable from the dashboard (bot_config["analyst_enabled"]).
All connection settings (base URL, model, intervals) are editable from the
Analyst tab and stored in runtime_state — no container restart needed.
When disabled or when Ollama is unreachable, the bot continues normally.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("argus.analyst")

DEFAULT_ANALYST_CONFIG: Dict[str, Any] = {
    "base_url": os.getenv("ANALYST_OLLAMA_BASE_URL", ""),
    "api_key": os.getenv("ANALYST_OLLAMA_API_KEY", ""),
    "model": os.getenv("ANALYST_OLLAMA_MODEL", "deepseek-r1"),
    "trade_review_interval_hours": float(
        os.getenv("ANALYST_TRADE_REVIEW_INTERVAL_HOURS", "4")
    ),
    "trade_lookback": int(os.getenv("ANALYST_TRADE_LOOKBACK", "50")),
}

SYSTEM_PROMPT = (
    "You are a quantitative strategy analyst for an automated paper-trading "
    "bot called Argus. The bot runs a long-only mean-reversion strategy on "
    "US equities: it buys when RSI dips below a threshold, price is below "
    "VWAP, and news sentiment is positive. Exits are ATR-scaled bracket "
    "orders (stop-loss + take-profit). Parameters are tuned nightly by a "
    "walk-forward grid search with out-of-sample validation.\n\n"
    "Your job is to analyze the bot's performance data and provide "
    "actionable, specific suggestions. Be concise and quantitative. "
    "Flag overfitting, insufficient sample sizes, regime mismatches, "
    "and parameter drift. Never suggest removing safety mechanisms "
    "(daily stop-loss, bracket orders, paper trading).\n\n"
    "You MUST respond with ONLY a valid JSON object. No markdown, no "
    "code fences, no preamble, no explanation. Use this exact structure:\n"
    '{"summary": "one-paragraph assessment", '
    '"warnings": ["warning1", "warning2"], '
    '"suggestions": ["suggestion1", "suggestion2"], '
    '"confidence": 0.0-1.0}'
)


class StrategyAnalyst:
    """LLM-powered strategy analyst using cloud Ollama (OpenAI-compatible API).

    Config is loaded from runtime_state on init (falling back to env vars),
    and can be updated at runtime via ``configure()`` — no restart needed.
    """

    def __init__(self) -> None:
        self._client = None
        self._last_trade_review: float = 0.0
        self._lock = threading.Lock()
        self._config: Dict[str, Any] = dict(DEFAULT_ANALYST_CONFIG)
        self._load_config()
        self._build_client()

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #

    @property
    def available(self) -> bool:
        return self._client is not None

    def enabled(self, db) -> bool:
        return bool(db.get_config().get("analyst_enabled", 0.0))

    def get_config(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._config)

    def configure(self, updates: Dict[str, Any], db) -> Dict[str, Any]:
        """Update analyst config at runtime. Rebuilds the client if the
        base URL, API key, or model changed. Stores to runtime_state for
        persistence. Returns the full config after update."""
        with self._lock:
            changed = False
            for key in ("base_url", "api_key", "model",
                         "trade_review_interval_hours", "trade_lookback"):
                if key in updates and updates[key] != self._config.get(key):
                    self._config[key] = updates[key]
                    changed = True
            if changed:
                self._persist_config(db)
                if "base_url" in updates or "api_key" in updates or "model" in updates:
                    self._build_client()
        return self.get_config()

    def review_optimization(
        self,
        ranked: List[tuple],
        winner: Optional[Dict[str, float]],
        regime: Dict[str, Any],
        db,
    ) -> Optional[Dict[str, Any]]:
        """Analyze grid search results after the nightly optimizer runs."""
        if not self.available or not self.enabled(db):
            return None

        reviewed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        top_n = min(10, len(ranked))
        candidates = []
        for i in range(top_n):
            score, params, (ret, dd, trades) = ranked[i]
            candidates.append(
                {
                    "rank": i + 1,
                    "score": round(score, 2),
                    "return_pct": round(ret * 100, 2),
                    "max_drawdown_pct": round(dd * 100, 2),
                    "trades": trades,
                    "params": {
                        k: (int(v) if k == "rsi_period" else round(v, 2))
                        for k, v in params.items()
                    },
                }
            )

        winner_info = None
        if winner:
            winner_info = {
                k: (int(v) if k == "rsi_period" else round(v, 2))
                for k, v in winner.items()
            }

        prompt_data = {
            "context": "Post-optimization review of the nightly walk-forward grid search.",
            "regime": {
                "regime": regime.get("regime", "UNKNOWN"),
                "symbol": regime.get("symbol", "SPY"),
                "close": regime.get("close"),
                "realized_vol_pct": regime.get("realized_vol_pct"),
            },
            "winner": winner_info,
            "top_candidates": candidates,
            "total_combinations_tested": len(ranked),
        }

        try:
            result = self._call_llm(prompt_data, "optimization")
        except Exception as exc:
            logger.error("Optimization review failed: %s", exc)
            try:
                db.add_log("ERROR", f"Optimization review failed: {exc}")
            except Exception:
                pass
            return None

        result["reviewed_at"] = reviewed_at
        result["type"] = "optimization"
        try:
            db.set_state("analyst_optimization", result)
        except Exception as exc:
            logger.error("Failed to store optimization review: %s", exc)
        return result

    def review_trades(
        self,
        trades: List[Dict[str, Any]],
        stats: Dict[str, Any],
        config: Dict[str, float],
        regime: Dict[str, Any],
        db,
    ) -> Optional[Dict[str, Any]]:
        """Analyze recent closed trades for failure patterns."""
        if not self.available or not self.enabled(db):
            return None

        reviewed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        lookback = int(self._config.get("trade_lookback", 50))
        trade_summaries = []
        for t in trades[-lookback:]:
            trade_summaries.append(
                {
                    "symbol": t.get("symbol"),
                    "entry_price": t.get("entry_price"),
                    "exit_price": t.get("exit_price"),
                    "realized_pnl": (
                        round(t["realized_pnl"], 2)
                        if t.get("realized_pnl") is not None
                        else None
                    ),
                    "entry_time": t.get("entry_time"),
                    "exit_time": t.get("exit_time"),
                }
            )

        prompt_data = {
            "context": "Periodic review of recent closed trades.",
            "regime": {
                "regime": regime.get("regime", "UNKNOWN"),
                "symbol": regime.get("symbol", "SPY"),
                "close": regime.get("close"),
                "realized_vol_pct": regime.get("realized_vol_pct"),
            },
            "current_config": {
                k: (int(v) if k == "rsi_period" else round(v, 2))
                for k, v in config.items()
            },
            "trade_stats": {
                "total": stats.get("total", 0),
                "total_pnl": round(stats.get("total_pnl", 0), 2),
                "wins": stats.get("wins", 0),
                "losses": stats.get("losses", 0),
                "gross_profit": round(stats.get("gross_profit", 0), 2),
                "gross_loss": round(stats.get("gross_loss", 0), 2),
                "best": (
                    round(stats["best"], 2)
                    if stats.get("best") is not None
                    else None
                ),
                "worst": (
                    round(stats["worst"], 2)
                    if stats.get("worst") is not None
                    else None
                ),
            },
            "recent_trades": trade_summaries,
        }

        try:
            result = self._call_llm(prompt_data, "trades")
        except Exception as exc:
            logger.error("Trade review failed: %s", exc)
            try:
                db.add_log("ERROR", f"Trade review failed: {exc}")
            except Exception:
                pass
            return None

        result["reviewed_at"] = reviewed_at
        result["type"] = "trades"
        try:
            db.set_state("analyst_trades", result)
        except Exception as exc:
            logger.error("Failed to store trade review: %s", exc)
        return result

    def should_review_trades(self) -> bool:
        """True when enough time has passed since the last trade review."""
        with self._lock:
            interval = float(self._config.get("trade_review_interval_hours", 4))
            elapsed = time.monotonic() - self._last_trade_review
            if elapsed < interval * 3600:
                return False
            self._last_trade_review = time.monotonic()
            return True

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _load_config(self) -> None:
        try:
            from shared.database import get_db

            db = get_db()
            stored = db.get_state("analyst_config")
            if stored and isinstance(stored, dict):
                for key in ("base_url", "api_key", "model",
                             "trade_review_interval_hours", "trade_lookback"):
                    if key in stored:
                        self._config[key] = stored[key]
                logger.info("Analyst config loaded from runtime_state")
        except Exception as exc:
            logger.warning("Could not load analyst config from DB: %s", exc)

    def _persist_config(self, db) -> None:
        try:
            db.set_state("analyst_config", self._config)
            logger.info("Analyst config persisted: base_url=%s model=%s",
                         self._config.get("base_url", ""),
                         self._config.get("model", ""))
        except Exception as exc:
            logger.error("Failed to persist analyst config: %s", exc)

    def _build_client(self) -> None:
        self._client = None
        base_url = self._config.get("base_url", "")
        api_key = self._config.get("api_key", "")
        model = self._config.get("model", "")
        if not base_url:
            logger.info("Analyst base URL not set — analyst unavailable")
            return
        try:
            from openai import OpenAI

            self._client = OpenAI(
                base_url=str(base_url),
                api_key=str(api_key) if api_key else "ollama",
            )
            logger.info(
                "Analyst client ready — model %s @ %s",
                model,
                base_url,
            )
        except Exception as exc:
            logger.error("OpenAI client unavailable for analyst: %s", exc)

    def _call_llm(
        self,
        data: Dict[str, Any],
        review_type: str,
    ) -> Dict[str, Any]:
        model = str(self._config.get("model", "deepseek-r1"))
        payload = json.dumps(data, default=str, indent=2)
        logger.info("Analyst calling %s for %s review (%d chars)",
                     model, review_type, len(payload))
        try:
            response = self._client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Analyze this {review_type} data and return your "
                            f"assessment as JSON:\n\n{payload}"
                        ),
                    },
                ],
                temperature=0.3,
                max_tokens=2048,
            )
        except Exception as exc:
            raise RuntimeError(
                f"OpenAI API call failed: {type(exc).__name__}: {exc}"
            )

        choice = response.choices[0]
        msg = choice.message
        text = msg.content
        finish = choice.finish_reason

        # DeepSeek models on ollama.com put output in the non-standard
        # "reasoning" field instead of "content". Fall back to it.
        if not text:
            text = getattr(msg, "reasoning", None) or ""

        if not text:
            details = f"finish_reason={finish}"
            if hasattr(msg, "refusal") and msg.refusal:
                details += f" refusal={msg.refusal}"
            raise RuntimeError(
                f"LLM returned blank content ({details})"
            )

        logger.info("Analyst %s review response received (%d chars, finish=%s)",
                     review_type, len(text), finish)
        try:
            result = self._parse_json(text)
        except json.JSONDecodeError:
            snippet = text[:500]
            logger.error("Analyst %s review — failed to parse JSON. "
                          "Raw response (first 500 chars): %s",
                          review_type, snippet)
            raise RuntimeError(
                f"LLM returned non-JSON response: {snippet}"
            )
        return {
            "summary": str(result.get("summary", "")),
            "warnings": [
                str(w) for w in result.get("warnings", []) if w
            ],
            "suggestions": [
                str(s) for s in result.get("suggestions", []) if s
            ],
            "confidence": max(0.0, min(1.0, float(result.get("confidence", 0.5)))),
        }

    @staticmethod
    def _parse_json(text: str) -> Dict[str, Any]:
        text = text.strip()
        # Strip  tags (DeepSeek-R1 chain-of-thought)
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        # Try to extract content from markdown code blocks
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
        # Try parsing the full text as JSON
        if text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        # Fallback: find the first JSON object in the text
        brace_depth = 0
        start = -1
        for i, c in enumerate(text):
            if c == "{":
                if start == -1:
                    start = i
                brace_depth += 1
            elif c == "}":
                brace_depth -= 1
                if brace_depth == 0 and start != -1:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        start = -1
        raise json.JSONDecodeError("No valid JSON object found in response", text, 0)


_analyst: Optional[StrategyAnalyst] = None
_analyst_lock = threading.Lock()


def get_analyst() -> StrategyAnalyst:
    global _analyst
    with _analyst_lock:
        if _analyst is None:
            _analyst = StrategyAnalyst()
        return _analyst
