"""Open trade monitoring with real Kraken OHLC candles."""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from config import TELEGRAM_CHAT_USERNAME
from exchange import fetch_ohlc
from risk import format_price
from telegram_bot import send_message
from trade_store import open_trades, update_outcome

logger = logging.getLogger("crypto-bot.monitor")


def original_signal_reference(trade: dict[str, Any]) -> tuple[str, int | None]:
    """Return a text reference and reply target for the original Telegram signal."""
    message_id = trade.get("signal_telegram_message_id")
    try:
        message_id_int = int(message_id) if message_id is not None else None
    except (TypeError, ValueError):
        message_id_int = None

    if TELEGRAM_CHAT_USERNAME and message_id_int:
        link = f"https://t.me/{TELEGRAM_CHAT_USERNAME}/{message_id_int}"
        return f"\nApri segnale originale: {link}", message_id_int
    if message_id_int:
        return "\nCollegamento: risposta al segnale originale", message_id_int
    return "\nCollegamento: ID segnale non disponibile su Telegram", None


def send_outcome_message(text: str, reply_to_message_id: int | None) -> dict[str, Any] | None:
    """Send outcome as a reply when possible, with a safe fallback."""
    result = send_message(text, reply_to_message_id=reply_to_message_id)
    if result is None and reply_to_message_id is not None:
        logger.warning("Retrying outcome without Telegram reply for message_id=%s", reply_to_message_id)
        result = send_message(text)
    return result


def check_trade(trade: dict[str, Any]) -> None:
    """Close one open LONG trade when TP1 or SL is touched by real OHLC."""
    frame = fetch_ohlc(trade["pair"], trade["timeframe"], limit=120)
    if frame.empty:
        return
    opened = pd.to_datetime(trade.get("timestamp"), utc=True)
    candles = frame[frame["timestamp"] > opened]
    for _, candle in candles.iterrows():
        tp_hit = float(candle["high"]) >= float(trade["target_1"])
        sl_hit = float(candle["low"]) <= float(trade["stop_loss"])
        if not tp_hit and not sl_hit:
            continue
        ambiguous = tp_hit and sl_hit
        if sl_hit:
            result = -abs(float(trade["net_loss_sl_eur"] or 0))
            reference_text, reply_to_message_id = original_signal_reference(trade)
            telegram_result = send_outcome_message(
                "🛑 Stop Loss raggiunto\n"
                f"ID Segnale: {trade['signal_id']}\n"
                f"LONG {trade['pair']}\n"
                f"Entry: {format_price(float(trade['entry']))}\n"
                f"Stop Loss: {format_price(float(trade['stop_loss']))}\n"
                f"Risultato netto: -€{format_price(abs(result))}"
                f"{reference_text}",
                reply_to_message_id,
            )
            update_outcome(
                trade["signal_id"],
                "SL",
                "SL",
                result,
                ambiguous,
                telegram_result.get("message_id") if telegram_result else None,
            )
        else:
            result = float(trade["net_profit_tp1_eur"] or 0)
            reference_text, reply_to_message_id = original_signal_reference(trade)
            telegram_result = send_outcome_message(
                "✅ Target 1 raggiunto\n"
                f"ID Segnale: {trade['signal_id']}\n"
                f"LONG {trade['pair']}\n"
                f"Entry: {format_price(float(trade['entry']))}\n"
                f"Target 1: {format_price(float(trade['target_1']))}\n"
                f"Risultato netto: +€{format_price(result)}"
                f"{reference_text}",
                reply_to_message_id,
            )
            update_outcome(
                trade["signal_id"],
                "TP1",
                "TP1",
                result,
                ambiguous,
                telegram_result.get("message_id") if telegram_result else None,
            )
        logger.info("Closed %s as %s ambiguous=%s", trade["signal_id"], "SL" if sl_hit else "TP1", ambiguous)
        return


def monitor_open_trades() -> None:
    for trade in open_trades():
        check_trade(trade)
