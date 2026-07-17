"""Public Binance historical klines used only for isolated research backfills."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import pandas as pd
import requests

_TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def binance_symbol(pair: str) -> str:
    base, _, _quote = pair.upper().strip().partition("/")
    aliases = {"XBT": "BTC", "XDG": "DOGE"}
    return f"{aliases.get(base, base)}USDT"


@dataclass(frozen=True)
class BinanceHistoricalKlineClient:
    api_base: str = "https://data-api.binance.vision"
    timeout_seconds: int = 20
    max_retries: int = 3
    sleep_seconds: float = 0.10

    def fetch_recent_closed_klines(
        self,
        pair: str,
        timeframe: str,
        limit: int = 3000,
    ) -> pd.DataFrame:
        if timeframe not in _TIMEFRAME_MS:
            raise ValueError(f"Unsupported Binance timeframe: {timeframe}")
        requested = max(1, int(limit))
        symbol = binance_symbol(pair)
        end_time: int | None = None
        collected: dict[int, list[Any]] = {}

        while len(collected) < requested:
            chunk_limit = min(1000, requested - len(collected))
            params: dict[str, Any] = {
                "symbol": symbol,
                "interval": timeframe,
                "limit": chunk_limit,
            }
            if end_time is not None:
                params["endTime"] = end_time
            payload = self._request(params)
            if not payload:
                break
            for row in payload:
                collected[int(row[0])] = row
            earliest = min(int(row[0]) for row in payload)
            next_end = earliest - 1
            if end_time is not None and next_end >= end_time:
                break
            end_time = next_end
            if len(payload) < chunk_limit:
                break
            time.sleep(self.sleep_seconds)

        now_ms = int(time.time() * 1000)
        interval_ms = _TIMEFRAME_MS[timeframe]
        rows = [
            row
            for timestamp, row in sorted(collected.items())
            if timestamp + interval_ms <= now_ms
        ][-requested:]
        if not rows:
            return pd.DataFrame(
                columns=["timestamp", "open", "high", "low", "close", "volume"]
            )
        frame = pd.DataFrame(
            [row[:6] for row in rows],
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        frame["timestamp"] = pd.to_datetime(
            frame["timestamp"].astype("int64"), unit="ms", utc=True
        )
        for column in ["open", "high", "low", "close", "volume"]:
            frame[column] = frame[column].astype(float)
        return frame.reset_index(drop=True)

    def _request(self, params: dict[str, Any]) -> list[list[Any]]:
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.get(
                    f"{self.api_base}/api/v3/klines",
                    params=params,
                    timeout=self.timeout_seconds,
                )
                if response.status_code == 429:
                    retry_after = float(response.headers.get("Retry-After", 1) or 1)
                    time.sleep(max(retry_after, self.sleep_seconds * attempt))
                    continue
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list):
                    raise RuntimeError(f"Unexpected Binance payload: {payload}")
                return payload
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
                time.sleep(self.sleep_seconds * attempt)
        raise RuntimeError(
            f"Binance historical fetch failed params={params}: {last_error}"
        )
