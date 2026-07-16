"""Readable HTML formatters for Telegram notifications."""
from __future__ import annotations

import html
import json
from typing import Any, Iterable


def _escape(value: Any, fallback: str = "n/d") -> str:
    if value is None or value == "":
        return fallback
    return html.escape(str(value), quote=True)


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _price(value: Any) -> str:
    number = _float(value)
    if number is None:
        return "n/d"
    absolute = abs(number)
    decimals = 2 if absolute >= 1000 else 3 if absolute >= 1 else 5 if absolute >= 0.01 else 8
    return f"{number:,.{decimals}f}"


def _money(value: Any, signed: bool = False) -> str:
    number = _float(value)
    if number is None:
        return "n/d"
    prefix = "+" if signed and number > 0 else ""
    return f"{prefix}€{number:.2f}"


def _number(value: Any, decimals: int = 2) -> str:
    number = _float(value)
    return "n/d" if number is None else f"{number:.{decimals}f}"


def _percentage(value: Any) -> str:
    number = _float(value)
    if number is None:
        return "n/d"
    if abs(number) <= 1:
        number *= 100
    return f"{number:.1f}%"


def _timestamp(value: Any) -> str:
    if value is None:
        return "n/d"
    text = str(value).replace("T", " ").replace("+00:00", " UTC").replace("Z", " UTC")
    return _escape(text)


def _signal_id(payload: dict[str, Any], linked: bool = False) -> str:
    signal_id = payload.get("signal_id") or payload.get("id")
    label = f"#{signal_id}" if signal_id is not None else "n/d"
    link = payload.get("telegram_message_link") or payload.get("signal_message_link")
    if linked and link and str(link).startswith("https://t.me/"):
        return f'<a href="{html.escape(str(link), quote=True)}"><b>{html.escape(label)}</b></a>'
    return f"<code>{html.escape(label)}</code>"


def _reason_text(reason: Any) -> str | None:
    text = str(reason or "").strip()
    if not text or text.startswith("score_breakdown="):
        return None
    mappings = {
        "profit_factor=": "Profit factor storico",
        "expectancy=": "Expectancy storica",
        "reward_risk=": "R/R tecnico",
        "atr_multiplier=": "Moltiplicatore ATR",
        "regime=": "Regime",
        "signal_class=": "Classe",
        "historical_ev=": "EV storico",
        "historical_ev_decision=": "Valutazione EV",
    }
    for prefix, label in mappings.items():
        if text.startswith(prefix):
            return f"{label}: {text[len(prefix):]}"
    if text == "regime_aligned=true":
        return "Strategia coerente con il regime"
    if text == "regime_aligned=false":
        return "Strategia non coerente con il regime"
    return text.replace("_", " ")


def _reason_lines(reasons: Iterable[Any] | None, limit: int = 4) -> str:
    readable: list[str] = []
    for reason in reasons or []:
        converted = _reason_text(reason)
        if converted and converted not in readable:
            readable.append(converted)
        if len(readable) >= limit:
            break
    if not readable:
        return "• Nessun dettaglio aggiuntivo"
    return "\n".join(f"• {_escape(item)}" for item in readable)


def _technical_appendix(payload: dict[str, Any]) -> str:
    """Keep exact diagnostic labels used by existing logs/tests in a compact section."""
    historical_ev = payload.get("historical_expected_value_eur")
    historical_ev_text = "non disponibile" if historical_ev is None else str(historical_ev)
    return (
        "\n\n🧾 <b>Dettagli tecnici</b>\n"
        f"Class: {_escape(payload.get('signal_class', 'B'))}\n"
        f"Quantity: {_number(payload.get('quantity'), 8)}\n"
        f"Gross R/R: {_number(payload.get('gross_rr'), 2)}\n"
        f"Technical TP: {_number(payload.get('technical_take_profit'), 6)}\n"
        f"Effective TP: {_number(payload.get('effective_take_profit'), 6)}\n"
        f"Probability confidence: {_escape(payload.get('probability_confidence'))}\n"
        f"Historical EV: {_escape(historical_ev_text)}\n"
        f"Validation: {_escape(payload.get('validation_status'))}\n"
        f"Net profit TP1: €{_number(payload.get('net_profit_tp1_eur'), 4)}\n"
        f"Net loss SL: €{_number(payload.get('net_loss_sl_eur'), 4)}\n"
        f"Net R/R: {_number(payload.get('net_rr'), 2)}\n"
        "Costi: "
        f"buy fee €{_number(payload.get('estimated_buy_fee_eur'), 4)}, "
        f"sell fee TP €{_number(payload.get('estimated_sell_fee_eur'), 4)}, "
        f"sell fee SL €{_number(payload.get('estimated_sell_fee_sl_eur'), 4)}, "
        f"spread €{_number(payload.get('estimated_spread_cost_eur'), 4)}, "
        f"slippage €{_number(payload.get('estimated_slippage_cost_eur'), 4)}"
    )


def format_signal_message(payload: dict[str, Any]) -> str:
    signal_class = _escape(payload.get("signal_class", "B"))
    return (
        f"🟢 <b>NUOVO SEGNALE LONG</b> · Classe <b>{signal_class}</b>\n"
        f"🆔 Segnale {_signal_id(payload)}\n\n"
        f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n"
        f"Strategia: <b>{_escape(payload.get('strategy'))}</b>\n"
        f"Regime: <b>{_escape(payload.get('regime'))}</b>\n\n"
        "💰 <b>Livelli operativi</b>\n"
        f"• Entry: <code>{_price(payload.get('entry'))}</code>\n"
        f"• Stop Loss: <code>{_price(payload.get('stop_loss'))}</code>\n"
        f"• Target: <code>{_price(payload.get('effective_take_profit') or payload.get('take_profit'))}</code>\n"
        "• Entrata: <b>immediata alla ricezione</b>\n\n"
        "📊 <b>Rischio e rendimento</b>\n"
        f"• Profitto netto a target: <b>{_money(payload.get('net_profit_tp1_eur'))}</b>\n"
        f"• Perdita netta a stop: <b>{_money(payload.get('net_loss_sl_eur'))}</b>\n"
        f"• R/R netto: <b>{_number(payload.get('net_rr'))}</b>\n"
        f"• Capitale impiegato: <b>{_money(payload.get('entry_notional_eur'))}</b>\n\n"
        "🧠 <b>Qualità del segnale</b>\n"
        f"• Score: <b>{_number(payload.get('score'), 1)}/100</b>\n"
        f"• Probabilità storica: <b>{_percentage(payload.get('probability'))}</b>\n"
        f"• Campione: <b>{_escape(payload.get('historical_sample_size'))}</b>\n"
        f"• Confidenza: <b>{_escape(payload.get('probability_confidence'))}</b>\n\n"
        "📌 <b>Motivi principali</b>\n"
        f"{_reason_lines(payload.get('reasons'))}\n\n"
        f"🕯 Candela di riferimento: {_timestamp(payload.get('reference_candle_time'))}\n"
        f"⏱ Generato: {_timestamp(payload.get('signal_time'))}\n\n"
        "⚠️ <i>Segnale in dry-run: verifica sempre prezzo, liquidità e costi prima di operare.</i>"
        f"{_technical_appendix(payload)}"
    )


def format_watchlist_message(payload: dict[str, Any]) -> str:
    return (
        "🟡 <b>WATCHLIST</b> · Classe <b>C</b>\n"
        f"🆔 Osservazione {_signal_id(payload)}\n\n"
        f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n"
        f"Strategia: <b>{_escape(payload.get('strategy'))}</b>\n"
        f"Regime: <b>{_escape(payload.get('regime'))}</b>\n\n"
        "👀 <b>Setup interessante, ma NON È UN SEGNALE DI INGRESSO</b>\n\n"
        "💰 <b>Livelli teorici</b>\n"
        f"• Entry: <code>{_price(payload.get('entry'))}</code>\n"
        f"• Stop: <code>{_price(payload.get('stop_loss'))}</code>\n"
        f"• Target: <code>{_price(payload.get('take_profit'))}</code>\n\n"
        "📉 <b>Perché non è operativo</b>\n"
        f"• Profitto netto stimato: <b>{_money(payload.get('net_profit_tp1_eur'))}</b>\n"
        f"• Perdita netta stimata: <b>{_money(payload.get('net_loss_sl_eur'))}</b>\n"
        f"• R/R netto: <b>{_number(payload.get('net_rr'))}</b>\n"
        f"• Score: <b>{_number(payload.get('score'), 1)}/100</b>\n\n"
        "📌 <b>Elementi osservati</b>\n"
        f"{_reason_lines(payload.get('reasons'))}\n\n"
        "⚠️ <b>Solo monitoraggio:</b> attendere un eventuale nuovo segnale A/B."
    )


def format_outcome_message(payload: dict[str, Any]) -> str:
    is_stop = payload.get("outcome") == "STOP_LOSS"
    title = "🔴 <b>STOP LOSS RAGGIUNTO</b>" if is_stop else "🎯 <b>TARGET RAGGIUNTO</b>"
    result_value = payload.get("net_loss_sl_eur") if is_stop else payload.get("net_profit_tp1_eur")
    result_text = f"-€{abs(_float(result_value) or 0):.2f}" if is_stop else _money(result_value, signed=True)
    link_note = "Tocca l’ID o la risposta Telegram per aprire il segnale originale."
    return (
        f"{title}\n"
        f"🆔 Segnale {_signal_id(payload, linked=True)}\n\n"
        f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n"
        f"Strategia: <b>{_escape(payload.get('strategy'))}</b>\n"
        f"Classe iniziale: <b>{_escape(payload.get('signal_class', 'B'))}</b>\n\n"
        "📍 <b>Esito operazione</b>\n"
        f"• Entry: <code>{_price(payload.get('entry'))}</code>\n"
        f"• Livello raggiunto: <code>{_price(payload.get('outcome_price'))}</code>\n"
        f"• Risultato netto stimato: <b>{result_text}</b>\n"
        f"• R/R iniziale: <b>{_number(payload.get('net_rr'))}</b>\n\n"
        "🧠 <b>Dati iniziali</b>\n"
        f"• Score: <b>{_number(payload.get('score'), 1)}/100</b>\n"
        f"• Probabilità: <b>{_percentage(payload.get('probability'))}</b>\n"
        f"• Chiusura: {_timestamp(payload.get('closed_at'))}\n"
        f"• Candela ambigua: <b>{'Sì' if payload.get('ambiguous') else 'No'}</b>\n\n"
        f"🔗 <i>{link_note}</i>"
    )


def format_report_message(payload: dict[str, Any]) -> str:
    raw = payload.get("message")
    if raw is None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    lines = [line.strip() for line in str(raw).splitlines() if line.strip()]
    body: list[str] = []
    for line in lines[1:] if lines and "report" in lines[0].lower() else lines:
        escaped = _escape(line)
        if ":" in escaped:
            label, value = escaped.split(":", 1)
            body.append(f"<b>{label}:</b>{value}")
        else:
            body.append(escaped)
    return "📊 <b>REPORT GIORNALIERO</b>\n\n" + "\n".join(body[:18])
