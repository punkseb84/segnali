"""Outcome formatter that exposes the real candle used for TP/SL resolution."""
from __future__ import annotations

import html
from typing import Any

from project.notification_engine.simple_formatters import (
    format_simple_report_message,
    format_simple_signal_message,
)


def _escape(value: Any) -> str:
    return html.escape(str(value if value is not None else "n/d"), quote=True)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _price(value: Any) -> str:
    number = _float(value)
    absolute = abs(number)
    decimals = 2 if absolute >= 1000 else 3 if absolute >= 1 else 5 if absolute >= 0.01 else 8
    return f"{number:,.{decimals}f}"


def format_audited_outcome_message(payload: dict[str, Any]) -> str:
    is_stop = str(payload.get("outcome")) == "STOP_LOSS"
    title = "🔴 <b>STOP LOSS RAGGIUNTO</b>" if is_stop else "🎯 <b>TAKE PROFIT RAGGIUNTO</b>"
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    result = -abs(_float(payload.get("net_loss_sl_eur"))) if is_stop else _float(payload.get("net_profit_tp1_eur"))
    open_price = payload.get("outcome_open_price")
    high = payload.get("outcome_high_price")
    low = payload.get("outcome_low_price")
    close_price = payload.get("close_price")
    actual_extreme = (
        f"• Minimo reale: <b>{_price(low)}</b>\n"
        if is_stop and low is not None
        else f"• Massimo reale: <b>{_price(high)}</b>\n"
        if not is_stop and high is not None
        else ""
    )
    candle_block = ""
    if all(value is not None for value in (open_price, high, low, close_price)):
        candle_block = (
            "\n📐 <b>Candela reale che ha chiuso il trade</b>\n"
            f"• Open: <b>{_price(open_price)}</b>\n"
            f"• High: <b>{_price(high)}</b>\n"
            f"• Low: <b>{_price(low)}</b>\n"
            f"• Close: <b>{_price(close_price)}</b>\n"
        )
    ambiguous = bool(payload.get("ambiguous"))
    ambiguity_line = (
        "• Candela ambigua: <b>SÌ · TP e SL toccati nella stessa candela</b>\n"
        if ambiguous
        else "• Candela ambigua: <b>NO</b>\n"
    )
    return (
        f"{title}\n"
        f"🆔 <code>#{_escape(signal_id)}</code>\n\n"
        f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n"
        f"Strategia: <b>{_escape(payload.get('strategy'))}</b>\n"
        f"Exchange: <b>{_escape(payload.get('exchange'))}</b>\n\n"
        "📍 <b>Esito</b>\n"
        f"• Entry: <b>{_price(payload.get('entry'))}</b>\n"
        f"• Livello operativo: <b>{_price(payload.get('outcome_price'))}</b>\n"
        f"{actual_extreme}"
        f"• Risultato netto stimato: <b>€{result:+.2f}</b>\n"
        f"• R/R iniziale: <b>{_float(payload.get('net_rr')):.2f}</b>\n"
        f"{candle_block}"
        f"{ambiguity_line}"
        f"• Candela di chiusura: <b>{_escape(payload.get('closed_at'))}</b>\n"
        f"• Risoluzione: <b>{_escape(payload.get('outcome_resolution'))}</b>"
    )


format_simple_outcome_message = format_audited_outcome_message
