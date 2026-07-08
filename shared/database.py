"""
Argus — thread-safe SQLite persistence layer.

This module is the single source of truth for all state exchanged between
the backend trading engine and the NiceGUI frontend. Both containers mount
the same Docker volume at /app/shared and open the same database file.

Concurrency model
-----------------
* WAL journal mode so the frontend (reader) never blocks the bot (writer).
* A process-wide re-entrant lock serialises writes issued from multiple
  threads inside one process (NiceGUI io_bound workers, asyncio executors).
* `busy_timeout` handles cross-process contention between containers.

Tables
------
bot_config      dynamic strategy parameters (rewritten nightly by the optimizer)
bot_status      single-row state machine: RUNNING / KILLED, equity, day anchor
positions       active tickers with entry price, live price and unrealized PnL
trades          historical executions with entry/exit timestamps and realized PnL
logs            system event log with strict UTC ISO-8601 timestamps
equity_history  periodic account-equity snapshots powering the dashboard curve
runtime_state   JSON key/value blobs the engine publishes for the dashboard
                (last cycle trace, market regime, operational environment) so
                the frontend can visualize engine internals without an HTTP
                hop to the backend
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = os.getenv(
    "DB_PATH",
    str(Path(__file__).resolve().parent / "argus_state.db"),
)

# Strategy parameters the optimizer is allowed to tune. Values are floats;
# integer-valued parameters (rsi_period) are cast on read by the consumers.
# Bracket distances are ATR multiples (volatility-adaptive) since v2.2.0 —
# the old fixed stop_loss_pct / take_profit_pct keys are retired and
# filtered out on read if they linger in an existing database.
DEFAULT_CONFIG: Dict[str, float] = {
    "rsi_period": 14.0,        # RSI lookback in 1-minute bars
    "rsi_buy_signal": 30.0,    # enter long when RSI drops below this level
    "rsi_exit_signal": 70.0,   # close a long early when RSI recovers above this
    "rsi_short_signal": 70.0,  # enter short when RSI rises above this level
    "rsi_short_exit": 30.0,    # cover short early when RSI drops below this
    "short_enabled": 0.0,      # 0 = off, 1 = on — short selling toggle
    "news_cutoff": 0.55,       # minimum sentiment score required to trade
    "atr_stop_mult": 1.5,      # bracket stop-loss distance, ATR multiples
    "atr_target_mult": 2.5,    # bracket take-profit distance, ATR multiples
    "analyst_enabled": 0.0,    # 0 = off, 1 = on — LLM strategy analyst toggle
    "watchlist_refresh_minutes": 15.0,      # dynamic-mode screener refresh cadence
    "watchlist_override_ttl_minutes": 30.0,  # analyst watchlist override expiry
    "screener_enabled": 0.0,       # 0 = off, 1 = on — opportunity screener
    "screener_pool_size": 200.0,   # how many most-active symbols to scan
    "screener_max_candidates": 5.0,  # top N candidates to surface
    # Operational environment — tunable from the dashboard, not env vars
    "position_size_usd": 500.0,
    "risk_per_trade_usd": 20.0,
    "max_positions": 5.0,
    "daily_stop_loss": 100.0,
    "min_price_usd": 5.0,
    "cooldown_minutes": 30.0,
    "poll_interval_seconds": 60.0,
    "bar_lookback_minutes": 180.0,
    "watchlist_size": 50.0,
}

STATUS_RUNNING = "RUNNING"
STATUS_KILLED = "KILLED"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_config (
    key         TEXT PRIMARY KEY,
    value       REAL NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_status (
    id                   INTEGER PRIMARY KEY CHECK (id = 1),
    status               TEXT NOT NULL,
    equity               REAL NOT NULL DEFAULT 0,
    daily_start_balance  REAL NOT NULL DEFAULT 0,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    symbol           TEXT PRIMARY KEY,
    qty              REAL NOT NULL,
    avg_entry_price  REAL NOT NULL,
    current_price    REAL,
    unrealized_pnl   REAL,
    market_value     REAL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    qty           REAL NOT NULL,
    entry_price   REAL NOT NULL,
    exit_price    REAL,
    entry_time    TEXT NOT NULL,
    exit_time     TEXT,
    realized_pnl  REAL
);

CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades (exit_time);

CREATE TABLE IF NOT EXISTS logs (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    level    TEXT NOT NULL,
    message  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_logs_id_desc ON logs (id DESC);

CREATE TABLE IF NOT EXISTS equity_history (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    equity  REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_equity_history_ts ON equity_history (ts);

CREATE TABLE IF NOT EXISTS runtime_state (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

# Columns added to existing tables after their initial release. CREATE TABLE
# IF NOT EXISTS never alters an existing table, so upgrades on a live volume
# go through ALTER TABLE guarded by a PRAGMA table_info check.
_MIGRATIONS: Dict[str, Dict[str, str]] = {
    "positions": {
        "current_price": "REAL",
        "unrealized_pnl": "REAL",
        "market_value": "REAL",
    },
}

# Keep roughly six weeks of 1-minute equity snapshots before trimming.
_EQUITY_HISTORY_MAX_ROWS = 60_000


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _optional_float(value: Any) -> Optional[float]:
    return None if value is None else float(value)


class Database:
    """Thread-safe wrapper around the shared Argus SQLite database."""

    def __init__(self, db_path: str = DB_PATH) -> None:
        self._db_path = db_path
        self._lock = threading.RLock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=15000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._seed_defaults()
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _migrate(self) -> None:
        for table, columns in _MIGRATIONS.items():
            existing = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            for column, column_type in columns.items():
                if column not in existing:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
                    )

    def _seed_defaults(self) -> None:
        now = _utcnow()
        for key, value in DEFAULT_CONFIG.items():
            self._conn.execute(
                "INSERT OR IGNORE INTO bot_config (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                (key, value, now),
            )
        self._conn.execute(
            "INSERT OR IGNORE INTO bot_status "
            "(id, status, equity, daily_start_balance, updated_at) "
            "VALUES (1, ?, 0, 0, ?)",
            (STATUS_RUNNING, now),
        )

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _query(self, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------ #
    # bot_config
    # ------------------------------------------------------------------ #

    def get_config(self) -> Dict[str, float]:
        rows = self._query("SELECT key, value FROM bot_config")
        config = dict(DEFAULT_CONFIG)
        # Only known keys: retired parameters from older releases must not
        # resurface in /config or the dashboard.
        config.update(
            {
                row["key"]: float(row["value"])
                for row in rows
                if row["key"] in DEFAULT_CONFIG
            }
        )
        return config

    def set_config(self, updates: Dict[str, float]) -> None:
        now = _utcnow()
        with self._lock:
            for key, value in updates.items():
                self._conn.execute(
                    "INSERT INTO bot_config (key, value, updated_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                    "updated_at = excluded.updated_at",
                    (key, float(value), now),
                )
            self._conn.commit()

    def get_config_updated_at(self) -> Optional[str]:
        rows = self._query("SELECT MAX(updated_at) AS ts FROM bot_config")
        return rows[0]["ts"] if rows else None

    # ------------------------------------------------------------------ #
    # bot_status
    # ------------------------------------------------------------------ #

    def get_status(self) -> Dict[str, Any]:
        rows = self._query("SELECT * FROM bot_status WHERE id = 1")
        row = rows[0]
        return {
            "status": row["status"],
            "equity": float(row["equity"]),
            "daily_start_balance": float(row["daily_start_balance"]),
            "updated_at": row["updated_at"],
        }

    def set_status(
        self,
        status: Optional[str] = None,
        equity: Optional[float] = None,
        daily_start_balance: Optional[float] = None,
    ) -> None:
        assignments = ["updated_at = ?"]
        params: List[Any] = [_utcnow()]
        if status is not None:
            assignments.append("status = ?")
            params.append(status)
        if equity is not None:
            assignments.append("equity = ?")
            params.append(float(equity))
        if daily_start_balance is not None:
            assignments.append("daily_start_balance = ?")
            params.append(float(daily_start_balance))
        self._execute(
            f"UPDATE bot_status SET {', '.join(assignments)} WHERE id = 1",
            tuple(params),
        )

    def is_killed(self) -> bool:
        return self.get_status()["status"] == STATUS_KILLED

    # ------------------------------------------------------------------ #
    # positions
    # ------------------------------------------------------------------ #

    def replace_positions(self, positions: List[Dict[str, Any]]) -> None:
        """Atomically replace the positions snapshot with the live one."""
        now = _utcnow()
        with self._lock:
            self._conn.execute("DELETE FROM positions")
            for pos in positions:
                self._conn.execute(
                    "INSERT INTO positions "
                    "(symbol, qty, avg_entry_price, current_price, "
                    " unrealized_pnl, market_value, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        pos["symbol"],
                        float(pos["qty"]),
                        float(pos["avg_entry_price"]),
                        _optional_float(pos.get("current_price")),
                        _optional_float(pos.get("unrealized_pnl")),
                        _optional_float(pos.get("market_value")),
                        now,
                    ),
                )
            self._conn.commit()

    def get_positions(self) -> List[Dict[str, Any]]:
        rows = self._query("SELECT * FROM positions ORDER BY symbol")
        return [
            {
                "symbol": row["symbol"],
                "qty": float(row["qty"]),
                "avg_entry_price": float(row["avg_entry_price"]),
                "current_price": _optional_float(row["current_price"]),
                "unrealized_pnl": _optional_float(row["unrealized_pnl"]),
                "market_value": _optional_float(row["market_value"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ #
    # trades
    # ------------------------------------------------------------------ #

    def record_trade(
        self,
        symbol: str,
        side: str,
        qty: float,
        entry_price: float,
        exit_price: Optional[float],
        entry_time: str,
        exit_time: Optional[str],
        realized_pnl: Optional[float],
    ) -> int:
        cursor = self._execute(
            "INSERT INTO trades "
            "(symbol, side, qty, entry_price, exit_price, entry_time, "
            " exit_time, realized_pnl) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                symbol,
                side,
                float(qty),
                float(entry_price),
                None if exit_price is None else float(exit_price),
                entry_time,
                exit_time,
                None if realized_pnl is None else float(realized_pnl),
            ),
        )
        return int(cursor.lastrowid)

    def get_trades(self, limit: int = 100) -> List[Dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in rows]

    def realized_pnl_since(self, iso_timestamp: str) -> float:
        rows = self._query(
            "SELECT COALESCE(SUM(realized_pnl), 0) AS pnl FROM trades "
            "WHERE exit_time IS NOT NULL AND exit_time >= ?",
            (iso_timestamp,),
        )
        return float(rows[0]["pnl"])

    def get_trade_stats(self) -> Dict[str, Any]:
        """All-time aggregates over closed trades with a known PnL."""
        rows = self._query(
            "SELECT "
            "  COUNT(*)                                            AS total, "
            "  COALESCE(SUM(realized_pnl), 0)                      AS total_pnl, "
            "  SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END)   AS wins, "
            "  SUM(CASE WHEN realized_pnl <= 0 THEN 1 ELSE 0 END)  AS losses, "
            "  COALESCE(SUM(CASE WHEN realized_pnl > 0 "
            "      THEN realized_pnl ELSE 0 END), 0)               AS gross_profit, "
            "  COALESCE(SUM(CASE WHEN realized_pnl <= 0 "
            "      THEN realized_pnl ELSE 0 END), 0)               AS gross_loss, "
            "  MAX(realized_pnl)                                   AS best, "
            "  MIN(realized_pnl)                                   AS worst "
            "FROM trades WHERE realized_pnl IS NOT NULL"
        )
        row = rows[0]
        return {
            "total": int(row["total"]),
            "total_pnl": float(row["total_pnl"]),
            "wins": int(row["wins"] or 0),
            "losses": int(row["losses"] or 0),
            "gross_profit": float(row["gross_profit"]),
            "gross_loss": float(row["gross_loss"]),
            "best": _optional_float(row["best"]),
            "worst": _optional_float(row["worst"]),
        }

    # ------------------------------------------------------------------ #
    # equity history
    # ------------------------------------------------------------------ #

    def record_equity(self, equity: float) -> None:
        """Append an equity snapshot for the dashboard curve.

        Flat stretches (market closed, engine idle) are compressed: when the
        equity is unchanged from the previous snapshot, a new row is written
        at most every five minutes so the curve keeps anchor points without
        thousands of identical rows accumulating overnight.
        """
        equity = float(equity)
        now = datetime.now(timezone.utc)
        with self._lock:
            last = self._conn.execute(
                "SELECT ts, equity FROM equity_history "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if last is not None and float(last["equity"]) == equity:
                try:
                    last_ts = datetime.fromisoformat(last["ts"])
                except ValueError:
                    last_ts = None
                if (
                    last_ts is not None
                    and (now - last_ts).total_seconds() < 300
                ):
                    return
            self._conn.execute(
                "INSERT INTO equity_history (ts, equity) VALUES (?, ?)",
                (now.isoformat(timespec="milliseconds"), equity),
            )
            self._conn.execute(
                "DELETE FROM equity_history WHERE id <= "
                "(SELECT MAX(id) FROM equity_history) - ?",
                (_EQUITY_HISTORY_MAX_ROWS,),
            )
            self._conn.commit()

    def get_equity_history(
        self, since: Optional[str] = None, max_points: int = 600
    ) -> List[Dict[str, Any]]:
        """Equity snapshots since a UTC ISO timestamp (all history when
        None), oldest first, downsampled to at most ``max_points`` rows."""
        if since is None:
            rows = self._query("SELECT ts, equity FROM equity_history ORDER BY id")
        else:
            rows = self._query(
                "SELECT ts, equity FROM equity_history WHERE ts >= ? ORDER BY id",
                (since,),
            )
        points = [
            {"ts": row["ts"], "equity": float(row["equity"])} for row in rows
        ]
        if len(points) > max_points > 0:
            stride = -(-len(points) // max_points)  # ceil division
            sampled = points[::stride]
            if sampled[-1] is not points[-1]:
                sampled.append(points[-1])  # never drop the latest snapshot
            points = sampled
        return points

    # ------------------------------------------------------------------ #
    # runtime state (engine → dashboard JSON blobs)
    # ------------------------------------------------------------------ #

    def set_state(self, key: str, value: Any) -> None:
        """Publish a JSON-serializable blob under ``key``."""
        self._execute(
            "INSERT INTO runtime_state (key, value, updated_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(value, default=str), _utcnow()),
        )

    def get_state(self, key: str, default: Any = None) -> Any:
        rows = self._query(
            "SELECT value FROM runtime_state WHERE key = ?", (key,)
        )
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except (TypeError, ValueError):
            return default

    def get_state_updated_at(self, key: str) -> Optional[str]:
        rows = self._query(
            "SELECT updated_at FROM runtime_state WHERE key = ?", (key,)
        )
        return rows[0]["updated_at"] if rows else None

    # ------------------------------------------------------------------ #
    # logs
    # ------------------------------------------------------------------ #

    def add_log(self, level: str, message: str) -> None:
        self._execute(
            "INSERT INTO logs (ts, level, message) VALUES (?, ?, ?)",
            (_utcnow(), level.upper(), message),
        )
        # Bound the table so years of 1-minute polling cannot bloat the file.
        self._execute(
            "DELETE FROM logs WHERE id <= "
            "(SELECT MAX(id) FROM logs) - 5000"
        )

    def get_logs(self, limit: int = 20) -> List[Dict[str, Any]]:
        rows = self._query(
            "SELECT ts, level, message FROM logs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


_db_instance: Optional[Database] = None
_db_instance_lock = threading.Lock()


def get_db() -> Database:
    """Process-wide singleton accessor."""
    global _db_instance
    with _db_instance_lock:
        if _db_instance is None:
            _db_instance = Database()
        return _db_instance
