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


def settings():
    return SimpleNamespace(
        enable_data_collector=False,
        scheduler_collector_seconds=300,
        collector_pairs=["BTC/USD"],
        collector_timeframes=["15m"],
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
