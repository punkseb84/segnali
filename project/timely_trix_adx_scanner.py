"""Execution-timeliness guard for the TRIX + ADX PAPER scanner."""
from __future__ import annotations

from datetime import timedelta, timezone
from typing import Any

import pandas as pd

from project.strategy_engine.service import GeneratedSignal
from project.trix_adx_scanner import TrixAdxScanner


class TimelyTrixAdxScanner(TrixAdxScanner):
    """Reject entries that can no longer be simulated near the 1h close price."""

    def __init__(
        self,
        *args: Any,
        max_signal_delay_minutes: int = 10,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.max_signal_delay_minutes = max(1, int(max_signal_delay_minutes))

    def evaluate_pair(self, pair: str) -> tuple[GeneratedSignal | None, str]:
        candidate, reason = super().evaluate_pair(pair)
        if candidate is None:
            return None, reason

        reference_open = pd.Timestamp(candidate.reference_candle_time)
        reference_open = (
            reference_open.tz_localize("UTC")
            if reference_open.tzinfo is None
            else reference_open.tz_convert("UTC")
        )
        candle_close = reference_open + timedelta(hours=1)
        now = pd.Timestamp.now(tz=timezone.utc)
        delay_minutes = max(0.0, (now - candle_close).total_seconds() / 60.0)
        if delay_minutes > self.max_signal_delay_minutes:
            self.logger.info(
                "TRIX_ADX candidate rejected pair=%s reason=SIGNAL_WINDOW_EXPIRED delay_minutes=%.2f limit=%s",
                pair,
                delay_minutes,
                self.max_signal_delay_minutes,
            )
            return None, "SIGNAL_WINDOW_EXPIRED"

        candidate.score_breakdown["signal_delay_minutes"] = round(delay_minutes, 4)
        candidate.score_breakdown["max_signal_delay_minutes"] = self.max_signal_delay_minutes
        return candidate, reason
