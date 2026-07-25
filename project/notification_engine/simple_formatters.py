"""Telegram formatters for the single Ichimoku LONG-only scanner."""
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
        "PRICE_NOT_ABOVE_CLOUD": "coppie con prezzo non ancora sopra la nuvola",
        "CLOUD_BREAKOUT_NOT_FRESH": "coppie già sopra la nuvola, senza nuovo breakout",
        "TENKAN_NOT_ABOVE_KIJUN": "coppie senza conferma Tenkan sopra Kijun",
        "ICHIMOKU_NOT_READY": "coppie con struttura Ichimoku non ancora pronta",
        "INSUFFICIENT_OHLC": "coppie con storico 1h insufficiente",
        "INVALID_ATR": "coppie con ATR non valido",
        "INVALID_STOP": "setup con stop non valido",
        "REFERENCE_MOVE_TOO_SMALL": "setup con movimento potenziale insufficiente rispetto ai costi",
        "NOT_TRADABLE": "setup non negoziabili con i limiti configurati",
        "SCORE_BELOW_MINIMUM": "setup sotto la qualità minima",
        "ACTIVE_PAIR_COOLDOWN": "segnali bloccati perché la coppia ha già una posizione attiva",
        "ICHIMOKU_DAILY_LIMIT": "segnali bloccati dal limite giornaliero",
        "PAIR_ERROR": "coppie non elaborate per errore dati",
    }
    if not isinstance(parsed, dict) or not parsed:
        return "• Nessun motivo disponibile"

    ordered = sorted(parsed.items(), key=lambda item: (-int(item[1]), str(item[0])))
    lines = []
    for reason, count in ordered:
        description = labels.get(str(reason), str(reason).replace("_", " ").lower())
        lines.append(f"• <b>{int(count)}</b> {html.escape(description)}")
    return "\n".join(lines)


def _format_ichimoku_scanner_report(raw: str) -> str:
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
        "☁️ <b>REPORT SCANNER ICHIMOKU</b>\n\n"
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
        "⚙️ <b>UNICA STRATEGIA ATTIVA</b>\n\n"
        "<b>Ichimoku Cloud Breakout</b>\n\n"
        "• Timeframe: <b>1 ora</b>\n"
        "• Ingresso: <b>prima chiusura sopra tutta la nuvola</b>\n"
        "• Conferma: <b>Tenkan Sen &gt; Kijun Sen</b>\n"
        "• Stop Loss iniziale: <b>1,5 ATR</b>\n"
        "• Uscita: <b>rientro nella nuvola o incrocio Tenkan/Kijun ribassista</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⏳ <i>Nessun ingresso valido al momento: il sistema continua a monitorare il mercato.</i>\n\n"
        "⚠️ <i>Segnali PAPER. Contenuto informativo, non consulenza finanziaria.</i>"
    )


def _reason_lines(payload: dict[str, Any]) -> str:
    reasons: list[str] = []
    for reason in payload.get("reasons") or []:
        text = str(reason)
        if text not in reasons:
            reasons.append(text)
        if len(reasons) >= 5:
            break
    return "\n".join(f"• {_escape(item)}" for item in reasons) or "• Regole Ichimoku soddisfatte"


def _format_ichimoku_signal_message(payload: dict[str, Any]) -> str:
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    breakdown = payload.get("score_breakdown") or {}
    return (
        "☁️ <b>SEGNALE LONG ICHIMOKU</b>\n"
        f"🆔 <code>#{_escape(signal_id)}</code>\n\n"
        f"<b>{_escape(payload.get('pair'))}</b> · <b>1h</b>\n"
        "Strategia: <b>Ichimoku Cloud Breakout</b>\n\n"
        "💰 <b>Livelli operativi</b>\n"
        f"• Entry indicativa: <b>{_price(payload.get('entry'))}</b>\n"
        f"• Stop Loss iniziale: <b>{_price(payload.get('stop_loss'))}</b>\n"
        "• Take Profit fisso: <b>non previsto</b>\n"
        "• Uscita 1: chiusura dentro o sotto la nuvola\n"
        "• Uscita 2: Tenkan Sen incrocia sotto Kijun Sen\n"
        "• Entrata: dopo la chiusura della candela 1h di conferma\n\n"
        "📊 <b>Valutazione</b>\n"
        f"• Score setup: <b>{_float(payload.get('score')):.1f}/100</b>\n"
        f"• Perdita netta stimata allo stop: <b>€{_float(payload.get('net_loss_sl_eur')):.2f}</b>\n"
        f"• ATR di riferimento: <b>{_price(breakdown.get('atr'))}</b>\n"
        "• Storico forward: <b>in costruzione</b>\n\n"
        "✅ <b>Perché è stato selezionato</b>\n"
        f"{_reason_lines(payload)}\n\n"
        "⚠️ Segnale sperimentale in paper trading: nessun metodo garantisce profitto."
    )


def format_simple_signal_message(payload: dict[str, Any]) -> str:
    strategy = str(payload.get("strategy") or "")
    if "ICHIMOKU" in strategy.upper():
        return _format_ichimoku_signal_message(payload)

    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    return (
        "🟢 <b>SEGNALE LONG</b>\n"
        f"🆔 <code>#{_escape(signal_id)}</code>\n\n"
        f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n"
        f"Strategia: <b>{_escape(strategy)}</b>\n\n"
        "💰 <b>Livelli operativi</b>\n"
        f"• Entry: <b>{_price(payload.get('entry'))}</b>\n"
        f"• Stop Loss: <b>{_price(payload.get('stop_loss'))}</b>\n"
        f"• Take Profit: <b>{_price(payload.get('take_profit'))}</b>\n\n"
        "⚠️ Segnale sperimentale in paper trading: nessun metodo garantisce profitto."
    )


def format_simple_report_message(payload: dict[str, Any]) -> str:
    """Format scanner reports while preserving trusted Telegram HTML."""
    raw = payload.get("message")
    if raw is None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    raw_text = str(raw)
    plain = _plain_html(raw_text).upper()
    if "REPORT SCANNER" in plain or "COPPIE MONITORATE" in plain:
        return _format_ichimoku_scanner_report(raw_text)
    if payload.get("trusted_html") is True:
        return raw_text
    return "☁️ <b>REPORT SCANNER ICHIMOKU</b>\n\n" + _escape(raw_text)


def format_simple_outcome_message(payload: dict[str, Any]) -> str:
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
    result = _float(payload.get("realized_net_eur"))
    if "realized_net_eur" not in payload:
        result = -abs(_float(payload.get("net_loss_sl_eur"))) if is_stop else _float(payload.get("net_profit_tp1_eur"))
    return (
        f"{title}\n"
        f"🆔 <code>#{_escape(signal_id)}</code>\n\n"
        f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n"
        f"• Entry: <b>{_price(payload.get('entry'))}</b>\n"
        f"• Uscita: <b>{_price(payload.get('outcome_price'))}</b>\n"
        f"• Risultato netto stimato: <b>€{result:+.2f}</b>\n"
        f"• Candela di chiusura: <b>{_escape(payload.get('closed_at'))}</b>\n"
        f"• Motivo: <b>{_escape(resolution)}</b>"
    )
