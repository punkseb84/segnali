from __future__ import annotations

import pandas as pd

from project.simple_strategy_scanner import SimpleStrategyScanner


_TIMEFRAME_DELTAS = {
    "15m": pd.Timedelta(minutes=15),
    "1h": pd.Timedelta(hours=1),
}


class ReliableSimpleStrategyScanner(SimpleStrategyScanner):
    def fetch_closed_frame(self, pair: str, timeframe: str, limit: int) -> pd.DataFrame:
        rows = self.client.fetch_all(
            """SELECT timestamp, open, high, low, close, volume
            FROM market_data.ohlc
            WHERE exchange = %s AND pair = %s AND timeframe = %s
            ORDER BY timestamp DESC
            LIMIT %s""",
            (self.exchange, pair, timeframe, limit + 2),
        )
        frame = pd.DataFrame(
            rows,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        if frame.empty:
            return frame
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        for column in ["open", "high", "low", "close", "volume"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame[
            frame["timestamp"] + _TIMEFRAME_DELTAS[timeframe]
            <= pd.Timestamp.now(tz="UTC")
        ]
        return frame.sort_values("timestamp").tail(limit).reset_index(drop=True)
