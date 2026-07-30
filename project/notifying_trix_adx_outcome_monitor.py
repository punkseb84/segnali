"""Retryable Telegram management notifications for TRIX + ADX PAPER positions."""
from __future__ import annotations

import json
from typing import Any

from project.trix_adx_outcome_monitor import TrixAdxOutcomeMonitor
from project.trix_adx_scanner import RUNTIME_VERSION


class NotifyingTrixAdxOutcomeMonitor(TrixAdxOutcomeMonitor):
    """Backfill unsent net break-even updates and monitor only TRIX positions."""

    @staticmethod
    def _state_dict(raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                return {}
            return dict(parsed) if isinstance(parsed, dict) else {}
        return {}

    def _backfill_pending_break_even_notifications(self) -> int:
        rows = self.postgres.fetch_all(
            """SELECT id, pair, timeframe, entry, quantity,
                      COALESCE(estimated_buy_fee_eur, 0),
                      COALESCE(score_breakdown, '{}'::jsonb)
               FROM signals.generated_signals
               WHERE status IN ('NEW', 'OPEN')
                 AND score_breakdown->>'runtime_version' = %s
                 AND COALESCE(
                       (score_breakdown->>'breakeven_armed')::boolean,
                       FALSE
                     ) = TRUE
                 AND COALESCE(
                       (score_breakdown->>'breakeven_notification_sent')::boolean,
                       FALSE
                     ) = FALSE
               ORDER BY created_at ASC
               LIMIT %s""",
            (RUNTIME_VERSION, self.max_signals_per_cycle),
        )
        published = 0
        for signal_id, pair, timeframe, entry, quantity, buy_fee, state_raw in rows:
            state = self._state_dict(state_raw)
            signal = {
                "id": int(signal_id),
                "pair": str(pair or "n/d"),
                "timeframe": str(timeframe or "1h"),
                "entry": float(entry),
                "quantity": float(quantity or 0.0),
                "estimated_buy_fee_eur": float(buy_fee or 0.0),
            }
            risk = float(state.get("risk_distance") or 0.0)
            trigger_price = float(entry) + risk
            break_even_price = float(
                state.get("breakeven_price")
                or self.true_break_even_price(signal)
            )
            timestamp = state.get("breakeven_armed_at") or "già registrato"
            self._publish_break_even(
                signal,
                trigger_price,
                break_even_price,
                timestamp,
            )
            published += 1

        if published:
            self.logger.info(
                "TRIX_BREAK_EVEN_BACKFILL published=%s",
                published,
            )
        return published

    def monitor_open_signals(self) -> int:
        self._backfill_pending_break_even_notifications()
        signals = self.fetch_open_signals()
        closed = 0
        for signal in signals:
            closed += 1 if self.check_signal(signal) else 0
        if signals:
            self.logger.info(
                "TRIX_POSITION_MONITOR checked=%s closed=%s",
                len(signals),
                closed,
            )
        return closed
