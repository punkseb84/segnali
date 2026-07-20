from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from project.daily_paper_formatters import (
    format_daily_paper_outcome_message,
    format_daily_paper_signal_message,
)
from project.daily_paper_position_monitor import DailyPaperPositionMonitor
from project.daily_paper_scanner import (
    LIVE_EXECUTION_MODE,
    PAPER_EXECUTION_MODE,
    PAPER_RUNTIME_VERSION,
    DailyPaperSignalScanner,
)
from project.strategy_engine.service import GeneratedSignal


def make_signal(*, score: float = 70.0, net_rr: float = 1.16, net_profit: float = 0.30) -> GeneratedSignal:
    now = datetime.now(timezone.utc)
    return GeneratedSignal(
        strategy="Breakout 20",
        pair="AVAX/USD",
        timeframe="15m",
        regime="RANGE",
        entry=25.0,
        stop_loss=24.7,
        take_profit=25.6,
        technical_take_profit=25.6,
        effective_take_profit=25.6,
        signal_time=now,
        reference_candle_time=now,
        entry_timing="ENTER_ON_SIGNAL_RECEIPT",
        quantity=4.0,
        entry_notional_eur=100.0,
        tp_notional_eur=102.4,
        sl_notional_eur=98.8,
        gross_profit_tp1_eur=2.4,
        gross_loss_sl_eur=1.2,
        estimated_buy_fee_eur=0.1,
        estimated_sell_fee_eur=0.1,
        estimated_sell_fee_sl_eur=0.1,
        estimated_spread_cost_eur=0.05,
        estimated_slippage_cost_eur=0.05,
        net_profit_tp1_eur=net_profit,
        net_loss_sl_eur=0.90,
        gross_rr=2.0,
        net_rr=net_rr,
        stop_distance=0.3,
        stop_pct=1.2,
        atr=0.2,
        stop_atr_ratio=1.5,
        tp_atr_ratio=3.0,
        historical_sample_size=0,
        historical_win_rate=None,
        historical_expected_value_eur=None,
        historical_mfe_percentile=0.0,
        historical_mae_percentile=0.0,
        probability_confidence="FORWARD_BUILDING",
        validation_status="FIXED_RULES_PASSED",
        validation_reason="test",
        signal_class="B",
        score=score,
        probability=None,
        reasons=["breakout confermato"],
        score_breakdown={},
    )


class QueryClient:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def fetch_all(self, query, params=()):
        self.calls.append((query, params))
        return self.rows


def test_daily_state_counts_only_delivered_paper_on_rome_calendar_day() -> None:
    now = datetime.now(timezone.utc)
    scanner = object.__new__(DailyPaperSignalScanner)
    scanner.client = QueryClient([(2, now)])
    state = scanner.paper_daily_state()
    query, params = scanner.client.calls[0]
    assert state == {"count": 2, "last_sent": now}
    assert "telegram_sent_at IS NOT NULL" in query
    assert "Europe/Rome" in query
    assert params == (PAPER_RUNTIME_VERSION, PAPER_EXECUTION_MODE)


def test_relaxed_paper_candidate_is_allowed_but_weak_candidate_is_not() -> None:
    scanner = object.__new__(DailyPaperSignalScanner)
    scanner.paper_min_score = 64.0
    scanner.paper_min_net_rr = 1.05
    scanner.paper_min_net_profit_eur = 0.20
    scanner.max_net_loss_eur = 1.0
    assert scanner.paper_candidate_allowed(make_signal()) is True
    assert scanner.paper_candidate_allowed(make_signal(net_rr=1.01)) is False
    assert scanner.paper_candidate_allowed(make_signal(net_profit=0.10)) is False


def test_publish_paper_stamps_mode_and_emits_signal() -> None:
    scanner = object.__new__(DailyPaperSignalScanner)
    scanner.logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    captured = {}

    def save_signal(signal):
        captured["saved"] = signal
        return 801

    def publish_signal_event(signal):
        captured["published"] = signal

    scanner.save_signal = save_signal
    scanner.publish_signal_event = publish_signal_event
    decision = {
        "stats": {"completed": 0, "profit_factor": 0.0, "expectancy_r": 0.0},
        "blockers": ["campione shadow 0/30"],
    }
    result = scanner._publish_paper(make_signal(), decision)
    breakdown = captured["saved"].score_breakdown
    assert result.signal_id == 801
    assert breakdown["runtime_version"] == PAPER_RUNTIME_VERSION
    assert breakdown["paper_runtime_version"] == PAPER_RUNTIME_VERSION
    assert breakdown["execution_mode"] == PAPER_EXECUTION_MODE
    assert breakdown["paper_only"] is True
    assert captured["published"].signal_id == 801


def test_paper_formatter_is_unmistakable() -> None:
    signal = make_signal()
    payload = {
        **signal.__dict__,
        "signal_id": 801,
        "score_breakdown": {"execution_mode": PAPER_EXECUTION_MODE},
    }
    message = format_daily_paper_signal_message(payload)
    assert "SEGNALE LONG · PAPER" in message
    assert "PAPER DAILY" in message
    assert "Non usare denaro reale" in message


def test_live_formatter_keeps_validated_label_separate() -> None:
    signal = make_signal()
    payload = {
        **signal.__dict__,
        "signal_id": 802,
        "score_breakdown": {"execution_mode": LIVE_EXECUTION_MODE},
    }
    message = format_daily_paper_signal_message(payload)
    assert "EDGE VALIDATO" in message
    assert "SEGNALE LONG · PAPER" not in message


class MonitorClient:
    def fetch_all(self, query, params=()):
        return [
            (
                801,
                "Breakout 20",
                "AVAX/USD",
                "15m",
                "RANGE",
                25.0,
                24.7,
                25.6,
                70.0,
                None,
                "B",
                0.30,
                0.90,
                1.16,
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
                "kraken",
                PAPER_EXECUTION_MODE,
            )
        ]


def test_position_monitor_preserves_paper_mode_for_outcome_reply() -> None:
    monitor = object.__new__(DailyPaperPositionMonitor)
    monitor.postgres = MonitorClient()
    monitor.max_signals_per_cycle = 50
    signals = monitor.fetch_open_signals()
    assert len(signals) == 1
    assert signals[0]["execution_mode"] == PAPER_EXECUTION_MODE
    assert signals[0]["exchange"] == "kraken"


def test_paper_outcome_is_clearly_labelled() -> None:
    message = format_daily_paper_outcome_message(
        {
            "outcome": "STOP_LOSS",
            "signal_id": 801,
            "pair": "AVAX/USD",
            "timeframe": "15m",
            "strategy": "Breakout 20",
            "exchange": "kraken",
            "entry": 25.0,
            "outcome_price": 24.7,
            "net_loss_sl_eur": 0.90,
            "net_rr": 1.16,
            "execution_mode": PAPER_EXECUTION_MODE,
            "closed_at": "2026-07-20 12:00:00+00:00",
            "outcome_resolution": "FULL_CANDLE_RANGE",
        }
    )
    assert "ESITO PAPER" in message
    assert "NESSUNA OPERAZIONE REALE" in message


def test_paper_performance_query_is_isolated_from_live() -> None:
    scanner = object.__new__(DailyPaperSignalScanner)
    scanner.client = QueryClient([])
    scanner.shadow_recent_window = 10
    result = scanner.fetch_paper_performance()
    query, params = scanner.client.calls[0]
    assert result["completed"] == 0
    assert "paper_runtime_version" in query
    assert "outcome_ambiguous" in query
    assert params == (PAPER_RUNTIME_VERSION, PAPER_EXECUTION_MODE)
