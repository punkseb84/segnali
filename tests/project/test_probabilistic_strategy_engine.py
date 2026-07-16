from __future__ import annotations

from project.strategy_engine.probabilistic_service import ProbabilisticStrategyEngine


class DummyResearchRepository:
    pass


class DummyDecisionEngine:
    latest_decision = None


def build_engine(**kwargs):
    return ProbabilisticStrategyEngine(
        DummyResearchRepository(),
        DummyDecisionEngine(),
        min_probability_sample_size=30,
        min_net_rr=1.05,
        min_live_setup_score=45.0,
        min_operative_signal_score=55.0,
        min_watchlist_signal_score=30.0,
        **kwargs,
    )


def test_negative_high_confidence_ev_is_a_score_penalty_by_default():
    engine = build_engine()

    result = engine.evaluate_historical_ev_gate(
        -0.10,
        {"confidence": "HIGH"},
    )

    assert result["block"] is False
    assert result["reason"] == "soft_high_confidence_negative"


def test_negative_ev_hard_block_can_be_explicitly_restored():
    engine = build_engine(hard_block_negative_historical_ev=True)

    result = engine.evaluate_historical_ev_gate(
        -0.10,
        {"confidence": "HIGH"},
    )

    assert result["block"] is True
    assert result["reason"] == "high_confidence_negative"


def test_accumulated_evidence_can_produce_class_b_without_every_condition_true():
    engine = build_engine()
    candidate = {
        "profit_factor": 1.08,
        "expectancy": 0.01,
        "live_setup_score": 60.0,
    }
    economics = {
        "net_profit_tp1_eur": 0.50,
        "net_loss_sl_eur": 0.50,
        "net_rr": 1.00,
    }
    validation = {"status": "PASSED", "reason": "OK"}
    probability_context = {
        "sample_size": 100,
        "probability": 0.45,
        "win_rate": 0.45,
        "confidence": "HIGH",
    }

    signal_class = engine.classify_signal(
        candidate,
        economics,
        validation,
        probability_context,
        regime_aligned=False,
        ev_decision={"block": False, "reason": "soft_high_confidence_negative"},
        score=60.0,
    )

    assert signal_class == "B"


def test_intermediate_setup_is_watchlist_class_c():
    engine = build_engine()
    candidate = {
        "profit_factor": 0.99,
        "expectancy": -0.01,
        "live_setup_score": 42.0,
    }
    economics = {
        "net_profit_tp1_eur": 0.20,
        "net_loss_sl_eur": 0.40,
        "net_rr": 0.50,
    }
    validation = {"status": "PASSED_WITH_WARNINGS", "reason": "VOLATILITY_WARNING"}
    probability_context = {
        "sample_size": 20,
        "probability": None,
        "win_rate": 0.40,
        "confidence": "LOW",
    }

    signal_class = engine.classify_signal(
        candidate,
        economics,
        validation,
        probability_context,
        regime_aligned=False,
        ev_decision={"block": False, "reason": "ev_unavailable"},
        score=40.0,
    )

    assert signal_class == "C"


def test_setup_below_minimum_probability_score_is_rejected_as_class_d():
    engine = build_engine()
    candidate = {
        "profit_factor": 1.30,
        "expectancy": 0.10,
        "live_setup_score": 20.0,
    }
    economics = {
        "net_profit_tp1_eur": 1.00,
        "net_loss_sl_eur": 0.50,
        "net_rr": 2.00,
    }
    validation = {"status": "PASSED", "reason": "OK"}
    probability_context = {
        "sample_size": 100,
        "probability": 0.60,
        "win_rate": 0.60,
        "confidence": "HIGH",
    }

    signal_class = engine.classify_signal(
        candidate,
        economics,
        validation,
        probability_context,
        regime_aligned=True,
        ev_decision={"block": False, "reason": "high_confidence_allowed"},
        score=25.0,
    )

    assert signal_class == "D"


def test_live_setup_score_contributes_twenty_percent_of_total_score():
    engine = build_engine()
    candidate = {
        "profit_factor": 1.10,
        "expectancy": 0.02,
        "live_setup_score": 70.0,
    }
    economics = {
        "tradable": True,
        "net_profit_tp1_eur": 0.50,
        "net_loss_sl_eur": 0.50,
        "net_rr": 1.00,
    }
    validation = {"status": "PASSED", "reason": "OK"}
    probability_context = {
        "sample_size": 100,
        "probability": 0.50,
        "win_rate": 0.50,
        "confidence": "HIGH",
    }

    breakdown = engine.build_score_breakdown(
        candidate,
        economics,
        validation,
        probability_context,
        regime_aligned=True,
        ev_decision={"block": False, "reason": "high_confidence_allowed"},
    )

    assert breakdown["live_setup_score"] == 14.0
