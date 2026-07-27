"""Compact Telegram formatters for the single Ichimoku LONG-only scanner."""
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


def _format_ichimoku_scanner_report(raw: str) -> str:
    plain = _plain_html(raw)
    monitored = _extract_report_value(plain, "Coppie monitorate")
    qualified = _extract_report_value(plain, "Setup qualificati ultimo ciclo")
    sent_cycle = _extract_report_value(plain, "Segnali inviati ultimo ciclo")
    sent_24h = _extract_report_value(plain, "Segnali ultime 24h")
    best = _extract_report_value(plain, "Miglior setup", "Nessuno")
    if best.upper() == "NONE":
        best = "Nessuno"
    return (
        "☁️ <b>REPORT ICHIMOKU</b>\n"
        f"Monitorate: <b>{_escape(monitored)}</b> · "
        f"Valide: <b>{_escape(qualified)}</b> · "
        f"Inviate: <b>{_escape(sent_cycle)}</b>\n"
        f"Ultime 24h: <b>{_escape(sent_24h)}</b>\n"
        f"Miglior setup: <b>{_escape(best)}</b>"
    )


def _format_ichimoku_signal_message(payload: dict[str, Any]) -> str:
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    breakdown = payload.get("score_breakdown") or {}
    entry = _float(payload.get("entry"))
    atr = _float(breakdown.get("atr"))
    tp1_price = entry + (2.0 * atr)
    budget = _float(payload.get("entry_notional_eur") or payload.get("trade_notional_eur"), 100.0)
    return (
        "🟢 <b>NUOVO SEGNALE</b> "
        f"<code>#{_escape(signal_id)}</code>\n"
        f"<b>{_escape(payload.get('pair'))}</b> · LONG · {_escape(payload.get('timeframe') or '1h')}\n\n"
        f"Entrata: <b>{_price(entry)}</b>\n"
        f"Stop Loss: <b>{_price(payload.get('stop_loss'))}</b>\n"
        f"TP1 50%: <b>{_price(tp1_price)}</b>\n\n"
        "Gestione: +1 ATR → stop a pareggio\n"
        "Dopo TP1: restante 50% in gestione Ichimoku\n"
        f"Budget operazione: <b>€{budget:.2f}</b>"
    )


def format_simple_signal_message(payload: dict[str, Any]) -> str:
    strategy = str(payload.get("strategy") or "")
    if "ICHIMOKU" in strategy.upper():
        return _format_ichimoku_signal_message(payload)

    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    return (
        "🟢 <b>NUOVO SEGNALE</b> "
        f"<code>#{_escape(signal_id)}</code>\n"
        f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n\n"
        f"Entrata: <b>{_price(payload.get('entry'))}</b>\n"
        f"Stop Loss: <b>{_price(payload.get('stop_loss'))}</b>\n"
        f"Take Profit: <b>{_price(payload.get('take_profit'))}</b>"
    )


def format_simple_report_message(payload: dict[str, Any]) -> str:
    raw = payload.get("message")
    if raw is None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    raw_text = str(raw)
    plain = _plain_html(raw_text).upper()
    if "REPORT SCANNER" in plain or "COPPIE MONITORATE" in plain:
        return _format_ichimoku_scanner_report(raw_text)
    if payload.get("trusted_html") is True:
        return raw_text
    return "☁️ <b>REPORT ICHIMOKU</b>\n" + _escape(raw_text)


def format_simple_outcome_message(payload: dict[str, Any]) -> str:
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    result = _float(payload.get("realized_net_eur"))
    if bool(payload.get("partial_take_profit")):
        return (
            "✅ <b>TP1 RAGGIUNTO</b> "
            f"<code>#{_escape(signal_id)}</code>\n"
            f"<b>{_escape(payload.get('pair'))}</b>\n"
            f"Chiudi 50% a <b>{_price(payload.get('outcome_price'))}</b> · "
            f"Risultato: <b>€{result:+.2f}</b>\n"
            "Restante 50% con stop a Break Even."
        )
    return (
        "🟢 <b>OPERAZIONE CHIUSA</b> "
        f"<code>#{_escape(signal_id)}</code>\n"
        f"<b>{_escape(payload.get('pair'))}</b> · Risultato: <b>€{result:+.2f}</b>"
    )
