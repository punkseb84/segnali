"""Telegram formatters for the simplified LONG-only scanner."""
from __future__ import annotations

import html
import json
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
    breakdown = payload.get("score_breakdown") or {}
    forward_completed = int(_float(breakdown.get("forward_completed"), 0.0))
    forward_pf = _float(breakdown.get("forward_profit_factor"), 0.0)
    strategy_state = str(breakdown.get("strategy_state") or "ACTIVE")
    forward_line = (
        f"• Storico forward strategia: <b>{forward_completed} trade · PF {forward_pf:.2f}</b>\n"
        if forward_completed
        else "• Storico forward strategia: <b>in costruzione</b>\n"
    )
    state_line = (
        "• Stato strategia: <b>PROBATION</b> · segnale-test con requisiti rafforzati\n"
        if strategy_state == "PROBATION"
        else f"• Stato strategia: <b>{_escape(strategy_state)}</b>\n"
    )
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
        f"• Perdita netta stimata SL: <b>€{_float(payload.get('net_loss_sl_eur')):.2f}</b>\n"
        f"{forward_line}"
        f"{state_line}\n"
        "✅ <b>Perché è stato selezionato</b>\n"
        f"{reason_lines}\n\n"
        "⚠️ Segnale sperimentale in paper trading: nessun metodo garantisce profitto."
    )


def format_simple_report_message(payload: dict[str, Any]) -> str:
    """Preserve HTML only for reports generated internally by the scanner."""
    raw = payload.get("message")
    if raw is None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if payload.get("trusted_html") is True:
        return str(raw)
    return "📊 <b>REPORT SCANNER</b>\n\n" + _escape(raw)


def format_simple_outcome_message(payload: dict[str, Any]) -> str:
    is_stop = str(payload.get("outcome")) == "STOP_LOSS"
    title = "🔴 <b>STOP LOSS RAGGIUNTO</b>" if is_stop else "🎯 <b>TAKE PROFIT RAGGIUNTO</b>"
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    result = -abs(_float(payload.get("net_loss_sl_eur"))) if is_stop else _float(payload.get("net_profit_tp1_eur"))
    return (
        f"{title}\n"
        f"🆔 <code>#{_escape(signal_id)}</code>\n\n"
        f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n"
        f"Strategia: <b>{_escape(payload.get('strategy'))}</b>\n\n"
        "📍 <b>Esito</b>\n"
        f"• Entry: <b>{_price(payload.get('entry'))}</b>\n"
        f"• Livello raggiunto: <b>{_price(payload.get('outcome_price'))}</b>\n"
        f"• Risultato netto stimato: <b>€{result:+.2f}</b>\n"
        f"• R/R iniziale: <b>{_float(payload.get('net_rr')):.2f}</b>\n\n"
        f"• Candela di chiusura: <b>{_escape(payload.get('closed_at'))}</b>\n"
        f"• Risoluzione: <b>{_escape(payload.get('outcome_resolution'))}</b>"
    )
