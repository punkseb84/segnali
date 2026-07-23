from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from project.price_action_scanner import (
    HORIZONTAL_RETEST,
    PRICE_ACTION_RUNTIME_VERSION,
    TRENDLINE_RETEST,
    PurePriceActionScanner,
)


def scanner_shell() -> PurePriceActionScanner:
    scanner = object.__new__(PurePriceActionScanner)
    scanner.swing_window = 2
    scanner.min_trendline_touches = 3
    scanner.min_horizontal_touches = 2
    scanner.retest_window_15m = 6
    scanner.min_signal_score = 72.0
    scanner.min_net_rr = 1.15
    scanner.min_tp1_net_profit_eur = 0.15
    scanner.max_net_loss_eur = 1.0
    scanner.trade_notional_eur = 100.0
    scanner.min_notional_eur = 10.0
    scanner.logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    return scanner


def descending_break_frame() -> pd.DataFrame:
    timestamps = pd.date_range("2026-07-17 00:00", periods=36, freq="1h", tz="UTC")
    rows = []
    for index, timestamp in enumerate(timestamps):
        line = 110.0 - index * 0.20
        close = line - 1.20
        high = line - 0.70
        if index in {5, 13, 21}:
            high = line
        rows.append(
            {
                "timestamp": timestamp,
                "open": close - 0.10,
                "high": high,
                "low": close - 0.60,
                "close": close,
                "volume": 100.0,
            }
        )
    final_line = 110.0 - 35 * 0.20
    rows[-1].update(
        {
            "open": final_line - 0.15,
            "high": final_line + 0.70,
            "low": final_line - 0.30,
            "close": final_line + 0.45,
        }
    )
    return pd.DataFrame(rows)


def retest_frame(level: float = 100.0) -> pd.DataFrame:
    timestamps = pd.date_range("2026-07-20 18:00", periods=20, freq="15min", tz="UTC")
    rows = []
    for index, timestamp in enumerate(timestamps):
        close = level + 0.35 + index * 0.01
        rows.append(
            {
                "timestamp": timestamp,
                "open": close - 0.05,
                "high": close + 0.12,
                "low": close - 0.12,
                "close": close,
                "volume": 100.0,
            }
        )
    rows[-3].update(
        {
            "open": level + 0.18,
            "high": level + 0.28,
            "low": level - 0.08,
            "close": level + 0.10,
        }
    )
    rows[-1].update(
        {
            "open": level + 0.04,
            "high": level + 0.42,
            "low": level - 0.12,
            "close": level + 0.34,
        }
    )
    return pd.DataFrame(rows)


def horizontal_frame() -> pd.DataFrame:
    timestamps = pd.date_range("2026-07-15 00:00", periods=60, freq="1h", tz="UTC")
    rows = []
    for index, timestamp in enumerate(timestamps):
        close = 98.0 + index * 0.04
        high = close + 0.20
        if index in {12, 28, 44}:
            high = 100.0
        rows.append(
            {
                "timestamp": timestamp,
                "open": close - 0.05,
                "high": high,
                "low": close - 0.25,
                "close": close,
                "volume": 100.0,
            }
        )
    rows[-1]["close"] = 100.50
    rows[-1]["high"] = 100.70
    return pd.DataFrame(rows)


def test_detects_three_touch_descending_trendline_and_fresh_break() -> None:
    scanner = scanner_shell()
    line = scanner._descending_trendline(descending_break_frame())
    assert line is not None
    assert line["slope"] < 0
    assert len(line["touches"]) >= 3
    broken = scanner._trendline_break(line)
    assert broken is not None
    assert broken["close"] > broken["level"]


def test_bullish_retest_requires_price_rejection_above_level() -> None:
    scanner = scanner_shell()
    frame = retest_frame()
    trigger = scanner._retest_trigger(
        frame,
        level=100.0,
        break_time=None,
        tolerance=0.15,
    )
    assert trigger is not None
    assert trigger["entry"] > 100.0
    assert trigger["structural_low"] < 100.0
    assert trigger["retest_count"] >= 1


def test_detects_repeated_horizontal_resistance_below_current_price() -> None:
    scanner = scanner_shell()
    level = scanner._horizontal_level(horizontal_frame())
    assert level is not None
    assert level["touches"] >= 2
    assert level["level"] == pytest.approx(100.0, abs=0.30)


def test_signal_metadata_declares_price_action_only(monkeypatch) -> None:
    scanner = scanner_shell()
    frame_15m = retest_frame()
    frame_1h = horizontal_frame()
    scanner.calculate_net_economics = lambda entry, stop, target: {
        "tradable": True,
        "quantity": 1.0,
        "entry_notional_eur": 100.0,
        "tp_notional_eur": 101.5,
        "sl_notional_eur": 99.0,
        "gross_profit_tp1_eur": 1.5,
        "gross_loss_sl_eur": 1.0,
        "estimated_buy_fee_eur": 0.10,
        "estimated_sell_fee_eur": 0.10,
        "estimated_sell_fee_sl_eur": 0.10,
        "estimated_spread_cost_eur": 0.02,
        "estimated_slippage_cost_eur": 0.0,
        "net_profit_tp1_eur": 1.28,
        "net_loss_sl_eur": 1.22,
        "gross_rr": 1.50,
        "net_rr": 1.20,
    }
    monkeypatch.setattr(scanner, "_next_resistance", lambda *args, **kwargs: None)
    signal, reason, diagnostic = scanner._build_signal(
        pair="BTC/USD",
        frame_15m=frame_15m,
        frame_1h=frame_1h,
        strategy=TRENDLINE_RETEST,
        regime="MIXED_STRUCTURE",
        level=100.0,
        trigger={
            "entry": 100.34,
            "structural_low": 99.55,
            "trigger_time": frame_15m.iloc[-1]["timestamp"],
            "retest_count": 1,
        },
        score=82.0,
        reasons=["trendline valida", "retest confermato"],
        metadata={"trendline_touch_count": 3},
    )
    assert reason == "OK"
    assert diagnostic["qualified"] is True
    assert signal is not None
    assert signal.strategy == TRENDLINE_RETEST
    assert signal.score_breakdown["runtime_version"] == PRICE_ACTION_RUNTIME_VERSION
    assert signal.score_breakdown["price_action_only"] is True
    assert signal.score_breakdown["oscillators_used"] is False
    assert signal.score_breakdown["dynamic_averages_used"] is False


def test_strategy_names_are_only_price_action_setups() -> None:
    assert TRENDLINE_RETEST.startswith("Price Action")
    assert HORIZONTAL_RETEST.startswith("Price Action")
