from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from project.capital_protection_v601 import CapitalProtectionReportScannerV601
from project.fair_shadow_signal_monitor import FairShadowValidationMonitor


def build_scanner(rows):
    scanner = object.__new__(CapitalProtectionReportScannerV601)
    scanner.shadow_min_samples = 30
    scanner.shadow_min_profit_factor = 1.30
    scanner.shadow_min_expectancy_r = 0.10
    scanner.shadow_min_win_margin = 0.03
    scanner.shadow_recent_window = 10
    scanner.shadow_max_loss_streak = 4
    scanner._admission_cache = {}
    scanner.fetch_shadow_rows = lambda strategy, regime, pair=None: rows
    scanner.live_circuit_breaker = lambda: {
        "active": False,
        "active_until": None,
        "consecutive_losses": 0,
        "losses_24h": 0,
    }
    return scanner


def signal_stub():
    return SimpleNamespace(
        strategy="Breakout 20",
        regime="RANGE",
        pair="AVAX/USD",
    )


def all_stops(count: int):
    now = datetime.now(timezone.utc)
    return [
        ("AVAX/USD", "STOP_LOSS", 0.0, 1.0, 1.2, now)
        for _ in range(count)
    ]


def positive_rows(count: int = 30):
    now = datetime.now(timezone.utc)
    rows = []
    for index in range(count):
        if index % 2 == 0:
            rows.append(("AVAX/USD", "TARGET_HIT", 2.0, 0.0, 2.0, now))
        else:
            rows.append(("AVAX/USD", "STOP_LOSS", 0.0, 1.0, 2.0, now))
    return rows


def test_empty_sample_does_not_emit_impossible_win_rate_threshold() -> None:
    scanner = build_scanner([])
    decision = scanner.admission_decision(signal_stub())
    assert decision["admitted"] is False
    assert decision["win_rate_gate"]["status"] == "INSUFFICIENT_SAMPLE"
    assert decision["win_rate_gate"]["required_win_rate"] is None
    assert not any(str(item).startswith("win rate ") for item in decision["blockers"])
    assert not any("103.0%" in str(item) for item in decision["blockers"])


def test_single_outcome_sample_relies_on_pf_expectancy_not_fake_win_threshold() -> None:
    scanner = build_scanner(all_stops(30))
    decision = scanner.admission_decision(signal_stub())
    assert decision["admitted"] is False
    assert decision["win_rate_gate"]["status"] == "SINGLE_OUTCOME_SAMPLE"
    assert not any(str(item).startswith("win rate ") for item in decision["blockers"])
    assert any("PF shadow" in str(item) for item in decision["blockers"])


def test_measurable_positive_sample_still_passes_win_rate_gate() -> None:
    scanner = build_scanner(positive_rows())
    decision = scanner.admission_decision(signal_stub())
    assert decision["admitted"] is True
    assert decision["win_rate_gate"]["status"] == "PASSED"
    assert decision["win_rate_gate"]["required_win_rate"] == pytest.approx(
        1 / 3 + 0.03
    )


def test_required_win_rate_is_never_above_one_hundred_percent() -> None:
    scanner = build_scanner([])
    gate = scanner.shadow_win_rate_gate(
        {
            "completed": 30,
            "wins": 29,
            "losses": 1,
            "win_rate": 29 / 30,
            "break_even_rate": 0.99,
        }
    )
    assert gate["evaluated"] is True
    assert gate["required_win_rate"] == 1.0


class QueuePostgres:
    def __init__(self, open_count: int = 12) -> None:
        self.open_count = open_count
        self.fetch_calls = []
        self.execute_calls = []

    def execute(self, query, params=()):
        self.execute_calls.append((query, params))

    def fetch_all(self, query, params=()):
        self.fetch_calls.append((query, params))
        if "FROM signals.shadow_signals" in query:
            now = datetime.now(timezone.utc)
            return [
                (
                    index,
                    "CAPITAL_PROTECTION_V6",
                    "Breakout 20",
                    "AVAX/USD",
                    "15m",
                    "RANGE",
                    "kraken",
                    20.0,
                    19.0,
                    22.0,
                    1.5,
                    1.0,
                    1.5,
                    80.0,
                    now,
                    now,
                )
                for index in range(1, self.open_count + 1)
            ]
        if "FROM market_data.ohlc" in query:
            return []
        return []


def test_shadow_monitor_uses_dedicated_large_limit_and_restores_live_limit() -> None:
    postgres = QueuePostgres(open_count=12)
    monitor = FairShadowValidationMonitor(
        postgres,
        max_signals_per_cycle=5,
        max_shadow_signals_per_cycle=200,
    )
    assert monitor.monitor_shadow_signals() == 0
    shadow_query_calls = [
        (query, params)
        for query, params in postgres.fetch_calls
        if "FROM signals.shadow_signals" in query
    ]
    assert len(shadow_query_calls) == 1
    assert shadow_query_calls[0][1] == (200,)
    assert monitor.max_signals_per_cycle == 5
    assert len(
        [query for query, _ in postgres.fetch_calls if "FROM market_data.ohlc" in query]
    ) == 12


def test_shadow_monitor_limit_has_safe_minimum() -> None:
    monitor = FairShadowValidationMonitor(
        QueuePostgres(open_count=0),
        max_signals_per_cycle=5,
        max_shadow_signals_per_cycle=1,
    )
    assert monitor.max_shadow_signals_per_cycle == 20
