"""
Argus — rolling log of every LLM call the system makes.

Every agent call (risk, portfolio, trade review, optimization review,
watchlist curation, decision memory, sentiment scoring) is recorded here
with its model, latency and outcome, so the dashboard and the debug API
can answer "is the analyst actually working?" without docker exec.

Storage is the shared runtime_state table (key ``analyst_call_log``),
bounded to the most recent entries. Only the backend process writes;
a module-level lock serialises the read-modify-write append across the
engine's worker threads.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("argus.llm_log")

CALL_LOG_KEY = "analyst_call_log"
MAX_ENTRIES = 400

_append_lock = threading.Lock()


def record_llm_call(
    agent: str,
    model: str,
    latency_ms: float,
    ok: bool,
    error: Optional[str] = None,
    request_chars: int = 0,
    response_chars: int = 0,
) -> None:
    """Append one call record. Never raises — a broken call log must not
    take down the trading loop."""
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "agent": agent,
        "model": model,
        "latency_ms": round(latency_ms),
        "ok": bool(ok),
    }
    if error:
        entry["error"] = str(error)[:300]
    if request_chars:
        entry["request_chars"] = int(request_chars)
    if response_chars:
        entry["response_chars"] = int(response_chars)

    try:
        from shared.database import get_db
        db = get_db()
        with _append_lock:
            log: List[Dict[str, Any]] = db.get_state(CALL_LOG_KEY) or []
            log.append(entry)
            db.set_state(CALL_LOG_KEY, log[-MAX_ENTRIES:])
    except Exception as exc:
        logger.warning("Could not record LLM call (%s/%s): %s", agent, model, exc)


def get_call_log(db, limit: int = 100) -> List[Dict[str, Any]]:
    """Most recent calls, newest first."""
    log: List[Dict[str, Any]] = db.get_state(CALL_LOG_KEY) or []
    return list(reversed(log[-limit:]))


def get_agent_stats(db, window_hours: float = 24.0) -> Dict[str, Dict[str, Any]]:
    """Per-agent aggregates over the given window: call count, error count,
    average latency, timestamp and outcome of the most recent call."""
    log: List[Dict[str, Any]] = db.get_state(CALL_LOG_KEY) or []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)

    stats: Dict[str, Dict[str, Any]] = {}
    for entry in log:
        agent = entry.get("agent", "unknown")
        agg = stats.setdefault(agent, {
            "calls": 0,
            "errors": 0,
            "latency_ms_sum": 0.0,
            "last_ts": None,
            "last_ok": None,
            "last_error": None,
            "last_model": None,
        })
        # The full log tail always feeds "last call" info; windowed counts
        # only include recent entries.
        agg["last_ts"] = entry.get("ts")
        agg["last_ok"] = entry.get("ok", False)
        agg["last_model"] = entry.get("model")
        if not entry.get("ok", False):
            agg["last_error"] = entry.get("error")
        else:
            agg["last_error"] = None

        try:
            ts = datetime.fromisoformat(entry["ts"])
        except (KeyError, ValueError):
            continue
        if ts < cutoff:
            continue
        agg["calls"] += 1
        if not entry.get("ok", False):
            agg["errors"] += 1
        agg["latency_ms_sum"] += float(entry.get("latency_ms", 0))

    for agg in stats.values():
        calls = agg["calls"]
        agg["avg_latency_ms"] = round(agg.pop("latency_ms_sum") / calls) if calls else None
    return stats
