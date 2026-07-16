from __future__ import annotations

from collections import Counter
from types import SimpleNamespace

from project.research_engine.robust_repository import (
    ROBUST_RESEARCH_VERSION,
    ROBUST_VALIDATION_STATUS,
    RobustResearchRepository,
)
from project.research_engine.robust_service import (
    ROBUST_STRATEGY_PROFILES,
    RobustWalkForwardResearchEngine,
)
from project.strategy_engine.robust_operational import RobustEdgeOperationalStrategyEngine


class FakeClient:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def fetch_all(self, query, params=()):
        self.calls.append((query, params))
        return self.rows


class FakeLogger:
    def info(self, *args, **kwargs):
        return None


def test_candidate_query_requires_all_robust_edge_conditions() -> None:
    client = FakeClient([])
    repository = RobustResearchRepository(client)

    assert repository.fetch_candidate_results(timeframe="15m", limit=20) == []

    query, params = client.calls[-1]
    assert "edge_status', '') = 'ELIGIBLE'" in query
    assert "profit_factor >= %s" in query
    assert "expectancy > %s" in query
    assert "net_profit > 0" in query
    assert "test_trades" in query
    assert "profit_factor > 1.0 OR expectancy > 0" not in query
    assert params[0] == ROBUST_RESEARCH_VERSION
    assert params[1] == ROBUST_VALIDATION_STATUS


def test_compact_long_profiles_never_research_trend_down() -> None:
    regimes = {
        regime
        for profiles in ROBUST_STRATEGY_PROFILES.values()
        for profile in profiles
        for regime in profile["regimes"]
    }
    assert "TREND_DOWN" not in regimes


def test_robust_combination_set_is_compact_and_versioned() -> None:
    engine = object.__new__(RobustWalkForwardResearchEngine)
    engine.priority_timeframe = "15m"
    engine.research_version = ROBUST_RESEARCH_VERSION

    combinations = list(engine.generate_combinations(["BTC/USD", "SOL/USD"], ["5m", "15m"]))

    assert combinations
    assert len(combinations) < 200
    assert {item[2] for item in combinations} == {"15m"}
    assert all(item[3]["research_version"] == ROBUST_RESEARCH_VERSION for item in combinations)
    assert all(item[3]["market_regime"] != "TREND_DOWN" for item in combinations)


def build_edge_engine() -> RobustWalkForwardResearchEngine:
    engine = object.__new__(RobustWalkForwardResearchEngine)
    engine.min_total_trades = 20
    engine.min_train_trades = 12
    engine.min_test_trades = 6
    engine.min_train_pf = 1.05
    engine.min_test_pf = 1.10
    engine.min_test_expectancy = 0.01
    return engine


def test_one_trade_profit_factor_cannot_be_eligible() -> None:
    engine = build_edge_engine()
    train = {"trades": 1, "profit_factor": 9.0, "expectancy": 0.5, "net_profit": 0.5}
    test = {"trades": 0, "profit_factor": 0.0, "expectancy": 0.0, "net_profit": 0.0}

    blockers = engine._edge_blockers(train, test, total_trades=1)

    assert "TOTAL_TRADES_BELOW_MINIMUM" in blockers
    assert "TEST_TRADES_BELOW_MINIMUM" in blockers
    assert "TEST_PROFIT_FACTOR_BELOW_MINIMUM" in blockers


def test_positive_train_and_oos_edge_is_eligible() -> None:
    engine = build_edge_engine()
    train = {"trades": 20, "profit_factor": 1.25, "expectancy": 0.04, "net_profit": 0.8}
    test = {"trades": 8, "profit_factor": 1.20, "expectancy": 0.03, "net_profit": 0.24}

    assert engine._edge_blockers(train, test, total_trades=28) == []


def test_operational_long_is_blocked_in_live_trend_down() -> None:
    engine = object.__new__(RobustEdgeOperationalStrategyEngine)
    engine._candidate_rejection_reasons = Counter()
    engine.logger = FakeLogger()
    best = {
        "strategy": "Liquidity Sweep",
        "pair": "SOL/USD",
        "timeframe": "15m",
        "live_setup_regime": "TREND_DOWN",
        "research_eligible": True,
        "validation": {
            "edge_status": "ELIGIBLE",
            "trades": 30,
            "test_trades": 9,
        },
        "parameters": {"market_regime": "RANGE"},
    }

    result = engine.evaluate_candidate(SimpleNamespace(regime="TREND_DOWN"), best, False)

    assert result is None
    assert engine._candidate_rejection_reasons["long_blocked_in_trend_down"] == 1
