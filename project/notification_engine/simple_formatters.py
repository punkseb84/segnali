"""Neutral legacy Telegram fallbacks.

The active public and private runtimes install their dedicated relative-strength
formatters. This module must never label generic events as Ichimoku.
"""
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
    direction = str((payload.get("score_breakdown") or {}).get("direction") or "LONG")
    return (
        "📌 <b>SEGNALE PAPER</b>\n"
        f"🆔 <code>#{_escape(signal_id)}</code>\n"
        f"<b>{_escape(payload.get('pair'))}</b> · {_escape(direction)} · {_escape(payload.get('timeframe'))}\n"
        f"Entrata: <b>{_price(payload.get('entry'))}</b>\n"
        f"Stop: <b>{_price(payload.get('stop_loss'))}</b>\n"
        f"Target: <b>{_price(payload.get('take_profit'))}</b>"
    )


def format_simple_report_message(payload: dict[str, Any]) -> str:
    """Neutral fallback; scanner reports are disabled in active runtimes."""
    raw = payload.get("message")
    if raw is None:
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if payload.get("trusted_html") is True:
        return str(raw)
    return "📊 <b>REPORT SISTEMA</b>\n" + _escape(raw)


def format_simple_outcome_message(payload: dict[str, Any]) -> str:
    signal_id = payload.get("signal_id") or payload.get("id") or "n/d"
    result = _float(payload.get("realized_net_eur"))
    return (
        "📍 <b>POSIZIONE PAPER CHIUSA</b>\n"
        f"🆔 <code>#{_escape(signal_id)}</code>\n"
        f"<b>{_escape(payload.get('pair'))}</b> · {_escape(payload.get('timeframe'))}\n"
        f"Uscita: <b>{_price(payload.get('outcome_price'))}</b>\n"
        f"Risultato netto stimato: <b>€{result:+.2f}</b>\n"
        f"Motivo: <b>{_escape(payload.get('outcome_resolution'))}</b>"
    )
