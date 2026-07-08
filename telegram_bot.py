"""Telegram Bot API helpers."""
from __future__ import annotations

import logging
from typing import Any

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger("crypto-bot.telegram")


def send_message(text: str, reply_to_message_id: int | None = None) -> dict[str, Any] | None:
    """Send a Telegram message and return Telegram result when available."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("Telegram credentials missing; message not sent: %s", text[:120])
        return None
    payload: dict[str, Any] = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id
    try:
        response = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json=payload, timeout=20)
        if not response.ok:
            logger.error("Telegram send failed: status=%s response=%s", response.status_code, response.text)
            return None
        data = response.json()
        return data.get("result")
    except requests.RequestException as exc:
        logger.error("Telegram send failed: %s", exc)
        return None


def test_telegram() -> bool:
    return send_message("✅ Test Telegram OK: crypto bot collegato.") is not None
