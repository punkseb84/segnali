from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from project.audited_formatters import format_audited_outcome_message
from project.audited_position_monitor import AuditedPositionMonitor
from project.database.migrations import SIGNAL_OUTCOME_AUDIT_MIGRATION
from project.strategy_engine.service import GeneratedSignal
from project.validated_live_scanner import (
    RELATIVE_STRENGTH_MIN_NET_RR,
    RELATIVE_STRENGTH_STRATEGY,
    ValidatedLiveStrategyScanner,
)
from project.validated_live_scanner_v513 import (
    RUNTIME_VERSION,
    ValidatedLiveStrategyScannerV513,
)
from project.version_scoped_harmonic_scanner import VersionScopedHarmonicLiveStrategyScanner


def market_frames(*, latest_macd: float = 0.20, latest_close: float = 100.50):
    frame_15m = pd.DataFrame(
        [
            {"close": 99.90, "low": 99.70, "ema20": 100.00, "ema50": 99.00, "atr": 1.00, "rsi": 52.0, "macd_hist": 0.05, "volume_ratio": 0.90},
            {"close": 100.00, "low": 99.85, "ema20": 100.00, "ema50": 99.10, "atr": 1.00, "rsi": 53.0, "macd_hist": 0.10, "volume_ratio": 0.95},
            {"close": 100.20, "low": 99.95, "ema20": 100.00, "ema50": 99.20, "atr": 1.00, "rsi": 54.0, "macd_hist": 0.15, "volume_ratio": 1.00},
            {"close": latest_close, "low": 100.05, "ema20": 100.00, "ema50": 99.30, "atr": 1.00, "rsi": 55.0, "macd_hist": latest_macd, "volume_ratio": 1.10},
        ]
    )
    frame_1h = pd.DataFrame(
        [
            {"close": 100.5, "ema20": 100.1, "ema50": 99.8, "ema200": 100.0},
            {"close": 101.0, "ema20": 100.5, "ema50": 100.0, "ema200": 100.0},
        ]
    )
    return frame_15m, frame_1h


def good_relative_snapshot() -> dict[str, float]:
    return {
        "rank_percentile": 0.85,
        "relative_24h": 0.012,
        "return_4h": 0.020,
        "median_return_4h": 0.004,
        "relative_1h": 0.004,
        "relative_1h_acceleration": 0.001,
        "btc_return_1h": -0.001,
    }


def fake_signal(net_rr: float = 1.13) -> GeneratedSignal:
    now = datetime.now(timezone.utc)
    return GeneratedSignal(
        strategy=RELATIVE_STRENGTH_STRATEGY,
        pair="XLM/USD",
        timeframe="15m",
        regime="TREND_UP",
        entry=0.19017,
        stop_loss=0.18900,
        take_profit=0.19249,
        technical_take_profit=0.19249,
        effective_take_profit=0.19249,
        signal_time=now,
        reference_candle_time=now,
        entry_timing="ENTER_ON_SIGNAL_RECEIPT",
        quantity=500.0,
        entry_notional_eur=95.0,
        tp_notional_eur=96.0,
        sl_notional_eur=94.5,
        gross_profit_tp1_eur=1.16,
        gross_loss_sl_eur=0.59,
        estimated_buy_fee_eur=0.1,
        estimated_sell_fee_eur=0.1,
        estimated_sell_fee_sl_eur=0.1,
        estimated_spread_cost_eur=0.0,
        estimated_slippage_cost_eur=0.0,
        net_profit_tp1_eur=0.97,
        net_loss_sl_eur=0.86,
        gross_rr=1.98,
        net_rr=net_rr,
        stop_distance=0.00117,
        stop_pct=0.615,
        atr=0.00080,
        stop_atr_ratio=1.46,
        tp_atr_ratio=2.90,
        historical_sample_size=0,
        historical_win_rate=None,
        historical_expected_value_eur=None,
        historical_mfe_percentile=0.0,
        historical_mae_percentile=0.0,
        probability_confidence="FORWARD_BUILDING",
        validation_status="FIXED_RULES_PASSED",
        validation_reason="test",
        signal_class="B",
        score=92.0,
        probability=None,
        reasons=[],
        score_breakdown={},
    )


def test_relative_strength_requires_complete_rsi_macd_confirmation() -> None:
    f15, f1h = market_frames(latest_macd=0.10)
    result = ValidatedLiveStrategyScanner.relative_strength_assessment(
        f15, f1h, "TREND_UP", good_relative_snapshot()
    )
    assert result["score"] == 92.0
    assert result["qualified"] is False
    assert "MACD 15m non in miglioramento" in result["blockers"]
    assert "leadership relativa valida con almeno due conferme di breve periodo" not in result["reasons"]


def test_relative_strength_rejects_extended_entry() -> None:
    f15, f1h = market_frames(latest_close=101.00)
    result = ValidatedLiveStrategyScanner.relative_strength_assessment(
        f15, f1h, "TREND_UP", good_relative_snapshot()
    )
    assert result["qualified"] is False
    assert "ingresso oltre 0,80 ATR sopra EMA20" in result["blockers"]


def test_relative_strength_accepts_only_complete_pullback_restart() -> None:
    f15, f1h = market_frames()
    result = ValidatedLiveStrategyScanner.relative_strength_assessment(
        f15, f1h, "TREND_UP", good_relative_snapshot()
    )
    assert result["score"] == 100.0
    assert result["qualified"] is True
    assert result["target_rr"] == 2.0


def test_relative_strength_specific_net_rr_rejects_signal_222(monkeypatch) -> None:
    def parent_builder(self, pair, frame_15m, regime, relative, assessment):
        return fake_signal(1.13), "OK", {"blockers": []}

    monkeypatch.setattr(
        VersionScopedHarmonicLiveStrategyScanner,
        "_build_signal_for_assessment",
        parent_builder,
    )
    scanner = object.__new__(ValidatedLiveStrategyScanner)
    signal, reason, diagnostic = scanner._build_signal_for_assessment(
        "XLM/USD", pd.DataFrame(), "TREND_UP", {}, {"strategy": RELATIVE_STRENGTH_STRATEGY}
    )
    assert signal is None
    assert reason == "RELATIVE_STRENGTH_NET_RR_TOO_LOW"
    assert str(RELATIVE_STRENGTH_MIN_NET_RR) in diagnostic["blockers"][0]


class FakePostgres:
    def __init__(self):
        self.fetch_calls = []
        self.execute_calls = []

    def fetch_all(self, query, params=()):
        self.fetch_calls.append((query, params))
        if "FROM market_data.ohlc" in query:
            return [
                (
                    datetime(2026, 7, 19, 17, 0, tzinfo=timezone.utc),
                    0.19030,
                    0.19260,
                    0.18890,
                    0.19000,
                )
            ]
        return []

    def execute(self, query, params=()):
        self.execute_calls.append((query, params))


def test_monitor_filters_exchange_and_persists_actual_ohlc() -> None:
    postgres = FakePostgres()
    monitor = AuditedPositionMonitor(postgres, ambiguous_candle_mode="conservative")
    signal = {
        "id": 222,
        "strategy": RELATIVE_STRENGTH_STRATEGY,
        "pair": "XLM/USD",
        "timeframe": "15m",
        "regime": "TREND_UP",
        "entry": 0.19017,
        "stop_loss": 0.18900,
        "take_profit": 0.19249,
        "score": 92.0,
        "probability": None,
        "signal_class": "B",
        "net_profit_tp1_eur": 0.97,
        "net_loss_sl_eur": 0.86,
        "net_rr": 1.13,
        "reference_candle_time": datetime(2026, 7, 19, 16, 30, tzinfo=timezone.utc),
        "created_at": datetime(2026, 7, 19, 16, 35, tzinfo=timezone.utc),
        "exchange": "kraken",
    }
    assert monitor.check_signal(signal) is True
    candle_query, candle_params = postgres.fetch_calls[0]
    assert "WHERE exchange = %s" in candle_query
    assert candle_params[0] == "kraken"
    audit_query, audit_params = postgres.execute_calls[0]
    assert "outcome_high_price" in audit_query
    assert "outcome_low_price" in audit_query
    assert audit_params[2] == pytest.approx(0.19260)
    assert audit_params[3] == pytest.approx(0.18890)
    assert audit_params[6] is True


def test_audited_formatter_displays_real_low_and_ambiguity() -> None:
    message = format_audited_outcome_message(
        {
            "outcome": "STOP_LOSS",
            "signal_id": 222,
            "pair": "XLM/USD",
            "timeframe": "15m",
            "strategy": RELATIVE_STRENGTH_STRATEGY,
            "exchange": "kraken",
            "entry": 0.19017,
            "outcome_price": 0.18900,
            "outcome_open_price": 0.19030,
            "outcome_high_price": 0.19260,
            "outcome_low_price": 0.18890,
            "close_price": 0.19000,
            "net_loss_sl_eur": 0.86,
            "net_rr": 1.13,
            "ambiguous": True,
            "closed_at": "2026-07-19 17:00:00+00:00",
            "outcome_resolution": "FULL_CANDLE_RANGE",
        }
    )
    assert "Minimo reale" in message
    assert "0.18890" in message
    assert "Candela ambigua" in message
    assert "SÌ" in message
    assert "Exchange: <b>kraken</b>" in message


def test_v513_performance_excludes_ambiguous_outcomes() -> None:
    class Client:
        def __init__(self):
            self.query = ""
            self.params = ()

        def fetch_all(self, query, params=()):
            self.query = query
            self.params = params
            return []

    scanner = object.__new__(ValidatedLiveStrategyScannerV513)
    scanner.client = Client()
    result = scanner.fetch_forward_performance()
    assert result
    assert "COALESCE(outcome_ambiguous, FALSE) = FALSE" in scanner.client.query
    assert RUNTIME_VERSION in scanner.client.params


def test_migration_adds_exchange_and_outcome_ohlc() -> None:
    assert "ADD COLUMN IF NOT EXISTS exchange" in SIGNAL_OUTCOME_AUDIT_MIGRATION
    assert "outcome_open_price" in SIGNAL_OUTCOME_AUDIT_MIGRATION
    assert "outcome_high_price" in SIGNAL_OUTCOME_AUDIT_MIGRATION
    assert "outcome_low_price" in SIGNAL_OUTCOME_AUDIT_MIGRATION
