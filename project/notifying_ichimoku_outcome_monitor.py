"""Ichimoku outcome monitor with one-time Telegram break-even updates."""
from __future__ import annotations

import json
from typing import Any

from project.ichimoku_outcome_monitor import IchimokuOutcomeMonitor
from project.shared.events import Event, EventType


class NotifyingIchimokuOutcomeMonitor(IchimokuOutcomeMonitor):
    """Publish Telegram updates for new and previously armed break-even states."""

    def _publish_break_even_update(
        self,
        *,
        signal_id: Any,
        pair: str,
        timeframe: str,
        entry: float,
        timestamp: Any,
        backfilled: bool = False,
    ) -> None:
        note = (
            "\n• Notifica recuperata dopo aggiornamento del bot"
            if backfilled
            else ""
        )
        message = (
            "🟡 <b>STOP SPOSTATO A BREAK EVEN</b>\n"
            f"🆔 <code>#{signal_id}</code>\n\n"
            f"<b>{pair}</b> · {timeframe}\n"
            f"• Entry: <b>{entry:.8f}</b>\n"
            f"• Nuovo Stop Loss: <b>{entry:.8f}</b>\n"
            "• Condizione raggiunta: <b>+1 ATR</b>\n"
            f"• Candela di conferma: <b>{timestamp}</b>{note}\n\n"
            "La posizione resta aperta. Il prossimo obiettivo è TP1 a +2 ATR."
        )
        self.event_bus.publish(
            Event(
                EventType.REPORT_READY,
                {
                    "message": message,
                    "trusted_html": True,
                    "signal_id": signal_id,
                    "pair": pair,
                    "timeframe": timeframe,
                    "management_update": "BREAK_EVEN_ARMED",
                },
            )
        )
        self.postgres.execute(
            """UPDATE signals.generated_signals
            SET score_breakdown = COALESCE(score_breakdown, '{}'::jsonb)
                || %s::jsonb
            WHERE id = %s""",
            (
                json.dumps(
                    {
                        "breakeven_notification_sent": True,
                        "breakeven_notification_sent_at": str(timestamp),
                    }
                ),
                signal_id,
            ),
        )
        self.logger.info(
            "ICHIMOKU_BREAK_EVEN_NOTIFICATION_PUBLISHED id=%s pair=%s backfilled=%s",
            signal_id,
            pair,
            backfilled,
        )

    def _backfill_pending_break_even_notifications(self) -> int:
        rows = self.postgres.fetch_all(
            """SELECT id, pair, timeframe, entry,
                      score_breakdown->>'breakeven_armed_at'
            FROM signals.generated_signals
            WHERE status IN ('NEW', 'OPEN')
              AND COALESCE((score_breakdown->>'breakeven_armed')::boolean, FALSE) = TRUE
              AND COALESCE((score_breakdown->>'breakeven_notification_sent')::boolean, FALSE) = FALSE
            ORDER BY created_at ASC
            LIMIT %s""",
            (self.max_signals_per_cycle,),
        )
        sent = 0
        for signal_id, pair, timeframe, entry, armed_at in rows:
            self._publish_break_even_update(
                signal_id=signal_id,
                pair=str(pair or "n/d"),
                timeframe=str(timeframe or "1h"),
                entry=float(entry),
                timestamp=armed_at or "già registrato nel database",
                backfilled=True,
            )
            sent += 1
        return sent

    def monitor_open_signals(self) -> int:
        backfilled = self._backfill_pending_break_even_notifications()
        closed = super().monitor_open_signals()
        if backfilled:
            self.logger.info(
                "ICHIMOKU_BREAK_EVEN_BACKFILL completed notifications=%s",
                backfilled,
            )
        return closed

    def _arm_break_even(
        self,
        signal: dict[str, Any],
        state: dict[str, Any],
        timestamp: Any,
    ) -> None:
        was_armed = bool(state.get("breakeven_armed"))
        super()._arm_break_even(signal, state, timestamp)
        if was_armed or bool(state.get("breakeven_notification_sent")):
            return

        entry = float(signal["entry"])
        signal_id = signal.get("id") or signal.get("signal_id") or "n/d"
        pair = str(signal.get("pair") or "n/d")
        timeframe = str(signal.get("timeframe") or "1h")
        self._publish_break_even_update(
            signal_id=signal_id,
            pair=pair,
            timeframe=timeframe,
            entry=entry,
            timestamp=timestamp,
            backfilled=False,
        )
        state.update(
            {
                "breakeven_notification_sent": True,
                "breakeven_notification_sent_at": str(timestamp),
            }
        )
