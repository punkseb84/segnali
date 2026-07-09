"""Kraken-only market data client for the Data Collector module."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

_TIMEFRAME_TO_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
_SYMBOL_ALIASES = {"BTC/USD": "XBTUSD", "DOGE/USD": "XDGUSD"}


def kraken_pair(pair: str) -> str:
    pair = pair.upper().strip()
    return _SYMBOL_ALIASES.get(pair, pair.replace("/", ""))


@dataclass(frozen=True)
class KrakenOhlcClient:
    api_base: str
    sleep_seconds: float = 1.2
    max_retries: int = 3
    timeout_seconds: int = 20

    def fetch_ohlc(self, pair: str, timeframe: str, since: datetime | None = None, limit: int = 720) -> pd.DataFrame:
        interval = _TIMEFRAME_TO_MINUTES.get(timeframe)
        if interval is None:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        params: dict[str, Any] = {"pair": kraken_pair(pair), "interval": interval}
        if since is not None:
            params["since"] = int(since.replace(tzinfo=timezone.utc).timestamp())
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.get(f"{self.api_base}/OHLC", params=params, timeout=self.timeout_seconds)
                if response.status_code == 429:
                    time.sleep(self.sleep_seconds * attempt * 2)
                    continue
                response.raise_for_status()
                payload = response.json()
                errors = payload.get("error") or []
                if errors:
                    raise RuntimeError(f"Kraken returned errors: {errors}")
                result = payload.get("result") or {}
                series_key = next((key for key in result if key != "last"), None)
                if not series_key:
                    return self._empty_frame()
                rows = result[series_key][-limit:]
                frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "vwap", "volume", "count"])
                frame = frame[["timestamp", "open", "high", "low", "close", "volume"]].copy()
                frame["timestamp"] = pd.to_datetime(frame["timestamp"].astype(int), unit="s", utc=True)
                for column in ["open", "high", "low", "close", "volume"]:
                    frame[column] = frame[column].astype(float)
                time.sleep(self.sleep_seconds)
                return frame.reset_index(drop=True)
            except (requests.RequestException, ValueError, RuntimeError) as exc:
                last_error = exc
                time.sleep(self.sleep_seconds * attempt)
        raise RuntimeError(f"Kraken fetch failed for {pair} {timeframe}: {last_error}")

    @staticmethod
    def _empty_frame() -> pd.DataFrame:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
