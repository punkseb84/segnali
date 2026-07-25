"""Outcome formatter that exposes the real candle used for strategy resolution."""
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
    resolution = str(payload.get("outcome_resolution") or "")
    is_ichimoku_exit = resolution.startswith("ICHIMOKU_")

    if is_stop:
        title = "🔴 <b>STOP LOSS RAGGIUNTO</b>"
    elif is_ichimoku_exit:
        title = "☁️ <b>USCITA ICHIMOKU CONFERMATA</b>"
    else:
        title = "🎯 <b>TAKE PROFIT RAGGIUNTO</b>"

    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    if payload.get("realized_net_eur") is not None:
        result = _float(payload.get("realized_net_eur"))
    else:
        result = -abs(_float(payload.get("net_loss_sl_eur"))) if is_stop else _float(payload.get("net_profit_tp1_eur"))

    open_price = payload.get("outcome_open_price")
    high = payload.get("outcome_high_price")
    low = payload.get("outcome_low_price")
    close_price = payload.get("close_price")
    candle_block = ""
    if all(value is not None for value in (open_price, high, low, close_price)):
        candle_block = (
            "\n📐 <b>Candela reale che ha chiuso il trade</b>\n"
            f"• Open: <b>{_price(open_price)}</b>\n"
            f"• High: <b>{_price(high)}</b>\n"
            f"• Low: <b>{_price(low)}</b>\n"
            f"• Close: <b>{_price(close_price)}</b>\n"
        )

    if resolution == "ICHIMOKU_CLOUD_EXIT":
        resolution_text = "chiusura dentro o sotto la nuvola"
    elif resolution == "ICHIMOKU_TENKAN_KIJUN_EXIT":
        resolution_text = "incrocio Tenkan Sen sotto Kijun Sen"
    elif resolution == "ATR_STOP_ICHIMOKU":
        resolution_text = "stop loss ATR"
    else:
        resolution_text = resolution or "n/d"

    return (
        f"{title}\n"
        f"🆔 <code>#{_escape(signal_id)}</code>\n\n"
        f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n"
        f"Strategia: <b>{_escape(payload.get('strategy'))}</b>\n"
        f"Exchange: <b>{_escape(payload.get('exchange'))}</b>\n\n"
        "📍 <b>Esito</b>\n"
        f"• Entry: <b>{_price(payload.get('entry'))}</b>\n"
        f"• Prezzo di uscita: <b>{_price(payload.get('outcome_price'))}</b>\n"
        f"• Risultato netto stimato: <b>€{result:+.2f}</b>\n"
        f"• Motivo: <b>{_escape(resolution_text)}</b>\n"
        f"{candle_block}"
        f"• Candela di chiusura: <b>{_escape(payload.get('closed_at'))}</b>"
    )


format_simple_outcome_message = format_audited_outcome_message
