from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from project.timely_trix_adx_scanner import TimelyTrixAdxScanner
from project.trix_adx_scanner import TrixAdxScanner


class _Logger:
    def info(self, *args, **kwargs) -> None:
        return None


def test_stale_hourly_candidate_is_rejected(monkeypatch) -> None:
    now = pd.Timestamp.now(tz="UTC")
    candidate = SimpleNamespace(
        reference_candle_time=now - pd.Timedelta(hours=1, minutes=30),
        score_breakdown={},
    )
    monkeypatch.setattr(
        TrixAdxScanner,
        "evaluate_pair",
        lambda self, pair: (candidate, "OK"),
    )

    scanner = object.__new__(TimelyTrixAdxScanner)
    scanner.max_signal_delay_minutes = 10
    scanner.logger = _Logger()

    result, reason = scanner.evaluate_pair("BTC/USD")

    assert result is None
    assert reason == "SIGNAL_WINDOW_EXPIRED"


def test_recent_hourly_candidate_keeps_delay_audit(monkeypatch) -> None:
    now = pd.Timestamp.now(tz="UTC")
    candidate = SimpleNamespace(
        reference_candle_time=now - pd.Timedelta(hours=1, minutes=4),
        score_breakdown={},
    )
    monkeypatch.setattr(
        TrixAdxScanner,
        "evaluate_pair",
        lambda self, pair: (candidate, "OK"),
    )

    scanner = object.__new__(TimelyTrixAdxScanner)
    scanner.max_signal_delay_minutes = 10
    scanner.logger = _Logger()

    result, reason = scanner.evaluate_pair("BTC/USD")

    assert result is candidate
    assert reason == "OK"
    assert 0.0 <= candidate.score_breakdown["signal_delay_minutes"] <= 10.0
    assert candidate.score_breakdown["max_signal_delay_minutes"] == 10
