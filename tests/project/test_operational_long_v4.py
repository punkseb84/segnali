from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from project.operational_long_scanner import (
    OPERATIONAL_LONG_STRATEGIES,
    OperationalLongStrategyScanner,
)
from project.operational_scheduler import OperationalMarketScheduler
from project.shared.events import EventBus


class FakeClient:
    def fetch_all(self, query, params=()):
        return []


class FakeCollector:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def sync_all_pairs(self, pairs, timeframes):
        selected = list(timeframes)
        self.calls.append(selected)
        return [
            SimpleNamespace(status="SUCCESS", timeframe=timeframe)
            for timeframe in selected
            for _pair in pairs
        ]


class FakeScanner:
    def __init__(self) -> None:
        self.evaluations = 0
        self.reports = 0

    def evaluate(self):
        self.evaluations += 1

    def publish_daily_signal_report(self):
        self.reports += 1
        return True


class FakeMonitor:
    def __init__(self) -> None:
        self.calls = 0

    def monitor_open_signals(self):
        self.calls += 1
        return 0


def build_scanner() -> OperationalLongStrategyScanner:
    return OperationalLongStrategyScanner(
        FakeClient(), EventBus(), ["BTC/USD", "ETH/USD"]
    )


def assessment(blockers, score=70.0):
    return {
        "strategy": "Breakout 20",
        "score": score,
        "qualified": False,
        "reasons": ["breakout core"],
        "blockers": list(blockers),
        "target_rr": 1.9,
    }


def test_six_strategies_remain_long_only() -> None:
    assert len(OPERATIONAL_LONG_STRATEGIES) == 6
    assert all("SHORT" not in name.upper() for name in OPERATIONAL_LONG_STRATEGIES)


def test_one_secondary_failure_does_not_cancel_valid_core() -> None:
    result = OperationalLongStrategyScanner._promote_assessment(
        assessment(["volume sotto 0,85 della media"], score=72.0),
        core_blockers={
            "massimo 20 candele non superato o non mantenuto",
            "contesto 1h ancora troppo ribassista",
        },
        max_secondary_blockers=1,
        min_score=66.0,
        confirmation_reason="due conferme su tre",
    )
    assert result["qualified"] is True
    assert result["confirmation_mode"] == "CORE_PLUS_CONFIRMATIONS"


def test_structural_failure_still_blocks_signal() -> None:
    result = OperationalLongStrategyScanner._promote_assessment(
        assessment(["massimo 20 candele non superato o non mantenuto"], score=90.0),
        core_blockers={
            "massimo 20 candele non superato o non mantenuto",
            "contesto 1h ancora troppo ribassista",
        },
        max_secondary_blockers=1,
        min_score=66.0,
        confirmation_reason="due conferme su tre",
    )
    assert result["qualified"] is False


def test_recovering_downtrend_is_reclassified_as_neutral() -> None:
    frame_15m = pd.DataFrame(
        [
            {
                "close": 99.0,
                "ema50": 100.0,
                "rsi": 46.0,
                "macd_hist": 0.10,
                "bb_width": 0.04,
            },
            {
                "close": 101.0,
                "ema50": 100.0,
                "rsi": 52.0,
                "macd_hist": 0.20,
                "bb_width": 0.04,
            },
        ]
    )
    frame_1h = pd.DataFrame(
        [
            {"close": 97.0, "ema20": 96.5, "ema50": 99.0, "ema200": 100.0},
            {"close": 98.0, "ema20": 97.0, "ema50": 99.0, "ema200": 100.0},
        ]
    )
    assert OperationalLongStrategyScanner.detect_regime(frame_15m, frame_1h) == "NEUTRAL"


def test_economic_target_candidates_expand_without_lowering_thresholds() -> None:
    values = OperationalLongStrategyScanner.economic_rr_candidates(1.75)
    assert values[0] >= 1.75
    assert 2.0 in values
    assert values[-1] == 3.2


def test_operational_cycle_refreshes_hourly_context_before_scan() -> None:
    settings = SimpleNamespace(
        operational_timeframe="15m",
        collector_pairs=["BTC/USD"],
    )
    collector = FakeCollector()
    scanner = FakeScanner()
    monitor = FakeMonitor()
    scheduler = OperationalMarketScheduler(
        settings,
        collector,
        None,
        None,
        scanner,
        monitor,
    )
    scheduler._hourly_context_due = lambda now=None: True  # type: ignore[method-assign]
    scheduler._collector_lock.acquire()
    scheduler._run_data_collector_sync(["15m"])

    assert collector.calls == [["15m", "1h"]]
    assert monitor.calls == 1
    assert scanner.evaluations == 1
