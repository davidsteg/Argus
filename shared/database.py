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
bot_config   dynamic strategy parameters (rewritten nightly by the optimizer)
bot_status   single-row state machine: RUNNING / KILLED, equity, day anchor
positions    active tickers with average entry price and quantity
trades       historical executions with entry/exit timestamps and realized PnL
logs         system event log with strict UTC ISO-8601 timestamps
"""

from __future__ import annotations

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
    "news_cutoff": 0.55,       # minimum sentiment score required to trade
    "atr_stop_mult": 1.5,      # bracket stop-loss distance, ATR multiples
    "atr_target_mult": 2.5,    # bracket take-profit distance, ATR multiples
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
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


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
            self._seed_defaults()
            self._conn.commit()

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

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
                    "(symbol, qty, avg_entry_price, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        pos["symbol"],
                        float(pos["qty"]),
                        float(pos["avg_entry_price"]),
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
