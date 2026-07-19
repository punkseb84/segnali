from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from project.harmonic_live_scanner import (
    HARMONIC_STRATEGY,
    LIVE_LONG_STRATEGIES,
    HarmonicLiveStrategyScanner,
)
from project.harmonic_patterns import classify_ratios
from project.shared.events import EventBus


class FakeClient:
    def __init__(self, rows=None):
        self.rows = rows or []

    def fetch_all(self, query, params=()):
        if "SELECT status, closed_at" in query:
            return self.rows
        return []


def build_scanner(client=None, **kwargs):
    return HarmonicLiveStrategyScanner(
        client or FakeClient(),
        EventBus(),
        ["BTC/USD"],
        trade_notional_eur=100.0,
        buy_fee_rate=0.001,
        sell_fee_rate=0.001,
        spread_rate=0.0005,
        min_notional_eur=10.0,
        **kwargs,
    )


def test_seventh_strategy_is_live_and_long_only():
    assert len(LIVE_LONG_STRATEGIES) == 7
    assert HARMONIC_STRATEGY in LIVE_LONG_STRATEGIES
    assert all("SHORT" not in name.upper() for name in LIVE_LONG_STRATEGIES)


def test_gartley_ratios_are_recognized():
    pattern, quality = classify_ratios(0.618, 0.618, 1.414, 0.786)
    assert pattern == "Gartley"
    assert quality > 0.95


def test_bat_butterfly_and_crab_are_recognized():
    assert classify_ratios(0.45, 0.618, 2.0, 0.886)[0] == "Bat"
    assert classify_ratios(0.786, 0.618, 1.9, 1.272)[0] == "Butterfly"
    assert classify_ratios(0.50, 0.618, 3.0, 1.618)[0] == "Crab"


def test_invalid_ratios_do_not_create_pattern():
    assert classify_ratios(0.95, 0.20, 1.0, 0.40)[0] is None


def test_stop_floor_is_wider_than_normal_noise():
    scanner = build_scanner()
    frame = pd.DataFrame(
        [{"low": 99.8, "high20_prev": 100.0}] * 10
    )
    distance = scanner.stop_distance_for_strategy(
        "Trend Pullback", frame, entry=100.0, atr=1.0
    )
    assert distance >= 1.45


def test_risk_sizing_caps_estimated_net_loss():
    scanner = build_scanner(max_net_loss_eur=1.0)
    economics = scanner.calculate_net_economics(
        entry=100.0,
        stop_loss=97.5,
        take_profit=104.0,
    )
    assert economics["tradable"] is True
    assert float(economics["entry_notional_eur"]) < 100.0
    assert float(economics["net_loss_sl_eur"]) <= 1.05


def test_three_recent_stops_are_detected():
    now = datetime.now(timezone.utc)
    scanner = build_scanner(
        FakeClient([
            ("STOP_LOSS", now),
            ("STOP_LOSS", now),
            ("STOP_LOSS", now),
            ("TARGET_HIT", now),
        ])
    )
    streak, last_closed = scanner.fetch_loss_streak()
    assert streak == 3
    assert last_closed == now
