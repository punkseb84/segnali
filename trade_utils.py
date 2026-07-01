"""Pure helpers for price formatting and theoretical trade evaluation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


def format_price(price: float | int | str, min_decimals: int = 3) -> str:
    """Format a crypto price with at least three decimals.

    Uses more precision for sub-unit prices while never showing fewer than
    ``min_decimals`` decimals. This avoids messages such as Entry 1.20 and
    Stop Loss 1.20 when the real prices differ.
    """
    value = float(price)
    decimals = max(min_decimals, 6 if abs(value) < 1 else 3)
    return f"{value:.{decimals}f}"


def parse_iso_datetime(value: str) -> datetime | None:
    """Parse an ISO datetime and return an aware UTC datetime when possible."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def timeframe_to_milliseconds(timeframe: str) -> int:
    """Convert a compact timeframe such as 15m or 1h to milliseconds."""
    unit = timeframe[-1]
    amount = int(timeframe[:-1])
    if unit == "m":
        seconds = amount * 60
    elif unit == "h":
        seconds = amount * 60 * 60
    elif unit == "d":
        seconds = amount * 24 * 60 * 60
    else:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    return seconds * 1000


def next_candle_open_after(timestamp_iso: str, timeframe: str) -> int | None:
    """Return the next candle-open timestamp after a signal timestamp.

    If a signal arrives at 03:46 on a 15m timeframe, this returns 04:00, so
    the already-open 03:45 candle is not used for stop/target checks.
    """
    parsed = parse_iso_datetime(timestamp_iso)
    if parsed is None:
        return None

    timestamp_ms = int(parsed.timestamp() * 1000)
    interval_ms = timeframe_to_milliseconds(timeframe)
    return ((timestamp_ms // interval_ms) + 1) * interval_ms


def _as_float(candle: dict[str, Any], key: str) -> float:
    return float(candle[key])


def _candle_timestamp(candle: dict[str, Any]) -> int:
    return int(candle["timestamp"])


def _store_decisive_candle(trade: dict[str, Any], candle: dict[str, Any]) -> None:
    trade["decisive_candle_timestamp"] = str(_candle_timestamp(candle))
    trade["decisive_candle_ohlc"] = {
        "open": _as_float(candle, "open"),
        "high": _as_float(candle, "high"),
        "low": _as_float(candle, "low"),
        "close": _as_float(candle, "close"),
    }


def evaluate_trade_candles(trade: dict[str, Any], candles: Iterable[dict[str, Any]], timeframe: str) -> str | None:
    """Evaluate an open theoretical trade using OHLC candles only.

    The function only considers candles that open after the signal was sent,
    never candles already open before the signal. It returns the latest event:
    TARGET_1, TARGET_2, STOP_LOSS, AMBIGUOUS, or None.
    """
    if trade.get("status") != "open":
        return None

    start_timestamp = trade.get("check_from_candle_timestamp")
    if start_timestamp is None:
        start_timestamp = next_candle_open_after(str(trade.get("opened_at", "")), timeframe)
        if start_timestamp is None:
            start_timestamp = int(trade.get("candle_timestamp", 0)) + timeframe_to_milliseconds(timeframe)
        trade["check_from_candle_timestamp"] = str(start_timestamp)

    last_checked = int(trade.get("last_checked_candle_timestamp") or 0)
    start_timestamp = int(start_timestamp)
    event: str | None = None

    for candle in sorted(candles, key=_candle_timestamp):
        candle_timestamp = _candle_timestamp(candle)
        if candle_timestamp < start_timestamp or candle_timestamp <= last_checked:
            continue

        high = _as_float(candle, "high")
        low = _as_float(candle, "low")
        trade["analyzed_candles"] = int(trade.get("analyzed_candles") or 0) + 1
        trade["last_checked_candle_timestamp"] = str(candle_timestamp)
        previous_min = trade.get("min_after_entry")
        previous_max = trade.get("max_after_entry")
        trade["min_after_entry"] = low if previous_min is None else min(float(previous_min), low)
        trade["max_after_entry"] = high if previous_max is None else max(float(previous_max), high)

        if trade["direction"] == "LONG":
            stop_hit = low <= float(trade["stop_loss"])
            target_1_hit = high >= float(trade["target_1"])
            target_2_hit = high >= float(trade["target_2"])
        else:
            stop_hit = high >= float(trade["stop_loss"])
            target_1_hit = low <= float(trade["target_1"])
            target_2_hit = low <= float(trade["target_2"])

        if stop_hit and (target_1_hit or target_2_hit):
            trade["status"] = "ambiguous"
            trade["result"] = "AMBIGUO"
            trade["closed_at"] = datetime.now(timezone.utc).isoformat()
            _store_decisive_candle(trade, candle)
            return "AMBIGUOUS"

        if stop_hit:
            trade["status"] = "closed"
            trade["stop_loss_hit"] = True
            trade["result"] = "SL"
            trade["closed_at"] = datetime.now(timezone.utc).isoformat()
            trade["result_r"] = 0.25 if trade.get("target_1_hit") else -1.0
            _store_decisive_candle(trade, candle)
            return "STOP_LOSS"

        if target_2_hit:
            trade["target_1_hit"] = True
            trade["target_2_hit"] = True
            trade["status"] = "closed"
            trade["result"] = "TP2"
            trade["closed_at"] = datetime.now(timezone.utc).isoformat()
            trade["result_r"] = 2.25
            _store_decisive_candle(trade, candle)
            return "TARGET_2"

        if target_1_hit and not trade.get("target_1_hit"):
            trade["target_1_hit"] = True
            trade["result"] = "TP1"
            _store_decisive_candle(trade, candle)
            event = "TARGET_1"

    return event
