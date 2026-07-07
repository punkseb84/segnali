"""SQLite persistence for signals, trades and daily state."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import DATABASE_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id TEXT UNIQUE,
    timestamp TEXT,
    mode TEXT,
    exchange TEXT,
    pair TEXT,
    direction TEXT,
    timeframe TEXT,
    entry REAL,
    stop_loss REAL,
    target_1 REAL,
    trade_amount_eur REAL,
    quantity REAL,
    net_profit_tp1_eur REAL,
    net_loss_sl_eur REAL,
    realized_result_eur REAL DEFAULT 0,
    fees_total_eur REAL,
    net_rr REAL,
    score INTEGER,
    status TEXT,
    outcome TEXT,
    market_regime TEXT,
    btc_trend TEXT,
    ema20 REAL,
    ema50 REAL,
    ema200 REAL,
    rsi REAL,
    macd_hist REAL,
    atr REAL,
    adx REAL,
    volume REAL,
    avg_volume_20 REAL,
    relative_volume REAL,
    support REAL,
    resistance REAL,
    swing_high REAL,
    swing_low REAL,
    distance_ema20_pct REAL,
    distance_resistance_pct REAL,
    hour INTEGER,
    weekday TEXT,
    reasons TEXT,
    penalties TEXT,
    ambiguous_candle INTEGER DEFAULT 0,
    created_at TEXT,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

COLUMNS = [
    "signal_id", "timestamp", "mode", "exchange", "pair", "direction", "timeframe", "entry", "stop_loss", "target_1",
    "trade_amount_eur", "quantity", "net_profit_tp1_eur", "net_loss_sl_eur", "realized_result_eur", "fees_total_eur", "net_rr",
    "score", "status", "outcome", "market_regime", "btc_trend", "ema20", "ema50", "ema200", "rsi", "macd_hist", "atr", "adx",
    "volume", "avg_volume_20", "relative_volume", "support", "resistance", "swing_high", "swing_low", "distance_ema20_pct",
    "distance_resistance_pct", "hour", "weekday", "reasons", "penalties", "ambiguous_candle", "created_at", "closed_at"
]


@contextmanager
def connect(path: Path = DATABASE_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(signals)").fetchall()}
        if "target_2" in cols:
            # Legacy TP2 data can remain in old DB files; the new code never writes it.
            pass


def encode(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


def insert_signal(record: dict[str, Any]) -> None:
    values = [encode(record.get(column)) for column in COLUMNS]
    placeholders = ",".join("?" for _ in COLUMNS)
    with connect() as conn:
        conn.execute(f"INSERT OR IGNORE INTO signals ({','.join(COLUMNS)}) VALUES ({placeholders})", values)


def update_outcome(signal_id: str, status: str, outcome: str, realized_result_eur: float, ambiguous_candle: bool = False) -> None:
    with connect() as conn:
        conn.execute(
            """
            UPDATE signals
            SET status=?, outcome=?, realized_result_eur=?, ambiguous_candle=?, closed_at=?
            WHERE signal_id=?
            """,
            (status, outcome, realized_result_eur, int(ambiguous_candle), datetime.now(timezone.utc).isoformat(), signal_id),
        )


def open_trades() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM signals WHERE status='OPEN' ORDER BY timestamp ASC").fetchall()
        return [dict(row) for row in rows]


def recent_signals(hours: int = 24) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM signals WHERE timestamp >= datetime('now', ?) ORDER BY timestamp ASC", (f"-{hours} hours",)).fetchall()
        return [dict(row) for row in rows]


def signals_since(date_prefix: str, status: str | None = None, pair: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM signals WHERE substr(timestamp,1,10)=?"
    args: list[Any] = [date_prefix]
    if status:
        sql += " AND status=?"
        args.append(status)
    if pair:
        sql += " AND pair=?"
        args.append(pair)
    with connect() as conn:
        return [dict(row) for row in conn.execute(sql, args).fetchall()]


def set_state(key: str, value: Any) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO app_state (key,value) VALUES (?,?)", (key, json.dumps(value)))


def get_state(key: str, default: Any = None) -> Any:
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default


def all_closed() -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM signals WHERE status IN ('TP1','SL')").fetchall()]
