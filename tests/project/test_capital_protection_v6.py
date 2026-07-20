from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from project.capital_protection_migration import CAPITAL_PROTECTION_SCHEMA
from project.capital_protection_scanner import (
    RUNTIME_VERSION,
    CapitalProtectionScanner,
)
from project.strategy_engine.service import GeneratedSignal


def make_signal() -> GeneratedSignal:
    now = datetime.now(timezone.utc)
    return GeneratedSignal(
        strategy="Trend Pullback",
        pair="BTC/USD",
        timeframe="15m",
        regime="TREND_UP",
        entry=100.0,
        stop_loss=98.0,
        take_profit=104.0,
        technical_take_profit=104.0,
        effective_take_profit=104.0,
        signal_time=now,
        reference_candle_time=now,
        entry_timing="ENTER_ON_SIGNAL_RECEIPT",
        quantity=1.0,
        entry_notional_eur=100.0,
        tp_notional_eur=104.0,
        sl_notional_eur=98.0,
        gross_profit_tp1_eur=4.0,
        gross_loss_sl_eur=2.0,
        estimated_buy_fee_eur=0.1,
        estimated_sell_fee_eur=0.1,
        estimated_sell_fee_sl_eur=0.1,
        estimated_spread_cost_eur=0.0,
        estimated_slippage_cost_eur=0.0,
        net_profit_tp1_eur=3.8,
        net_loss_sl_eur=2.2,
        gross_rr=2.0,
        net_rr=1.73,
        stop_distance=2.0,
        stop_pct=2.0,
        atr=1.2,
        stop_atr_ratio=1.67,
        tp_atr_ratio=3.33,
        historical_sample_size=0,
        historical_win_rate=None,
        historical_expected_value_eur=None,
        historical_mfe_percentile=0.0,
        historical_mae_percentile=0.0,
        probability_confidence="FORWARD_BUILDING",
        validation_status="FIXED_RULES_PASSED",
        validation_reason="test",
        signal_class="B",
        score=90.0,
        probability=None,
        reasons=["valid setup"],
        score_breakdown={"runtime_version": RUNTIME_VERSION},
    )


def rows_all_stops(count: int = 30):
    now = datetime.now(timezone.utc)
    return [
        ("BTC/USD", "STOP_LOSS", 0.0, 1.0, 1.60, now + timedelta(minutes=i))
        for i in range(count)
    ]


def rows_positive(count: int = 30):
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(count):
        if i % 2 == 0:
            rows.append(("BTC/USD", "TARGET_HIT", 2.0, 0.0, 2.0, now + timedelta(minutes=i)))
        else:
            rows.append(("BTC/USD", "STOP_LOSS", 0.0, 1.0, 2.0, now + timedelta(minutes=i)))
    return rows


def build_admission_scanner(rows):
    scanner = object.__new__(CapitalProtectionScanner)
    scanner.shadow_min_samples = 30
    scanner.shadow_min_profit_factor = 1.30
    scanner.shadow_min_expectancy_r = 0.10
    scanner.shadow_min_win_margin = 0.03
    scanner.shadow_recent_window = 10
    scanner.shadow_max_loss_streak = 4
    scanner._admission_cache = {}
    scanner.fetch_shadow_rows = lambda strategy, regime, pair=None: rows
    scanner.live_circuit_breaker = lambda: {
        "active": False,
        "active_until": None,
        "consecutive_losses": 0,
        "losses_24h": 0,
    }
    return scanner


def test_one_hundred_percent_shadow_stops_never_admit_live() -> None:
    scanner = build_admission_scanner(rows_all_stops())
    decision = scanner.admission_decision(make_signal())
    assert decision["admitted"] is False
    assert decision["stats"]["profit_factor"] == 0.0
    assert decision["stats"]["expectancy_r"] == -1.0
    assert any("PF shadow" in blocker for blocker in decision["blockers"])


def test_positive_stable_shadow_sample_can_admit_live() -> None:
    scanner = build_admission_scanner(rows_positive())
    decision = scanner.admission_decision(make_signal())
    assert decision["admitted"] is True
    assert decision["stats"]["completed"] == 30
    assert decision["stats"]["profit_factor"] == pytest.approx(2.0)
    assert decision["stats"]["expectancy_r"] == pytest.approx(0.5)


def test_insufficient_sample_is_never_admitted_even_when_all_wins() -> None:
    scanner = build_admission_scanner(rows_positive(10))
    decision = scanner.admission_decision(make_signal())
    assert decision["admitted"] is False
    assert any("campione shadow" in blocker for blocker in decision["blockers"])


def test_pair_veto_blocks_bad_pair_after_eight_trades() -> None:
    scanner = build_admission_scanner(rows_positive())
    good_rows = rows_positive()
    bad_pair_rows = rows_all_stops(8)
    scanner.fetch_shadow_rows = (
        lambda strategy, regime, pair=None: bad_pair_rows if pair else good_rows
    )
    decision = scanner.admission_decision(make_signal())
    assert decision["admitted"] is False
    assert any("veto coppia" in blocker for blocker in decision["blockers"])


class BreakerClient:
    def __init__(self, rows):
        self.rows = rows

    def fetch_all(self, query, params=()):
        return self.rows


def test_three_consecutive_live_stops_activate_circuit_breaker() -> None:
    now = datetime.now(timezone.utc)
    scanner = object.__new__(CapitalProtectionScanner)
    scanner.client = BreakerClient(
        [
            ("STOP_LOSS", now - timedelta(hours=1)),
            ("STOP_LOSS", now - timedelta(hours=2)),
            ("STOP_LOSS", now - timedelta(hours=3)),
        ]
    )
    scanner.live_stop_trigger = 3
    scanner.live_stop_24h_limit = 99
    scanner.live_cooldown_hours = 24
    breaker = scanner.live_circuit_breaker()
    assert breaker["active"] is True
    assert breaker["consecutive_losses"] == 3


def test_two_live_stops_in_24h_activate_daily_breaker() -> None:
    now = datetime.now(timezone.utc)
    scanner = object.__new__(CapitalProtectionScanner)
    scanner.client = BreakerClient(
        [
            ("STOP_LOSS", now - timedelta(hours=1)),
            ("TARGET_HIT", now - timedelta(hours=2)),
            ("STOP_LOSS", now - timedelta(hours=3)),
        ]
    )
    scanner.live_stop_trigger = 3
    scanner.live_stop_24h_limit = 2
    scanner.live_cooldown_hours = 24
    breaker = scanner.live_circuit_breaker()
    assert breaker["active"] is True
    assert breaker["consecutive_losses"] == 1
    assert breaker["losses_24h"] == 2


class ShadowInsertClient:
    def __init__(self):
        self.calls = []

    def fetch_all(self, query, params=()):
        self.calls.append((query, params))
        if "SELECT id" in query:
            return []
        if "INSERT INTO signals.shadow_signals" in query:
            return [(501,)]
        return []


def test_closed_gate_saves_shadow_signal_not_live_signal() -> None:
    scanner = object.__new__(CapitalProtectionScanner)
    scanner.client = ShadowInsertClient()
    scanner._shadow_saved_cycle = 0
    scanner.max_shadow_per_cycle = 3
    scanner.signal_cooldown_minutes = 90
    scanner.exchange = "kraken"
    decision = {
        "admitted": False,
        "blockers": ["campione shadow 0/30"],
    }
    shadow_id = scanner.save_shadow_signal(make_signal(), decision)
    assert shadow_id == 501
    insert_query, insert_params = scanner.client.calls[-1]
    assert "INSERT INTO signals.shadow_signals" in insert_query
    assert insert_params[0] == RUNTIME_VERSION
    assert insert_params[5] == "kraken"
    assert "SHADOW" in insert_params[13]


def test_shadow_schema_has_separate_non_live_table() -> None:
    assert "CREATE TABLE IF NOT EXISTS signals.shadow_signals" in CAPITAL_PROTECTION_SCHEMA
    assert "outcome_ambiguous" in CAPITAL_PROTECTION_SCHEMA
    assert "runtime_version" in CAPITAL_PROTECTION_SCHEMA


def test_runtime_version_is_capital_protection_v6() -> None:
    assert RUNTIME_VERSION == "CAPITAL_PROTECTION_V6"
