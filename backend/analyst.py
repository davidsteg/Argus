"""
Argus — Multi-agent LLM strategy analyst.

Merges the TradingAgents multi-agent architecture into Argus's real-time
trading loop. Three specialized agents:

1. Pre-trade Risk Agent — evaluates each signal before it becomes a trade
   (sector concentration, correlation, recent losses, position sizing sanity)
2. Portfolio Manager Agent — approves/rejects trades based on overall
   portfolio state, decides execution order
3. Post-trade Review Agent — analyzes closed trades for failure patterns
   (existing review_trades, enhanced with decision memory)

All agents are advisory by default. When analyst_enabled is on, the risk
agent and portfolio manager can block trades. Decision memory is stored
in runtime_state and fed back into future prompts.
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
    "sentiment_model": os.getenv("ANALYST_SENTIMENT_MODEL", ""),
    "watchlist_model": os.getenv("ANALYST_WATCHLIST_MODEL", ""),
    "risk_model": os.getenv("ANALYST_RISK_MODEL", ""),
    "trade_review_interval_hours": float(
        os.getenv("ANALYST_TRADE_REVIEW_INTERVAL_HOURS", "4")
    ),
    "trade_lookback": int(os.getenv("ANALYST_TRADE_LOOKBACK", "50")),
}

# ------------------------------------------------------------------ #
# Agent system prompts
# ------------------------------------------------------------------ #

RISK_AGENT_PROMPT = (
    "You are a risk management agent for an automated paper-trading bot "
    "called Argus. The bot runs a long-only mean-reversion strategy on "
    "US equities: it buys when RSI dips below a threshold, price is below "
    "VWAP, and news sentiment is positive. Exits are ATR-scaled bracket "
    "orders (stop-loss + take-profit).\n\n"
    "Your job is to evaluate each BUY signal before it becomes a trade. "
    "You can approve, reject, or flag for review. Consider:\n"
    "- Sector concentration: is this the 3rd tech ETF already held?\n"
    "- Correlation: does this move with existing positions?\n"
    "- Recent losses: has this symbol been stopped out recently?\n"
    "- Position sizing: does the risk amount make sense?\n"
    "- Regime fit: is this symbol appropriate for current market conditions?\n\n"
    "Be conservative. It's better to skip a marginal trade than to add risk.\n\n"
    "You MUST respond with ONLY a valid JSON object. No markdown, no "
    "code fences, no preamble. Use this exact structure:\n"
    '{"approved": true/false, '
    '"reason": "brief explanation", '
    '"risk_score": 0.0-1.0, '
    '"warnings": ["warning1", "warning2"]}'
)

PORTFOLIO_MANAGER_PROMPT = (
    "You are a portfolio manager for an automated paper-trading bot called "
    "Argus. The bot runs a long-only mean-reversion strategy on US equities.\n\n"
    "Your job is to review all pending BUY signals that passed the risk agent "
    "and decide which ones to execute and in what order. Consider:\n"
    "- Portfolio diversification: avoid over-concentration in one sector\n"
    "- Signal quality: prioritize higher-confidence signals\n"
    "- Available capital: respect the max positions limit\n"
    "- Opportunity cost: is this the best use of a position slot?\n\n"
    "You MUST respond with ONLY a valid JSON object. No markdown, no "
    "code fences, no preamble. Use this exact structure:\n"
    '{"approved_symbols": ["SYMBOL1", "SYMBOL2"], '
    '"rejected_symbols": ["SYMBOL3"], '
    '"reason": "brief explanation", '
    '"confidence": 0.0-1.0}'
)

DECISION_MEMORY_PROMPT = (
    "You are a decision memory system for an automated trading bot. "
    "Your job is to analyze past trading decisions and their outcomes, "
    "extracting lessons that can improve future decisions.\n\n"
    "For each past decision, consider:\n"
    "- Was the decision correct given what was known at the time?\n"
    "- What pattern does this trade represent (good setup, false signal, etc.)?\n"
    "- What should the bot do differently next time?\n\n"
    "You MUST respond with ONLY a valid JSON object. No markdown, no "
    "code fences, no preamble. Use this exact structure:\n"
    '{"lessons": ["lesson1", "lesson2"], '
    '"patterns": [{"pattern": "description", "frequency": "common|rare", '
    '"action": "what to do"}], '
    '"summary": "one-paragraph assessment"}'
)

TRADE_REVIEW_PROMPT = (
    "You are a quantitative performance reviewer for an automated "
    "paper-trading bot called Argus. The bot runs a long-only "
    "mean-reversion strategy on US equities: it buys when RSI dips below "
    "a threshold, price is below VWAP, and news sentiment is positive. "
    "Exits are ATR-scaled bracket orders (stop-loss + take-profit).\n\n"
    "Your job is to review recent closed trades and the aggregate stats "
    "for failure patterns. Consider:\n"
    "- Are losses concentrated in certain symbols, times of day, or "
    "regimes?\n"
    "- Is the win rate / profit factor healthy for this strategy?\n"
    "- Do the lessons from past decisions suggest a recurring mistake?\n\n"
    "You MUST respond with ONLY a valid JSON object. No markdown, no "
    "code fences, no preamble. Use this exact structure:\n"
    '{"summary": "one-paragraph assessment", '
    '"warnings": ["warning1", "warning2"], '
    '"suggestions": ["suggestion1", "suggestion2"], '
    '"confidence": 0.0-1.0}'
)

OPTIMIZATION_REVIEW_PROMPT = (
    "You are a quantitative reviewer for the nightly walk-forward "
    "parameter optimizer of an automated paper-trading bot called Argus. "
    "The optimizer grid-searches RSI/ATR bracket parameters, ranks them "
    "by in-sample yield-to-drawdown, and validates the winner on an "
    "unseen out-of-sample window.\n\n"
    "Your job is to decide whether to accept the optimizer's winning "
    "parameters, override with a different ranked candidate, or reject "
    "and keep the current parameters. Watch for overfitting (a huge "
    "train/validation gap), too few trades to be meaningful, or "
    "parameter drift that doesn't fit the current market regime.\n\n"
    "You MUST respond with ONLY a valid JSON object. No markdown, no "
    "code fences, no preamble. Use this exact structure:\n"
    '{"decision": {"action": "accept|override|reject", '
    '"override_rank": 1, "reason": "brief explanation"}, '
    '"summary": "one-paragraph assessment", '
    '"warnings": ["warning1", "warning2"], '
    '"suggestions": ["suggestion1", "suggestion2"], '
    '"confidence": 0.0-1.0}'
)

WATCHLIST_REVIEW_PROMPT = (
    "You are a watchlist curator for an automated paper-trading bot "
    "called Argus. The bot runs a long-only mean-reversion strategy: "
    "RSI dip below threshold, price below VWAP, positive news sentiment.\n\n"
    "Your job is to select symbols for the trading watchlist. You MUST "
    "select ONLY from the provided candidate_pool (the live most-actives "
    "screener) — symbols outside that pool will be discarded. Consider "
    "sector diversification, current market regime, and whether a symbol "
    "is a good fit for a mean-reversion dip-buying strategy.\n\n"
    "You MUST respond with ONLY a valid JSON object. No markdown, no "
    "code fences, no preamble. Use this exact structure:\n"
    '{"watchlist": ["SYMBOL1", "SYMBOL2"], '
    '"summary": "brief explanation of the selection"}'
)


class StrategyAnalyst:
    """Multi-agent LLM strategy analyst.

    Three specialized agents:
    - Risk agent: pre-trade signal evaluation
    - Portfolio manager: trade approval and ordering
    - Review agent: post-trade analysis with decision memory
    """

    def __init__(self) -> None:
        self._client = None
        self._watchlist_client = None
        self._risk_client = None
        self._last_trade_review: float = 0.0
        self._last_watchlist_review: float = 0.0
        self._lock = threading.Lock()
        self._config: Dict[str, Any] = dict(DEFAULT_ANALYST_CONFIG)
        self._load_config()
        self._build_client()
        self._build_watchlist_client()
        self._build_risk_client()

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
        with self._lock:
            changed = False
            for key in ("base_url", "api_key", "model", "sentiment_model",
                         "watchlist_model", "risk_model",
                         "trade_review_interval_hours", "trade_lookback"):
                if key in updates and updates[key] != self._config.get(key):
                    self._config[key] = updates[key]
                    changed = True
            if changed:
                self._persist_config(db)
                if "base_url" in updates or "api_key" in updates or "model" in updates:
                    self._build_client()
                if "base_url" in updates or "api_key" in updates or "watchlist_model" in updates:
                    self._build_watchlist_client()
                if "base_url" in updates or "api_key" in updates or "risk_model" in updates:
                    self._build_risk_client()
        return self.get_config()

    # ------------------------------------------------------------------ #
    # Pre-trade Risk Agent
    # ------------------------------------------------------------------ #

    def evaluate_signal_risk(
        self,
        signal: Dict[str, Any],
        portfolio: Dict[str, Any],
        regime: Dict[str, Any],
        db,
    ) -> Dict[str, Any]:
        """Evaluate a single BUY signal before it becomes a trade.

        Returns {"approved": bool, "reason": str, "risk_score": float,
                 "warnings": list}
        """
        if not self.available or not self.enabled(db):
            return {"approved": True, "reason": "analyst disabled", "risk_score": 0.5, "warnings": []}

        held_symbols = [p["symbol"] for p in portfolio.get("positions", [])]
        recent_trades = db.get_trades(50)
        recent_losses = [
            t["symbol"] for t in recent_trades
            if t.get("realized_pnl", 0) < 0
        ]

        prompt_data = {
            "signal": {
                "symbol": signal.get("symbol"),
                "price": signal.get("price"),
                "rsi": signal.get("rsi"),
                "vwap": signal.get("vwap"),
                "atr": signal.get("atr"),
                "sentiment": signal.get("sentiment"),
                "sentiment_source": signal.get("sentiment_source"),
            },
            "portfolio": {
                "equity": portfolio.get("equity"),
                "positions": held_symbols,
                "position_count": len(held_symbols),
                "max_positions": portfolio.get("max_positions", 5),
            },
            "regime": {
                "regime": regime.get("regime", "UNKNOWN"),
                "symbol": regime.get("symbol", "SPY"),
                "close": regime.get("close"),
                "realized_vol_pct": regime.get("realized_vol_pct"),
            },
            "recent_losses": list(set(recent_losses[-10:])),
        }

        try:
            result = self._call_llm(
                prompt_data, "risk",
                system_prompt=RISK_AGENT_PROMPT,
                client_override=self._risk_client,
                model_override=self._config.get("risk_model") or None,
                max_tokens=1024,
            )
        except Exception as exc:
            logger.warning("Risk agent failed for %s: %s", signal.get("symbol"), exc)
            return {"approved": True, "reason": "risk agent unavailable", "risk_score": 0.5, "warnings": []}

        return {
            "approved": bool(result.get("approved", True)),
            "reason": str(result.get("reason", "")),
            "risk_score": max(0.0, min(1.0, float(result.get("risk_score", 0.5)))),
            "warnings": [str(w) for w in result.get("warnings", []) if w],
        }

    # ------------------------------------------------------------------ #
    # Portfolio Manager Agent
    # ------------------------------------------------------------------ #

    def portfolio_manager(
        self,
        pending_signals: List[Dict[str, Any]],
        portfolio: Dict[str, Any],
        regime: Dict[str, Any],
        db,
    ) -> Dict[str, Any]:
        """Review all pending signals and decide which to execute.

        Returns {"approved_symbols": list, "rejected_symbols": list,
                 "reason": str, "confidence": float}
        """
        if not self.available or not self.enabled(db):
            return {
                "approved_symbols": [s["symbol"] for s in pending_signals],
                "rejected_symbols": [],
                "reason": "analyst disabled",
                "confidence": 0.5,
            }

        if not pending_signals:
            return {"approved_symbols": [], "rejected_symbols": [], "reason": "no signals", "confidence": 1.0}

        held_symbols = [p["symbol"] for p in portfolio.get("positions", [])]
        open_slots = portfolio.get("max_positions", 5) - len(held_symbols)

        prompt_data = {
            "pending_signals": [
                {
                    "symbol": s.get("symbol"),
                    "price": s.get("price"),
                    "rsi": s.get("rsi"),
                    "sentiment": s.get("sentiment"),
                    "risk_score": s.get("_risk_score", 0.5),
                }
                for s in pending_signals
            ],
            "portfolio": {
                "equity": portfolio.get("equity"),
                "held_symbols": held_symbols,
                "open_slots": open_slots,
                "max_positions": portfolio.get("max_positions", 5),
            },
            "regime": {
                "regime": regime.get("regime", "UNKNOWN"),
                "symbol": regime.get("symbol", "SPY"),
                "close": regime.get("close"),
                "realized_vol_pct": regime.get("realized_vol_pct"),
            },
        }

        try:
            result = self._call_llm(
                prompt_data, "portfolio",
                system_prompt=PORTFOLIO_MANAGER_PROMPT,
                max_tokens=1024,
            )
        except Exception as exc:
            logger.warning("Portfolio manager failed: %s", exc)
            return {
                "approved_symbols": [s["symbol"] for s in pending_signals],
                "rejected_symbols": [],
                "reason": "portfolio manager unavailable",
                "confidence": 0.5,
            }

        approved = result.get("approved_symbols", [])
        rejected = result.get("rejected_symbols", [])
        if not isinstance(approved, list):
            approved = [s["symbol"] for s in pending_signals]
        if not isinstance(rejected, list):
            rejected = []

        return {
            "approved_symbols": [str(s).strip().upper() for s in approved if s],
            "rejected_symbols": [str(s).strip().upper() for s in rejected if s],
            "reason": str(result.get("reason", "")),
            "confidence": max(0.0, min(1.0, float(result.get("confidence", 0.5)))),
        }

    # ------------------------------------------------------------------ #
    # Decision Memory
    # ------------------------------------------------------------------ #

    def update_decision_memory(
        self,
        symbol: str,
        decision: str,
        outcome: Optional[Dict[str, Any]],
        db,
    ) -> None:
        """Store a trading decision and its outcome for future reference.

        A "close" decision attaches its outcome to the most recent
        outcome-less decision for the symbol, so lesson extraction sees
        decision → result pairs instead of dangling entries."""
        memory = db.get_state("decision_memory") or {"decisions": [], "lessons": []}
        memory.setdefault("decisions", [])
        memory.setdefault("lessons", [])

        outcome_payload = None
        if outcome:
            outcome_payload = {
                "pnl": outcome.get("realized_pnl"),
                "exit_price": outcome.get("exit_price"),
                "exit_time": outcome.get("exit_time"),
                "hold_duration": outcome.get("hold_duration"),
            }

        attached = False
        if decision == "close" and outcome_payload is not None:
            for entry in reversed(memory["decisions"]):
                if entry.get("symbol") == symbol and "outcome" not in entry:
                    entry["outcome"] = outcome_payload
                    attached = True
                    break

        if not attached:
            entry = {
                "symbol": symbol,
                "decision": decision,
                "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if outcome_payload is not None:
                entry["outcome"] = outcome_payload
            memory["decisions"].append(entry)
        # Keep last 200 decisions
        memory["decisions"] = memory["decisions"][-200:]

        try:
            db.set_state("decision_memory", memory)
        except Exception as exc:
            logger.error("Failed to store decision memory: %s", exc)

    def get_decision_lessons(self, db) -> List[str]:
        """Return recent lessons from decision memory."""
        memory = db.get_state("decision_memory") or {}
        return memory.get("lessons", [])[-5:]

    def extract_lessons(self, db) -> None:
        """Analyze recent decisions and extract lessons."""
        memory = db.get_state("decision_memory") or {}
        decisions = memory.get("decisions", [])
        if len(decisions) < 10:
            return

        recent = decisions[-20:]
        prompt_data = {
            "recent_decisions": [
                {
                    "symbol": d.get("symbol"),
                    "decision": d.get("decision"),
                    "outcome": d.get("outcome"),
                }
                for d in recent
            ],
        }

        try:
            result = self._call_llm(
                prompt_data, "memory",
                system_prompt=DECISION_MEMORY_PROMPT,
                max_tokens=2048,
            )
        except Exception as exc:
            logger.warning("Decision memory extraction failed: %s", exc)
            return

        new_lessons = [str(l) for l in result.get("lessons", []) if l]
        if new_lessons:
            memory["lessons"] = (memory.get("lessons", []) + new_lessons)[-20:]
            try:
                db.set_state("decision_memory", memory)
            except Exception as exc:
                logger.error("Failed to store decision lessons: %s", exc)

    # ------------------------------------------------------------------ #
    # Existing methods (unchanged)
    # ------------------------------------------------------------------ #

    def review_optimization(
        self,
        ranked: List[tuple],
        winner: Optional[Dict[str, float]],
        regime: Dict[str, Any],
        db,
    ) -> Optional[Dict[str, Any]]:
        if not self.available or not self.enabled(db):
            return winner

        reviewed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        top_n = min(10, len(ranked))
        candidates = []
        for i in range(top_n):
            score, params, (ret, dd, trades) = ranked[i]
            candidates.append({
                "rank": i + 1,
                "score": round(score, 2),
                "return_pct": round(ret * 100, 2),
                "max_drawdown_pct": round(dd * 100, 2),
                "trades": trades,
                "params": {k: (int(v) if k == "rsi_period" else round(v, 2)) for k, v in params.items()},
            })

        winner_info = None
        if winner:
            winner_info = {k: (int(v) if k == "rsi_period" else round(v, 2)) for k, v in winner.items()}

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
            result = self._call_llm(
                prompt_data, "optimization",
                system_prompt=OPTIMIZATION_REVIEW_PROMPT,
                max_tokens=4096,
            )
        except Exception as exc:
            logger.error("Optimization review failed: %s", exc)
            try:
                db.add_log("ERROR", f"Optimization review failed: {exc}")
            except Exception:
                pass
            return winner

        result["reviewed_at"] = reviewed_at
        result["type"] = "optimization"
        try:
            db.set_state("analyst_optimization", result)
        except Exception as exc:
            logger.error("Failed to store optimization review: %s", exc)

        decision = result.get("decision", {})
        action = decision.get("action", "accept")
        if action == "reject":
            db.add_log("ANALYST", f"LLM rejected optimizer winner — keeping current params. Reason: {decision.get('reason', 'not specified')}")
            return None
        elif action == "override":
            override_rank = int(decision.get("override_rank", 1))
            if 1 <= override_rank <= len(ranked):
                _, override_params, _ = ranked[override_rank - 1]
                db.add_log("ANALYST", f"LLM overrode optimizer winner — using rank {override_rank}. Reason: {decision.get('reason', 'not specified')}")
                return override_params
            else:
                db.add_log("WARNING", f"LLM requested invalid rank {override_rank} (max {len(ranked)}) — falling back to winner")
        return winner

    def review_trades(
        self,
        trades: List[Dict[str, Any]],
        stats: Dict[str, Any],
        config: Dict[str, float],
        regime: Dict[str, Any],
        db,
    ) -> Optional[Dict[str, Any]]:
        if not self.available or not self.enabled(db):
            return None

        reviewed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lookback = int(self._config.get("trade_lookback", 50))
        trade_summaries = []
        for t in trades[-lookback:]:
            trade_summaries.append({
                "symbol": t.get("symbol"),
                "entry_price": t.get("entry_price"),
                "exit_price": t.get("exit_price"),
                "realized_pnl": round(t["realized_pnl"], 2) if t.get("realized_pnl") is not None else None,
                "entry_time": t.get("entry_time"),
                "exit_time": t.get("exit_time"),
            })

        lessons = self.get_decision_lessons(db)

        prompt_data = {
            "context": "Periodic review of recent closed trades.",
            "regime": {
                "regime": regime.get("regime", "UNKNOWN"),
                "symbol": regime.get("symbol", "SPY"),
                "close": regime.get("close"),
                "realized_vol_pct": regime.get("realized_vol_pct"),
            },
            "current_config": {k: (int(v) if k == "rsi_period" else round(v, 2)) for k, v in config.items()},
            "trade_stats": {
                "total": stats.get("total", 0),
                "total_pnl": round(stats.get("total_pnl", 0), 2),
                "wins": stats.get("wins", 0),
                "losses": stats.get("losses", 0),
                "gross_profit": round(stats.get("gross_profit", 0), 2),
                "gross_loss": round(stats.get("gross_loss", 0), 2),
                "best": round(stats["best"], 2) if stats.get("best") is not None else None,
                "worst": round(stats["worst"], 2) if stats.get("worst") is not None else None,
            },
            "recent_trades": trade_summaries,
            "lessons_from_past_decisions": lessons,
        }

        try:
            result = self._call_llm(
                prompt_data, "trades",
                system_prompt=TRADE_REVIEW_PROMPT,
                max_tokens=8192,
            )
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
        with self._lock:
            interval = float(self._config.get("trade_review_interval_hours", 4))
            elapsed = time.monotonic() - self._last_trade_review
            if elapsed < interval * 3600:
                return False
            self._last_trade_review = time.monotonic()
            return True

    def review_watchlist(
        self,
        current_symbols: List[str],
        regime: Dict[str, Any],
        db,
    ) -> Optional[List[str]]:
        if not self.available or not self.enabled(db):
            return None

        # Curate from the live screener pool, not from the LLM's own
        # previous output — otherwise the watchlist drifts on stale,
        # possibly hallucinated symbols with no fresh liquidity data.
        try:
            import universe as universe_module
            candidate_pool = universe_module.get_screener_watchlist()
        except Exception as exc:
            logger.warning("Screener pool unavailable for watchlist review: %s", exc)
            candidate_pool = list(current_symbols)
        if not candidate_pool:
            candidate_pool = list(current_symbols)

        prompt_data = {
            "context": "Watchlist curation for a long-only mean-reversion bot.",
            "regime": {
                "regime": regime.get("regime", "UNKNOWN"),
                "symbol": regime.get("symbol", "SPY"),
                "close": regime.get("close"),
                "realized_vol_pct": regime.get("realized_vol_pct"),
            },
            "current_watchlist": current_symbols,
            "candidate_pool": candidate_pool,
            "constraints": {
                "min_price": 5.0,
                "max_symbols": len(current_symbols),
                "rule": "select symbols ONLY from candidate_pool — anything else is discarded",
                "strategy": "long-only mean-reversion, RSI dip below threshold, price below VWAP, positive sentiment",
            },
        }

        try:
            result = self._call_llm(
                prompt_data, "watchlist",
                system_prompt=WATCHLIST_REVIEW_PROMPT,
                client_override=self._watchlist_client,
                model_override=self._config.get("watchlist_model") or None,
                max_tokens=4096,
            )
        except Exception as exc:
            logger.error("Watchlist review failed: %s", exc)
            try:
                db.add_log("ERROR", f"Watchlist review failed: {exc}")
            except Exception:
                pass
            return None

        new_symbols = result.get("watchlist", [])
        if not new_symbols or not isinstance(new_symbols, list):
            return None

        new_symbols = [s.strip().upper() for s in new_symbols if isinstance(s, str) and s.strip()]
        # Reject hallucinated tickers: only symbols from the real screener
        # pool (or the current list) are allowed to go live.
        allowed = set(candidate_pool) | set(current_symbols)
        new_symbols = [s for s in new_symbols if s in allowed]
        if len(new_symbols) < 3:
            return None

        try:
            db.set_state("analyst_watchlist", {
                "previous": current_symbols,
                "suggested": new_symbols,
                "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "summary": result.get("summary", ""),
            })
        except Exception as exc:
            logger.error("Failed to store watchlist review: %s", exc)

        return new_symbols

    def should_review_watchlist(self) -> bool:
        with self._lock:
            elapsed = time.monotonic() - self._last_watchlist_review
            if elapsed < 3600:
                return False
            self._last_watchlist_review = time.monotonic()
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
                for key in ("base_url", "api_key", "model", "sentiment_model",
                             "watchlist_model", "risk_model",
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
                         self._config.get("base_url", ""), self._config.get("model", ""))
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
            self._client = OpenAI(base_url=str(base_url), api_key=str(api_key) if api_key else "ollama")
            logger.info("Analyst client ready — model %s @ %s", model, base_url)
        except Exception as exc:
            logger.error("OpenAI client unavailable for analyst: %s", exc)

    def _build_watchlist_client(self) -> None:
        self._watchlist_client = None
        wl_model = self._config.get("watchlist_model", "")
        if not wl_model:
            self._watchlist_client = self._client
            return
        base_url = self._config.get("base_url", "")
        api_key = self._config.get("api_key", "")
        if not base_url:
            return
        try:
            from openai import OpenAI
            self._watchlist_client = OpenAI(base_url=str(base_url), api_key=str(api_key) if api_key else "ollama")
            logger.info("Watchlist client ready — model %s @ %s", wl_model, base_url)
        except Exception as exc:
            logger.error("Watchlist client unavailable: %s", exc)
            self._watchlist_client = self._client

    def _build_risk_client(self) -> None:
        self._risk_client = None
        risk_model = self._config.get("risk_model", "")
        if not risk_model:
            self._risk_client = self._client
            return
        base_url = self._config.get("base_url", "")
        api_key = self._config.get("api_key", "")
        if not base_url:
            return
        try:
            from openai import OpenAI
            self._risk_client = OpenAI(base_url=str(base_url), api_key=str(api_key) if api_key else "ollama")
            logger.info("Risk client ready — model %s @ %s", risk_model, base_url)
        except Exception as exc:
            logger.error("Risk client unavailable: %s", exc)
            self._risk_client = self._client

    def _call_llm(
        self,
        data: Dict[str, Any],
        review_type: str,
        system_prompt: Optional[str] = None,
        client_override=None,
        model_override: Optional[str] = None,
        max_tokens: int = 2048,
    ) -> Dict[str, Any]:
        client = client_override or self._client
        model = model_override or str(self._config.get("model", "deepseek-r1"))
        prompt = system_prompt or (
            "You are a quantitative strategy analyst for an automated paper-trading "
            "bot called Argus. Respond with valid JSON only, no markdown, no preamble."
        )
        payload = json.dumps(data, default=str, indent=2)
        logger.info("Analyst calling %s for %s review (%d chars)", model, review_type, len(payload))
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"Analyze this {review_type} data and return your assessment as JSON:\n\n{payload}"},
                ],
                temperature=0.3,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            raise RuntimeError(f"OpenAI API call failed: {type(exc).__name__}: {exc}")

        choice = response.choices[0]
        msg = choice.message
        text = msg.content
        finish = choice.finish_reason

        if not text:
            text = getattr(msg, "reasoning", None) or ""
        if not text:
            details = f"finish_reason={finish}"
            if hasattr(msg, "refusal") and msg.refusal:
                details += f" refusal={msg.refusal}"
            raise RuntimeError(f"LLM returned blank content ({details})")

        logger.info("Analyst %s review response received (%d chars, finish=%s)", review_type, len(text), finish)
        try:
            result = self._parse_json(text)
        except json.JSONDecodeError:
            snippet = text[:500]
            logger.error("Analyst %s review — failed to parse JSON. Raw response (first 500 chars): %s", review_type, snippet)
            raise RuntimeError(f"LLM returned non-JSON response: {snippet}")

        return result

    @staticmethod
    def _parse_json(text: str) -> Dict[str, Any]:
        text = text.strip()
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        m = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
        if text.startswith("{"):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
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
                        return json.loads(text[start:i + 1])
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
