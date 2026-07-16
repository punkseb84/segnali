from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

from project.research_engine import memory_service
from project.research_engine.memory_service import MemoryEfficientResearchEngine
from project.research_engine.service import ProgressiveResearchEngine, ResearchBatchResult
from project.scheduler import PlatformScheduler
from project.strategy_engine.economic_guard import EconomicallyGuardedProbabilisticStrategyEngine


def test_railway_config_uses_direct_modular_entrypoint() -> None:
    config = json.loads(Path("railway.json").read_text(encoding="utf-8"))

    assert config["deploy"]["startCommand"] == "python -m project.synchronized_main"
    assert config["deploy"]["restartPolicyType"] == "ON_FAILURE"


def test_sitecustomize_forces_single_numeric_thread() -> None:
    import sitecustomize  # noqa: F401

    for variable in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        assert os.environ[variable] == "1"
    assert os.environ["MALLOC_ARENA_MAX"] == "2"


def test_railway_strategy_interval_never_repeats_before_new_collector_data() -> None:
    settings = SimpleNamespace(
        scheduler_strategy_seconds=60,
        scheduler_collector_seconds=300,
        run_mode="RAILWAY_LIGHT",
    )

    scheduler = PlatformScheduler(settings)

    assert scheduler.effective_strategy_interval() == 300


def test_non_railway_strategy_interval_is_preserved() -> None:
    settings = SimpleNamespace(
        scheduler_strategy_seconds=60,
        scheduler_collector_seconds=300,
        run_mode="RESEARCH",
    )

    scheduler = PlatformScheduler(settings)

    assert scheduler.effective_strategy_interval() == 60


def test_collector_timeframes_use_cost_aware_frequencies() -> None:
    settings = SimpleNamespace(
        operational_timeframe="15m",
        collector_timeframes=["5m", "15m", "1h", "4h"],
    )

    scheduler = PlatformScheduler(settings)

    assert scheduler.collector_timeframe_groups() == (
        ["15m"],
        ["5m"],
        ["1h", "4h"],
    )


class CandidateRepository:
    def __init__(self) -> None:
        self.ohlc_calls = 0

    def fetch_candidate_results(self, timeframe=None, limit=10):
        return [
            {
                "strategy": "Breakout",
                "pair": "BTC/USD",
                "timeframe": timeframe or "15m",
                "profit_factor": 1.20,
                "expectancy": 0.10,
            },
            {
                "strategy": "Momentum",
                "pair": "BTC/USD",
                "timeframe": timeframe or "15m",
                "profit_factor": 1.15,
                "expectancy": 0.08,
            },
            {
                "strategy": "Mean Reversion",
                "pair": "BTC/USD",
                "timeframe": timeframe or "15m",
                "profit_factor": 1.10,
                "expectancy": 0.05,
            },
        ]

    def fetch_best_result(self, timeframe=None):
        return None

    def fetch_ohlc(self, pair, timeframe, limit=720):
        self.ohlc_calls += 1
        return [(index, 100, 101, 99, 100, 10) for index in range(80)]


class DummyDecisionEngine:
    latest_decision = None


def test_live_indicators_are_built_once_per_pair_and_timeframe(monkeypatch) -> None:
    repository = CandidateRepository()
    engine = EconomicallyGuardedProbabilisticStrategyEngine(
        repository,
        DummyDecisionEngine(),
        operational_timeframe="15m",
    )
    build_calls = 0

    def fake_build(prices):
        nonlocal build_calls
        build_calls += 1
        return object()

    monkeypatch.setattr(engine, "_build_live_frame", fake_build)
    monkeypatch.setattr(
        engine,
        "_score_prebuilt_frame",
        lambda frame, candidate: {
            "score": 60.0,
            "regime": "RANGE",
            "breakdown": {"data_quality": 5.0},
        },
    )

    candidates = engine.fetch_strategy_candidates(limit=100)

    assert len(candidates) == 3
    assert repository.ohlc_calls == 1
    assert build_calls == 1


def test_research_wrapper_releases_memory_after_batch(monkeypatch) -> None:
    engine = object.__new__(MemoryEfficientResearchEngine)
    engine.logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    expected = ResearchBatchResult(None, 0, "IDLE", "done")
    calls = []

    monkeypatch.setattr(
        ProgressiveResearchEngine,
        "process_one_batch",
        lambda self, sleep_after=True: expected,
    )
    monkeypatch.setattr(
        memory_service,
        "release_unused_memory",
        lambda: calls.append("released") or None,
    )

    result = engine.process_one_batch(sleep_after=False)

    assert result is expected
    assert calls == ["released"]
