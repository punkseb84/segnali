"""Telegram formatter for the simplified rule-based scanner."""
from __future__ import annotations

import html
from typing import Any


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


def format_simple_signal_message(payload: dict[str, Any]) -> str:
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    reasons = []
    for reason in payload.get("reasons") or []:
        text = str(reason)
        if "=" not in text and text not in reasons:
            reasons.append(text)
        if len(reasons) >= 4:
            break
    reason_lines = "\n".join(f"• {_escape(item)}" for item in reasons) or "• Regole della strategia soddisfatte"
    return (
        "🟢 <b>SEGNALE LONG</b>\n"
        f"🆔 <code>#{_escape(signal_id)}</code>\n\n"
        f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n"
        f"Strategia: <b>{_escape(payload.get('strategy'))}</b>\n"
        f"Regime: <b>{_escape(payload.get('regime'))}</b>\n\n"
        "💰 <b>Livelli operativi</b>\n"
        f"• Entry: <b>{_price(payload.get('entry'))}</b>\n"
        f"• Stop Loss: <b>{_price(payload.get('stop_loss'))}</b>\n"
        f"• Take Profit: <b>{_price(payload.get('take_profit'))}</b>\n"
        "• Entrata: alla ricezione del messaggio\n\n"
        "📊 <b>Valutazione</b>\n"
        f"• Score setup: <b>{_float(payload.get('score')):.1f}/100</b>\n"
        f"• R/R netto: <b>{_float(payload.get('net_rr')):.2f}</b>\n"
        f"• Profitto netto stimato TP: <b>€{_float(payload.get('net_profit_tp1_eur')):.2f}</b>\n"
        f"• Perdita netta stimata SL: <b>€{_float(payload.get('net_loss_sl_eur')):.2f}</b>\n\n"
        "✅ <b>Perché è stato selezionato</b>\n"
        f"{reason_lines}\n\n"
        "⚠️ Segnale sperimentale in paper trading: nessun metodo garantisce profitto."
    )
