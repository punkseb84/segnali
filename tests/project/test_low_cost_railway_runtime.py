from __future__ import annotations

from types import SimpleNamespace

from project.config.settings import PlatformSettings
from project.low_cost_main import apply_low_cost_policy
from project.low_cost_scheduler import LowCostPlatformScheduler


def test_low_cost_policy_removes_research_from_parent_and_reduces_cadence() -> None:
    original = PlatformSettings(
        enable_research_engine=True,
        run_research_on_startup=True,
        operational_timeframe="15m",
        collector_timeframes=["5m", "15m", "1h", "4h"],
        scheduler_collector_seconds=300,
        scheduler_decision_seconds=300,
        scheduler_strategy_seconds=60,
        scheduler_position_monitor_seconds=60,
        strategy_candidate_limit=100,
        strategy_ohlc_limit=720,
        research_batch_size=100,
        max_research_runtime_minutes=10,
    )

    optimized = apply_low_cost_policy(original)

    assert optimized.enable_research_engine is False
    assert optimized.run_research_on_startup is False
    assert optimized.collector_timeframes == ["15m", "1h"]
    assert optimized.scheduler_collector_seconds == 900
    assert optimized.scheduler_decision_seconds == 900
    assert optimized.scheduler_strategy_seconds == 900
    assert optimized.scheduler_position_monitor_seconds == 900
    assert optimized.strategy_candidate_limit == 25
    assert optimized.strategy_ohlc_limit == 500
    assert optimized.research_batch_size == 60
    assert optimized.max_research_runtime_minutes == 6


def test_low_cost_policy_preserves_operational_timeframe_without_unused_context() -> None:
    original = PlatformSettings(
        operational_timeframe="15m",
        collector_timeframes=["15m"],
    )

    optimized = apply_low_cost_policy(original)

    assert optimized.collector_timeframes == ["15m"]


def test_isolated_research_defaults(monkeypatch) -> None:
    monkeypatch.delenv("LOW_COST_RESEARCH_INTERVAL_HOURS", raising=False)
    monkeypatch.delenv("LOW_COST_RESEARCH_STARTUP_DELAY_SECONDS", raising=False)

    scheduler = LowCostPlatformScheduler(SimpleNamespace())

    assert scheduler.research_interval_hours == 6
    assert scheduler.startup_research_delay_seconds == 30
