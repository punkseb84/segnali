from __future__ import annotations

from datetime import datetime, timezone

from project.harmonic_live_scanner import LIVE_LONG_STRATEGIES
from project.reliable_harmonic_scanner import RUNTIME_VERSION
from project.shared.events import EventBus, EventType
from project.version_scoped_harmonic_scanner import VersionScopedHarmonicLiveStrategyScanner


class CapturingClient:
    def __init__(self, current_runtime_rows=None) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.current_runtime_rows = list(current_runtime_rows or [])

    def fetch_all(self, query, params=()):
        normalized = tuple(params or ())
        self.calls.append((query, normalized))
        if "SELECT status, closed_at" in query:
            return self.current_runtime_rows
        if "SELECT COUNT(*) FROM signals.generated_signals" in query:
            return [(0,)]
        return []

    def execute(self, query, params=()):
        return None


def build_scanner(client: CapturingClient) -> VersionScopedHarmonicLiveStrategyScanner:
    return VersionScopedHarmonicLiveStrategyScanner(
        client,
        EventBus(),
        ["BTC/USD"],
        enable_daily_signal_report=True,
        daily_signal_report_hours=24,
    )


def test_loss_streak_query_is_limited_to_current_runtime_and_delivered_signals() -> None:
    client = CapturingClient()
    scanner = build_scanner(client)

    assert scanner.fetch_loss_streak() == (0, None)
    query, params = client.calls[0]
    assert "score_breakdown->>'runtime_version' = %s" in query
    assert "telegram_sent_at IS NOT NULL" in query
    assert query.count("%s") == len(LIVE_LONG_STRATEGIES) + 1
    assert tuple(params[:-1]) == LIVE_LONG_STRATEGIES
    assert params[-1] == RUNTIME_VERSION


def test_three_current_runtime_stops_are_counted() -> None:
    now = datetime.now(timezone.utc)
    client = CapturingClient(
        [
            ("STOP_LOSS", now),
            ("STOP_LOSS", now),
            ("STOP_LOSS", now),
            ("TARGET_HIT", now),
        ]
    )
    scanner = build_scanner(client)

    streak, last_closed = scanner.fetch_loss_streak()
    assert streak == 3
    assert last_closed == now


def test_report_count_is_limited_to_current_runtime() -> None:
    client = CapturingClient()
    event_bus = EventBus()
    messages: list[str] = []
    event_bus.subscribe(
        EventType.REPORT_READY,
        lambda event: messages.append(str(event.payload.get("message") or "")),
    )
    scanner = VersionScopedHarmonicLiveStrategyScanner(
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
        "loss_streak": 0,
        "loss_guard_active": False,
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
    assert "score_breakdown->>'runtime_version' = %s" in report_query
    assert report_query.count("%s") == 2 + len(LIVE_LONG_STRATEGIES)
    assert len(report_params) == 2 + len(LIVE_LONG_STRATEGIES)
    assert report_params[-1] == RUNTIME_VERSION
    assert messages and "Modalità prudente: <b>non attiva</b>" in messages[0]
    assert "Stop consecutivi V5.1.2: <b>0</b>" in messages[0]
