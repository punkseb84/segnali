"""Telegram formatters for the single Donchian LONG-only scanner."""
from __future__ import annotations

import html
import json
import re
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


def _plain_html(value: Any) -> str:
    text = re.sub(r"<[^>]+>", "", str(value or ""))
    return html.unescape(text).strip()


def _extract_report_value(text: str, label: str, default: str = "0") -> str:
    pattern = rf"{re.escape(label)}\s*:\s*([^\n]+)"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return _plain_html(match.group(1)) if match else default


def _format_rejection_lines(raw_rejections: str) -> str:
    try:
        parsed = json.loads(raw_rejections)
    except (TypeError, ValueError, json.JSONDecodeError):
        parsed = {}

    labels = {
        "BELOW_EMA200": "coppie sotto EMA 200",
        "ADX_BELOW_MINIMUM": "coppie con ADX insufficiente",
        "DIRECTION_NOT_BULLISH": "coppie senza direzione rialzista (+DI ≤ -DI)",
        "NO_DONCHIAN_BREAKOUT": "coppie senza breakout Donchian",
        "BREAKOUT_NOT_FRESH": "breakout già avvenuti e non più validi",
        "INSUFFICIENT_OHLC": "coppie con storico insufficiente",
        "INDICATORS_NOT_READY": "coppie con indicatori non ancora pronti",
        "NET_RR_TOO_LOW": "setup con rapporto rischio/rendimento insufficiente",
        "NET_PROFIT_TOO_LOW": "setup con profitto netto stimato insufficiente",
        "ACTIVE_PAIR_COOLDOWN": "segnali bloccati dal cooldown della coppia",
        "DAILY_SIGNAL_LIMIT": "segnali bloccati dal limite giornaliero",
    }
    if not isinstance(parsed, dict) or not parsed:
        return "• Nessun motivo disponibile"

    ordered = sorted(parsed.items(), key=lambda item: (-int(item[1]), str(item[0])))
    lines = []
    for reason, count in ordered:
        description = labels.get(str(reason), str(reason).replace("_", " ").lower())
        lines.append(f"• <b>{int(count)}</b> {html.escape(description)}")
    return "\n".join(lines)


def _format_donchian_scanner_report(raw: str) -> str:
    plain = _plain_html(raw)
    monitored = _extract_report_value(plain, "Coppie monitorate")
    qualified = _extract_report_value(plain, "Setup qualificati ultimo ciclo")
    sent_cycle = _extract_report_value(plain, "Segnali inviati ultimo ciclo")
    sent_24h = _extract_report_value(plain, "Segnali ultime 24h")
    best = _extract_report_value(plain, "Miglior setup", "Nessuno")
    if best.upper() == "NONE":
        best = "Nessuno"
    rejections = _extract_report_value(plain, "Motivi senza segnale", "{}")
    rejection_lines = _format_rejection_lines(rejections)

    return (
        "📊 <b>REPORT SCANNER CRYPTO</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔎 <b>RIEPILOGO ULTIMO CICLO</b>\n\n"
        f"🪙 Coppie monitorate: <b>{_escape(monitored)}</b>\n"
        f"✅ Setup qualificati: <b>{_escape(qualified)}</b>\n"
        f"📨 Segnali inviati: <b>{_escape(sent_cycle)}</b>\n"
        f"🕒 Segnali nelle ultime 24 ore: <b>{_escape(sent_24h)}</b>\n"
        f"⭐ Miglior setup rilevato: <b>{_escape(best)}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🚫 <b>MOTIVI DI ESCLUSIONE</b>\n\n"
        f"{rejection_lines}\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚙️ <b>STRATEGIA ATTIVA</b>\n\n"
        "<b>Donchian Breakout + EMA 200 + ADX + ATR</b>\n\n"
        "• Breakout Donchian: <b>20 periodi</b>\n"
        "• Filtro trend: <b>EMA 200</b>\n"
        "• Forza del trend: <b>ADX ≥ 25</b>\n"
        "• Direzione: <b>+DI &gt; -DI</b>\n"
        "• Stop Loss: <b>1,5 ATR</b>\n"
        "• Take Profit: <b>3 ATR</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⏳ <i>Nessun ingresso valido al momento: il sistema continua a monitorare il mercato.</i>\n\n"
        "⚠️ <i>Segnali PAPER. Contenuto informativo, non consulenza finanziaria.</i>"
    )


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
    """Format scanner reports while preserving trusted Telegram HTML."""
    raw = payload.get("message")
    if raw is None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    raw_text = str(raw)
    plain = _plain_html(raw_text).upper()
    if "REPORT SCANNER CRYPTO" in plain or "COPPIE MONITORATE" in plain:
        return _format_donchian_scanner_report(raw_text)
    if payload.get("trusted_html") is True:
        return raw_text
    return "📊 <b>REPORT SCANNER</b>\n\n" + _escape(raw_text)


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
