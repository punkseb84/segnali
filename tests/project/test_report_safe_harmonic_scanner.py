from __future__ import annotations

from project.harmonic_live_scanner import LIVE_LONG_STRATEGIES
from project.report_safe_harmonic_scanner import ReportSafeHarmonicLiveStrategyScanner
from project.shared.events import EventBus, EventType


class CapturingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def fetch_all(self, query, params=()):
        normalized = tuple(params or ())
        self.calls.append((query, normalized))
        if "SELECT COUNT(*) FROM signals.generated_signals" in query:
            return [(0,)]
        return []

    def execute(self, query, params=()):
        return None


def test_report_query_matches_seven_strategy_parameters() -> None:
    client = CapturingClient()
    event_bus = EventBus()
    messages: list[str] = []
    event_bus.subscribe(
        EventType.REPORT_READY,
        lambda event: messages.append(str(event.payload.get("message") or "")),
    )
    scanner = ReportSafeHarmonicLiveStrategyScanner(
        client,
        event_bus,
        ["BTC/USD"],
        enable_daily_signal_report=True,
        daily_signal_report_hours=24,
    )
    scanner._last_cycle_summary = {
        "pairs_scanned": 1,
        "qualified": 0,
        "published": 0,
        "open_after": 0,
        "max_open": 5,
        "regimes": {"TREND_UP": 1},
        "rejections": {"NO_SETUP_TREND_UP": 1},
        "strategy_states": {
            name: {"state": "ACTIVE", "next_review": None}
            for name in LIVE_LONG_STRATEGIES
        },
        "performance": {"completed": 0},
    }

    assert scanner.publish_daily_signal_report() is True
    report_query, report_params = client.calls[0]
    assert report_query.count("%s") == 1 + len(LIVE_LONG_STRATEGIES)
    assert len(report_params) == 1 + len(LIVE_LONG_STRATEGIES)
    assert report_params[0] == 24
    assert tuple(report_params[1:]) == LIVE_LONG_STRATEGIES
    assert messages and "REPORT SCANNER LONG V5.1" in messages[0]
    assert "Harmonic Pattern Reversal" in messages[0]
