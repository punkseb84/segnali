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
        min_profit_factor=1.05,
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


def economics(net_profit: float = 0.60, net_rr: float = 1.50) -> dict[str, float | bool]:
    return {
        "net_profit_tp1_eur": net_profit,
        "net_loss_sl_eur": 0.40,
        "net_rr": net_rr,
        "gross_rr": 1.80,
        "tradable": True,
    }


def classify(
    engine: ProgressiveOperationalStrategyEngine,
    best: dict[str, Any],
    *,
    net_profit: float = 0.60,
    net_rr: float = 1.50,
    sample: int = 30,
    score: float = 70.0,
    live_score: float = 70.0,
    regime_aligned: bool = True,
    validation_status: str = "PASSED",
    validation_reason: str = "OK",
) -> str:
    best["live_setup_score"] = live_score
    return engine.classify_signal(
        best,
        economics(net_profit, net_rr),
        {"status": validation_status, "reason": validation_reason},
        probability(sample),
        regime_aligned=regime_aligned,
        ev_decision={"block": False, "reason": "allowed"},
        score=score,
    )


def test_candidates_below_forty_trades_are_not_removed() -> None:
    repository = FakeRepository(
        candidates=[candidate(5), candidate(12), candidate(25), candidate(None)]
    )
    engine = build_engine(repository=repository)

    results = engine.fetch_strategy_candidates(limit=10)

    assert len(results) == 4
    assert {item["research_sample_band"] for item in results} == {
        "VERY_LOW_SAMPLE",
        "LIMITED_SAMPLE",
        "B_CONFIDENCE",
        "MISSING_USE_MARKET_CONTEXT",
    }
    assert engine._candidate_rejection_reasons["insufficient_research_trades"] == 0


def test_valid_candidate_with_twenty_five_research_trades_is_class_b() -> None:
    engine = build_engine()
    best = candidate(25)
    best.update({"research_trades": 25, "research_trades_missing": False})

    assert classify(engine, best) == "B"
    assert best["_classification_details"]["blockers"] == []


def test_class_a_requires_robust_setup_specific_sample() -> None:
    engine = build_engine()
    medium = candidate(25)
    medium.update({"research_trades": 25, "research_trades_missing": False})
    robust = candidate(50)
    robust.update({"research_trades": 50, "research_trades_missing": False})

    assert classify(engine, medium, score=85.0, live_score=80.0) == "B"
    assert classify(engine, robust, score=85.0, live_score=80.0) == "A"


def test_old_result_without_trade_field_can_be_class_b() -> None:
    engine = build_engine()
    best = candidate(None)
    best.update({"research_trades": 0, "research_trades_missing": True})

    assert classify(engine, best, sample=35) == "B"
    assert "RESEARCH_SAMPLE_MISSING" in best["_classification_details"]["confidence_notes"]


def test_small_research_sample_is_confidence_note_not_hidden_veto() -> None:
    engine = build_engine()
    best = candidate(5)
    best.update({"research_trades": 5, "research_trades_missing": False})

    assert classify(engine, best, sample=200, score=79.1, live_score=60.0) == "B"
    details = best["_classification_details"]
    assert details["research_trades"] == 5
    assert details["probability_sample"] == 200
    assert "RESEARCH_SAMPLE_LIMITED" in details["confidence_notes"]


def test_historical_profit_factor_weight_is_shrunk_for_tiny_sample() -> None:
    engine = build_engine()
    tiny = candidate(5)
    tiny.update(
        {
            "research_trades": 5,
            "research_trades_missing": False,
            "live_setup_score": 70.0,
        }
    )
    robust = candidate(50)
    robust.update(
        {
            "research_trades": 50,
            "research_trades_missing": False,
            "live_setup_score": 70.0,
        }
    )
    validation = {"status": "PASSED", "reason": "OK"}
    ev = {"block": False, "reason": "allowed"}

    tiny_score = engine.build_score_breakdown(
        tiny, economics(), validation, probability(30), True, ev
    )
    robust_score = engine.build_score_breakdown(
        robust, economics(), validation, probability(30), True, ev
    )

    assert tiny_score["profit_factor_score"] < robust_score["profit_factor_score"]
    assert tiny_score["expectancy_score"] < robust_score["expectancy_score"]


def test_case_153_like_setup_is_not_c_when_all_operational_requirements_pass() -> None:
    engine = build_engine(require_regime_alignment_for_operative=True)
    best = candidate(8, strategy="Volatility Expansion")
    best.update(
        {
            "profit_factor": 8.8057,
            "expectancy": 0.077031,
            "research_trades": 8,
            "research_trades_missing": False,
        }
    )

    signal_class = classify(
        engine,
        best,
        net_profit=0.55,
        net_rr=1.05,
        sample=200,
        score=79.1,
        live_score=60.0,
        regime_aligned=True,
    )

    assert signal_class == "B"
    assert best["_classification_details"]["blockers"] == []


def test_rr_displayed_as_105_but_really_below_threshold_remains_c() -> None:
    engine = build_engine()
    best = candidate(30)
    best.update({"research_trades": 30, "research_trades_missing": False})

    signal_class = classify(engine, best, net_profit=0.55, net_rr=1.0478)

    assert signal_class == "C"
    assert "NET_RR_BELOW_MINIMUM" in best["_classification_details"]["blockers"]


def test_exact_rr_threshold_passes_without_float_noise() -> None:
    engine = build_engine()
    best = candidate(30)
    best.update({"research_trades": 30, "research_trades_missing": False})

    assert classify(engine, best, net_profit=0.30, net_rr=1.05) == "B"


def test_low_live_score_has_explicit_blocker() -> None:
    engine = build_engine()
    best = candidate(30)
    best.update({"research_trades": 30, "research_trades_missing": False})

    assert classify(engine, best, score=79.1, live_score=42.0) == "C"
    assert "LIVE_SCORE_BELOW_MINIMUM" in best["_classification_details"]["blockers"]


def test_high_score_regime_mismatch_can_remain_b_when_buffer_is_met() -> None:
    engine = build_engine(
        require_regime_alignment_for_operative=True,
        allow_high_score_regime_mismatch_b=True,
    )
    best = candidate(30, strategy="Trend Following")
    best.update({"research_trades": 30, "research_trades_missing": False})

    assert classify(
        engine,
        best,
        score=65.0,
        live_score=55.0,
        regime_aligned=False,
    ) == "B"
    assert best["_classification_details"]["regime_buffer_used"] is True
