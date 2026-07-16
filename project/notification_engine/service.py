"""Notification Engine: format and dispatch Telegram events without trading decisions."""
from __future__ import annotations

import html
import os
import re
import time
from typing import Any

import requests

from project.database.postgres import PostgresClient, PostgresConfig
from project.notification_engine.formatters import (
    format_outcome_message,
    format_report_message,
    format_signal_message,
    format_watchlist_message,
)
from project.shared.events import Event, EventBus, EventType
from project.shared.logging import get_module_logger


class NotificationEngine:
    def __init__(
        self,
        event_bus: EventBus,
        telegram_token: str | None = None,
        telegram_chat_id: str | None = None,
        enabled: bool = False,
        max_message_length: int = 3900,
        max_retries: int = 3,
        postgres: PostgresClient | None = None,
    ) -> None:
        self.event_bus = event_bus
        self.telegram_token = telegram_token if telegram_token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = telegram_chat_id if telegram_chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = enabled
        self.max_message_length = max(500, max_message_length)
        self.max_retries = max(1, max_retries)
        self.postgres = postgres or self._postgres_from_environment()
        self.logger = get_module_logger("notification")

    @staticmethod
    def _postgres_from_environment() -> PostgresClient | None:
        database_url = os.getenv("DATABASE_URL", "")
        return PostgresClient(PostgresConfig(database_url)) if database_url else None

    def subscribe(self) -> None:
        self.event_bus.subscribe(EventType.NEW_SIGNAL, self.handle_event)
        self.event_bus.subscribe(EventType.WATCHLIST, self.handle_event)
        self.event_bus.subscribe(EventType.STOP_LOSS, self.handle_event)
        self.event_bus.subscribe(EventType.TARGET_HIT, self.handle_event)
        self.event_bus.subscribe(EventType.REPORT_READY, self.handle_event)
        self.logger.info(
            "Notification Engine subscribed enabled=%s telegram_configured=%s html=true signal_links=true",
            self.enabled,
            bool(self.telegram_token and self.telegram_chat_id),
        )

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
            message = format_report_message(payload)

        if not self.enabled:
            self.logger.info("Notification skipped disabled event=%s message_length=%s", event.type, len(message))
            return

        result = self.send_telegram(message, reply_to_message_id=reply_to_message_id)
        if event.type == EventType.NEW_SIGNAL and result:
            self.save_signal_reference(payload.get("signal_id"), result)

    def format_signal(self, payload: dict[str, Any]) -> str:
        return format_signal_message(payload)

    def format_watchlist(self, payload: dict[str, Any]) -> str:
        return format_watchlist_message(payload)

    def format_outcome(self, payload: dict[str, Any]) -> str:
        return format_outcome_message(payload)

    def split_message(self, message: str) -> list[str]:
        """Split long HTML messages without cutting normal balanced paragraphs."""
        if len(message) <= self.max_message_length:
            return [message]

        chunks: list[str] = []
        current = ""
        for block in message.split("\n\n"):
            candidate = block if not current else f"{current}\n\n{block}"
            if len(candidate) <= self.max_message_length:
                current = candidate
                continue
            if current:
                chunks.append(current)
                current = ""
            if len(block) <= self.max_message_length:
                current = block
                continue

            # Oversized free-text blocks lose styling rather than producing invalid HTML.
            plain = html.unescape(re.sub(r"<[^>]+>", "", block))
            for index in range(0, len(plain), self.max_message_length):
                chunks.append(html.escape(plain[index : index + self.max_message_length]))
        if current:
            chunks.append(current)
        return chunks or [""]

    @staticmethod
    def build_message_link(chat: dict[str, Any], message_id: int) -> str | None:
        username = str(chat.get("username") or "").lstrip("@")
        if username:
            return f"https://t.me/{username}/{message_id}"
        chat_id = str(chat.get("id") or "")
        if chat_id.startswith("-100") and len(chat_id) > 4:
            return f"https://t.me/c/{chat_id[4:]}/{message_id}"
        return None

    def save_signal_reference(self, signal_id: Any, telegram_result: dict[str, Any]) -> None:
        if self.postgres is None or signal_id is None:
            return
        message_id = telegram_result.get("message_id")
        chat = telegram_result.get("chat") or {}
        if message_id is None:
            return
        chat_id = str(chat.get("id") or self.telegram_chat_id)
        message_link = self.build_message_link(chat, int(message_id))
        try:
            self.postgres.execute(
                """UPDATE signals.generated_signals
                SET telegram_message_id = %s,
                    telegram_chat_id = %s,
                    telegram_message_link = %s,
                    telegram_sent_at = NOW()
                WHERE id = %s""",
                (int(message_id), chat_id, message_link, int(signal_id)),
            )
            self.logger.info(
                "Telegram signal reference saved signal_id=%s message_id=%s link_available=%s",
                signal_id,
                message_id,
                bool(message_link),
            )
        except Exception as exc:  # Notification delivery must not crash the platform.
            self.logger.warning("Telegram signal reference save failed signal_id=%s error=%s", signal_id, exc)

    def fetch_signal_reference(self, signal_id: Any) -> dict[str, Any]:
        if self.postgres is None or signal_id is None:
            return {}
        try:
            rows = self.postgres.fetch_all(
                """SELECT telegram_message_id, telegram_chat_id, telegram_message_link
                FROM signals.generated_signals
                WHERE id = %s
                LIMIT 1""",
                (int(signal_id),),
            )
        except Exception as exc:
            self.logger.warning("Telegram signal reference lookup failed signal_id=%s error=%s", signal_id, exc)
            return {}
        if not rows:
            return {}
        message_id, chat_id, message_link = rows[0]
        if message_id is None:
            return {}
        if not message_link and str(chat_id or "").startswith("-100"):
            message_link = self.build_message_link({"id": chat_id}, int(message_id))
        return {
            "telegram_message_id": int(message_id),
            "telegram_chat_id": str(chat_id or ""),
            "telegram_message_link": message_link,
        }

    def send_telegram(self, message: str, reply_to_message_id: int | None = None) -> dict[str, Any] | None:
        if not self.telegram_token or not self.telegram_chat_id:
            self.logger.warning("Telegram notification skipped: token/chat_id missing")
            return None

        first_result: dict[str, Any] | None = None
        for part_index, part in enumerate(self.split_message(message)):
            request_payload: dict[str, Any] = {
                "chat_id": self.telegram_chat_id,
                "text": part,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_to_message_id is not None:
                request_payload["reply_parameters"] = {
                    "message_id": int(reply_to_message_id),
                    "allow_sending_without_reply": True,
                }

            for attempt in range(1, self.max_retries + 1):
                try:
                    response = requests.post(
                        f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                        json=request_payload,
                        timeout=15,
                    )
                    response.raise_for_status()
                    response_data = response.json()
                    if not response_data.get("ok", True):
                        raise requests.RequestException(str(response_data))
                    result = response_data.get("result") or {}
                    if part_index == 0 and result:
                        first_result = result
                    self.logger.info(
                        "Telegram notification sent html=true reply=%s part=%s",
                        reply_to_message_id is not None,
                        part_index + 1,
                    )
                    break
                except (requests.RequestException, ValueError) as exc:
                    status_code = getattr(getattr(exc, "response", None), "status_code", None)
                    response_text = getattr(getattr(exc, "response", None), "text", "")
                    self.logger.warning(
                        "Telegram notification failed attempt=%s/%s status=%s response=%s error=%s",
                        attempt,
                        self.max_retries,
                        status_code,
                        response_text,
                        exc,
                    )
                    if attempt == self.max_retries:
                        return first_result
                    time.sleep(min(2 ** (attempt - 1), 5))
        return first_result
