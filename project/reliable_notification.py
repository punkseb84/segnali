"""Telegram notification engine that prevents invisible signals from blocking the scanner."""
from __future__ import annotations

import os
from typing import Any

import project.notification_engine.service as notification_service
from project.notification_engine.service import NotificationEngine
from project.shared.events import Event, EventType


class ReliableNotificationEngine(NotificationEngine):
    """Persist delivery outcomes and enrich final reports with the rolling paper budget."""

    def _attach_budget_report(self, payload: dict[str, Any]) -> None:
        if self.postgres is None or payload.get("partial_take_profit"):
            return
        result = float(payload.get("realized_net_eur") or 0.0)
        strategy = str(payload.get("strategy") or "").strip()
        try:
            initial_budget = float(os.getenv("PAPER_INITIAL_BUDGET_EUR", "100"))
        except (TypeError, ValueError):
            initial_budget = 100.0
        try:
            if strategy:
                rows = self.postgres.fetch_all(
                    """SELECT COALESCE(SUM(
                               COALESCE(net_profit_tp1_eur, 0)
                               - COALESCE(net_loss_sl_eur, 0)
                           ), 0)
                       FROM signals.generated_signals
                       WHERE closed_at IS NOT NULL
                         AND strategy = %s""",
                    (strategy,),
                )
            else:
                rows = self.postgres.fetch_all(
                    """SELECT COALESCE(SUM(
                               COALESCE(net_profit_tp1_eur, 0)
                               - COALESCE(net_loss_sl_eur, 0)
                           ), 0)
                       FROM signals.generated_signals
                       WHERE closed_at IS NOT NULL"""
                )
            cumulative = float(rows[0][0] or 0.0) if rows else result
        except Exception as exc:
            self.logger.warning(
                "Budget aggregation failed; using current result only error=%s",
                exc,
            )
            cumulative = result
        budget_after = initial_budget + cumulative
        payload["budget_before_eur"] = budget_after - result
        payload["budget_after_eur"] = budget_after

    def _compact_break_even_message(self, payload: dict[str, Any]) -> str:
        signal_id = payload.get("signal_id") or "n/d"
        pair = str(payload.get("pair") or "n/d")
        timeframe = str(payload.get("timeframe") or "1h")
        entry: float | None = None
        if self.postgres is not None and signal_id != "n/d":
            try:
                rows = self.postgres.fetch_all(
                    """SELECT entry
                       FROM signals.generated_signals
                       WHERE id = %s
                       LIMIT 1""",
                    (int(signal_id),),
                )
                if rows and rows[0][0] is not None:
                    entry = float(rows[0][0])
            except Exception as exc:
                self.logger.warning(
                    "Break-even compact lookup failed signal_id=%s error=%s",
                    signal_id,
                    exc,
                )

        configured_stop = payload.get("break_even_price")
        try:
            break_even_price = (
                float(configured_stop)
                if configured_stop is not None
                else entry
            )
        except (TypeError, ValueError):
            break_even_price = entry

        if break_even_price is not None:
            stop_line = f"Stop spostato a: <b>{break_even_price:.8f}</b>\n"
        else:
            stop_line = "Stop spostato al <b>pareggio configurato</b>\n"

        trigger_label = str(payload.get("trigger_label") or "+1 ATR")
        next_objective = str(payload.get("next_objective") or "TP1 50% a +2 ATR")
        net_note = (
            "Il livello copre i costi stimati e include il buffer configurato.\n"
            if payload.get("break_even_price") is not None
            else ""
        )
        return (
            "🟡 <b>BREAK EVEN</b> "
            f"<code>#{signal_id}</code>\n"
            f"<b>{pair}</b> · {timeframe}\n\n"
            f"{stop_line}"
            f"Condizione: <b>{trigger_label}</b>\n"
            f"{net_note}"
            "Posizione aperta: <b>100%</b>\n"
            f"Prossimo obiettivo: <b>{next_objective}</b>"
        )

    def handle_event(self, event: Event) -> None:
        payload = dict(event.payload)
        reply_to_message_id: int | None = None

        if event.type in {EventType.STOP_LOSS, EventType.TARGET_HIT}:
            reference = self.fetch_signal_reference(payload.get("signal_id"))
            payload.update(reference)
            reply_to_message_id = reference.get("telegram_message_id")
            self._attach_budget_report(payload)

        if (
            event.type == EventType.REPORT_READY
            and payload.get("management_update") == "BREAK_EVEN_ARMED"
        ):
            payload["message"] = self._compact_break_even_message(payload)
            payload["trusted_html"] = True

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

        if (
            event.type == EventType.REPORT_READY
            and payload.get("management_update") == "BREAK_EVEN_ARMED"
        ):
            if result:
                self.mark_break_even_notification_sent(payload.get("signal_id"))
            else:
                self.logger.warning(
                    "Break-even Telegram delivery failed; retry remains pending signal_id=%s",
                    payload.get("signal_id"),
                )
            return

        if event.type != EventType.NEW_SIGNAL:
            return
        if result:
            self.save_signal_reference(payload.get("signal_id"), result)
        else:
            self.mark_signal_delivery_failed(payload.get("signal_id"), "TELEGRAM_SEND_FAILED")

    def mark_break_even_notification_sent(self, signal_id: Any) -> None:
        if self.postgres is None or signal_id is None:
            return
        try:
            self.postgres.execute(
                """UPDATE signals.generated_signals
                   SET score_breakdown = COALESCE(score_breakdown, '{}'::jsonb)
                       || jsonb_build_object(
                           'breakeven_notification_sent', TRUE,
                           'breakeven_notification_sent_at', NOW()::text
                       )
                   WHERE id = %s
                     AND status IN ('NEW', 'OPEN')""",
                (int(signal_id),),
            )
            self.logger.info("Break-even Telegram delivery persisted signal_id=%s", signal_id)
        except Exception as exc:
            self.logger.error(
                "Break-even Telegram delivery persistence failed signal_id=%s error=%s",
                signal_id,
                exc,
            )

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
