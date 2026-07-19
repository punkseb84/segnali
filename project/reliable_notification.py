"""Telegram notification engine that prevents invisible signals from blocking the scanner."""
from __future__ import annotations

from typing import Any

import project.notification_engine.service as notification_service
from project.notification_engine.service import NotificationEngine
from project.shared.events import Event, EventType


class ReliableNotificationEngine(NotificationEngine):
    """Persist delivery failure so an unseen signal is never treated as an open exposure."""

    def handle_event(self, event: Event) -> None:
        payload = dict(event.payload)
        reply_to_message_id: int | None = None

        if event.type in {EventType.STOP_LOSS, EventType.TARGET_HIT}:
            reference = self.fetch_signal_reference(payload.get("signal_id"))
            payload.update(reference)
            reply_to_message_id = reference.get("telegram_message_id")

        if event.type == EventType.NEW_SIGNAL:
            message = self.format_signal(payload)
        elif event.type == EventType.WATCHLIST:
            message = self.format_watchlist(payload)
        elif event.type in {EventType.STOP_LOSS, EventType.TARGET_HIT}:
            message = self.format_outcome(payload)
        else:
            message = notification_service.format_report_message(payload)

        if not self.enabled:
            self.logger.info(
                "Notification skipped disabled event=%s message_length=%s",
                event.type,
                len(message),
            )
            if event.type == EventType.NEW_SIGNAL:
                self.mark_signal_delivery_failed(payload.get("signal_id"), "ENGINE_DISABLED")
            return

        result = self.send_telegram(message, reply_to_message_id=reply_to_message_id)
        if event.type != EventType.NEW_SIGNAL:
            return
        if result:
            self.save_signal_reference(payload.get("signal_id"), result)
        else:
            self.mark_signal_delivery_failed(payload.get("signal_id"), "TELEGRAM_SEND_FAILED")

    def mark_signal_delivery_failed(self, signal_id: Any, reason: str) -> None:
        if self.postgres is None or signal_id is None:
            return
        try:
            self.postgres.execute(
                """UPDATE signals.generated_signals
                SET status = 'DELIVERY_FAILED',
                    closed_at = COALESCE(closed_at, NOW()),
                    outcome_resolution = %s
                WHERE id = %s
                  AND status IN ('NEW', 'OPEN')
                  AND telegram_sent_at IS NULL""",
                (reason, int(signal_id)),
            )
            self.logger.warning(
                "Telegram signal marked delivery_failed signal_id=%s reason=%s",
                signal_id,
                reason,
            )
        except Exception as exc:
            self.logger.error(
                "Telegram delivery failure persistence failed signal_id=%s error=%s",
                signal_id,
                exc,
            )
