from unittest.mock import Mock

from project.ichimoku_outcome_monitor import IchimokuOutcomeMonitor
from project.notifying_ichimoku_outcome_monitor import (
    NotifyingIchimokuOutcomeMonitor,
)


class _FakePostgres:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def fetch_all(self, sql, params=()):
        self.calls.append((sql, params))
        return self.responses.pop(0)


def _monitor(responses):
    monitor = NotifyingIchimokuOutcomeMonitor.__new__(
        NotifyingIchimokuOutcomeMonitor
    )
    monitor.postgres = _FakePostgres(responses)
    monitor.logger = Mock()
    return monitor


def _signal(exchange="kraken"):
    return {
        "id": 245,
        "pair": "ETH/USD",
        "timeframe": "1h",
        "exchange": exchange,
    }


def test_series_exchange_resolution_is_case_insensitive():
    monitor = _monitor([[('Kraken',)]])

    resolved = monitor._resolve_series_exchange(_signal())

    assert resolved == "Kraken"
    assert len(monitor.postgres.calls) == 1
    assert "LOWER(TRIM(exchange))" in monitor.postgres.calls[0][0]
    assert monitor.postgres.calls[0][1] == ("kraken", "ETH/USD", "1h")


def test_series_exchange_resolution_falls_back_to_pair_and_timeframe():
    monitor = _monitor([[], [('Kraken',)]])

    resolved = monitor._resolve_series_exchange(_signal("legacy-label"))

    assert resolved == "Kraken"
    assert len(monitor.postgres.calls) == 2
    assert monitor.postgres.calls[1][1] == ("ETH/USD", "1h")
    assert "PAIR_TIMEFRAME_FALLBACK" in str(monitor.logger.info.call_args)


def test_check_signal_passes_resolved_exchange_to_outcome_monitor(monkeypatch):
    monitor = _monitor([])
    monkeypatch.setattr(monitor, "_resolve_reference_candle", lambda signal: None)
    monkeypatch.setattr(monitor, "_resolve_series_exchange", lambda signal: "Kraken")
    captured = {}

    def fake_parent_check(self, signal):
        captured.update(signal)
        return False

    monkeypatch.setattr(IchimokuOutcomeMonitor, "check_signal", fake_parent_check)

    assert monitor.check_signal(_signal()) is False
    assert captured["exchange"] == "Kraken"
