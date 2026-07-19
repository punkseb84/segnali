from __future__ import annotations

from datetime import datetime, timezone

from project.reliable_harmonic_scanner import RUNTIME_VERSION
from project.version_scoped_main import startup_loss_guard_status


class FakeScanner:
    def __init__(self, streak: int, last_closed: datetime | None) -> None:
        self._streak = streak
        self._last_closed = last_closed
        self.loss_streak_trigger = 3
        self.loss_guard_hours = 12

    def fetch_loss_streak(self):
        return self._streak, self._last_closed


def test_runtime_marker_is_v5_1_2() -> None:
    assert RUNTIME_VERSION == "HARMONIC_LIVE_V5_1_2"


def test_startup_guard_reports_not_active_without_current_losses() -> None:
    scanner = FakeScanner(0, None)
    streak, active, label = startup_loss_guard_status(
        scanner, now=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    )
    assert streak == 0
    assert active is False
    assert label == "NON ATTIVA"


def test_startup_guard_reports_actual_expiry_in_italian_time() -> None:
    scanner = FakeScanner(
        3,
        datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc),
    )
    streak, active, label = startup_loss_guard_status(
        scanner, now=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    )
    assert streak == 3
    assert active is True
    assert label.startswith("ATTIVA fino al ")
    assert "ora italiana" in label


def test_startup_guard_expires_automatically() -> None:
    scanner = FakeScanner(
        4,
        datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc),
    )
    _, active, label = startup_loss_guard_status(
        scanner, now=datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    )
    assert active is False
    assert label == "NON ATTIVA"
