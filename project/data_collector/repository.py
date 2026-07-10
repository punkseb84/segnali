"""Persistence functions owned by the Data Collector module."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

import pandas as pd

from project.database.postgres import PostgresClient

_TIMEFRAME_TO_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


@dataclass(frozen=True)
class OhlcIntegrityReport:
    pair: str
    timeframe: str
    rows: int
    duplicate_rows: int
    missing_required_values: int
    latest_timestamp: datetime | None


class MarketDataRepository:
    """Repository restricted to market_data schema tables."""

    def __init__(self, client: PostgresClient, exchange: str) -> None:
        self.client = client
        self.exchange = exchange

    def upsert_pair(self, pair: str) -> None:
        base, _, quote = pair.partition("/")
        self.client.execute(
            """INSERT INTO market_data.pairs(exchange, symbol, base_asset, quote_asset)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT(exchange, symbol) DO UPDATE SET is_active = TRUE""",
            (self.exchange, pair, base, quote),
        )

    def upsert_timeframe(self, timeframe: str) -> None:
        self.client.execute(
            """INSERT INTO market_data.timeframes(name, minutes)
            VALUES (%s, %s)
            ON CONFLICT(name) DO UPDATE SET minutes = EXCLUDED.minutes""",
            (timeframe, _TIMEFRAME_TO_MINUTES[timeframe]),
        )

    def latest_timestamp(self, pair: str, timeframe: str) -> datetime | None:
        rows = self.client.fetch_all(
            """SELECT MAX(timestamp) FROM market_data.ohlc
            WHERE exchange = %s AND pair = %s AND timeframe = %s""",
            (self.exchange, pair, timeframe),
        )
        return rows[0][0] if rows and rows[0][0] is not None else None

    def insert_ohlc(self, pair: str, timeframe: str, frame: pd.DataFrame) -> int:
        if frame.empty:
            return 0
        rows: list[Sequence[object]] = [
            (self.exchange, pair, timeframe, row.timestamp.to_pydatetime(), row.open, row.high, row.low, row.close, row.volume)
            for row in frame.itertuples(index=False)
        ]
        return self.client.executemany(
            """INSERT INTO market_data.ohlc(exchange, pair, timeframe, timestamp, open, high, low, close, volume)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT(exchange, pair, timeframe, timestamp) DO NOTHING""",
            rows,
        )

    def write_sync_log(self, pair: str, timeframe: str, started_at: datetime, status: str, rows_inserted: int, error: str | None = None) -> None:
        self.client.execute(
            """INSERT INTO market_data.sync_log(exchange, pair, timeframe, started_at, finished_at, status, rows_inserted, error_message)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (self.exchange, pair, timeframe, started_at, datetime.now(timezone.utc), status, rows_inserted, error),
        )

    def verify_integrity(self, pair: str, timeframe: str) -> OhlcIntegrityReport:
        rows = self.client.fetch_all(
            """
            SELECT COUNT(*) AS rows,
                   COUNT(*) - COUNT(DISTINCT timestamp) AS duplicate_rows,
                   SUM(CASE WHEN open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL OR volume IS NULL THEN 1 ELSE 0 END) AS missing_required_values,
                   MAX(timestamp) AS latest_timestamp
            FROM market_data.ohlc
            WHERE exchange = %s AND pair = %s AND timeframe = %s
            """,
            (self.exchange, pair, timeframe),
        )
        row = rows[0]
        return OhlcIntegrityReport(pair, timeframe, int(row[0] or 0), int(row[1] or 0), int(row[2] or 0), row[3])
