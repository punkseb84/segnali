from __future__ import annotations

import threading
from types import SimpleNamespace

import schedule

from project.scheduler import PlatformScheduler


class FakeDecisionEngine:
    def __init__(self, order):
        self.order = order

    def evaluate_market(self):
        self.order.append("decision")


class FakeStrategyEngine:
    def __init__(self, order):
        self.order = order

    def evaluate(self):
        self.order.append("strategy")

    def publish_daily_signal_report(self):
        self.order.append("report")
        return False


class BlockingDataCollector:
    def __init__(self, order):
        self.order = order
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []

    def sync_all_pairs(self, pairs, timeframes):
        selected = list(timeframes)
        self.calls.append(selected)
        label = f"collector:{','.join(selected)}"
        self.order.append(label)
        if selected != ["15m"]:
            self.started.set()
            self.release.wait(timeout=2)


class BlockingResearchEngine:
    def __init__(self, order):
        self.order = order
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def seed_combinations(self, pairs, timeframes, chunk_size=1000):
        self.order.append("research_seed")
        return 1

    def process_one_batch(self, sleep_after=True):
        self.calls += 1
        self.order.append("research_batch")
        self.started.set()
        self.release.wait(timeout=2)


def settings(**overrides):
    values = dict(
        enable_data_collector=False,
        scheduler_collector_seconds=300,
        collector_pairs=["BTC/USD"],
        collector_timeframes=["5m", "15m", "1h", "4h"],
        operational_timeframe="15m",
        run_data_collector_on_startup=False,
        enable_decision_engine=True,
        scheduler_decision_seconds=300,
        enable_strategy_engine=True,
        scheduler_strategy_seconds=60,
        enable_position_monitor=False,
        scheduler_position_monitor_seconds=60,
        enable_research_engine=True,
        run_mode="RAILWAY_LIGHT",
        research_sleep_between_batches_seconds=60,
        railway_light_research_seconds=60,
        research_batch_size=1,
        max_research_runtime_minutes=1,
        run_research_on_startup=True,
        enable_notification_engine=False,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_operational_timeframe_sync_runs_before_strategy_and_full_sync_is_background():
    schedule.clear()
    order = []
    collector = BlockingDataCollector(order)
    scheduler = PlatformScheduler(
        settings(
            enable_data_collector=True,
            run_data_collector_on_startup=True,
            enable_research_engine=False,
        ),
        data_collector=collector,
        decision_engine=FakeDecisionEngine(order),
        strategy_engine=FakeStrategyEngine(order),
    )

    scheduler.configure()

    assert collector.started.wait(timeout=0.5)
    assert order[0] == "collector:15m"
    assert order.index("collector:15m") < order.index("decision")
    assert order.index("decision") < order.index("strategy")
    assert order.index("strategy") < order.index("collector:5m,1h,4h")
    assert collector.calls == [["15m"], ["5m", "1h", "4h"]]
    assert scheduler._collector_thread is not None
    assert scheduler._collector_thread.is_alive()
    assert any(job.interval == 300 and job.unit == "seconds" for job in schedule.jobs)

    collector.release.set()
    scheduler._collector_thread.join(timeout=1)
    schedule.clear()


def test_collector_syncs_cannot_overlap():
    schedule.clear()
    order = []
    collector = BlockingDataCollector(order)
    scheduler = PlatformScheduler(
        settings(enable_research_engine=False),
        data_collector=collector,
    )

    assert scheduler.start_data_collector_sync(["5m", "1h"]) is True
    assert collector.started.wait(timeout=0.5)
    assert scheduler.start_data_collector_sync(["4h"]) is False
    assert collector.calls == [["5m", "1h"]]

    collector.release.set()
    assert scheduler._collector_thread is not None
    scheduler._collector_thread.join(timeout=1)
    schedule.clear()


def test_operational_engines_run_before_non_blocking_research():
    schedule.clear()
    order = []
    research = BlockingResearchEngine(order)
    scheduler = PlatformScheduler(
        settings(),
        research_engine=research,
        decision_engine=FakeDecisionEngine(order),
        strategy_engine=FakeStrategyEngine(order),
    )

    scheduler.configure()

    assert research.started.wait(timeout=0.5)
    assert order.index("decision") < order.index("research_seed")
    assert order.index("strategy") < order.index("research_seed")
    assert any(job.interval == 3600 and job.unit == "seconds" for job in schedule.jobs)
    assert scheduler._research_thread is not None
    assert scheduler._research_thread.is_alive()

    research.release.set()
    scheduler._research_thread.join(timeout=1)
    schedule.clear()


def test_research_batches_cannot_overlap():
    schedule.clear()
    order = []
    research = BlockingResearchEngine(order)
    scheduler = PlatformScheduler(settings(), research_engine=research)

    assert scheduler.start_research_batch() is True
    assert research.started.wait(timeout=0.5)
    assert scheduler.start_research_batch() is False
    assert research.calls == 1

    research.release.set()
    assert scheduler._research_thread is not None
    scheduler._research_thread.join(timeout=1)
    schedule.clear()
