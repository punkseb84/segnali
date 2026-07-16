from __future__ import annotations

from types import SimpleNamespace

from project.strategy_engine.pair_regime import PairRegimeEconomicsFirstStrategyEngine


class FakeRepository:
    def __init__(self) -> None:
        self.client = SimpleNamespace(execute=lambda *args, **kwargs: None, fetch_all=lambda *args, **kwargs: [])


class DummyDecision:
    latest_decision = None


def build_engine() -> PairRegimeEconomicsFirstStrategyEngine:
    return PairRegimeEconomicsFirstStrategyEngine(
        FakeRepository(),
        DummyDecision(),
        operational_timeframe="15m",
        min_research_trades=0,
        min_watchlist_net_profit_eur=0.20,
        min_watchlist_net_rr=0.85,
        min_watchlist_live_score=45.0,
        allowed_signal_classes=["A", "B", "C"],
        operative_signal_classes=["A", "B"],
        watchlist_signal_classes=["C"],
    )


def test_pair_live_regime_overrides_global_btc_regime_for_priority() -> None:
    engine = build_engine()
    global_decision = SimpleNamespace(
        regime="COMPRESSION",
        enabled_strategies=["Compression Breakout", "Breakout"],
    )
    candidates = [
        {
            "strategy": "Compression Breakout",
            "pair": "BTC/USD",
            "live_setup_regime": "COMPRESSION",
            "economic_target_feasible": True,
            "live_setup_score": 55,
            "research_trades": 80,
            "expectancy": 0.05,
            "profit_factor": 1.15,
        },
        {
            "strategy": "Trend Following",
            "pair": "SOL/USD",
            "live_setup_regime": "TREND_UP",
            "economic_target_feasible": True,
            "live_setup_score": 70,
            "research_trades": 100,
            "expectancy": 0.08,
            "profit_factor": 1.20,
        },
        {
            "strategy": "Trend Following",
            "pair": "ETH/USD",
            "live_setup_regime": "RANGE",
            "economic_target_feasible": True,
            "live_setup_score": 90,
            "research_trades": 200,
            "expectancy": 1.0,
            "profit_factor": 9.0,
        },
    ]

    ordered = engine.prioritize_candidates(global_decision, candidates)

    assert ordered[0]["pair"] == "SOL/USD"
    assert ordered[1]["pair"] == "BTC/USD"
    assert ordered[2]["pair"] == "ETH/USD"


def test_unknown_pair_regime_falls_back_to_global_decision() -> None:
    engine = build_engine()
    decision = SimpleNamespace(
        regime="COMPRESSION",
        enabled_strategies=["Compression Breakout", "Breakout"],
    )
    candidate = {
        "strategy": "Compression Breakout",
        "live_setup_regime": "INSUFFICIENT_DATA",
    }

    assert engine._pair_regime(candidate, decision) == "COMPRESSION"
    assert engine._is_pair_regime_aligned(candidate, decision) is True


def test_near_b_watchlist_has_priority_over_higher_score_but_weak_economics() -> None:
    engine = build_engine()
    high_score_weak = SimpleNamespace(
        net_profit_tp1_eur=0.05,
        net_rr=0.20,
        score=78.0,
        score_breakdown={"live_setup_raw": 80.0, "regime_aligned_flag": 1.0},
    )
    lower_score_near_b = SimpleNamespace(
        net_profit_tp1_eur=0.25,
        net_rr=0.90,
        score=64.0,
        score_breakdown={"live_setup_raw": 55.0, "regime_aligned_flag": 1.0},
    )

    assert engine._watchlist_priority(lower_score_near_b) > engine._watchlist_priority(high_score_weak)
