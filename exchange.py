"""Kraken public OHLC client."""
from __future__ import annotations

import logging
import time
from typing import Any

import pandas as pd
import requests

from config import KRAKEN_API_BASE, KRAKEN_API_SLEEP_SECONDS, KRAKEN_MAX_RETRIES, KRAKEN_TIMEOUT_SECONDS

logger = logging.getLogger("crypto-bot.exchange")

_TIMEFRAME_TO_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
_SYMBOL_ALIASES = {
    "BTC/USD": "XBTUSD",
    "DOGE/USD": "XDGUSD",
}


def kraken_pair(pair: str) -> str:
    """Convert a common pair such as BTC/USD into a Kraken pair id."""
    pair = pair.upper().strip()
    if pair in _SYMBOL_ALIASES:
        return _SYMBOL_ALIASES[pair]
    return pair.replace("/", "")


def fetch_ohlc(pair: str, timeframe: str, limit: int = 300, retries: int | None = None, timeout: int | None = None) -> pd.DataFrame:
    """Fetch Kraken OHLC candles and return timestamp/open/high/low/close/volume."""
    retries = KRAKEN_MAX_RETRIES if retries is None else retries
    timeout = KRAKEN_TIMEOUT_SECONDS if timeout is None else timeout
    interval = _TIMEFRAME_TO_MINUTES.get(timeframe)
    if interval is None:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    params = {"pair": kraken_pair(pair), "interval": interval}
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.get(f"{KRAKEN_API_BASE}/OHLC", params=params, timeout=timeout)
            if response.status_code == 429:
                wait = attempt * 3
                logger.warning("Kraken rate limit for %s %s; retry in %ss", pair, timeframe, wait)
                time.sleep(wait)
                continue
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            errors = payload.get("error") or []
            if errors:
                logger.warning("Kraken returned errors for %s %s: %s", pair, timeframe, errors)
                return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
            result = payload.get("result") or {}
            series_key = next((key for key in result.keys() if key != "last"), None)
            if not series_key:
                return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
            rows = result[series_key][-limit:]
            frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "vwap", "volume", "count"])
            frame = frame[["timestamp", "open", "high", "low", "close", "volume"]].copy()
            frame["timestamp"] = pd.to_datetime(frame["timestamp"].astype(int), unit="s", utc=True)
            for column in ["open", "high", "low", "close", "volume"]:
                frame[column] = frame[column].astype(float)
            time.sleep(KRAKEN_API_SLEEP_SECONDS)
            return frame.reset_index(drop=True)
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            logger.warning("Kraken fetch failed for %s %s attempt %s/%s: %s", pair, timeframe, attempt, retries, exc)
            time.sleep(KRAKEN_API_SLEEP_SECONDS + attempt * 2)
    logger.error("Kraken fetch failed permanently for %s %s: %s", pair, timeframe, last_error)
    return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
