"""Version-isolated wrapper for the validated seven-strategy scanner."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from project.harmonic_live_scanner import LIVE_LONG_STRATEGIES
from project.strategy_engine.service import GeneratedSignal
from project.validated_live_scanner import ValidatedLiveStrategyScanner


RUNTIME_VERSION = "HARMONIC_LIVE_V5_1_3"


class ValidatedLiveStrategyScannerV513(ValidatedLiveStrategyScanner):
    """Stamp and evaluate only outcomes created by the corrected V5.1.3 runtime."""

    def _build_signal_for_assessment(
        self,
        pair: str,
        frame_15m: pd.DataFrame,
        regime: str,
        relative: dict[str, float],
        assessment: dict[str, Any],
    ) -> tuple[GeneratedSignal | None, str, dict[str, Any]]:
        signal, reason, diagnostic = super()._build_signal_for_assessment(
            pair, frame_15m, regime, relative, assessment
        )
        if signal is None:
            return signal, reason, diagnostic
        breakdown = dict(signal.score_breakdown or {})
        breakdown["runtime_version"] = RUNTIME_VERSION
        return replace(signal, score_breakdown=breakdown), reason, diagnostic

    def fetch_forward_performance(self) -> dict[str, dict[str, Any]]:
        placeholders = ", ".join(["%s"] * len(LIVE_LONG_STRATEGIES))
        rows = self.client.fetch_all(
            f"""SELECT strategy, status, net_profit_tp1_eur, net_loss_sl_eur, closed_at
            FROM signals.generated_signals
            WHERE strategy IN ({placeholders})
              AND score_breakdown->>'runtime_version' = %s
              AND telegram_sent_at IS NOT NULL
              AND COALESCE(outcome_ambiguous, FALSE) = FALSE
              AND status IN ('TARGET_HIT', 'STOP_LOSS')
              AND created_at >= NOW() - INTERVAL '120 days'
            ORDER BY closed_at ASC, id ASC""",
            (*LIVE_LONG_STRATEGIES, RUNTIME_VERSION),
        )
        grouped: dict[str, list[tuple[Any, ...]]] = {
            name: [] for name in LIVE_LONG_STRATEGIES
        }
        for row in rows:
            grouped.setdefault(str(row[0]), []).append(row)
        return {
            strategy: self.calculate_enhanced_performance(items)
            for strategy, items in grouped.items()
        }

    def fetch_loss_streak(self) -> tuple[int, datetime | None]:
        placeholders = ", ".join(["%s"] * len(LIVE_LONG_STRATEGIES))
        rows = self.client.fetch_all(
            f"""SELECT status, closed_at FROM signals.generated_signals
            WHERE strategy IN ({placeholders})
              AND score_breakdown->>'runtime_version' = %s
              AND telegram_sent_at IS NOT NULL
              AND COALESCE(outcome_ambiguous, FALSE) = FALSE
              AND status IN ('TARGET_HIT', 'STOP_LOSS')
              AND closed_at IS NOT NULL
              AND closed_at >= NOW() - INTERVAL '30 days'
            ORDER BY closed_at DESC, id DESC
            LIMIT 12""",
            (*LIVE_LONG_STRATEGIES, RUNTIME_VERSION),
        )
        streak = 0
        last_closed: datetime | None = None
        for status, closed_at in rows:
            if last_closed is None and isinstance(closed_at, datetime):
                last_closed = (
                    closed_at if closed_at.tzinfo else closed_at.replace(tzinfo=timezone.utc)
                )
            if str(status) != "STOP_LOSS":
                break
            streak += 1
        return streak, last_closed

    def evaluate(self) -> GeneratedSignal | None:
        result = super().evaluate()
        self._last_cycle_summary["runtime_version"] = RUNTIME_VERSION
        self.logger.info(
            "VALIDATED_LIVE_V5_1_3 runtime=%s strict_relative_strength=true ambiguous_excluded=true",
            RUNTIME_VERSION,
        )
        return result
