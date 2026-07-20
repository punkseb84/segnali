"""Telegram formatters for V6.1 daily paper and admitted live signals."""
from __future__ import annotations

from typing import Any

from project.audited_formatters import format_audited_outcome_message
from project.notification_engine.simple_formatters import format_simple_signal_message


PAPER_EXECUTION_MODE = "PAPER_DAILY"
LIVE_EXECUTION_MODE = "LIVE_ADMITTED"


def _execution_mode(payload: dict[str, Any]) -> str:
    breakdown = payload.get("score_breakdown") or {}
    return str(
        payload.get("execution_mode")
        or breakdown.get("execution_mode")
        or ""
    )


def format_daily_paper_signal_message(payload: dict[str, Any]) -> str:
    message = format_simple_signal_message(payload)
    mode = _execution_mode(payload)
    if mode == PAPER_EXECUTION_MODE:
        message = message.replace(
            "🟢 <b>SEGNALE LONG</b>",
            "🧪 <b>SEGNALE LONG · PAPER</b>",
            1,
        )
        marker = (
            "\n🧪 <b>Modalità</b>\n"
            "• PAPER DAILY: operazione simulata e monitorata automaticamente\n"
            "• Non usare denaro reale: edge ancora in validazione\n"
        )
        message = message.replace("\n💰 <b>Livelli operativi</b>", marker + "\n💰 <b>Livelli operativi</b>", 1)
        message = message.replace(
            "⚠️ Segnale sperimentale in paper trading: nessun metodo garantisce profitto.",
            "⚠️ PAPER TRADING: il segnale costruisce lo storico pubblico del sistema e non è una garanzia di profitto.",
        )
    elif mode == LIVE_EXECUTION_MODE:
        message = message.replace(
            "🟢 <b>SEGNALE LONG</b>",
            "✅ <b>SEGNALE LONG · EDGE VALIDATO</b>",
            1,
        )
    return message


def format_daily_paper_outcome_message(payload: dict[str, Any]) -> str:
    message = format_audited_outcome_message(payload)
    mode = _execution_mode(payload)
    if mode == PAPER_EXECUTION_MODE:
        prefix = "🧪 <b>ESITO PAPER · NESSUNA OPERAZIONE REALE</b>\n\n"
        return prefix + message
    if mode == LIVE_EXECUTION_MODE:
        return "✅ <b>ESITO SEGNALE VALIDATO</b>\n\n" + message
    return message
