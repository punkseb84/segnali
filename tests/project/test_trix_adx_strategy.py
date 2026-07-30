from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pandas as pd

from project.notifying_trix_adx_outcome_monitor import NotifyingTrixAdxOutcomeMonitor
from project.trix_adx_formatters import format_trix_adx_signal_message
from project.trix_adx_outcome_monitor import TrixAdxOutcomeMonitor
from project.trix_adx_scanner import TrixAdxScanner


def _frame(closes: list[float], step_hours: int = 1) -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = [start + timedelta(hours=step_hours * index) for index in range(len(closes))]
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": closes,
            "high": [value + 0.5 for value in closes],
            "low": [value - 0.5 for value in closes],
            "close": closes,
            "volume": [100.0] * len(closes),
        }
    )


def test_trix_detects_a_fresh_zero_cross() -> None:
    scanner = object.__new__(TrixAdxScanner)
    scanner.trix_length = 20
    scanner.trix_signal_length = 9

    downtrend = [120.0 - (30.0 * index / 79.0) for index in range(80)]
    recovery = [90.0 + (40.0 * index / 9.0) for index in range(10)]
    data = scanner.add_entry_indicators(_frame(downtrend + recovery))

    assert float(data.iloc[-2]["trix"]) <= 0.0
    assert float(data.iloc[-1]["trix"]) > 0.0
    assert float(data.iloc[-1]["trix"]) > float(data.iloc[-1]["trix_signal"])


def test_four_hour_context_confirms_strong_bullish_trend() -> None:
    scanner = object.__new__(TrixAdxScanner)
    scanner.adx_length = 14

    sideways = [100.0 + math.sin(index / 4.0) * 0.8 for index in range(210)]
    last_value = sideways[-1]
    trend = [last_value + ((140.0 - last_value) * index / 29.0) for index in range(1, 30)]
    data = scanner.add_context_indicators(_frame(sideways + trend, step_hours=4))
    latest = data.iloc[-1]
    previous = data.iloc[-2]

    assert float(latest["close"]) > float(latest["ema50"]) > float(latest["ema200"])
    assert float(latest["macd"]) > float(latest["macd_signal"])
    assert float(latest["macd"]) > 0.0
    assert float(latest["adx"]) > 25.0
    assert float(latest["adx"]) > float(previous["adx"])


def test_true_break_even_covers_estimated_costs() -> None:
    monitor = object.__new__(TrixAdxOutcomeMonitor)
    monitor.sell_fee_rate = 0.001
    monitor.spread_rate = 0.0005
    monitor.slippage_rate = 0.0
    monitor.break_even_buffer_rate = 0.0002

    entry = 193.041
    quantity = 100.0 / entry
    signal = {
        "entry": entry,
        "quantity": quantity,
        "estimated_buy_fee_eur": entry * quantity * 0.001,
    }

    break_even_price = monitor.true_break_even_price(signal)
    realized_net = monitor._leg_net(signal, break_even_price, 1.0)

    assert break_even_price > entry
    assert realized_net > 0.0


def test_delivery_candle_ignores_high_and_low_before_telegram() -> None:
    monitor = object.__new__(NotifyingTrixAdxOutcomeMonitor)
    delivery_open = pd.Timestamp("2026-07-30T14:00:00Z")
    frame = pd.DataFrame(
        {
            "timestamp": [delivery_open - pd.Timedelta(hours=1), delivery_open],
            "open": [100.0, 100.0],
            "high": [100.5, 105.0],
            "low": [99.5, 97.0],
            "close": [100.0, 100.5],
            "ema50": [99.0, 99.0],
            "atr": [1.0, 1.0],
            "trix": [0.1, 0.1],
            "trix_signal": [0.05, 0.05],
        }
    )
    processed: list[pd.Timestamp] = []

    monitor._fetch_closed_frame = lambda signal: frame.copy()
    monitor._add_indicators = lambda value: value
    monitor._load_state = lambda signal_id: {
        "initial_stop": 98.0,
        "risk_distance": 2.0,
    }
    monitor._mark_processed = lambda signal_id, timestamp, extra=None: processed.append(timestamp)
    monitor._arm_break_even = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("delivery high must not arm break-even")
    )
    monitor._take_partial_profit = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("delivery high must not hit TP1")
    )
    monitor._close_signal = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("delivery low must not trigger the stop")
    )
    monitor.trailing_atr_multiple = 2.5

    signal = {
        "id": 700,
        "pair": "BTC/USD",
        "entry": 100.0,
        "stop_loss": 98.0,
        "telegram_sent_at": delivery_open + pd.Timedelta(minutes=2),
        "created_at": delivery_open + pd.Timedelta(minutes=1),
    }

    assert monitor.check_signal(signal) is False
    assert processed == [delivery_open]


def test_telegram_signal_explains_r_based_management() -> None:
    payload = {
        "signal_id": 501,
        "pair": "BTC/USD",
        "timeframe": "1h",
        "entry": 100.0,
        "stop_loss": 98.5,
        "entry_notional_eur": 100.0,
        "score_breakdown": {
            "risk_distance": 1.5,
            "be_trigger_price": 101.5,
            "tp1_price": 103.0,
            "adx_4h": 31.2,
            "volume_ratio": 1.3,
        },
    }

    message = format_trix_adx_signal_message(payload)

    assert "PAPER" in message
    assert "+1R" in message
    assert "+2R" in message
    assert "pareggio netto" in message
    assert "TP1 50%" in message
