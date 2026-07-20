from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest

from project.daily_paper_scanner import DailyPaperSignalScanner
from project.structure_aware_daily_scanner import (
    STRUCTURE_RUNTIME_VERSION,
    StructureAwareDailyPaperScanner,
)
from tests.project.test_daily_paper_v61 import make_signal


def near_frame() -> pd.DataFrame:
    rows = []
    for index in range(32):
        close = 1.965 + index * 0.0004
        high = close + 0.004
        low = close - 0.004
        if index in {8, 15, 23}:
            high = 2.000
            close = 1.994
            low = 1.984
        rows.append(
            {
                "timestamp": pd.Timestamp("2026-07-20 10:00", tz="UTC")
                + pd.Timedelta(minutes=15 * index),
                "open": close - 0.001,
                "high": high,
                "low": low,
                "close": close,
                "volume": 100.0,
                "atr": 0.012,
            }
        )
    rows[-1].update(
        {
            "open": 1.981,
            "high": 1.986,
            "low": 1.977,
            "close": 1.984,
            "atr": 0.012,
        }
    )
    return pd.DataFrame(rows)


def near_signal(*, target: float = 2.019):
    return replace(
        make_signal(score=94.0, net_rr=1.08, net_profit=1.06),
        strategy="Trend Pullback",
        pair="NEAR/USD",
        regime="TREND_UP",
        entry=1.984,
        stop_loss=1.963,
        take_profit=target,
        technical_take_profit=target,
        effective_take_profit=target,
        stop_distance=0.021,
        stop_pct=0.021 / 1.984 * 100.0,
        atr=0.012,
        stop_atr_ratio=1.75,
        tp_atr_ratio=(target - 1.984) / 0.012,
        net_loss_sl_eur=0.98,
        reasons=[
            "trend 1h favorevole",
            "pullback verso media dinamica",
            "recupero del momentum 15m",
        ],
    )


def scanner_shell() -> StructureAwareDailyPaperScanner:
    scanner = object.__new__(StructureAwareDailyPaperScanner)
    scanner.structure_min_net_rr = 1.20
    scanner.structure_min_net_profit_eur = 0.20
    scanner.resistance_lookback_15m = 120
    scanner.resistance_lookback_1h = 120
    scanner.resistance_buffer_atr = 0.10
    scanner._structure_hourly_frames = {}
    scanner.logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    return scanner


def test_near_231_is_rejected_before_resistance(monkeypatch) -> None:
    signal = near_signal()

    def parent_builder(self, pair, frame_15m, regime, relative, assessment):
        return signal, "OK", {"pair": pair, "strategy": signal.strategy, "blockers": []}

    monkeypatch.setattr(
        DailyPaperSignalScanner,
        "_build_signal_for_assessment",
        parent_builder,
    )
    scanner = scanner_shell()
    scanner.calculate_net_economics = lambda entry, stop, target: {
        "tradable": True,
        "net_rr": 0.38,
        "net_profit_tp1_eur": 0.37,
    }

    result, reason, diagnostic = scanner._build_signal_for_assessment(
        "NEAR/USD", near_frame(), "TREND_UP", {}, {"strategy": "Trend Pullback"}
    )

    assert result is None
    assert reason == "INSUFFICIENT_ROOM_TO_FIRST_RESISTANCE"
    assert diagnostic["first_resistance"] == pytest.approx(2.0, abs=0.003)
    assert diagnostic["safe_target_below_resistance"] < 2.0
    assert diagnostic["net_rr_to_first_resistance"] == pytest.approx(0.38)
    assert "spazio insufficiente" in diagnostic["blockers"][0]


def test_target_already_below_resistance_is_kept(monkeypatch) -> None:
    signal = near_signal(target=1.995)

    def parent_builder(self, pair, frame_15m, regime, relative, assessment):
        return signal, "OK", {"pair": pair, "strategy": signal.strategy, "blockers": []}

    monkeypatch.setattr(
        DailyPaperSignalScanner,
        "_build_signal_for_assessment",
        parent_builder,
    )
    scanner = scanner_shell()

    result, reason, _ = scanner._build_signal_for_assessment(
        "NEAR/USD", near_frame(), "TREND_UP", {}, {"strategy": "Trend Pullback"}
    )

    assert result is not None
    assert reason == "OK"
    assert result.take_profit == pytest.approx(1.995)
    assert result.score_breakdown["structure_runtime_version"] == STRUCTURE_RUNTIME_VERSION
    assert result.score_breakdown["first_resistance_found"] is True


def test_target_is_capped_when_room_remains_profitable(monkeypatch) -> None:
    signal = near_signal()

    def parent_builder(self, pair, frame_15m, regime, relative, assessment):
        return signal, "OK", {"pair": pair, "strategy": signal.strategy, "blockers": []}

    monkeypatch.setattr(
        DailyPaperSignalScanner,
        "_build_signal_for_assessment",
        parent_builder,
    )
    scanner = scanner_shell()
    scanner.calculate_net_economics = lambda entry, stop, target: {
        "tradable": True,
        "quantity": 50.0,
        "entry_notional_eur": 99.2,
        "tp_notional_eur": 99.94,
        "sl_notional_eur": 98.15,
        "gross_profit_tp1_eur": 0.74,
        "gross_loss_sl_eur": 1.05,
        "estimated_buy_fee_eur": 0.10,
        "estimated_sell_fee_eur": 0.10,
        "estimated_sell_fee_sl_eur": 0.10,
        "estimated_spread_cost_eur": 0.05,
        "estimated_slippage_cost_eur": 0.0,
        "net_profit_tp1_eur": 0.55,
        "net_loss_sl_eur": 0.40,
        "gross_rr": 1.40,
        "net_rr": 1.30,
    }

    result, reason, _ = scanner._build_signal_for_assessment(
        "NEAR/USD", near_frame(), "TREND_UP", {}, {"strategy": "Trend Pullback"}
    )

    assert result is not None
    assert reason == "OK"
    assert result.take_profit < 2.0
    assert result.take_profit < signal.take_profit
    assert result.net_rr == pytest.approx(1.30)
    assert result.score_breakdown["target_capped_by_resistance"] is True
    assert "target limitato" in result.reasons[0]


def test_round_number_detection_requires_proximity_or_market_reaction() -> None:
    scanner = scanner_shell()
    frame = near_frame()
    resistance = scanner.first_resistance("NEAR/USD", frame, 1.984, 0.012)
    assert resistance is not None
    assert resistance["level"] == pytest.approx(2.0, abs=0.003)
    assert resistance["touches"] >= 2
