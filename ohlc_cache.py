"""SQLite OHLC cache used by research and Railway deployments."""
from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from config import EXCHANGE_NAME, KRAKEN_API_SLEEP_SECONDS, OHLC_DB_PATH, RESEARCH_OHLC_LIMIT
from exchange import fetch_ohlc

logger = logging.getLogger("crypto-bot.ohlc-cache")

_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def init_ohlc_cache() -> None:
    Path(OHLC_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(OHLC_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ohlc_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exchange TEXT NOT NULL,
                pair TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(exchange, pair, timeframe, timestamp)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ohlc_lookup ON ohlc_data(exchange, pair, timeframe, timestamp)")


def _normalize_ts(value: object) -> str:
    ts = pd.to_datetime(value, utc=True)
    return ts.isoformat()


def save_ohlc_to_cache(pair: str, timeframe: str, frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    init_ohlc_cache()
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        (EXCHANGE_NAME, pair, timeframe, _normalize_ts(row.timestamp), float(row.open), float(row.high), float(row.low), float(row.close), float(row.volume), now)
        for row in frame[_COLUMNS].itertuples(index=False)
    ]
    with sqlite3.connect(OHLC_DB_PATH) as conn:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO ohlc_data(exchange, pair, timeframe, timestamp, open, high, low, close, volume, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return conn.total_changes - before


def get_ohlc_from_cache(pair: str, timeframe: str, start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
    init_ohlc_cache()
    query = "SELECT timestamp, open, high, low, close, volume FROM ohlc_data WHERE exchange=? AND pair=? AND timeframe=?"
    params: list[object] = [EXCHANGE_NAME, pair, timeframe]
    if start_date:
        query += " AND timestamp >= ?"
        params.append(_normalize_ts(start_date))
    if end_date:
        query += " AND timestamp <= ?"
        params.append(_normalize_ts(end_date))
    query += " ORDER BY timestamp"
    with sqlite3.connect(OHLC_DB_PATH) as conn:
        frame = pd.read_sql_query(query, conn, params=params)
    if frame.empty:
        return pd.DataFrame(columns=_COLUMNS)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = frame[column].astype(float)
    return frame


def sync_ohlc_cache(pair: str, timeframe: str, start_date: str | None = None, end_date: str | None = None) -> pd.DataFrame:
    cached = get_ohlc_from_cache(pair, timeframe, start_date, end_date)
    if len(cached) >= min(RESEARCH_OHLC_LIMIT, 200):
        return cached.tail(RESEARCH_OHLC_LIMIT).reset_index(drop=True)
    fresh = fetch_ohlc(pair, timeframe, RESEARCH_OHLC_LIMIT)
    inserted = save_ohlc_to_cache(pair, timeframe, fresh)
    logger.info("OHLC cache sync pair=%s timeframe=%s inserted=%s cached_before=%s", pair, timeframe, inserted, len(cached))
    time.sleep(KRAKEN_API_SLEEP_SECONDS)
    return get_ohlc_from_cache(pair, timeframe, start_date, end_date).tail(RESEARCH_OHLC_LIMIT).reset_index(drop=True)


def update_missing_ohlc(pair: str, timeframe: str) -> pd.DataFrame:
    return sync_ohlc_cache(pair, timeframe)
