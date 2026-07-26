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
    resolution = str(payload.get("outcome_resolution") or "")
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    result = _float(payload.get("realized_net_eur"))

    if bool(payload.get("partial_take_profit")) or resolution == "ICHIMOKU_TP1_2ATR":
        return (
            "🟢 <b>TP1 PARZIALE RAGGIUNTO</b>\n"
            f"🆔 <code>#{_escape(signal_id)}</code>\n\n"
            f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n"
            "Strategia: <b>Ichimoku Cloud Breakout</b>\n\n"
            "💰 <b>Gestione della posizione</b>\n"
            f"• Livello +2 ATR: <b>{_price(payload.get('outcome_price'))}</b>\n"
            "• Posizione chiusa: <b>50%</b>\n"
            f"• Profitto netto stimato sulla metà: <b>€{result:+.2f}</b>\n"
            "• Stop della metà restante: <b>Break Even</b>\n"
            "• Posizione ancora aperta: <b>50%</b>\n\n"
            "⏳ <b>Non chiudere ancora la parte restante.</b>\n"
            "Il bot invierà un secondo messaggio esplicito quando sarà il momento di uscire definitivamente."
        )

    is_stop = str(payload.get("outcome")) == "STOP_LOSS"
    is_ichimoku_exit = resolution.startswith("ICHIMOKU_")
    tp1_hit = bool(payload.get("tp1_hit"))
    prior_net = _float(payload.get("tp1_realized_net_eur"))
    final_leg_net = result - prior_net if tp1_hit else result

    if tp1_hit:
        if resolution == "ICHIMOKU_BREAK_EVEN_STOP":
            title = "🟡 <b>CHIUDI ORA · POSIZIONE CHIUSA AL 100%</b>"
        elif result >= 0:
            title = "🟢 <b>CHIUDI ORA · POSIZIONE CHIUSA AL 100%</b>"
        else:
            title = "🔴 <b>CHIUDI ORA · POSIZIONE CHIUSA AL 100%</b>"
    elif resolution == "ICHIMOKU_BREAK_EVEN_STOP":
        title = "🟡 <b>STOP A BREAK EVEN RAGGIUNTO</b>"
    elif is_stop:
        title = "🔴 <b>STOP LOSS RAGGIUNTO</b>"
    elif is_ichimoku_exit:
        title = "☁️ <b>USCITA ICHIMOKU CONFERMATA</b>"
    else:
        title = "🎯 <b>TAKE PROFIT RAGGIUNTO</b>"

    if payload.get("realized_net_eur") is None:
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
        resolution_text = "stop loss iniziale a 1,5 ATR"
    elif resolution == "ICHIMOKU_BREAK_EVEN_STOP":
        resolution_text = "ritorno al prezzo di ingresso dopo l’attivazione del break-even"
    else:
        resolution_text = resolution or "n/d"

    management_block = ""
    if tp1_hit:
        management_block = (
            "\n💰 <b>Riepilogo delle due uscite</b>\n"
            f"• Prima metà chiusa a TP1: <b>€{prior_net:+.2f}</b>\n"
            f"• Seconda metà chiusa adesso: <b>€{final_leg_net:+.2f}</b>\n"
            f"• Risultato netto totale: <b>€{result:+.2f}</b>\n"
            "• Posizione residua: <b>0% · operazione conclusa</b>\n"
        )

    return (
        f"{title}\n"
        f"🆔 <code>#{_escape(signal_id)}</code>\n\n"
        f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n"
        f"Strategia: <b>{_escape(payload.get('strategy'))}</b>\n"
        f"Exchange: <b>{_escape(payload.get('exchange'))}</b>\n\n"
        "📍 <b>Uscita operativa</b>\n"
        f"• Azione: <b>{'chiudi il 50% restante' if tp1_hit else 'chiudi la posizione'}</b>\n"
        f"• Prezzo uscita finale: <b>{_price(payload.get('outcome_price'))}</b>\n"
        f"• Motivo: <b>{_escape(resolution_text)}</b>\n"
        f"{management_block}"
        f"{candle_block}"
        f"• Candela di chiusura: <b>{_escape(payload.get('closed_at'))}</b>"
    )


format_simple_outcome_message = format_audited_outcome_message
