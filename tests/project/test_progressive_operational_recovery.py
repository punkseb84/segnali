from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from project.strategy_engine.progressive_operational import ProgressiveOperationalStrategyEngine


class FakeClient:
    def execute(self, *args, **kwargs) -> None:
        return None

    def fetch_all(self, *args, **kwargs):
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
        return SimpleNamespace(
            regime="COMPRESSION",
            enabled_strategies=["Compression Breakout", "Breakout"],
        )


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


def candidate(trades: int | None, strategy: str = "Breakout") -> dict[str, Any]:
    validation = {
        "status": "FILTERED_LIVE_ALIGNED_BACKTEST",
        "reward_risk": 1.5,
        "atr_multiplier": 1.0,
    }
    if trades is not None:
        validation["trades"] = trades
    return {
        "strategy": strategy,
        "pair": "BTC/USD",
        "timeframe": "15m",
        "profit_factor": 1.30,
        "expectancy": 0.20,
        "net_profit": 5.0,
        "parameters": {
            "rsi_range": "40-70",
            "adx_min": 15,
            "atr_multiplier": 1.0,
            "relative_volume_min": 0.8,
            "reward_risk": 1.5,
            "market_regime": "COMPRESSION",
        },
        "validation": validation,
    }


def build_engine(repository=None, **overrides) -> ProgressiveOperationalStrategyEngine:
    values = dict(
        research_repository=repository or FakeRepository(),
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
        min_research_trades_b=20,
        min_live_setup_score=45,
        min_operative_signal_score=55,
        min_watchlist_signal_score=30,
        min_probability_sample_size=30,
        max_take_profit_distance_pct=0.05,
        max_tp_atr_multiple=8.0,
        allowed_signal_classes=["A", "B", "C"],
        operative_signal_classes=["A", "B"],
        watchlist_signal_classes=["C"],
    )
    values.update(overrides)
    return ProgressiveOperationalStrategyEngine(**values)


def probability(sample: int = 30) -> dict[str, Any]:
    return {
        "probability": 0.60,
        "sample_size": sample,
        "confidence": "MEDIUM",
        "win_rate": 0.60,
        "mfe_percentile": 2.0,
        "mae_percentile": -1.0,
    }


def economics() -> dict[str, float | bool]:
    return {
        "net_profit_tp1_eur": 0.60,
        "net_loss_sl_eur": 0.40,
        "net_rr": 1.50,
        "gross_rr": 1.80,
        "tradable": True,
    }


def test_candidates_below_forty_trades_are_not_removed() -> None:
    repository = FakeRepository(
        candidates=[candidate(12), candidate(25), candidate(None)]
    )
    engine = build_engine(repository=repository)

    results = engine.fetch_strategy_candidates(limit=10)

    assert len(results) == 3
    assert {item["research_sample_band"] for item in results} == {
        "WATCHLIST_ONLY",
        "B_READY",
        "MISSING_USE_LIVE_SAMPLE",
    }
    assert engine._candidate_rejection_reasons["insufficient_research_trades"] == 0


def test_valid_candidate_with_twenty_five_research_trades_can_be_class_b() -> None:
    engine = build_engine()
    best = candidate(25)
    best.update({"research_trades": 25, "live_setup_score": 70.0})

    signal_class = engine.classify_signal(
        best,
        economics(),
        {"status": "PASSED", "reason": "OK"},
        probability(30),
        regime_aligned=True,
        ev_decision={"block": False, "reason": "allowed"},
        score=70.0,
    )

    assert signal_class == "B"


def test_class_a_with_medium_sample_is_downgraded_only_to_b() -> None:
    engine = build_engine()
    best = candidate(25)
    best.update({"research_trades": 25, "live_setup_score": 80.0})

    signal_class = engine.classify_signal(
        best,
        economics(),
        {"status": "PASSED", "reason": "OK"},
        probability(30),
        regime_aligned=True,
        ev_decision={"block": False, "reason": "allowed"},
        score=85.0,
    )

    assert signal_class == "B"


def test_old_result_without_trade_field_uses_live_probability_sample() -> None:
    engine = build_engine()
    best = candidate(None)
    best.update(
        {
            "research_trades": 0,
            "research_trades_missing": True,
            "live_setup_score": 70.0,
        }
    )

    signal_class = engine.classify_signal(
        best,
        economics(),
        {"status": "PASSED", "reason": "OK"},
        probability(35),
        regime_aligned=True,
        ev_decision={"block": False, "reason": "allowed"},
        score=70.0,
    )

    assert signal_class == "B"


def test_genuinely_small_sample_remains_watchlist_c() -> None:
    engine = build_engine()
    best = candidate(8)
    best.update({"research_trades": 8, "live_setup_score": 70.0})

    signal_class = engine.classify_signal(
        best,
        economics(),
        {"status": "PASSED", "reason": "OK"},
        probability(8),
        regime_aligned=True,
        ev_decision={"block": False, "reason": "allowed"},
        score=70.0,
    )

    assert signal_class == "C"


def test_high_score_regime_mismatch_can_remain_b_when_requested_gate_is_on() -> None:
    engine = build_engine(
        require_regime_alignment_for_operative=True,
        allow_high_score_regime_mismatch_b=True,
    )
    best = candidate(30, strategy="Trend Following")
    best.update({"research_trades": 30, "live_setup_score": 65.0})

    signal_class = engine.classify_signal(
        best,
        economics(),
        {"status": "PASSED", "reason": "OK"},
        probability(30),
        regime_aligned=False,
        ev_decision={"block": False, "reason": "allowed"},
        score=65.0,
    )

    assert signal_class == "B"
