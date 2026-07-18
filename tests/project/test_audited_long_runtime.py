from __future__ import annotations

import project.simple_main as simple_main
from project.audited_long_scanner import AuditedLongStrategyScanner
from project.audited_runtime import main as audited_main
from project.shared.events import EventBus


class CapturingClient:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.queries: list[str] = []

    def fetch_all(self, query, params=()):
        self.queries.append(str(query))
        return self.rows


def build_scanner(client=None, **kwargs) -> AuditedLongStrategyScanner:
    return AuditedLongStrategyScanner(
        client or CapturingClient(),
        EventBus(),
        ["BTC/USD", "ETH/USD", "SOL/USD"],
        **kwargs,
    )


def test_qualified_active_setup_outranks_unqualified_higher_score() -> None:
    states = {
        "Trend Pullback": {"state": "ACTIVE"},
        "Breakout 20": {"state": "ACTIVE"},
    }
    qualified = {
        "strategy": "Trend Pullback",
        "score": 72.0,
        "qualified": True,
    }
    unqualified = {
        "strategy": "Breakout 20",
        "score": 95.0,
        "qualified": False,
    }

    ordered = sorted(
        [unqualified, qualified],
        key=lambda item: AuditedLongStrategyScanner.assessment_priority(item, states),
        reverse=True,
    )

    assert ordered[0]["strategy"] == "Trend Pullback"


def test_qualified_active_setup_outranks_paused_setup() -> None:
    states = {
        "Trend Pullback": {"state": "ACTIVE"},
        "Breakout 20": {"state": "PAUSED"},
    }
    active = {
        "strategy": "Trend Pullback",
        "score": 70.0,
        "qualified": True,
    }
    paused = {
        "strategy": "Breakout 20",
        "score": 98.0,
        "qualified": True,
    }

    ordered = sorted(
        [paused, active],
        key=lambda item: AuditedLongStrategyScanner.assessment_priority(item, states),
        reverse=True,
    )

    assert ordered[0]["strategy"] == "Trend Pullback"


def test_relative_strength_query_is_bounded_to_three_days() -> None:
    client = CapturingClient()
    scanner = build_scanner(client)

    assert scanner.fetch_relative_strength_snapshot() == {}
    assert client.queries
    assert "timestamp >= NOW() - INTERVAL '3 days'" in client.queries[-1]


def test_runtime_activates_audited_scanner() -> None:
    assert simple_main.EnhancedLongStrategyScanner is AuditedLongStrategyScanner
    assert callable(audited_main)


def test_portfolio_limit_defaults_to_five_open_signals() -> None:
    scanner = build_scanner()

    assert scanner.max_open_signals == 5
    assert scanner.max_signals_per_cycle == 3
