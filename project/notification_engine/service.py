"""Notification Engine: dispatch events without making trading decisions."""
from __future__ import annotations

import os
import time
from typing import Any

import requests

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
    ) -> None:
        self.event_bus = event_bus
        self.telegram_token = telegram_token if telegram_token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = telegram_chat_id if telegram_chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = enabled
        self.max_message_length = max(500, max_message_length)
        self.max_retries = max(1, max_retries)
        self.logger = get_module_logger("notification")

    def subscribe(self) -> None:
        self.event_bus.subscribe(EventType.NEW_SIGNAL, self.handle_event)
        self.event_bus.subscribe(EventType.WATCHLIST, self.handle_event)
        self.event_bus.subscribe(EventType.STOP_LOSS, self.handle_event)
        self.event_bus.subscribe(EventType.TARGET_HIT, self.handle_event)
        self.event_bus.subscribe(EventType.REPORT_READY, self.handle_event)
        self.logger.info("Notification Engine subscribed enabled=%s telegram_configured=%s", self.enabled, bool(self.telegram_token and self.telegram_chat_id))

    def handle_event(self, event: Event) -> None:
        if event.type == EventType.NEW_SIGNAL:
            message = self.format_signal(event.payload)
        elif event.type == EventType.WATCHLIST:
            message = self.format_watchlist(event.payload)
        elif event.type in {EventType.STOP_LOSS, EventType.TARGET_HIT}:
            message = self.format_outcome(event.payload)
        else:
            message = str(event.payload.get("message", event.payload))
        if not self.enabled:
            self.logger.info("Notification skipped disabled event=%s message=%s", event.type, message)
            return
        self.send_telegram(message)

    def format_signal(self, payload: dict[str, Any]) -> str:
        probability = payload.get("probability")
        probability_text = "non disponibile" if probability is None else f"{probability:.2%}"
        return (
            "🟢 NEW SIGNAL\n"
            f"ID: {payload.get('signal_id')}\n"
            f"Class: {payload.get('signal_class', 'B')}\n"
            f"Strategy: {payload.get('strategy')}\n"
            f"Pair: {payload.get('pair')} {payload.get('timeframe')}\n"
            f"Regime: {payload.get('regime')}\n"
            f"Entry: {payload.get('entry'):.6f}\n"
            f"Entry minute: {payload.get('signal_time')}\n"
            f"Entry timing: entra immediatamente alla ricezione del segnale\n"
            f"Candela riferimento: {payload.get('reference_candle_time')}\n"
            f"Quantity: {payload.get('quantity'):.8f}\n"
            f"Stop: {payload.get('stop_loss'):.6f}\n"
            f"Stop distance: {payload.get('stop_distance'):.6f} USD / {payload.get('stop_pct'):.3f}%\n"
            f"Technical TP: {payload.get('technical_take_profit'):.6f}\n"
            f"Effective TP: {payload.get('effective_take_profit'):.6f}\n"
            f"ATR {payload.get('timeframe')}: {payload.get('atr'):.6f}\n"
            f"Stop/ATR: {payload.get('stop_atr_ratio'):.2f}\n"
            f"TP/ATR: {payload.get('tp_atr_ratio'):.2f}\n"
            f"Gross R/R: {payload.get('gross_rr'):.2f}\n"
            f"Net profit TP1: €{payload.get('net_profit_tp1_eur'):.4f}\n"
            f"Net loss SL: €{payload.get('net_loss_sl_eur'):.4f}\n"
            f"Net R/R: {payload.get('net_rr'):.2f}\n"
            f"Historical sample: {payload.get('historical_sample_size')}\n"
            f"Historical win rate: {payload.get('historical_win_rate') if payload.get('historical_win_rate') is not None else 'non disponibile'}\n"
            f"Historical EV: {payload.get('historical_expected_value_eur') if payload.get('historical_expected_value_eur') is not None else 'non disponibile'}\n"
            f"Probability: {probability_text}\n"
            f"Probability confidence: {payload.get('probability_confidence')}\n"
            f"Validation: {payload.get('validation_status')}\n"
            f"Costi stimati Binance: buy fee €{payload.get('estimated_buy_fee_eur'):.4f}, sell fee TP €{payload.get('estimated_sell_fee_eur'):.4f}, sell fee SL €{payload.get('estimated_sell_fee_sl_eur'):.4f}, spread €{payload.get('estimated_spread_cost_eur'):.4f}, slippage €{payload.get('estimated_slippage_cost_eur'):.4f}\n"
            f"Score: {payload.get('score'):.2f}\n"
            f"Reasons: {', '.join(payload.get('reasons', []))}"
        )

    def format_watchlist(self, payload: dict[str, Any]) -> str:
        return (
            "👀 WATCHLIST – NON È UN SEGNALE DI INGRESSO\n"
            f"ID: {payload.get('signal_id')}\n"
            f"Class: {payload.get('signal_class', 'C')}\n"
            f"Strategy: {payload.get('strategy')}\n"
            f"Pair: {payload.get('pair')} {payload.get('timeframe')}\n"
            f"Regime: {payload.get('regime')}\n"
            f"Entry teorica: {payload.get('entry'):.6f}\n"
            f"Stop teorico: {payload.get('stop_loss'):.6f}\n"
            f"Target teorico: {payload.get('take_profit'):.6f}\n"
            f"Historical EV: {payload.get('historical_expected_value_eur') if payload.get('historical_expected_value_eur') is not None else 'non disponibile'}\n"
            f"Confidenza: {payload.get('probability_confidence')}\n"
            f"Score: {payload.get('score'):.2f}\n"
            f"Motivi/declassamento: {', '.join(payload.get('reasons', []))}"
        )

    def format_outcome(self, payload: dict[str, Any]) -> str:
        is_stop = payload.get("outcome") == "STOP_LOSS"
        title = "🛑 Stop Loss raggiunto" if is_stop else "✅ Target 1 raggiunto"
        return (
            f"{title}\n"
            f"ID: {payload.get('signal_id')}\n"
            f"Strategy: {payload.get('strategy')}\n"
            f"Pair: {payload.get('pair')} {payload.get('timeframe')}\n"
            f"Entry: {payload.get('entry'):.6f}\n"
            f"Outcome price: {payload.get('outcome_price'):.6f}\n"
            f"Closed at: {payload.get('closed_at')}\n"
            f"Ambiguous candle: {payload.get('ambiguous')}"
        )

    def split_message(self, message: str) -> list[str]:
        return [message[index:index + self.max_message_length] for index in range(0, len(message), self.max_message_length)] or [""]

    def send_telegram(self, message: str) -> None:
        if not self.telegram_token or not self.telegram_chat_id:
            self.logger.warning("Telegram notification skipped: token/chat_id missing")
            return
        for part in self.split_message(message):
            for attempt in range(1, self.max_retries + 1):
                try:
                    response = requests.post(
                        f"https://api.telegram.org/bot{self.telegram_token}/sendMessage",
                        json={"chat_id": self.telegram_chat_id, "text": part},
                        timeout=15,
                    )
                    response.raise_for_status()
                    self.logger.info("Telegram notification sent")
                    break
                except requests.RequestException as exc:
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
                        return
                    time.sleep(min(2 ** (attempt - 1), 5))
