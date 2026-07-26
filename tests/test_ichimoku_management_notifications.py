from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

from project.hour_aligned_ichimoku_scheduler import HourAlignedIchimokuScheduler
from project.reliable_notification import ReliableNotificationEngine
from project.shared.events import Event, EventBus, EventType


def test_hour_close_sync_starts_once_in_utc_window():
    scheduler = HourAlignedIchimokuScheduler.__new__(HourAlignedIchimokuScheduler)
    scheduler.settings = SimpleNamespace(operational_timeframe="1h")
    scheduler._last_hour_close_sync_key = None
    scheduler.start_data_collector_sync = Mock(return_value=True)
    scheduler.logger = Mock()

    now = datetime(2026, 7, 26, 13, 1, tzinfo=timezone.utc)

    assert scheduler._sync_after_hour_close_if_due(now) is True
    assert scheduler._sync_after_hour_close_if_due(now) is False
    scheduler.start_data_collector_sync.assert_called_once_with(["1h"])


def test_hour_close_sync_does_not_run_outside_window():
    scheduler = HourAlignedIchimokuScheduler.__new__(HourAlignedIchimokuScheduler)
    scheduler.settings = SimpleNamespace(operational_timeframe="1h")
    scheduler._last_hour_close_sync_key = None
    scheduler.start_data_collector_sync = Mock(return_value=True)
    scheduler.logger = Mock()

    now = datetime(2026, 7, 26, 13, 6, tzinfo=timezone.utc)

    assert scheduler._sync_after_hour_close_if_due(now) is False
    scheduler.start_data_collector_sync.assert_not_called()


class _FakePostgres:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))


def test_break_even_is_marked_sent_only_after_telegram_success(monkeypatch):
    postgres = _FakePostgres()
    engine = ReliableNotificationEngine(EventBus(), enabled=True, postgres=postgres)
    monkeypatch.setattr(engine, "send_telegram", lambda *args, **kwargs: {"message_id": 1})

    engine.handle_event(
        Event(
            EventType.REPORT_READY,
            {
                "message": "break even",
                "trusted_html": True,
                "signal_id": 241,
                "management_update": "BREAK_EVEN_ARMED",
            },
        )
    )

    assert len(postgres.calls) == 1
    assert postgres.calls[0][1] == (241,)
    assert "breakeven_notification_sent" in postgres.calls[0][0]


def test_break_even_remains_pending_after_telegram_failure(monkeypatch):
    postgres = _FakePostgres()
    engine = ReliableNotificationEngine(EventBus(), enabled=True, postgres=postgres)
    monkeypatch.setattr(engine, "send_telegram", lambda *args, **kwargs: None)

    engine.handle_event(
        Event(
            EventType.REPORT_READY,
            {
                "message": "break even",
                "trusted_html": True,
                "signal_id": 241,
                "management_update": "BREAK_EVEN_ARMED",
            },
        )
    )

    assert postgres.calls == []
