"""Pure helpers for price formatting and theoretical trade evaluation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


def format_price(price: float | int | str, min_decimals: int = 4) -> str:
    """Format a crypto price with at least four decimals.

    Uses more precision for sub-unit prices while never showing fewer than
    ``min_decimals`` decimals. This avoids messages such as Entry 1.20 and
    Stop Loss 1.20 when the real prices differ.
    """
    value = float(price)
    decimals = max(min_decimals, 6 if abs(value) < 1 else 4)
    return f"{value:.{decimals}f}"


def generate_signal_id(symbol: str, direction: str, when: datetime) -> str:
    """Create a readable unique signal id: SIG-YYYYMMDD-HHMM-SYMBOL-DIRECTION."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    clean_symbol = "".join(character for character in symbol.upper() if character.isalnum())
    clean_direction = direction.upper().strip()
    return f"SIG-{when:%Y%m%d}-{when:%H%M}-{clean_symbol}-{clean_direction}"


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


def evaluate_trade_candles(trade: dict[str, Any], candles: Iterable[dict[str, Any]], timeframe: str, same_candle_priority: str = "SL") -> str | None:
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
            trade["ambiguous_candle"] = True
            priority = same_candle_priority.upper().strip()
            if priority == "TP":
                if target_2_hit:
                    trade["target_1_hit"] = True
                    trade["target_2_hit"] = True
                    trade["status"] = "closed"
                    trade["result"] = "TP2"
                    trade["closed_at"] = datetime.now(timezone.utc).isoformat()
                    trade["result_r"] = 2.25
                    _store_decisive_candle(trade, candle)
                    return "TARGET_2"
                trade["target_1_hit"] = True
                trade["result"] = "TP1"
                _store_decisive_candle(trade, candle)
                event = "TARGET_1"
                continue
            trade["status"] = "closed"
            trade["stop_loss_hit"] = True
            trade["result"] = "SL"
            trade["closed_at"] = datetime.now(timezone.utc).isoformat()
            trade["result_r"] = 0.25 if trade.get("target_1_hit") else -1.0
            _store_decisive_candle(trade, candle)
            return "STOP_LOSS"

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


def calculate_trade_metrics(
    direction: str,
    entry: float,
    stop_loss: float,
    target_1: float,
    target_2: float,
    capital: float,
    buy_fee_percent: float,
    sell_fee_percent: float,
    spread_percent: float,
    slippage_percent: float,
) -> dict[str, float]:
    """Estimate real trade economics including fees, spread, and slippage."""
    buy_fee_rate = buy_fee_percent / 100
    sell_fee_rate = sell_fee_percent / 100
    half_spread_rate = spread_percent / 100 / 2
    slippage_rate = slippage_percent / 100

    def long_pnl(exit_price: float) -> dict[str, float]:
        entry_effective = entry * (1 + half_spread_rate + slippage_rate)
        exit_effective = exit_price * (1 - half_spread_rate - slippage_rate)
        quantity = capital / (entry_effective * (1 + buy_fee_rate))
        gross_cost = quantity * entry_effective
        buy_fee = gross_cost * buy_fee_rate
        gross_exit = quantity * exit_effective
        sell_fee = gross_exit * sell_fee_rate
        net_pnl = gross_exit - sell_fee - gross_cost - buy_fee
        return {
            "net_pnl": net_pnl,
            "gross_pnl": gross_exit - gross_cost,
            "buy_fee": buy_fee,
            "sell_fee": sell_fee,
            "spread_cost": abs(quantity * entry * half_spread_rate) + abs(quantity * exit_price * half_spread_rate),
            "slippage_cost": abs(quantity * entry * slippage_rate) + abs(quantity * exit_price * slippage_rate),
        }

    def short_pnl(exit_price: float) -> dict[str, float]:
        entry_effective = entry * (1 - half_spread_rate - slippage_rate)
        exit_effective = exit_price * (1 + half_spread_rate + slippage_rate)
        gross_pnl = capital * ((entry_effective - exit_effective) / entry_effective)
        buy_fee = capital * buy_fee_rate
        sell_fee = capital * sell_fee_rate
        spread_cost = capital * (spread_percent / 100)
        slippage_cost = capital * (slippage_percent / 100) * 2
        net_pnl = gross_pnl - buy_fee - sell_fee
        return {
            "net_pnl": net_pnl,
            "gross_pnl": gross_pnl,
            "buy_fee": buy_fee,
            "sell_fee": sell_fee,
            "spread_cost": spread_cost,
            "slippage_cost": slippage_cost,
        }

    pnl_func = long_pnl if direction.upper() == "LONG" else short_pnl
    stop = pnl_func(stop_loss)
    t1 = pnl_func(target_1)
    t2 = pnl_func(target_2)
    theoretical_risk = abs(entry - stop_loss)
    theoretical_profit_1 = abs(target_1 - entry)
    theoretical_profit_2 = abs(target_2 - entry)
    loss_abs = abs(stop["net_pnl"])
    blended_net_profit = 0.5 * t1["net_pnl"] + 0.5 * t2["net_pnl"]
    blended_gross_profit = 0.5 * t1["gross_pnl"] + 0.5 * t2["gross_pnl"]
    net_rr_1 = t1["net_pnl"] / loss_abs if loss_abs else 0.0
    net_rr_2 = t2["net_pnl"] / loss_abs if loss_abs else 0.0
    net_rr_blended = blended_net_profit / loss_abs if loss_abs else 0.0

    return {
        "capital_at_entry": capital,
        "theoretical_risk": theoretical_risk,
        "theoretical_profit_target_1": theoretical_profit_1,
        "theoretical_profit_target_2": theoretical_profit_2,
        "theoretical_rr_target_1": theoretical_profit_1 / theoretical_risk if theoretical_risk else 0.0,
        "theoretical_rr_target_2": theoretical_profit_2 / theoretical_risk if theoretical_risk else 0.0,
        "net_profit_target_1": t1["net_pnl"],
        "net_profit_target_2": t2["net_pnl"],
        "net_profit_blended": blended_net_profit,
        "net_loss_stop": stop["net_pnl"],
        "net_rr_target_1": net_rr_1,
        "net_rr_target_2": net_rr_2,
        "net_rr_blended": net_rr_blended,
        "gross_profit_target_1": t1["gross_pnl"],
        "gross_profit_target_2": t2["gross_pnl"],
        "gross_profit_blended": blended_gross_profit,
        "gross_loss_stop": stop["gross_pnl"],
        "estimated_buy_fee": t1["buy_fee"],
        "estimated_sell_fee_target_1": t1["sell_fee"],
        "estimated_sell_fee_target_2": t2["sell_fee"],
        "estimated_sell_fee_stop": stop["sell_fee"],
        "estimated_spread_cost_target_1": t1["spread_cost"],
        "estimated_spread_cost_target_2": t2["spread_cost"],
        "estimated_spread_cost_stop": stop["spread_cost"],
        "estimated_slippage_cost_target_1": t1["slippage_cost"],
        "estimated_slippage_cost_target_2": t2["slippage_cost"],
        "estimated_slippage_cost_stop": stop["slippage_cost"],
    }


def pnl_for_outcome(trade: dict[str, Any], event: str) -> dict[str, float]:
    """Return net/gross/cost estimates for a final trade outcome."""
    metrics = trade.get("cost_metrics", {})
    if event == "TARGET_2":
        return {
            "net_pnl": float(metrics.get("net_profit_target_2", 0)),
            "gross_pnl": float(metrics.get("gross_profit_target_2", 0)),
            "fees": float(metrics.get("estimated_buy_fee", 0)) + float(metrics.get("estimated_sell_fee_target_2", 0)),
            "spread": float(metrics.get("estimated_spread_cost_target_2", 0)),
            "slippage": float(metrics.get("estimated_slippage_cost_target_2", 0)),
        }
    if event == "STOP_LOSS" and trade.get("target_1_hit"):
        return {
            "net_pnl": 0.5 * float(metrics.get("net_profit_target_1", 0)) + 0.5 * float(metrics.get("net_loss_stop", 0)),
            "gross_pnl": 0.5 * float(metrics.get("gross_profit_target_1", 0)) + 0.5 * float(metrics.get("gross_loss_stop", 0)),
            "fees": float(metrics.get("estimated_buy_fee", 0)) + 0.5 * float(metrics.get("estimated_sell_fee_target_1", 0)) + 0.5 * float(metrics.get("estimated_sell_fee_stop", 0)),
            "spread": 0.5 * float(metrics.get("estimated_spread_cost_target_1", 0)) + 0.5 * float(metrics.get("estimated_spread_cost_stop", 0)),
            "slippage": 0.5 * float(metrics.get("estimated_slippage_cost_target_1", 0)) + 0.5 * float(metrics.get("estimated_slippage_cost_stop", 0)),
        }
    return {
        "net_pnl": float(metrics.get("net_loss_stop", 0)),
        "gross_pnl": float(metrics.get("gross_loss_stop", 0)),
        "fees": float(metrics.get("estimated_buy_fee", 0)) + float(metrics.get("estimated_sell_fee_stop", 0)),
        "spread": float(metrics.get("estimated_spread_cost_stop", 0)),
        "slippage": float(metrics.get("estimated_slippage_cost_stop", 0)),
    }
