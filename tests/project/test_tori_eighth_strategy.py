from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest

from project.daily_paper_formatters import format_daily_paper_signal_message
from project.daily_paper_scanner import PAPER_EXECUTION_MODE
from project.tori_trendline_scanner import (
    EIGHT_LONG_STRATEGIES,
    TORI_TRENDLINE_STRATEGY,
    ToriTrendlineDailyPaperScanner,
)
from project.tori_trendline_v11 import ToriTrendlineDailyPaperScannerV11
from tests.project.test_daily_paper_v61 import make_signal


def scanner_shell() -> ToriTrendlineDailyPaperScannerV11:
    scanner = object.__new__(ToriTrendlineDailyPaperScannerV11)
    scanner.tori_min_touch_separation_bars = 3
    scanner.tori_touch_tolerance_atr = 0.28
    scanner.tori_break_buffer_atr = 0.10
    scanner.tori_max_extension_atr = 0.80
    scanner.tori_min_gross_rr = 2.0
    scanner._tori_assessment = None
    scanner.logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    return scanner


def one_hour_frame(start: str = "2026-07-16 12:00", periods: int = 120) -> pd.DataFrame:
    timestamps = pd.date_range(start, periods=periods, freq="1h", tz="UTC")
    rows = []
    for index, timestamp in enumerate(timestamps):
        close = 100.0 + index * 0.01
        rows.append(
            {
                "timestamp": timestamp,
                "open": close - 0.1,
                "high": close + 0.4,
                "low": close - 0.4,
                "close": close,
                "volume": 100.0,
            }
        )
    return pd.DataFrame(rows)


def four_hour_break_frame() -> pd.DataFrame:
    timestamps = pd.date_range("2026-07-16 00:00", periods=30, freq="4h", tz="UTC")
    rows = []
    for index, timestamp in enumerate(timestamps):
        line = 110.0 - 0.30 * index
        close = line - 1.50
        high = line - 1.00
        if index in {4, 10, 16}:
            high = line
        rows.append(
            {
                "timestamp": timestamp,
                "open": close - 0.10,
                "high": high,
                "low": close - 0.80,
                "close": close,
                "volume": 100.0,
            }
        )
    # Last closed candle breaks above the descending action line.
    final_line = 110.0 - 0.30 * 29
    rows[-1].update(
        {
            "open": final_line + 0.05,
            "high": final_line + 1.00,
            "low": final_line - 0.20,
            "close": final_line + 0.60,
        }
    )
    return pd.DataFrame(rows)


def frame_15m_at_break_end(*, stale_minutes: int = 0) -> pd.DataFrame:
    # Last 4h candle in four_hour_break_frame starts at 20:00 and ends at 00:00.
    last_end = pd.Timestamp("2026-07-21 00:00", tz="UTC") + pd.Timedelta(minutes=stale_minutes)
    timestamps = pd.date_range(end=last_end - pd.Timedelta(minutes=15), periods=20, freq="15min")
    rows = []
    for index, timestamp in enumerate(timestamps):
        close = 101.5 + index * 0.01
        rows.append(
            {
                "timestamp": timestamp,
                "open": close - 0.02,
                "high": close + 0.05,
                "low": close - 0.05,
                "close": close,
                "volume": 100.0,
            }
        )
    return pd.DataFrame(rows)


def test_eighth_strategy_is_registered_once() -> None:
    assert len(EIGHT_LONG_STRATEGIES) == 8
    assert len(set(EIGHT_LONG_STRATEGIES)) == 8
    assert TORI_TRENDLINE_STRATEGY in EIGHT_LONG_STRATEGIES


def test_aggregate_4h_keeps_only_complete_groups() -> None:
    frame = one_hour_frame(periods=10)
    result = ToriTrendlineDailyPaperScanner.aggregate_closed_4h(frame)
    assert len(result) == 2
    assert result.iloc[0]["open"] == pytest.approx(frame.iloc[0]["open"])
    assert result.iloc[0]["close"] == pytest.approx(frame.iloc[3]["close"])
    assert result.iloc[0]["volume"] == pytest.approx(400.0)


def test_detects_descending_action_line_with_three_separated_touches() -> None:
    scanner = scanner_shell()
    frame = four_hour_break_frame()
    action = scanner._descending_action_line(frame)
    assert action is not None
    assert action["slope"] < 0
    assert len(action["touches"]) >= 3
    assert all(
        right - left >= scanner.tori_min_touch_separation_bars
        for left, right in zip(action["touches"], action["touches"][1:])
    )


def test_assessment_requires_fresh_confirmed_4h_break(monkeypatch) -> None:
    scanner = scanner_shell()
    frame4h = four_hour_break_frame()
    # Align the last 4h start to 20:00 UTC on 20 July, ending at midnight.
    frame4h["timestamp"] = pd.date_range(
        "2026-07-16 04:00", periods=30, freq="4h", tz="UTC"
    )
    monkeypatch.setattr(scanner, "aggregate_closed_4h", lambda frame: frame4h.copy())
    monkeypatch.setattr(
        scanner,
        "_descending_action_line",
        lambda frame: {
            "slope": -0.10,
            "intercept": 104.0,
            "touches": [4, 10, 16],
            "quality": 42,
        },
    )
    current_line = 104.0 - 0.10 * 29
    frame4h.loc[28, "close"] = current_line - 0.15
    frame4h.loc[29, ["open", "close", "high", "low"]] = [
        current_line + 0.02,
        current_line + 0.30,
        current_line + 0.50,
        current_line - 0.10,
    ]
    monkeypatch.setattr(
        scanner,
        "_ascending_safety_line",
        lambda frame, start_index: {"current_value": current_line - 1.0},
    )

    result = scanner.tori_trendline_assessment(
        frame_15m_at_break_end(), one_hour_frame(), "TREND_UP"
    )
    assert result["qualified"] is True
    assert result["tori_touch_count"] == 3
    assert result["tori_break_age_minutes"] == pytest.approx(0.0)
    assert result["tori_safety_line"] < result["tori_action_line"] + 1.0


def test_stale_4h_break_is_rejected(monkeypatch) -> None:
    scanner = scanner_shell()
    frame4h = four_hour_break_frame()
    frame4h["timestamp"] = pd.date_range(
        "2026-07-16 04:00", periods=30, freq="4h", tz="UTC"
    )
    monkeypatch.setattr(scanner, "aggregate_closed_4h", lambda frame: frame4h.copy())
    monkeypatch.setattr(
        ToriTrendlineDailyPaperScanner,
        "tori_trendline_assessment",
        lambda self, frame_15m, frame_1h, regime: {
            "strategy": TORI_TRENDLINE_STRATEGY,
            "score": 90.0,
            "qualified": True,
            "reasons": [],
            "blockers": [],
            "target_rr": 2.0,
        },
    )
    result = scanner.tori_trendline_assessment(
        frame_15m_at_break_end(stale_minutes=180), one_hour_frame(), "TREND_UP"
    )
    assert result["qualified"] is False
    assert any("non recente" in blocker for blocker in result["blockers"])


def test_safety_line_stop_is_not_artificially_capped_to_2_5_percent() -> None:
    scanner = scanner_shell()
    scanner._tori_assessment = {
        "tori_safety_line": 96.50,
        "tori_atr_4h": 1.50,
    }
    distance = scanner.stop_distance_for_strategy(
        TORI_TRENDLINE_STRATEGY, pd.DataFrame(), 100.0, 0.40
    )
    assert distance == pytest.approx(3.65)
    assert distance > 2.50

    scanner._tori_assessment = {
        "tori_safety_line": 95.0,
        "tori_atr_4h": 1.50,
    }
    assert scanner.stop_distance_for_strategy(
        TORI_TRENDLINE_STRATEGY, pd.DataFrame(), 100.0, 0.40
    ) == 0.0


def test_resistance_cap_must_preserve_full_two_r(monkeypatch) -> None:
    scanner = scanner_shell()
    candidate = replace(
        make_signal(score=90.0, net_rr=1.30, net_profit=0.50),
        strategy=TORI_TRENDLINE_STRATEGY,
        gross_rr=1.70,
        entry=100.0,
        stop_loss=98.0,
        take_profit=103.4,
        technical_take_profit=104.0,
        effective_take_profit=103.4,
    )
    monkeypatch.setattr(
        ToriTrendlineDailyPaperScanner,
        "_build_signal_for_assessment",
        lambda self, pair, frame_15m, regime, relative, assessment: (
            candidate,
            "OK",
            {"blockers": []},
        ),
    )
    result, reason, diagnostic = scanner._build_signal_for_assessment(
        "BTC/USD",
        pd.DataFrame(),
        "TREND_UP",
        {},
        {
            "strategy": TORI_TRENDLINE_STRATEGY,
            "tori_action_line": 99.5,
            "tori_atr_4h": 2.0,
            "tori_break_age_minutes": 0.0,
        },
    )
    assert result is None
    assert reason == "TORI_INSUFFICIENT_2R_ROOM"
    assert diagnostic["gross_rr_after_structure"] == pytest.approx(1.70)


def test_formatter_shows_4h_setup_and_15m_monitor() -> None:
    signal = replace(
        make_signal(),
        strategy=TORI_TRENDLINE_STRATEGY,
        pair="BTC/USD",
        timeframe="15m",
        score_breakdown={
            "execution_mode": PAPER_EXECUTION_MODE,
            "execution_timeframe": "4h",
        },
    )
    message = format_daily_paper_signal_message(
        {**signal.__dict__, "signal_id": 999}
    )
    assert "setup <b>4h</b> · monitor 15m" in message
    assert "ingresso confermato su candela 4h chiusa" in message
