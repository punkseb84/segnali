from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd

from project.data_collector.repository import MarketDataRepository
from project.database.migrations import SIGNAL_LIFECYCLE_INTEGRITY_MIGRATION
from project.position_monitor.service import PositionMonitor
from project.strategy_engine.operational_integrity import OperationalIntegrityStrategyEngine
from project.synchronized_scheduler import SynchronizedPlatformScheduler


class CaptureLogger:
    def info(self, *args, **kwargs) -> None:
        return None

    def exception(self, *args, **kwargs) -> None:
        return None


class CaptureClient:
    def __init__(self, rows=None) -> None:
        self.rows = list(rows or [])
        self.fetch_calls = []
        self.execute_calls = []
        self.executemany_calls = []

    def fetch_all(self, query, params=()):
        self.fetch_calls.append((query, params))
        return self.rows

    def execute(self, query, params=()):
        self.execute_calls.append((query, params))

    def executemany(self, query, rows):
        values = list(rows)
        self.executemany_calls.append((query, values))
        return len(values)


class CaptureBus:
    def __init__(self) -> None:
        self.events = []

    def publish(self, event) -> None:
        self.events.append(event)


def test_current_kraken_candle_is_updated_instead_of_frozen() -> None:
    client = CaptureClient()
    repository = MarketDataRepository(client, "KRAKEN")
    frame = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-07-16T18:00:00Z"),
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 12.0,
            }
        ]
    )

    assert repository.insert_ohlc("BTC/USD", "15m", frame) == 1
    query = client.executemany_calls[0][0]
    assert "ON CONFLICT" in query
    assert "DO UPDATE SET" in query
    assert "close = EXCLUDED.close" in query
    assert "high = EXCLUDED.high" in query
    assert "low = EXCLUDED.low" in query


def make_integrity_engine(client: CaptureClient) -> OperationalIntegrityStrategyEngine:
    engine = object.__new__(OperationalIntegrityStrategyEngine)
    engine.watchlist_signal_classes = ["C"]
    engine.operative_signal_classes = ["A", "B"]
    engine.signal_cooldown_minutes = 45
    engine.research_repository = SimpleNamespace(client=client)
    engine.logger = CaptureLogger()
    return engine


def test_operational_duplicate_is_pair_level_not_strategy_level() -> None:
    client = CaptureClient(rows=[(321,)])
    engine = make_integrity_engine(client)
    generated = SimpleNamespace(
        signal_class="B",
        pair="BTC/USD",
        timeframe="15m",
        strategy="Volatility Expansion",
        entry=64222.10,
        reference_candle_time=datetime(2026, 7, 16, 18, 0, tzinfo=timezone.utc),
    )

    assert engine.find_active_duplicate(generated) == 321
    query, params = client.fetch_calls[0]
    assert "strategy =" not in query
    assert "pair = %s" in query
    assert "status IN ('NEW', 'OPEN')" in query
    assert "reference_candle_time = %s" in query
    assert params[0:2] == ("BTC/USD", "15m")


def signal(reference: datetime) -> dict:
    return {
        "id": 7,
        "strategy": "Breakout",
        "pair": "BTC/USD",
        "timeframe": "15m",
        "regime": "HIGH_VOLATILITY",
        "entry": 100.0,
        "stop_loss": 99.0,
        "take_profit": 102.0,
        "score": 70.0,
        "probability": 0.6,
        "signal_class": "B",
        "net_profit_tp1_eur": 0.5,
        "net_loss_sl_eur": 0.4,
        "net_rr": 1.25,
        "reference_candle_time": reference,
        "created_at": reference,
    }


def test_entry_candle_uses_close_and_can_record_target() -> None:
    reference = datetime(2026, 7, 16, 18, 0, tzinfo=timezone.utc)
    # Both barriers appear in the full candle range, but the close is above target.
    # Entry-candle range may include pre-signal movement, so close-only is intentional.
    postgres = CaptureClient(rows=[(reference, 103.0, 98.0, 102.5)])
    bus = CaptureBus()
    monitor = PositionMonitor(postgres, bus)

    assert monitor.check_signal(signal(reference)) is True
    audit_query, audit_params = postgres.execute_calls[0]
    assert "outcome_resolution" in audit_query
    assert audit_params[0] == 102.0
    assert audit_params[4] == "ENTRY_CANDLE_CLOSE"
    assert postgres.execute_calls[1][1] == ("TARGET_HIT", 7)
    assert bus.events[0].payload["outcome"] == "TARGET_HIT"
    assert bus.events[0].payload["outcome_resolution"] == "ENTRY_CANDLE_CLOSE"


def test_entry_candle_wick_before_signal_does_not_create_false_stop() -> None:
    reference = datetime(2026, 7, 16, 18, 0, tzinfo=timezone.utc)
    postgres = CaptureClient(rows=[(reference, 101.0, 98.0, 100.2)])
    monitor = PositionMonitor(postgres, CaptureBus())

    assert monitor.check_signal(signal(reference)) is False
    assert postgres.execute_calls == []


class FakeCollector:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    def sync_all_pairs(self, pairs, timeframes):
        self.order.append("collector")
        return [SimpleNamespace(status="SUCCESS")]


class FakeDecision:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    def evaluate_market(self):
        self.order.append("decision")


class FakeStrategy:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    def evaluate(self):
        self.order.append("strategy")

    def publish_daily_signal_report(self):
        return False


def test_operational_cycle_runs_after_fresh_collector_data() -> None:
    order: list[str] = []
    settings = SimpleNamespace(
        collector_pairs=["BTC/USD"],
        operational_timeframe="15m",
        run_mode="RAILWAY_LIGHT",
    )
    scheduler = SynchronizedPlatformScheduler(
        settings,
        data_collector=FakeCollector(order),
        decision_engine=FakeDecision(order),
        strategy_engine=FakeStrategy(order),
    )
    scheduler._collector_lock.acquire()

    scheduler._run_data_collector_sync(["15m"])

    assert order == ["collector", "decision", "strategy"]
    assert scheduler._collector_lock.acquire(blocking=False) is True
    scheduler._collector_lock.release()


def test_lifecycle_migration_persists_auditable_outcomes() -> None:
    assert "outcome_price" in SIGNAL_LIFECYCLE_INTEGRITY_MIGRATION
    assert "outcome_candle_time" in SIGNAL_LIFECYCLE_INTEGRITY_MIGRATION
    assert "outcome_resolution" in SIGNAL_LIFECYCLE_INTEGRITY_MIGRATION
    assert "idx_generated_signals_reference_candle" in SIGNAL_LIFECYCLE_INTEGRITY_MIGRATION
