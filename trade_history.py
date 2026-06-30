"""Persistent trade history and outcome monitoring for the crypto signal bot."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from ccxt.base.errors import DDoSProtection, ExchangeError, NetworkError, RateLimitExceeded

logger = logging.getLogger("crypto-signal-worker")

TRADE_HISTORY_FILE = Path(os.getenv("TRADE_HISTORY_FILE", "trade_history.json"))
INVESTMENT_AMOUNT_EUR = float(os.getenv("REPORT_INVESTMENT_EUR", "10"))


def load_trades() -> list[dict[str, Any]]:
    """Load the persistent trade history."""
    if not TRADE_HISTORY_FILE.exists():
        return []

    try:
        with TRADE_HISTORY_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", TRADE_HISTORY_FILE, exc)
        return []

    return data if isinstance(data, list) else []


def save_trades(trades: list[dict[str, Any]]) -> None:
    """Save the persistent trade history."""
    try:
        with TRADE_HISTORY_FILE.open("w", encoding="utf-8") as file:
            json.dump(trades, file, indent=2, sort_keys=True)
    except OSError as exc:
        logger.error("Could not write %s: %s", TRADE_HISTORY_FILE, exc)


def build_trade_id(signal: dict[str, Any]) -> str:
    """Create a stable unique trade id from the signal."""
    return f"{signal['symbol']}:{signal['direction']}:{signal['candle_timestamp']}"


def calculate_pl_eur(trade: dict[str, Any]) -> float:
    """Calculate theoretical P/L using fixed spot size."""
    entry = float(trade["entry"])
    direction = trade["direction"]
    status = trade.get("status", "OPEN")

    if status == "TP1":
        target_1 = float(trade["target_1"])
        if direction == "LONG":
            pct = (target_1 - entry) / entry
        else:
            pct = (entry - target_1) / entry
        return INVESTMENT_AMOUNT_EUR * pct

    if status == "SL":
        stop_loss = float(trade["stop_loss"])
        if direction == "LONG":
            pct = (entry - stop_loss) / entry
        else:
            pct = (stop_loss - entry) / entry
        return -INVESTMENT_AMOUNT_EUR * pct

    return 0.0


def append_trade(signal: dict[str, Any], timeframe: str, reasons: list[str] | None = None) -> bool:
    """Append a new trade to history if it is not already present."""
    trades = load_trades()
    trade_id = build_trade_id(signal)
    if any(trade.get("id") == trade_id for trade in trades):
        return False

    trade = {
        "id": trade_id,
        "symbol": signal["symbol"],
        "direction": signal["direction"],
        "entry": float(signal["entry"]),
        "stop_loss": float(signal["stop_loss"]),
        "target_1": float(signal["target_1"]),
        "target_2": float(signal["target_2"]),
        "timeframe": timeframe,
        "candle_timestamp": str(signal["candle_timestamp"]),
        "created_at_utc": pd.to_datetime(int(signal["candle_timestamp"]), unit="ms", utc=True).isoformat(),
        "status": "OPEN",
        "closed_at_utc": None,
        "pl_eur": 0.0,
        "reasons": reasons or [],
        "notes": [],
    }
    trades.append(trade)
    save_trades(trades)
    logger.info("Stored trade in history: %s", trade_id)
    return True


def _fetch_candles_after(exchange: Any, symbol: str, timeframe: str, after_timestamp_ms: int) -> pd.DataFrame | None:
    """Fetch candles after the signal candle."""
    try:
        rows = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=after_timestamp_ms + 1, limit=500)
    except (NetworkError, RateLimitExceeded, DDoSProtection) as exc:
        logger.warning("Temporary Kraken issue while monitoring %s: %s", symbol, exc)
        return None
    except ExchangeError as exc:
        logger.warning("Kraken exchange error while monitoring %s: %s", symbol, exc)
        return None
    except Exception as exc:  # Defensive guard around ccxt edge cases.
        logger.warning("Unexpected monitoring error for %s: %s", symbol, exc)
        return None

    if not rows:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "datetime"])

    frame = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    frame["datetime"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    return frame[frame["timestamp"] > after_timestamp_ms]


def _resolve_trade_status(trade: dict[str, Any], candles: pd.DataFrame) -> dict[str, Any] | None:
    """Return status update for a trade if TP1 or SL has been reached."""
    direction = trade["direction"]
    target_1 = float(trade["target_1"])
    stop_loss = float(trade["stop_loss"])

    for _, candle in candles.iterrows():
        high = float(candle["high"])
        low = float(candle["low"])
        candle_time = pd.to_datetime(candle["timestamp"], unit="ms", utc=True).isoformat()

        if direction == "LONG":
            target_hit = high >= target_1
            stop_hit = low <= stop_loss
        else:
            target_hit = low <= target_1
            stop_hit = high >= stop_loss

        if target_hit and stop_hit:
            # OHLC data cannot tell which level was touched first inside the same candle.
            # Use a conservative result and mark the trade for manual review.
            return {
                "status": "SL",
                "closed_at_utc": candle_time,
                "note": "TP1 e SL nella stessa candela: esito conservativo SL, verificare manualmente su timeframe inferiore.",
            }

        if target_hit:
            return {"status": "TP1", "closed_at_utc": candle_time, "note": None}

        if stop_hit:
            return {"status": "SL", "closed_at_utc": candle_time, "note": None}

    return None


def update_open_trades(exchange: Any) -> int:
    """Update all OPEN trades by checking subsequent OHLC candles."""
    trades = load_trades()
    updated = 0

    for trade in trades:
        if trade.get("status") != "OPEN":
            continue

        symbol = trade["symbol"]
        timeframe = trade.get("timeframe", "15m")
        after_ts = int(trade["candle_timestamp"])
        candles = _fetch_candles_after(exchange, symbol, timeframe, after_ts)
        if candles is None or candles.empty:
            continue

        status_update = _resolve_trade_status(trade, candles)
        if status_update is None:
            continue

        trade["status"] = status_update["status"]
        trade["closed_at_utc"] = status_update["closed_at_utc"]
        if status_update.get("note"):
            trade.setdefault("notes", []).append(status_update["note"])
        trade["pl_eur"] = round(calculate_pl_eur(trade), 6)
        updated += 1
        logger.info("Trade %s closed as %s", trade.get("id"), trade["status"])

    if updated:
        save_trades(trades)

    return updated


def import_existing_trades_from_json(path: str | Path) -> int:
    """Import old trades from a JSON file if the user provides one on Railway.

    Expected shape: a list of objects containing at least symbol, direction,
    entry, stop_loss, target_1, target_2, candle_timestamp, and optionally timeframe.
    """
    import_path = Path(path)
    if not import_path.exists():
        logger.warning("Import file does not exist: %s", import_path)
        return 0

    try:
        with import_path.open("r", encoding="utf-8") as file:
            imported = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not import old trades from %s: %s", import_path, exc)
        return 0

    if not isinstance(imported, list):
        logger.warning("Import file must contain a JSON list: %s", import_path)
        return 0

    trades = load_trades()
    existing_ids = {trade.get("id") for trade in trades}
    added = 0

    for item in imported:
        if not isinstance(item, dict):
            continue
        try:
            signal = {
                "symbol": item["symbol"],
                "direction": item.get("direction", "LONG"),
                "entry": float(item["entry"]),
                "stop_loss": float(item["stop_loss"]),
                "target_1": float(item["target_1"]),
                "target_2": float(item["target_2"]),
                "candle_timestamp": str(item["candle_timestamp"]),
            }
            trade_id = build_trade_id(signal)
        except (KeyError, TypeError, ValueError):
            continue

        if trade_id in existing_ids:
            continue

        trades.append(
            {
                "id": trade_id,
                "symbol": signal["symbol"],
                "direction": signal["direction"],
                "entry": signal["entry"],
                "stop_loss": signal["stop_loss"],
                "target_1": signal["target_1"],
                "target_2": signal["target_2"],
                "timeframe": item.get("timeframe", "15m"),
                "candle_timestamp": signal["candle_timestamp"],
                "created_at_utc": pd.to_datetime(int(signal["candle_timestamp"]), unit="ms", utc=True).isoformat(),
                "status": item.get("status", "OPEN"),
                "closed_at_utc": item.get("closed_at_utc"),
                "pl_eur": float(item.get("pl_eur", 0.0)),
                "reasons": item.get("reasons", []),
                "notes": item.get("notes", []),
            }
        )
        existing_ids.add(trade_id)
        added += 1

    if added:
        save_trades(trades)

    return added
