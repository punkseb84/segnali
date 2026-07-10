"""Notification Engine: dispatch events without making trading decisions."""
from __future__ import annotations

import os
from typing import Any

import requests

from project.shared.events import Event, EventBus, EventType
from project.shared.logging import get_module_logger


class NotificationEngine:
    def __init__(self, event_bus: EventBus, telegram_token: str | None = None, telegram_chat_id: str | None = None, enabled: bool = False) -> None:
        self.event_bus = event_bus
        self.telegram_token = telegram_token if telegram_token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = telegram_chat_id if telegram_chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = enabled
        self.logger = get_module_logger("notification")

    def subscribe(self) -> None:
        self.event_bus.subscribe(EventType.NEW_SIGNAL, self.handle_event)
        self.event_bus.subscribe(EventType.REPORT_READY, self.handle_event)
        self.logger.info("Notification Engine subscribed enabled=%s telegram_configured=%s", self.enabled, bool(self.telegram_token and self.telegram_chat_id))

    def handle_event(self, event: Event) -> None:
        if event.type == EventType.NEW_SIGNAL:
            message = self.format_signal(event.payload)
        else:
            message = str(event.payload.get("message", event.payload))
        if not self.enabled:
            self.logger.info("Notification skipped disabled event=%s message=%s", event.type, message)
            return
        self.send_telegram(message)

    def format_signal(self, payload: dict[str, Any]) -> str:
        return (
            "🟢 NEW SIGNAL\n"
            f"ID: {payload.get('signal_id')}\n"
            f"Strategy: {payload.get('strategy')}\n"
            f"Pair: {payload.get('pair')} {payload.get('timeframe')}\n"
            f"Regime: {payload.get('regime')}\n"
            f"Entry: {payload.get('entry'):.6f}\n"
            f"Stop: {payload.get('stop_loss'):.6f}\n"
            f"Take Profit: {payload.get('take_profit'):.6f}\n"
            f"Score: {payload.get('score'):.2f}\n"
            f"Probability: {payload.get('probability'):.2%}\n"
            f"Reasons: {', '.join(payload.get('reasons', []))}"
        )

    def send_telegram(self, message: str) -> None:
        if not self.telegram_token or not self.telegram_chat_id:
            self.logger.warning("Telegram notification skipped: token/chat_id missing")
            return
        response = requests.post(
            f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
            json={"chat_id": self.telegram_chat_id, "text": message},
            timeout=15,
        )
        response.raise_for_status()
        self.logger.info("Telegram notification sent")
