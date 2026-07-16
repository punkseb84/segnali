from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from project.database.migrations import WATCHLIST_ISOLATION_MIGRATION
from project.position_monitor.service import PositionMonitor
from project.strategy_engine.economic_guard import EconomicallyGuardedProbabilisticStrategyEngine
from project.strategy_engine.probabilistic_service import ProbabilisticStrategyEngine


class FakeClient:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.fetch_calls = []
        self.execute_calls = []

    def fetch_all(self, query, params=()):
        self.fetch_calls.append((query, params))
        return self.rows

    def execute(self, query, params=()):
        self.execute_calls.append((query, params))


class DummyResearchRepository:
    def __init__(self, client):
        self.client = client


class DummyDecisionEngine:
    latest_decision = None


class FakePostgres:
    def __init__(self, rows):
        self.rows = rows
        self.fetch_calls = []

    def fetch_all(self, query, params=()):
        self.fetch_calls.append((query, params))
        return self.rows

    def execute(self, query, params=()):
        raise AssertionError("No update expected in this test")


def build_engine(client: FakeClient):
    return EconomicallyGuardedProbabilisticStrategyEngine(
        DummyResearchRepository(client),
        DummyDecisionEngine(),
        operative_signal_classes=["A", "B"],
        watchlist_signal_classes=["C"],
        signal_cooldown_minutes=45,
    )


def signal(signal_class: str):
    return SimpleNamespace(
        strategy="Range Reversal",
        pair="BTC/USD",
        timeframe="15m",
        regime="COMPRESSION",
        signal_class=signal_class,
    )


def test_watchlist_duplicate_query_is_separate_from_operative_query():
    client = FakeClient()
    engine = build_engine(client)

    assert engine.find_active_duplicate(signal("C")) is None
    watchlist_query = client.fetch_calls[-1][0]
    assert "status = 'WATCHLIST'" in watchlist_query
    assert "signal_class = %s" in watchlist_query

    assert engine.find_active_duplicate(signal("B")) is None
    operative_query = client.fetch_calls[-1][0]
    assert "status IN ('NEW', 'OPEN')" in operative_query
    assert "signal_class IN" in operative_query
    assert "WATCHLIST" not in operative_query


def test_class_c_is_persisted_as_watchlist(monkeypatch):
    client = FakeClient()
    engine = build_engine(client)
    monkeypatch.setattr(
        ProbabilisticStrategyEngine,
        "save_signal",
        lambda self, generated_signal: 91,
    )

    signal_id = engine.save_signal(signal("C"))

    assert signal_id == 91
    assert len(client.execute_calls) == 1
    query, params = client.execute_calls[0]
    assert "SET status = 'WATCHLIST'" in query
    assert params == (91,)


def test_operative_signal_does_not_receive_watchlist_status(monkeypatch):
    client = FakeClient()
    engine = build_engine(client)
    monkeypatch.setattr(
        ProbabilisticStrategyEngine,
        "save_signal",
        lambda self, generated_signal: 92,
    )

    assert engine.save_signal(signal("B")) == 92
    assert client.execute_calls == []


def test_position_monitor_fetches_only_a_b_and_accepts_null_probability():
    created_at = datetime.now(timezone.utc)
    postgres = FakePostgres(
        [
            (
                7,
                "Breakout",
                "BTC/USD",
                "15m",
                "COMPRESSION",
                100.0,
                99.0,
                102.0,
                60.0,
                None,
                "B",
                created_at,
            )
        ]
    )
    monitor = PositionMonitor(postgres)

    open_signals = monitor.fetch_open_signals()

    assert len(open_signals) == 1
    assert open_signals[0]["probability"] is None
    assert open_signals[0]["signal_class"] == "B"
    query = postgres.fetch_calls[0][0]
    assert "signal_class IN ('A', 'B')" in query
    assert "status IN ('NEW', 'OPEN')" in query


def test_migration_converts_existing_open_c_rows_to_watchlist():
    assert "SET status = 'WATCHLIST'" in WATCHLIST_ISOLATION_MIGRATION
    assert "signal_class = 'C'" in WATCHLIST_ISOLATION_MIGRATION
    assert "status IN ('NEW', 'OPEN')" in WATCHLIST_ISOLATION_MIGRATION
