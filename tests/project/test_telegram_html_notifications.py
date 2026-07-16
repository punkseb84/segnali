from __future__ import annotations

from typing import Any

from project.notification_engine.formatters import (
    format_outcome_message,
    format_signal_message,
    format_watchlist_message,
)
from project.notification_engine.service import NotificationEngine
from project.shared.events import Event, EventBus, EventType


def signal_payload() -> dict[str, Any]:
    return {
        "signal_id": 123,
        "signal_class": "B",
        "strategy": "Compression Breakout",
        "pair": "BTC/USD",
        "timeframe": "15m",
        "regime": "COMPRESSION",
        "entry": 64210.50,
        "stop_loss": 63950.10,
        "take_profit": 64680.90,
        "technical_take_profit": 64680.90,
        "effective_take_profit": 64680.90,
        "signal_time": "2026-07-16T08:00:00Z",
        "reference_candle_time": "2026-07-16T07:45:00Z",
        "quantity": 0.00155,
        "entry_notional_eur": 100.0,
        "gross_rr": 1.8,
        "net_profit_tp1_eur": 0.52,
        "net_loss_sl_eur": 0.42,
        "net_rr": 1.24,
        "historical_sample_size": 90,
        "historical_expected_value_eur": 0.11,
        "probability_confidence": "HIGH",
        "validation_status": "PASSED",
        "estimated_buy_fee_eur": 0.10,
        "estimated_sell_fee_eur": 0.10,
        "estimated_sell_fee_sl_eur": 0.10,
        "estimated_spread_cost_eur": 0.05,
        "estimated_slippage_cost_eur": 0.0,
        "score": 67.4,
        "probability": 0.58,
        "reasons": [
            "profit_factor=1.2400",
            "expectancy=0.080000",
            "regime=COMPRESSION",
            "regime_aligned=true",
            "score_breakdown={\"too\": \"long\"}",
        ],
    }


class FakePostgres:
    def __init__(self) -> None:
        self.reference: tuple[int, str, str | None] | None = None
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params=None) -> None:
        values = tuple(params or ())
        self.executed.append((sql, values))
        if "telegram_message_id" in sql and len(values) == 4:
            self.reference = (int(values[0]), str(values[1]), values[2])

    def fetch_all(self, sql: str, params=None):
        if "SELECT telegram_message_id" in sql and self.reference:
            return [self.reference]
        return []


class FakeTelegramResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return {
            "ok": True,
            "result": {
                "message_id": 321,
                "chat": {"id": -1001234567890, "type": "supergroup"},
            },
        }


def test_signal_formatter_is_html_and_readable() -> None:
    message = format_signal_message(signal_payload())

    assert "<b>NUOVO SEGNALE LONG</b>" in message
    assert "Livelli operativi" in message
    assert "Rischio e rendimento" in message
    assert "Score: <b>67.4/100</b>" in message
    assert "score breakdown" not in message.lower()
    assert len(message) < 3900


def test_watchlist_is_visually_distinct_and_explicitly_not_operative() -> None:
    message = format_watchlist_message(signal_payload() | {"signal_class": "C"})

    assert "🟡 <b>WATCHLIST</b>" in message
    assert "NON È UN SEGNALE DI INGRESSO" in message
    assert "Solo monitoraggio" in message


def test_outcome_id_links_to_original_signal() -> None:
    message = format_outcome_message(
        signal_payload()
        | {
            "outcome": "TARGET_HIT",
            "outcome_price": 64680.90,
            "closed_at": "2026-07-16T08:30:00Z",
            "telegram_message_link": "https://t.me/c/1234567890/321",
        }
    )

    assert '<a href="https://t.me/c/1234567890/321"><b>#123</b></a>' in message
    assert "TARGET RAGGIUNTO" in message
    assert "+€0.52" in message


def test_notification_saves_signal_message_and_replies_on_outcome(monkeypatch) -> None:
    sent_payloads: list[dict[str, Any]] = []

    def fake_post(url: str, json: dict[str, Any], timeout: int):
        sent_payloads.append(json)
        return FakeTelegramResponse()

    monkeypatch.setattr("project.notification_engine.service.requests.post", fake_post)
    postgres = FakePostgres()
    notification = NotificationEngine(
        EventBus(),
        telegram_token="token",
        telegram_chat_id="-1001234567890",
        enabled=True,
        postgres=postgres,
    )

    notification.handle_event(Event(EventType.NEW_SIGNAL, signal_payload()))

    assert sent_payloads[0]["parse_mode"] == "HTML"
    assert "reply_parameters" not in sent_payloads[0]
    assert postgres.reference == (
        321,
        "-1001234567890",
        "https://t.me/c/1234567890/321",
    )

    notification.handle_event(
        Event(
            EventType.STOP_LOSS,
            signal_payload()
            | {
                "outcome": "STOP_LOSS",
                "outcome_price": 63950.10,
                "closed_at": "2026-07-16T08:45:00Z",
                "ambiguous": False,
            },
        )
    )

    outcome_request = sent_payloads[1]
    assert outcome_request["parse_mode"] == "HTML"
    assert outcome_request["reply_parameters"]["message_id"] == 321
    assert "https://t.me/c/1234567890/321" in outcome_request["text"]
    assert "STOP LOSS RAGGIUNTO" in outcome_request["text"]


def test_private_chat_still_uses_reply_even_without_direct_link(monkeypatch) -> None:
    sent_payloads: list[dict[str, Any]] = []

    class PrivateResponse(FakeTelegramResponse):
        def json(self) -> dict[str, Any]:
            return {
                "ok": True,
                "result": {"message_id": 55, "chat": {"id": 987654321, "type": "private"}},
            }

    def fake_post(url: str, json: dict[str, Any], timeout: int):
        sent_payloads.append(json)
        return PrivateResponse()

    monkeypatch.setattr("project.notification_engine.service.requests.post", fake_post)
    postgres = FakePostgres()
    notification = NotificationEngine(
        EventBus(),
        telegram_token="token",
        telegram_chat_id="987654321",
        enabled=True,
        postgres=postgres,
    )

    notification.handle_event(Event(EventType.NEW_SIGNAL, signal_payload()))
    assert postgres.reference == (55, "987654321", None)

    notification.handle_event(
        Event(
            EventType.TARGET_HIT,
            signal_payload()
            | {
                "outcome": "TARGET_HIT",
                "outcome_price": 64680.90,
                "closed_at": "2026-07-16T08:30:00Z",
                "ambiguous": False,
            },
        )
    )

    assert sent_payloads[1]["reply_parameters"]["message_id"] == 55
    assert "<code>#123</code>" in sent_payloads[1]["text"]
