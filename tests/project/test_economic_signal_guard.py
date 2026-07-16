from __future__ import annotations

from project.strategy_engine.economic_guard import EconomicallyGuardedProbabilisticStrategyEngine


class DummyResearchRepository:
    pass


class DummyDecisionEngine:
    latest_decision = None


def build_engine():
    return EconomicallyGuardedProbabilisticStrategyEngine(
        DummyResearchRepository(),
        DummyDecisionEngine(),
        min_probability_sample_size=30,
        min_tp1_net_profit_eur=0.30,
        min_net_rr=1.05,
        min_live_setup_score=45.0,
        min_operative_signal_score=55.0,
        min_watchlist_signal_score=30.0,
    )


def probability_context():
    return {
        "sample_size": 100,
        "probability": 0.60,
        "win_rate": 0.60,
        "confidence": "HIGH",
    }


def candidate():
    return {
        "profit_factor": 26.2828,
        "expectancy": 0.200580,
        "live_setup_score": 70.0,
    }


def test_low_profit_and_low_rr_are_downgraded_to_watchlist():
    engine = build_engine()
    economics = {
        "net_profit_tp1_eur": 0.054663,
        "net_loss_sl_eur": 0.4022,
        "net_rr": 0.1359,
    }

    signal_class = engine.classify_signal(
        candidate(),
        economics,
        {"status": "PASSED", "reason": "OK"},
        probability_context(),
        regime_aligned=False,
        ev_decision={"block": False, "reason": "high_confidence_allowed"},
        score=80.0,
    )

    assert signal_class == "C"


def test_valid_economics_keep_probabilistic_class_b():
    engine = build_engine()
    economics = {
        "net_profit_tp1_eur": 0.50,
        "net_loss_sl_eur": 0.40,
        "net_rr": 1.10,
    }

    signal_class = engine.classify_signal(
        {
            "profit_factor": 1.08,
            "expectancy": 0.01,
            "live_setup_score": 60.0,
        },
        economics,
        {"status": "PASSED", "reason": "OK"},
        {
            "sample_size": 100,
            "probability": 0.45,
            "win_rate": 0.45,
            "confidence": "HIGH",
        },
        regime_aligned=False,
        ev_decision={"block": False, "reason": "soft_high_confidence_negative"},
        score=60.0,
    )

    assert signal_class == "B"
