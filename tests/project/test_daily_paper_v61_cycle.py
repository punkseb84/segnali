from __future__ import annotations

from types import SimpleNamespace

from project.daily_paper_scanner import (
    PAPER_EXECUTION_MODE,
    DailyPaperSignalScanner,
)
from tests.project.test_daily_paper_v61 import make_signal


def test_closed_live_gate_still_publishes_ranked_paper_signal() -> None:
    scanner = object.__new__(DailyPaperSignalScanner)
    scanner.pairs = ["AVAX/USD"]
    scanner.logger = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        exception=lambda *args, **kwargs: None,
    )
    scanner.max_signals_per_cycle = 3
    scanner.min_signal_score = 68.0
    scanner.min_net_rr = 1.10
    scanner.min_tp1_net_profit_eur = 0.25
    scanner.max_open_signals = 5
    scanner.max_correlated_per_cycle = 2
    scanner.loss_streak_trigger = 3
    scanner.loss_guard_hours = 12
    scanner.max_net_loss_eur = 1.0
    scanner.paper_min_score = 64.0
    scanner.paper_min_net_rr = 1.05
    scanner.paper_min_net_profit_eur = 0.20
    scanner.paper_max_per_day = 3
    scanner.paper_max_per_cycle = 1
    scanner.paper_min_interval_minutes = 120
    scanner.paper_pair_cooldown_minutes = 360
    scanner.shadow_recent_window = 10

    scanner.expire_stale_signals = lambda: None
    scanner.fetch_relative_strength_snapshot = lambda: {}
    scanner.fetch_forward_performance = lambda: {}
    scanner.determine_strategy_state = (
        lambda strategy, performance, now: {"state": "ACTIVE"}
    )
    scanner.fetch_loss_streak = lambda: (0, None)
    candidate = make_signal()
    diagnostic = {
        "pair": candidate.pair,
        "strategy": candidate.strategy,
        "regime": candidate.regime,
        "score": candidate.score,
        "blockers": [],
    }
    scanner.evaluate_pair_detailed = lambda pair: (candidate, "OK", diagnostic)
    scanner.fetch_live_exposure_pairs = lambda: []
    scanner.paper_daily_state = lambda: {"count": 0, "last_sent": None}
    scanner.admission_decision = lambda signal: {
        "admitted": False,
        "blockers": ["campione shadow 0/30"],
        "stats": {
            "completed": 0,
            "profit_factor": 0.0,
            "expectancy_r": 0.0,
        },
        "pair_stats": {},
        "circuit_breaker": {"active": False},
    }
    shadow_calls = []
    scanner.save_shadow_signal = lambda signal, decision: shadow_calls.append(signal) or 501
    scanner.paper_duplicate_exists = lambda signal: False
    scanner.live_duplicate_exists = lambda signal: False
    scanner.correlated_with_pairs = lambda pair, pairs: []
    scanner.live_circuit_breaker = lambda: {
        "active": False,
        "active_until": None,
        "consecutive_losses": 0,
        "losses_24h": 0,
    }
    scanner.fetch_paper_performance = lambda: {
        "completed": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "expectancy_eur": 0.0,
        "expectancy_r": 0.0,
        "break_even_rate": 1.0,
        "max_loss_streak": 0,
        "recent_completed": 0,
        "recent_profit_factor": 0.0,
        "recent_expectancy_r": 0.0,
    }
    scanner.aggregate_forward_performance = lambda performance: {}

    captured = {}

    def save_signal(signal):
        captured["saved"] = signal
        return 801

    def publish_signal_event(signal):
        captured["published"] = signal

    scanner.save_signal = save_signal
    scanner.publish_signal_event = publish_signal_event

    result = scanner.evaluate()

    assert result is not None
    assert len(shadow_calls) == 1
    assert captured["saved"].score_breakdown["execution_mode"] == PAPER_EXECUTION_MODE
    assert captured["published"].signal_id == 801
    assert scanner._last_cycle_summary["paper_published"] == 1
    assert scanner._last_cycle_summary["published"] == 0
    assert scanner._last_cycle_summary["shadow_saved"] == 1
    assert scanner._last_cycle_summary["paper_today"] == 1
