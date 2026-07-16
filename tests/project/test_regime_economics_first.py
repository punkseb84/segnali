from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from project.strategy_engine.regime_economics import RegimeEconomicsFirstStrategyEngine


class FakeClient:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params=None) -> None:
        self.executed.append((sql, tuple(params or ())))

    def fetch_all(self, sql: str, params=None):
        return []


class FakeRepository:
    def __init__(self, candidates=None, prices=None) -> None:
        self.client = FakeClient()
        self.candidates = list(candidates or [])
        self.prices = list(prices or make_prices())

    def fetch_candidate_results(self, timeframe=None, limit=10):
        return self.candidates[:limit]

    def fetch_best_result(self, timeframe=None):
        return self.candidates[0] if self.candidates else None

    def fetch_ohlc(self, pair, timeframe, limit=720):
        return self.prices[:limit]

    def fetch_candidate_diagnostics(self, timeframe=None):
        return {"timeframe": timeframe, "edge_results": len(self.candidates)}


class FakeBus:
    def __init__(self) -> None:
        self.events = []

    def publish(self, event) -> None:
        self.events.append(event)


class DummyDecision:
    latest_decision = None

    def evaluate_market(self):
        return SimpleNamespace(regime="COMPRESSION", enabled_strategies=["Compression Breakout", "Breakout"])


def make_prices(count: int = 260) -> list[tuple[Any, ...]]:
    chronological = []
    for index in range(count):
        close = 100.0 + index * 0.04
        chronological.append(
            (
                index,
                close - 0.05,
                close + 1.20,
                close - 0.70,
                close,
                1000.0 + index,
            )
        )
    return list(reversed(chronological))


def candidate(strategy: str, trades: int, profit_factor: float = 1.20) -> dict[str, Any]:
    return {
        "strategy": strategy,
        "pair": "BTC/USD",
        "timeframe": "15m",
        "profit_factor": profit_factor,
        "expectancy": 0.08,
        "net_profit": 3.0,
        "parameters": {
            "rsi_range": "40-70",
            "adx_min": 15,
            "atr_multiplier": 1.0,
            "relative_volume_min": 0.8,
            "reward_risk": 0.8,
            "market_regime": "COMPRESSION",
        },
        "validation": {
            "status": "FILTERED_LIVE_ALIGNED_BACKTEST",
            "trades": trades,
            "reward_risk": 0.8,
            "atr_multiplier": 1.0,
        },
    }


def build_engine(**overrides) -> RegimeEconomicsFirstStrategyEngine:
    values = dict(
        research_repository=FakeRepository(),
        decision_engine=DummyDecision(),
        event_bus=FakeBus(),
        operational_timeframe="15m",
        trade_notional_eur=100.0,
        buy_fee_rate=0.001,
        sell_fee_rate=0.001,
        spread_rate=0.0005,
        min_tp1_net_profit_eur=0.30,
        min_net_rr=1.05,
        min_research_trades=40,
        min_probability_sample_size=1000,
        max_take_profit_distance_pct=0.05,
        max_tp_atr_multiple=8.0,
        allowed_signal_classes=["A", "B", "C"],
        operative_signal_classes=["A", "B"],
        watchlist_signal_classes=["C"],
    )
    values.update(overrides)
    return RegimeEconomicsFirstStrategyEngine(**values)


def test_candidates_with_small_historical_samples_are_removed() -> None:
    repository = FakeRepository(
        candidates=[
            candidate("Breakout", trades=12, profit_factor=8.0),
            candidate("Compression Breakout", trades=65, profit_factor=1.18),
        ]
    )
    engine = build_engine(research_repository=repository)

    results = engine.fetch_strategy_candidates(limit=10)

    assert len(results) == 1
    assert results[0]["strategy"] == "Compression Breakout"
    assert results[0]["research_trades"] == 65
    assert engine._candidate_rejection_reasons["insufficient_research_trades"] == 1


def test_regime_and_economic_feasibility_have_priority_over_profit_factor() -> None:
    engine = build_engine()
    decision = SimpleNamespace(
        regime="COMPRESSION",
        enabled_strategies=["Compression Breakout", "Breakout"],
    )
    candidates = [
        {
            "strategy": "Trend Following",
            "pair": "BTC/USD",
            "economic_target_feasible": True,
            "live_setup_score": 90,
            "research_trades": 300,
            "expectancy": 1.0,
            "profit_factor": 12.0,
        },
        {
            "strategy": "Compression Breakout",
            "pair": "ETH/USD",
            "economic_target_feasible": True,
            "live_setup_score": 55,
            "research_trades": 80,
            "expectancy": 0.05,
            "profit_factor": 1.15,
        },
        {
            "strategy": "Breakout",
            "pair": "SOL/USD",
            "economic_target_feasible": False,
            "live_setup_score": 80,
            "research_trades": 100,
            "expectancy": 0.20,
            "profit_factor": 2.0,
        },
    ]

    ordered = engine.prioritize_candidates(decision, candidates)

    assert ordered[0]["strategy"] == "Compression Breakout"
    assert ordered[1]["strategy"] == "Breakout"
    assert ordered[2]["strategy"] == "Trend Following"


def test_dynamic_target_raises_reward_risk_only_within_realistic_cap() -> None:
    engine = build_engine(dynamic_economic_target=True)
    prices = make_prices()

    plan = engine._reward_risk_plan(
        pair="BTC/USD",
        prices=prices,
        entry=110.0,
        stop_loss=109.0,
        stop_distance=1.0,
        atr_value=1.0,
        original_rr=0.50,
    )

    assert plan["economic_target_feasible"] is True
    assert plan["economic_target_adjusted"] is True
    assert plan["effective_reward_risk"] > 0.50
    assert plan["effective_reward_risk"] <= plan["reward_risk_cap"]
    assert plan["economic_preview_net_profit"] >= 0.30
    assert plan["economic_preview_net_rr"] >= 1.05


def test_operative_candidate_is_downgraded_when_regime_is_not_aligned() -> None:
    engine = build_engine(
        min_live_setup_score=0,
        min_operative_signal_score=0,
        require_regime_alignment_for_operative=True,
    )
    best = candidate("Trend Following", trades=100)
    best.update(
        {
            "live_setup_score": 90.0,
            "economic_target_feasible": True,
        }
    )
    economics = {
        "net_profit_tp1_eur": 0.60,
        "net_loss_sl_eur": 0.40,
        "net_rr": 1.50,
        "tradable": True,
    }
    probability = {
        "probability": 0.60,
        "sample_size": 100,
        "confidence": "HIGH",
        "win_rate": 0.60,
    }

    signal_class = engine.classify_signal(
        best,
        economics,
        {"status": "PASSED", "reason": "OK"},
        probability,
        regime_aligned=False,
        ev_decision={"block": False, "reason": "high_confidence_allowed"},
        score=80.0,
    )

    assert signal_class == "C"


def test_only_near_b_watchlists_are_published() -> None:
    bus = FakeBus()
    engine = build_engine(event_bus=bus, enable_watchlist_alerts=True)

    far = SimpleNamespace(
        signal_class="C",
        signal_id=1,
        pair="BTC/USD",
        strategy="Breakout",
        timeframe="15m",
        net_profit_tp1_eur=0.05,
        net_rr=0.20,
        score_breakdown={"live_setup_raw": 70.0, "regime_aligned_flag": 1.0},
    )
    near = SimpleNamespace(
        signal_class="C",
        signal_id=2,
        pair="ETH/USD",
        strategy="Compression Breakout",
        timeframe="15m",
        net_profit_tp1_eur=0.25,
        net_rr=0.90,
        score_breakdown={"live_setup_raw": 55.0, "regime_aligned_flag": 1.0},
    )

    engine.publish_signal_event(far)
    engine.publish_signal_event(near)

    assert len(bus.events) == 1
    assert bus.events[0].payload["signal_id"] == 2
