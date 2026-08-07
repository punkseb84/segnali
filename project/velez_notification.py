"""Strict notification guard for the active Velez Mode 2 runtime."""
from __future__ import annotations

from typing import Any

from project.reliable_notification import ReliableNotificationEngine
from project.shared.events import Event, EventType
from project.velez_mode2_scanner import RUNTIME_VERSION


class VelezNotificationEngine(ReliableNotificationEngine):
    @staticmethod
    def _runtime_from_payload(payload: dict[str, Any]) -> str:
        state = payload.get("score_breakdown") or payload.get("state") or {}
        if isinstance(state, dict):
            return str(state.get("runtime_version") or "")
        return ""

    def handle_event(self, event: Event) -> None:
        if event.type == EventType.NEW_SIGNAL:
            payload = dict(event.payload)
            runtime = self._runtime_from_payload(payload)
            if runtime != RUNTIME_VERSION:
                self.logger.error(
                    "VELEZ_NOTIFICATION_BLOCKED signal_id=%s payload_runtime=%s expected_runtime=%s",
                    payload.get("signal_id") or payload.get("id"),
                    runtime or "MISSING",
                    RUNTIME_VERSION,
                )
                self.mark_signal_delivery_failed(
                    payload.get("signal_id") or payload.get("id"),
                    f"RUNTIME_MISMATCH_{runtime or 'MISSING'}",
                )
                return
        super().handle_event(event)
