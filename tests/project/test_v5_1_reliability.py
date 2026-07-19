from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from project.harmonic_live_scanner import HarmonicLiveStrategyScanner
from project.reliable_harmonic_scanner import (
    RUNTIME_VERSION,
    ReliableHarmonicLiveStrategyScanner,
)
from project.reliable_notification import ReliableNotificationEngine
from project.shared.events import EventBus


class FakeClient:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.queries = []
        self.executions = []

    def fetch_all(self, query, params=()):
        self.queries.append((query, params))
        return self.rows

    def execute(self, query, params=()):
        self.executions.append((query, params))


def build_scanner(client=None):
    return ReliableHarmonicLiveStrategyScanner(
        client or FakeClient(),
        EventBus(),
        ["BTC/USD"],
        exchange="Kraken",
    )


def test_datetime_safe_filter_excludes_current_open_candle() -> None:
    boundary = pd.Timestamp.now(tz="UTC").floor("15min")
    rows = [
        (boundary.to_pydatetime(), 101, 102, 100, 101.5, 10),
        ((boundary - pd.Timedelta(minutes=15)).to_pydatetime(), 100, 101, 99, 100.5, 9),
    ]
    scanner = build_scanner(FakeClient(rows))
    frame = scanner.fetch_closed_frame("BTC/USD", "15m", 10)
    assert len(frame) == 1
    assert frame.iloc[-1]["timestamp"] == boundary - pd.Timedelta(minutes=15)


def test_harmonic_target_never_exceeds_real_projection_or_cap() -> None:
    scanner = build_scanner()
    scanner._building_assessment = {"harmonic_target": 101.0}
    target = scanner.technical_target(
        "Harmonic Pattern Reversal",
        pd.DataFrame(),
        entry=100.0,
        stop_distance=2.0,
        atr=1.0,
        target_rr=1.8,
    )
    assert target == 101.0


def test_loss_guard_raises_thresholds_but_does_not_hard_block_adaptive_setup(monkeypatch) -> None:
    scanner = build_scanner()
    scanner._loss_guard_active = True
    observed = {}

    def fake_parent(self, pair, frame, regime, relative, assessment):
        observed["guard_inside"] = self._loss_guard_active
        return None, "TEST_REJECTION", {"pair": pair}

    monkeypatch.setattr(
        HarmonicLiveStrategyScanner,
        "_build_signal_for_assessment",
        fake_parent,
    )
    result = scanner._build_signal_for_assessment(
        "BTC/USD",
        pd.DataFrame([{"atr": 1.0, "close": 100.0, "ema20": 99.0, "rsi": 55.0, "volume_ratio": 1.0, "timestamp": pd.Timestamp.now(tz="UTC")}]),
        "TREND_UP",
        {},
        {"strategy": "Trend Pullback", "score": 80.0},
    )
    assert observed["guard_inside"] is False
    assert scanner._loss_guard_active is True
    assert result[1] == "TEST_REJECTION"


def test_open_slots_count_only_delivered_recent_live_signals() -> None:
    client = FakeClient([])
    scanner = build_scanner(client)
    scanner.fetch_open_exposure_pairs()
    query, _ = client.queries[-1]
    assert "telegram_sent_at IS NOT NULL" in query
    assert "INTERVAL '48 hours'" in query
    assert "strategy IN" in query


def test_forward_metrics_are_isolated_by_runtime_version() -> None:
    client = FakeClient([])
    scanner = build_scanner(client)
    scanner.fetch_forward_performance()
    query, params = client.queries[-1]
    assert "score_breakdown->>'runtime_version' = %s" in query
    assert params[-1] == RUNTIME_VERSION


def test_failed_telegram_delivery_is_marked_non_operational() -> None:
    postgres = FakeClient()
    engine = ReliableNotificationEngine(
        EventBus(),
        enabled=True,
        telegram_token="token",
        telegram_chat_id="chat",
        postgres=postgres,
    )
    engine.mark_signal_delivery_failed(42, "TELEGRAM_SEND_FAILED")
    query, params = postgres.executions[-1]
    assert "status = 'DELIVERY_FAILED'" in query
    assert params == ("TELEGRAM_SEND_FAILED", 42)


def test_stale_live_signals_are_expired_before_scanning() -> None:
    client = FakeClient()
    scanner = build_scanner(client)
    scanner.expire_stale_signals()
    query, _ = client.executions[-1]
    assert "status = 'EXPIRED'" in query
    assert "INTERVAL '48 hours'" in query
