"""Strict notification guard for the active Velez Mode 2 runtime."""
from __future__ import annotations

from typing import Any

from project.reliable_notification import ReliableNotificationEngine
from project.shared.events import Event, EventType
from project.velez_mode2_scanner import RUNTIME_VERSION


BLOCKED_RESEARCH_REPORT_PREFIXES = (
    "VELEZ_",
    "COST_AWARE_",
    "SUPERTREND_",
    "EDGE_DISCOVERY_",
)
ALLOWED_RESEARCH_REPORT_PREFIX = "CRYPTORESEARCH_V1_CAND000006_"


class VelezNotificationEngine(ReliableNotificationEngine):
    @staticmethod
    def _runtime_from_payload(payload: dict[str, Any]) -> str:
        state = payload.get("score_breakdown") or payload.get("state") or {}
        if isinstance(state, dict):
            return str(state.get("runtime_version") or "")
        return ""

    @staticmethod
    def _is_obsolete_research_report(event: Event) -> bool:
        if event.type != EventType.REPORT_READY:
            return False
        payload = dict(event.payload)
        report_type = str(payload.get("report_type") or "").upper()
        if report_type.startswith(ALLOWED_RESEARCH_REPORT_PREFIX):
            return False
        return any(report_type.startswith(prefix) for prefix in BLOCKED_RESEARCH_REPORT_PREFIXES)

    def handle_event(self, event: Event) -> None:
        if self._is_obsolete_research_report(event):
            report_type = str(dict(event.payload).get("report_type") or "UNKNOWN")
            self.logger.warning("OBSOLETE_RESEARCH_REPORT_BLOCKED report_type=%s", report_type)
            return

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
